"""The activity journal and the live event stream the web UI listens to."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from . import db

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def publish(kind: str, data: Any) -> None:
    """Fan an event out to every open page. A page that stops reading loses events, not the hub."""
    for q in list(_subscribers):
        try:
            q.put_nowait({"kind": kind, "data": data})
        except asyncio.QueueFull:
            pass


def activity(session: str | None, tool: str, arg: str = "", why: str = "", status: str = "ok", note: str = "") -> dict:
    row = {
        "ts": time.time(),
        "session": session,
        "tool": tool,
        "arg": (arg or "")[:300],
        "why": (why or "")[:300],
        "status": status,
        "note": (note or "")[:500],
    }
    row["id"] = db.run(
        "insert into activity(ts, session, tool, arg, why, status, note) values(:ts, :session, :tool, :arg, :why, :status, :note)",
        row,
    )
    publish("activity", row)
    return row


def recent(limit: int = 80) -> list[dict]:
    rows = db.q("select * from activity order by id desc limit ?", (limit,))
    return [dict(r) for r in rows]
