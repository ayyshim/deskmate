"""Knocking on the glass: an agent asks the human for something only a person can do."""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field

from . import config, journal, notify

_ids = itertools.count(1)


@dataclass
class Knock:
    id: int
    session: str
    label: str
    question: str
    ts: float = field(default_factory=time.time)
    answer: str | None = None
    notified: str = ""  # how the notification went, e.g. "sent to Discord" or "notifications are off"
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict:
        return {
            "id": self.id,
            "session": self.session,
            "label": self.label,
            "question": self.question,
            "ts": self.ts,
            "notified": self.notified,
        }


_open: dict[int, Knock] = {}


def waiting() -> list[dict]:
    return [k.public() for k in _open.values()]


async def post(session: str, label: str, question: str) -> Knock:
    """Show the knock on the live view and send the notification; k.notified says how that went."""
    k = Knock(next(_ids), session, label, question.strip()[:1000])
    _open[k.id] = k
    journal.publish("knocks", waiting())
    try:
        result = await notify.send(
            f"{k.question[:300]}\nAnswer at {config.HUB_URL}",
            title=f"Deskmate · {label} needs you",
            event="knock",
        )
    except BaseException:  # cancelled while sending: no banner may outlive its session's call
        _open.pop(k.id, None)
        journal.publish("knocks", waiting())
        raise
    k.notified = result["detail"]
    journal.publish("knocks", waiting())
    return k


async def wait(k: Knock, timeout: float) -> str | None:
    """The human's answer ("" for Resume without a reply), or None on timeout."""
    try:
        await asyncio.wait_for(k.done.wait(), timeout)
        return k.answer
    except asyncio.TimeoutError:
        return None
    finally:
        _open.pop(k.id, None)
        journal.publish("knocks", waiting())


async def ask(session: str, label: str, question: str, timeout: float) -> str | None:
    """Post a knock and wait for the answer (None on timeout)."""
    return await wait(await post(session, label, question), timeout)


def answer(knock_id: int, text: str) -> bool:
    k = _open.get(knock_id)
    if not k:
        return False
    k.answer = text
    k.done.set()
    return True
