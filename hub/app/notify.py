"""Notifications: a short message to the user's phone or team chat (§3.5 of the design).

The URL is a secret file (config.notify_url(), read on every send, so a new URL works without a
restart) and NOTIFY_KIND says what answers at the other end: discord, slack, ntfy, or webhook (any URL
that takes a JSON POST). Knocks, the notify tool and the secretary's morning post all go through
send(), which never raises: a notification is a courtesy, and a failed one must not fail the work that
sent it. Neither the URL nor anything taken from it is ever logged or returned.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from . import config, db

log = logging.getLogger(__name__)

KINDS = ("discord", "slack", "ntfy", "webhook")
NAMES = {"discord": "Discord", "slack": "Slack", "ntfy": "ntfy", "webhook": "the webhook"}
TEXT_CAP = 1900  # Discord takes 2000 characters; one cut for every kind keeps messages short
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}

# The notify tool's limits. Knocks and the morning post do not count against them.
SESSION_GAP = 120.0  # seconds between two messages from one session
DAILY_MAX = 40  # messages a day from all sessions together

Image = tuple[str, bytes, str]  # file name, bytes, media type


def guess_kind(url: str) -> str:
    """What a URL most likely is, from its host (setup fills NOTIFY_KIND with the same rule)."""
    host = (urlsplit(url or "").hostname or "").lower()
    if host in ("discord.com", "discordapp.com") or host.endswith((".discord.com", ".discordapp.com")):
        return "discord"
    if host == "hooks.slack.com":
        return "slack"
    if host == "ntfy.sh" or host.startswith("ntfy."):
        return "ntfy"
    return "webhook"


def kind() -> str:
    """Either none or one of KINDS. A NOTIFY_KIND this hub does not know is guessed from the URL's host."""
    k = config.NOTIFY_KIND
    if k in ("", "none", "off"):
        return "none"
    return k if k in KINDS else guess_kind(config.notify_url())


def _cut(text: str, cap: int = TEXT_CAP) -> str:
    text = (text or "").strip()
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def _slack_escape(text: str) -> str:
    """Slack reads <…> as links and mentions (<!channel>); escaped, the text stays text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def request_for(kind: str, text: str, title: str = "", images: list[Image] | tuple = (), event: str = "notify") -> tuple[dict, str]:
    """The keyword arguments for httpx's post() for one message, and a note on anything not sent."""
    images = list(images or [])
    if kind == "discord":
        # allowed_mentions: a session's text must never ping @everyone or a role.
        body = {"content": _cut(f"**{title}**\n{text}" if title else text), "allowed_mentions": {"parse": []}}
        if images:
            files = [(f"files[{i}]", (name, data, mime)) for i, (name, data, mime) in enumerate(images)]
            return {"data": {"payload_json": json.dumps(body)}, "files": files}, ""
        return {"json": body}, ""
    if kind == "slack":
        body = {"text": _cut(f"*{_slack_escape(title)}*\n{_slack_escape(text)}" if title else _slack_escape(text))}
        return {"json": body}, "images not sent: Slack webhooks take text only" if images else ""
    if kind == "ntfy":
        # The title as a query parameter: ntfy reads it there as well as from a header, and a query
        # string carries any character where a header would need encoding. send() merges it with the
        # URL's own parameters (an access token, say): httpx would replace those with params.
        kwargs = {"content": _cut(text).encode(), "headers": {"Content-Type": "text/plain; charset=utf-8"}}
        if title:
            kwargs["params"] = {"title": title}
        return kwargs, "images not sent: ntfy gets text only" if images else ""
    body = {"source": "deskmate", "event": event, "title": title, "text": _cut(text)}
    if images:
        body["images"] = [{"name": n, "type": m, "data": base64.b64encode(d).decode()} for n, d, m in images]
    return {"json": body}, ""


def _result(ok: bool, kind: str, detail: str, status: int | None = None) -> dict:
    return {"ok": ok, "kind": kind, "status": status, "detail": detail}


async def send(text: str, *, title: str = "", images: list[Image] | tuple = (), event: str = "notify") -> dict:
    """Send one message. Returns {ok, kind, status, detail}; detail is a short sentence for people,
    e.g. "sent to Discord" or "Slack answered 404". Never raises.

    title: a first line in bold where the kind has one, e.g. "Deskmate · <session label>".
    images: (file name, bytes, media type) tuples, sent where the kind takes files.
    event: what the message is about, for webhooks that route on it: notify, knock or brief."""
    k = kind()
    if k == "none":
        return _result(False, k, "notifications are off")
    try:
        url = config.notify_url()
        if not url:
            return _result(False, k, "no notification address is set up")
        if urlsplit(url).scheme not in ("http", "https"):
            return _result(False, k, "the notification address is not an http(s) URL")
        kwargs, note = request_for(k, text, title, images, event)
        if "params" in kwargs:
            kwargs["params"] = httpx.URL(url).params.merge(kwargs["params"])
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, **kwargs)
    except Exception as exc:  # never raise; never show the URL (exception texts can carry it)
        log.warning("notification via %s failed: %s", k, exc.__class__.__name__)
        return _result(False, k, f"could not reach {NAMES[k]} ({exc.__class__.__name__})")
    ok = 200 <= r.status_code < 300
    if not ok:
        log.warning("notification via %s answered %s", k, r.status_code)
    detail = f"sent to {NAMES[k]}" if ok else f"{NAMES[k]} answered {r.status_code}"
    return _result(ok, k, f"{detail}; {note}" if note else detail, r.status_code)


async def discord(text: str) -> int | None:
    """Kept for callers written when Discord was the only kind: sends through the configured kind and
    returns the HTTP status, or None if nothing was sent."""
    return (await send(text))["status"]


# ---------------------------------------------------------------- the notify tool's images and limits


def load_images(names: list[str] | None, folder: Path) -> tuple[list[Image], list[str]]:
    """Image files from the exchange folder by plain file name: at most MAX_IMAGES, each at most
    MAX_IMAGE_BYTES. Returns the images and one note per file left out."""
    images: list[Image] = []
    notes: list[str] = []
    for raw in names or []:
        name = Path(str(raw)).name
        mime = IMAGE_TYPES.get(Path(name).suffix.lower())
        if not name or name in (".", ".."):
            notes.append(f"{raw!s}: not a plain file name")
        elif mime is None:
            notes.append(f"{name}: not an image (png, jpg, gif or webp)")
        elif len(images) >= MAX_IMAGES:
            notes.append(f"{name}: more than {MAX_IMAGES} images")
        else:
            path = folder / name
            try:
                size = path.stat().st_size
                if size > MAX_IMAGE_BYTES:
                    notes.append(f"{name}: over {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
                    continue
                images.append((name, path.read_bytes(), mime))
            except OSError:
                notes.append(f"{name}: not in the exchange folder")
    return images, notes


_last: dict[str, float] = {}


def _today() -> str:
    try:
        tz = ZoneInfo(config.TZ)
    except Exception:  # an unknown zone name: count days in UTC rather than fail
        tz = dt.timezone.utc
    return dt.datetime.now(tz).date().isoformat()


def _daily() -> tuple[str, int]:
    today = _today()
    day, _, count = (db.get("notify.daily") or "").partition(" ")
    if day == today and count.isdigit():
        return today, int(count)
    return today, 0


def limit(session: str, now: float | None = None) -> str | None:
    """Why this session may not send a message now, or None."""
    now = time.time() if now is None else now
    last = _last.get(session)
    if last is not None and now - last < SESSION_GAP:
        wait = int(SESSION_GAP - (now - last)) + 1
        return f"one message per {int(SESSION_GAP // 60)} minutes per session; try again in {wait} s"
    if _daily()[1] >= DAILY_MAX:
        return f"{DAILY_MAX} messages today already; the count starts again at midnight"
    return None


def count(session: str, now: float | None = None) -> None:
    """Record a message that went out, or was tried, for limit()."""
    now = time.time() if now is None else now
    for sid, ts in list(_last.items()):
        if now - ts >= SESSION_GAP:
            del _last[sid]
    _last[session] = now
    today, n = _daily()
    db.put("notify.daily", f"{today} {n + 1}")
