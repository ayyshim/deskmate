"""Who may use the desk's mouse and keyboard right now (§3.2 of the design).

Browser tools need no lease: each session works in its own tab over CDP. Mouse and keyboard
input is global, so `desk_input` takes the lease first. The human beats everyone: while they
hold the desk, or have paused the agents, every action is refused.
"""

from __future__ import annotations

import asyncio
import time

from . import config, journal

HUMAN = "human"


class Busy(RuntimeError):
    """The desk cannot be used right now; the message says why and what to do."""


class Lease:
    def __init__(self) -> None:
        self.holder: str | None = None
        self.since = 0.0
        self.last = 0.0
        self.paused = False
        self._changed = asyncio.Condition()

    def state(self) -> dict:
        return {
            "holder": self.holder,
            "since": self.since,
            "last": self.last,
            "paused": self.paused,
            "human": self.holder == HUMAN,
        }

    def refusal(self) -> str | None:
        """Why agents may not act at all right now, or None."""
        if self.holder == HUMAN:
            return f"{config.OWNER} has the desk. Wait until they hand it back, or ask them with desk_ask_human."
        if self.paused:
            return f"{config.OWNER} paused the agents. Try again later."
        return None

    def _free_for(self, session: str) -> bool:
        if self.holder in (None, session):
            return True
        return time.time() - self.last > config.INPUT_IDLE_RELEASE

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()
        journal.publish("lease", self.state())

    async def acquire(self, session: str, label_of) -> None:
        """Take or renew the input lease, waiting up to INPUT_WAIT seconds for another session."""
        deadline = time.time() + config.INPUT_WAIT
        async with self._changed:
            while True:
                reason = self.refusal()
                if reason:
                    raise Busy(reason)
                if self._free_for(session):
                    if self.holder != session:
                        self.since = time.time()
                    self.holder = session
                    self.last = time.time()
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    held = int(time.time() - self.since)
                    raise Busy(
                        f"busy: {label_of(self.holder)} has held the mouse and keyboard for {held} s. "
                        "Try again in a minute. Browser tools work in your own tab and need no lease."
                    )
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=min(remaining, 2.0))
                except asyncio.TimeoutError:
                    pass
        journal.publish("lease", self.state())

    def touch(self, session: str) -> None:
        if self.holder == session:
            self.last = time.time()

    async def release(self, session: str) -> None:
        if self.holder == session:
            self.holder = None
            await self._notify()

    async def take_over(self) -> None:
        self.holder = HUMAN
        self.since = self.last = time.time()
        await self._notify()

    async def hand_back(self) -> None:
        if self.holder == HUMAN:
            self.holder = None
            await self._notify()

    async def set_paused(self, paused: bool) -> None:
        self.paused = paused
        await self._notify()

    async def watch_idle(self) -> None:
        """Release an agent's lease after INPUT_IDLE_RELEASE seconds without input."""
        while True:
            await asyncio.sleep(5)
            if self.holder not in (None, HUMAN) and time.time() - self.last > config.INPUT_IDLE_RELEASE:
                self.holder = None
                await self._notify()


lease = Lease()
