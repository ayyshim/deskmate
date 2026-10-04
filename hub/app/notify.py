"""Discord messages through the webhook file mounted from ~/.config/claude-notify (never copied)."""

from __future__ import annotations

import logging

import httpx

from . import config

log = logging.getLogger(__name__)


async def discord(text: str) -> int | None:
    """Post a message; returns the HTTP status, or None if there is no webhook or it failed."""
    try:
        url = config.WEBHOOK_FILE.read_text().strip()
    except OSError:
        return None
    if not url.startswith("https://"):
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json={"content": text[:1900]})
            return r.status_code
    except httpx.HTTPError as exc:
        log.warning("discord post failed: %s", exc.__class__.__name__)
        return None
