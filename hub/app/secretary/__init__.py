"""The secretary (design §3.3–§3.5, milestones M4–M6): it reads the Claude Code sessions, the docs folder,
memory notes and git history the user chose, and turns them into the Secretary pages, digests, a daily
brief and answers to questions. See README.md in this folder for the parts and the shapes they share.

web.py calls start() in its lifespan (it returns the background tasks, none when SECRETARY=off) and
hook(event, payload) for the Stop, SessionEnd and PreCompact hooks; api.router serves /api/sec.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def start() -> list:
    """Run the migrations and start the background work. Never raises: the desk must start regardless."""
    try:
        from .scheduler import SCHED

        return SCHED.start()
    except Exception:
        log.exception("secretary did not start")
        return []


def hook(event: str, payload: dict) -> None:
    """A Claude Code hook arrived (fast: it only schedules work)."""
    try:
        from .scheduler import SCHED

        SCHED.hook(event, payload)
    except Exception:
        log.exception("secretary hook")
