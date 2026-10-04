"""Knocking on the glass: an agent asks the human for something only a person can do."""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field

from . import journal, notify

_ids = itertools.count(1)


@dataclass
class Knock:
    id: int
    session: str
    label: str
    question: str
    ts: float = field(default_factory=time.time)
    answer: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict:
        return {"id": self.id, "session": self.session, "label": self.label, "question": self.question, "ts": self.ts}


_open: dict[int, Knock] = {}


def waiting() -> list[dict]:
    return [k.public() for k in _open.values()]


async def ask(session: str, label: str, question: str, timeout: float) -> str | None:
    """Show the knock, post to Discord, and wait for the answer (None on timeout)."""
    k = Knock(next(_ids), session, label, question.strip()[:1000])
    _open[k.id] = k
    journal.publish("knocks", waiting())
    await notify.discord(
        f"**Deskmate · {label} needs you**\n{k.question[:300]}\nAnswer at http://127.0.0.1:7800"
    )
    try:
        await asyncio.wait_for(k.done.wait(), timeout)
        return k.answer
    except asyncio.TimeoutError:
        return None
    finally:
        _open.pop(k.id, None)
        journal.publish("knocks", waiting())


def answer(knock_id: int, text: str) -> bool:
    k = _open.get(knock_id)
    if not k:
        return False
    k.answer = text
    k.done.set()
    return True
