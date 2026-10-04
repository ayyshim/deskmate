"""The secretary's side of the model layer: may a model call run, and what plan usage looks like.

The model layer itself (llm.py: digest_session, daily_brief, ask, usage_state, probe_usage, available)
is built separately. This module treats it as optional: if it is missing or broken, everything that
needs a model reads as "no Claude login" and the rest of the secretary keeps working.
"""

from __future__ import annotations

import importlib
import logging
import time

from .. import config
from . import store

try:  # the plan-usage rule and its wording live with the model layer; usage.py needs no SDK
    from . import usage as _rules
except Exception:  # pragma: no cover - only while the model layer is missing
    _rules = None

log = logging.getLogger(__name__)

STALE_S = 300.0
_mod = {"m": None, "tried": 0.0}

# What a failed model call means to the user, by code: llm.ModelError.kind for digests and the brief, llm.ask's
# "error" for Ask. (a short phrase for labels and the brief's error line, a sentence for the Ask page)
FAILURES: dict[str, tuple[str, str]] = {
    "no_login": ("there is no Claude login",
                 "Ask needs the secretary's Claude login. Add it with ./deskmate config set-secret claude-token, "
                 "then ./deskmate restart hub."),
    "auth": ("the Claude login was refused",
             "The secretary's Claude login was refused. Add a fresh one with ./deskmate config set-secret "
             "claude-token, then ./deskmate restart hub."),
    "rate_limited": ("the plan's usage limit was reached",
                     "The plan's usage limit is reached. Ask again after it resets."),
    "timeout": ("it took too long", "The answer took too long. Try a narrower question."),
    "max_turns": ("it ran out of steps", "The search ran out of steps before it finished. Try a narrower question."),
    "bad_output": ("the model gave no usable answer", "The model gave no usable answer. Ask again."),
    "refused_output": ("a search turned up a file the secretary may not read",
                       "The answer was stopped because a search turned up a file the secretary may not read. "
                       "Ask again more narrowly."),
    "no_roots": ("no shared folder is readable", "No shared folders are readable, so there is nothing to answer from."),
    "empty": ("the question was empty", "The question is empty."),
    "cli_missing": ("the Claude CLI is missing",
                    "The Claude CLI is missing from the hub image. Update Deskmate with ./deskmate update."),
    "cli_error": ("the Claude CLI stopped", "The Claude CLI stopped before it answered. Ask again."),
    "failed": ("the model call failed", "The answer failed. Ask again."),
}
# The model layer's error classes (llm.py) by name, for rows that recorded only the class name.
_CLASS_CODES = {"ModelUnavailable": "no_login", "ModelAuthFailed": "auth", "ModelRateLimited": "rate_limited",
                "ModelTimeout": "timeout", "TimeoutError": "timeout", "ModelMaxTurns": "max_turns",
                "ModelBadOutput": "bad_output", "ModelCliMissing": "cli_missing", "ModelCliError": "cli_error",
                "ModelFailed": "failed"}


def failure_code(exc_or_name) -> str:
    """The failure code of an exception (a ModelError carries .kind) or of a recorded class name."""
    if isinstance(exc_or_name, BaseException):
        kind = getattr(exc_or_name, "kind", None)
        if isinstance(kind, str) and kind in FAILURES:
            return kind
        exc_or_name = exc_or_name.__class__.__name__
    return _CLASS_CODES.get(str(exc_or_name or ""), "failed")


def failure_phrase(code: str | None) -> str:
    return FAILURES.get(code or "failed", FAILURES["failed"])[0]


def failure_sentence(code: str | None) -> str:
    return FAILURES.get(code or "failed", FAILURES["failed"])[1]


def llm():
    """app.secretary.llm, or None while it does not exist or fails to import (retried every 30 s)."""
    if _mod["m"] is not None:
        return _mod["m"]
    if time.monotonic() - _mod["tried"] < 30 and _mod["tried"]:
        return None
    _mod["tried"] = time.monotonic()
    try:
        _mod["m"] = importlib.import_module("app.secretary.llm")
    except Exception as exc:  # ImportError while it is being built, or any error inside it
        log.info("model layer not available: %s", exc.__class__.__name__)
        _mod["m"] = None
    return _mod["m"]


def available() -> bool:
    """A model call could run: the secretary is on and the model layer has a Claude login."""
    if not config.SECRETARY:
        return False
    m = llm()
    if m is None:
        return False
    try:
        return bool(m.available())
    except Exception:
        return False


def pct(x) -> str:
    try:
        return f"{round(float(x) * 100)}%"
    except (TypeError, ValueError):
        return "?%"


def _window(w) -> dict | None:
    if not isinstance(w, dict) or w.get("utilization") is None:
        return None
    try:
        util_ = float(w["utilization"])
    except (TypeError, ValueError):
        return None
    ra = w.get("resets_at")
    try:
        ra = int(float(ra)) if ra is not None else None
    except (TypeError, ValueError):
        ra = None
    return {"utilization": util_, "resets_at": ra, "status": str(w.get("status") or "")}


def raw_usage() -> dict | None:
    m = llm()
    if m is None:
        return None
    try:
        u = m.usage_state()
        return u if isinstance(u, dict) else None
    except Exception:
        return None


def window_over(w: dict | None, limit: float) -> bool:
    """usage.py's rule: a known window at or above its limit, or one the API called "rejected"."""
    if _rules is not None:
        return _rules.window_over(w, limit)
    return bool(w) and (w.get("status") == "rejected" or float(w["utilization"]) >= limit)


def usage_snapshot() -> dict:
    """The Usage shape of the API (plan meter). Never calls the model.

    Either window pauses the digests and the brief (§13): the 5-hour one at SECRETARY_PAUSE_AT, the 7-day one
    at SECRETARY_PAUSE_AT_WEEK. The rule and the reason text are usage.py's, so this, llm.usage_state() and
    every label built from it agree; the model layer saying "paused" is enough on its own too."""
    base = {"ok": False, "ts": None, "stale": False, "five_hour": None, "seven_day": None,
            "pause_at": float(config.PAUSE_AT), "pause_at_week": float(config.PAUSE_AT_WEEK), "over_pause": False,
            "reason": None, "error": None}
    if not config.SECRETARY:
        return dict(base, error="off")
    if not available():
        return dict(base, error="no_token")
    u = raw_usage()
    if u is None:
        return dict(base, error="not_yet")
    fh, sd = _window(u.get("five_hour")), _window(u.get("seven_day"))
    ts = u.get("ts")
    if ts is None:
        raw = store.kv_get("sec.usage_ts")
        ts = float(raw) if raw else None
    stale = bool(ts and time.time() - float(ts) > STALE_S)
    over5, over7 = window_over(fh, base["pause_at"]), window_over(sd, base["pause_at_week"])
    over = bool(u.get("paused")) or over5 or over7
    reason = None
    if over:
        said = _rules.pause_reason(fh, sd) if _rules is not None else ""
        reason = said or str(u.get("reason") or "").strip() or "Plan usage is high."
    err = None
    if fh is None and sd is None:
        why = str(u.get("reason") or "").lower()
        err = "probe_failed" if ("fail" in why or "error" in why) else "not_yet"
    return dict(base, ok=fh is not None or sd is not None, ts=float(ts) if ts else None, stale=stale, five_hour=fh,
                seven_day=sd, over_pause=over, reason=reason, error=err)


async def probe() -> None:
    """Ask the model layer to refresh plan usage; it calls the model at most once a minute."""
    m = llm()
    if m is None or not available():
        return
    try:
        await m.probe_usage()
        store.kv_put("sec.usage_ts", time.time())
    except Exception as exc:
        log.info("usage probe failed: %s", exc.__class__.__name__)


def usage_label() -> str | None:
    """'paused at 61% of 5 h' (or '… of the week') when plan usage holds the digests back, else None."""
    u = usage_snapshot()
    if not u["over_pause"]:
        return None
    fh, sd = u["five_hour"], u["seven_day"]
    if window_over(fh, u["pause_at"]):
        return f"paused at {pct(fh['utilization'])} of 5\u00a0h"  # a no-break space: "5 h" never splits
    if window_over(sd, u["pause_at_week"]):
        return f"paused at {pct(sd['utilization'])} of the week"
    return "paused for plan usage"
