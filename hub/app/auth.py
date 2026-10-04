"""Who may use the hub (§3.5 of the design).

/mcp and /hooks take `Authorization: Bearer <hub token>`. The UI takes a cookie set once by the
sign-in link that `./deskmate open` prints. Requests from another origin are refused, because the
desk's own browser shares this machine's localhost. Shared by web.py and the secretary's routes.

Every request must also name the hub by its own address in the Host header (TrustedHosts). That
stops DNS rebinding (a page on evil.example that makes the browser resolve evil.example to
127.0.0.1) and, in the bridge network modes, a page in the desk that finds the hub by its service
name (http://hub:<port>/).
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request
from starlette.responses import PlainTextResponse

from . import config

COOKIE = "deskmate"
# The published port equals the hub's own port in every network mode, so these are exact.
ALLOWED_HOSTS = frozenset({f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}", f"[::1]:{config.PORT}"})
ALLOWED_ORIGINS = frozenset({f"http://{h}" for h in ALLOWED_HOSTS})


def ui_secret() -> str:
    return hmac.new(config.TOKEN.encode(), b"deskmate-ui", hashlib.sha256).hexdigest()


def bearer_ok(headers) -> bool:
    auth = headers.get("authorization", "")
    return bool(config.TOKEN) and hmac.compare_digest(auth, f"Bearer {config.TOKEN}")


def ui_ok(cookies, headers) -> bool:
    origin = headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return False
    return hmac.compare_digest(cookies.get(COOKIE, ""), ui_secret())


def need_ui(request: Request) -> None:
    """FastAPI routes call this first; the secretary's routes can use it as a dependency."""
    if not ui_ok(request.cookies, request.headers):
        raise HTTPException(401, "Sign in with the link from `./deskmate open`.")


def host_ok(host: str | None) -> bool:
    """Whether a Host header names this hub: 127.0.0.1, localhost or [::1], with the hub's port."""
    return (host or "").strip().lower() in ALLOWED_HOSTS


class TrustedHosts:
    """ASGI middleware for the whole app: any other Host header gets 421, as the MCP SDK answers.

    Not Starlette's TrustedHostMiddleware: that one ignores the port and answers 400. A websocket
    with a foreign Host is closed before the handshake, which the server turns into a 403.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = next((v.decode("latin-1") for k, v in scope.get("headers") or [] if k == b"host"), None)
            if not host_ok(host):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                response = PlainTextResponse(f"Misdirected request: open Deskmate at {config.HUB_URL}", status_code=421)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
