"""How much of the plan's 5-hour and 7-day windows is gone, and whether the secretary should hold back.

The percentages arrive only as response headers of an ordinary Messages API call
(`anthropic-ratelimit-unified-5h-utilization`, `-5h-reset`, `-7d-…`); the Agent SDK does not pass them
on. So, like Claude Code's own /usage and Fleet's meter, probe_usage() makes one tiny request of its
own: Haiku, max_tokens=1, a one-word prompt, read for its headers and thrown away. At most one a
minute, marked before the request so a failing token backs off at the same pace.

The SDK's rate_limit_event frames (one per CLI process) add what they know: a window that says
"rejected" is full until it resets, whatever the last probe read.

Nothing here raises. Unknown means None, and None never pauses anything: a meter is not worth stopping
the secretary for, and a full plan still says so through the next call's rejection.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

from .. import config

log = logging.getLogger(__name__)

PROBE_URL = "https://api.anthropic.com/v1/messages"
PROBE_MODEL = "claude-haiku-4-5-20251001"
PROBE_TTL_S = 60.0
PROBE_TIMEOUT_S = 15.0
# Window name (as the SDK's rate_limit_type spells it) → the abbreviation in the header names.
WINDOWS = {"five_hour": "5h", "seven_day": "7d"}
LABELS = {"five_hour": "5-hour", "seven_day": "7-day"}

_state: dict = {"five_hour": None, "seven_day": None, "ts": None, "error": None, "last_probe": 0.0}


def oauth_token() -> str:
    """The Claude login (a `claude setup-token` token), read on every use so a new one needs no restart."""
    try:
        return config.secret("claude_token", "CLAUDE_CODE_OAUTH_TOKEN") or config.OAUTH_TOKEN or ""
    except Exception:
        return config.OAUTH_TOKEN or ""


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        # A subscription token is an OAuth credential; without this beta it is refused as an API key.
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def parse_windows(headers) -> dict:
    """Every window with both a utilization and a reset; a malformed one is skipped, not fatal."""
    out = {}
    for name, key in WINDOWS.items():
        used = headers.get(f"anthropic-ratelimit-unified-{key}-utilization")
        reset = headers.get(f"anthropic-ratelimit-unified-{key}-reset")
        if used is None or reset is None:
            continue
        try:
            out[name] = {"utilization": float(used), "resets_at": int(round(float(reset))),
                         "status": str(headers.get(f"anthropic-ratelimit-unified-{key}-status") or "")}
        except (TypeError, ValueError):
            continue
    return out


async def probe_usage() -> None:
    """Read the plan windows with one 1-token Haiku request; at most once a minute. Never raises."""
    try:
        now = time.monotonic()
        if _state["last_probe"] and now - _state["last_probe"] < PROBE_TTL_S:
            return
        _state["last_probe"] = now
        token = oauth_token()
        if not token:
            _state["error"] = "no Claude login"
            return
        body = {"model": PROBE_MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "quota"}]}
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
                r = await client.post(PROBE_URL, headers=_headers(token), json=body)
        except httpx.HTTPError as exc:
            _state["error"] = f"usage probe failed ({exc.__class__.__name__})"
            return
        # A 429 is the most important reading there is, and it still carries the window headers.
        if r.status_code >= 400 and r.status_code != 429:
            _state["error"] = f"usage probe failed (HTTP {r.status_code})"
            return
        windows = parse_windows(r.headers)
        if not windows:
            _state["error"] = "usage probe failed (no usage headers)"
            return
        for name in WINDOWS:
            _state[name] = windows.get(name)
        _state["ts"], _state["error"] = time.time(), None
    except Exception as exc:  # never load-bearing
        log.info("usage probe: %s", exc.__class__.__name__)
        _state["error"] = f"usage probe failed ({exc.__class__.__name__})"


def note_rate_limits(events: list) -> None:
    """Fold the SDK's rate_limit_event frames in: a rejected window is full until it resets."""
    try:
        for e in events or []:
            name = e.get("rate_limit_type")
            if name not in WINDOWS:
                continue
            status = str(e.get("status") or "")
            resets = e.get("resets_at")
            cur = dict(_state.get(name) or {})
            if status == "rejected":
                cur["utilization"] = max(1.0, float(cur.get("utilization") or 0))
            elif e.get("utilization") is not None:
                cur["utilization"] = float(e["utilization"])
            elif not cur:
                continue  # "allowed" with no number tells us nothing new
            if resets:
                cur["resets_at"] = int(resets)
            cur["status"] = status
            if cur.get("utilization") is not None and cur.get("resets_at"):
                _state[name] = cur
                _state["ts"] = time.time()
    except Exception as exc:
        log.info("rate limit event: %s", exc.__class__.__name__)


def _window(name: str, now: float) -> dict | None:
    w = _state.get(name)
    if not isinstance(w, dict) or w.get("utilization") is None:
        return None
    ra = w.get("resets_at")
    if ra and ra <= now:
        return None  # the window has reset since we read it: unknown, not full
    return {"utilization": float(w["utilization"]), "resets_at": int(ra) if ra else None,
            "status": str(w.get("status") or "")}


def _when(ts: int | None, week: bool) -> str:
    if not ts:
        return ""
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ts, ZoneInfo(config.TZ or "UTC"))
    except Exception:
        dt = datetime.fromtimestamp(ts)
    return dt.strftime("%a %-d %b %H:%M" if week else "%H:%M")


def limits() -> dict:
    """Where each window pauses digests and the brief: SECRETARY_PAUSE_AT and SECRETARY_PAUSE_AT_WEEK."""
    return {"five_hour": float(config.PAUSE_AT), "seven_day": float(getattr(config, "PAUSE_AT_WEEK", 0.85))}


def window_over(w: dict | None, limit: float) -> bool:
    """The one pause rule, shared with gate.py: a known window at or above its limit, or one the API
    called "rejected" (full until it resets). Unknown (None) never pauses."""
    if not isinstance(w, dict) or w.get("utilization") is None:
        return False
    return str(w.get("status") or "") == "rejected" or float(w["utilization"]) >= limit


def _over(name: str, w: dict | None, limit: float) -> str:
    if not window_over(w, limit):
        return ""
    full = str(w.get("status") or "") == "rejected"
    week = name == "seven_day"
    pct = round(float(w["utilization"]) * 100)
    why = "is full" if full else f"is at {pct}% (the secretary pauses at {round(limit * 100)}%)"
    when = _when(w.get("resets_at"), week)
    return f"The {LABELS[name]} window {why}" + (f"; it resets {'on ' if week else 'at '}{when}." if when else ".")


def pause_reason(five_hour: dict | None, seven_day: dict | None) -> str:
    """Why digests and the brief wait, one sentence per full window, or "" when neither is. gate.py shows
    this same text, so the status card, the meter and the Ask warning all say the same thing."""
    lim = limits()
    return " ".join(r for r in (_over("five_hour", five_hour, lim["five_hour"]),
                                _over("seven_day", seven_day, lim["seven_day"])) if r)


def usage_state() -> dict:
    """{five_hour, seven_day: {utilization, resets_at, status} | None, paused, reason, ts}. Never raises.

    Digests and the brief pause when the 5-hour window reaches SECRETARY_PAUSE_AT or the 7-day window
    reaches SECRETARY_PAUSE_AT_WEEK (or either says "rejected"); Ask still runs, with a warning."""
    try:
        now = time.time()
        fh, sd = _window("five_hour", now), _window("seven_day", now)
        reason = pause_reason(fh, sd)
        paused = bool(reason)
        if not paused and fh is None and sd is None:
            reason = _state["error"] or "Plan usage has not been read yet."
        return {"five_hour": fh, "seven_day": sd, "paused": paused, "reason": reason, "ts": _state["ts"]}
    except Exception as exc:
        log.info("usage state: %s", exc.__class__.__name__)
        return {"five_hour": None, "seven_day": None, "paused": False, "reason": "Plan usage is unknown.", "ts": None}


def reset() -> None:
    """Forget everything (tests, and a new login)."""
    _state.update({"five_hour": None, "seven_day": None, "ts": None, "error": None, "last_probe": 0.0})
