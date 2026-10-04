"""Client for deskd, the desk's control daemon, over its unix socket."""

from __future__ import annotations

import base64

import httpx

from . import config


class DeskError(RuntimeError):
    """deskd refused or failed an action; the message is deskd's own."""


_client = httpx.AsyncClient(
    transport=httpx.AsyncHTTPTransport(uds=config.DESKD_SOCKET),
    base_url="http://deskd",
    timeout=httpx.Timeout(60.0),
)


async def act(action: str, **params):
    try:
        r = await _client.post("/computer-use/computer", json={"action": action, **params})
    except httpx.HTTPError as exc:
        raise DeskError(f"the desk is not answering ({exc.__class__.__name__}). Is the desk container running?") from exc
    body = r.json()
    if not body.get("success"):
        raise DeskError(body.get("error") or "deskd failed")
    return body.get("data")


async def health() -> dict:
    try:
        r = await _client.get("/")
        return r.json()
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)}


async def screenshot_png() -> bytes:
    data = await act("screenshot")
    return base64.b64decode(data["image"])


async def cursor() -> tuple[int, int]:
    data = await act("cursor_position")
    return int(data["x"]), int(data["y"])
