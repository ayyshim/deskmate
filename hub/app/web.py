"""The hub's HTTP side on 127.0.0.1:<HUB_PORT>: MCP, hooks, the web UI and its live views.

Auth (§3.5, auth.py): /mcp and /hooks take `Authorization: Bearer <hub token>`. The UI takes a
cookie set once by the sign-in link that `./deskmate open` prints. Requests from another origin are
refused, because the desk's own browser can reach this machine's localhost, and every request must
carry the hub's own address as its Host (auth.TrustedHosts), or it gets 421.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from . import auth, config, db, desk, journal, knock, notify, sessions
from .browser import browser
from .lease import lease
from .tools import mcp

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
# Kept as module names so the routes below read as before; the definitions live in auth.py.
COOKIE = auth.COOKIE
ALLOWED_ORIGINS = auth.ALLOWED_ORIGINS
_ui_secret = auth.ui_secret
_bearer_ok = auth.bearer_ok
_ui_ok = auth.ui_ok
_need_ui = auth.need_ui


class RevalidatedStatic(StaticFiles):
    """Static files the browser may keep but must check first (Cache-Control: no-cache; a 304 is cheap).
    Without it, the heuristic cache can serve last version's ES modules after `./deskmate update`."""

    async def get_response(self, path: str, scope) -> Response:
        resp = await super().get_response(path, scope)
        resp.headers.setdefault("Cache-Control", "no-cache")
        return resp


DESK_DOWN = "The desk is not running, so its Downloads folder cannot be read. Start it with ./deskmate up."


class BearerGuard:
    """Wraps the MCP endpoint: a bearer token or 401."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if not _bearer_ok(headers):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    if not config.TOKEN:
        raise SystemExit("The hub token is empty: run `./deskmate setup` (it writes secrets/hub-token in the data folder).")
    db.conn()
    tasks = [asyncio.create_task(lease.watch_idle())]

    async def connect_browser() -> None:
        try:
            await browser.ensure()
        except Exception as exc:
            log.warning("browser not ready yet: %s", exc)

    tasks.append(asyncio.create_task(connect_browser()))
    try:
        from . import secretary  # M4

        tasks.extend(secretary.start())
    except (ImportError, AttributeError):
        pass
    async with mcp.session_manager.run():
        yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(auth.TrustedHosts)  # the whole app: pages, API, /mcp, /hooks, /vnc, static files
try:
    # The secretary (M4–M6) lives in app/secretary and brings its own /api/sec routes.
    from .secretary.api import router as _secretary_router

    app.include_router(_secretary_router)
except ImportError:
    pass
_mcp_app = mcp.streamable_http_app()
app.router.routes.append(Route("/mcp", endpoint=BearerGuard(_mcp_app.routes[0].endpoint)))
app.mount("/static", RevalidatedStatic(directory=STATIC), name="static")
if Path("/opt/novnc").is_dir():
    app.mount("/novnc", RevalidatedStatic(directory="/opt/novnc"), name="novnc")


# ---------------------------------------------------------------- pages


@app.get("/")
async def index(request: Request):
    # Both answers live at "/": neither may be cached, or the page after signing in could be the old sign-in page.
    if not _ui_ok(request.cookies, request.headers):
        return FileResponse(STATIC / "signin.html", headers={"Cache-Control": "no-store"})
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/login")
async def login(t: str = ""):
    if not config.TOKEN or not hmac.compare_digest(t, config.TOKEN):
        raise HTTPException(401, "That sign-in link is not valid. Run `./deskmate open` for a fresh one.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _ui_secret(), httponly=True, samesite="strict", max_age=400 * 86400)
    return resp


# ---------------------------------------------------------------- desk API


@app.get("/api/state")
async def state(request: Request):
    _need_ui(request)
    count, w, h = config.monitors()
    st = lease.state()
    st["label"] = sessions.label(st["holder"]) if st["holder"] else None
    return {
        "owner": config.OWNER,
        "network": config.NETWORK,
        "notify": notify.kind(),
        "monitors": {"count": count, "width": w, "height": h},
        "lease": st,
        "sessions": sessions.active(),
        "tabs": await browser.all_tabs(),
        "knocks": knock.waiting(),
        "activity": journal.recent(80),
        "desk": await desk.health(),
    }


@app.get("/api/events")
async def events(request: Request):
    _need_ui(request)
    q = journal.subscribe()

    async def stream():
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield f"event: {ev['kind']}\ndata: {json.dumps(ev['data'], default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            journal.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.post("/api/takeover")
async def takeover(request: Request):
    _need_ui(request)
    await lease.take_over()
    journal.activity("human", "take over", "", "Agents wait until you hand back")
    return {"ok": True}


@app.post("/api/handback")
async def handback(request: Request):
    _need_ui(request)
    await lease.hand_back()
    journal.activity("human", "hand back", "", "Agents may continue")
    return {"ok": True}


@app.post("/api/pause")
async def pause(request: Request):
    _need_ui(request)
    body = await request.json()
    await lease.set_paused(bool(body.get("paused")))
    journal.activity("human", "pause agents" if lease.paused else "resume agents", "", "")
    return {"ok": True, "paused": lease.paused}


@app.post("/api/knock/{knock_id}")
async def answer_knock(knock_id: int, request: Request):
    _need_ui(request)
    body = await request.json()
    if not knock.answer(knock_id, str(body.get("text") or "")):
        raise HTTPException(404, "That knock was already answered or timed out.")
    return {"ok": True}


@app.post("/api/clipboard")
async def clipboard_to_desk(request: Request):
    _need_ui(request)
    body = await request.json()
    await desk.act("set_clipboard", text=str(body.get("text") or ""))
    return {"ok": True}


@app.get("/api/clipboard")
async def clipboard_from_desk(request: Request):
    _need_ui(request)
    data = await desk.act("get_clipboard")
    return {"text": (data or {}).get("text", "")}


@app.post("/api/upload")
async def upload(request: Request, name: str):
    _need_ui(request)
    fname = Path(name).name
    if not fname or fname in (".", ".."):
        raise HTTPException(400, "Bad file name.")
    data = await request.body()
    await desk.act("write_file", path=f"Uploads/{fname}", data=base64.b64encode(data).decode())
    journal.activity("human", "upload", fname, "", note=f"{len(data)} bytes to ~/Uploads")
    return {"ok": True}


@app.get("/api/downloads")
async def downloads(request: Request):
    """The desk's ~/Downloads. The Desk page asks every 15 s; with the desk down that is a 503 and one
    sentence, not a traceback in the log each time."""
    _need_ui(request)
    try:
        data = await desk.act("list_files", path="Downloads")
    except desk.DeskDown:
        raise HTTPException(503, DESK_DOWN) from None
    return [e for e in (data or {}).get("entries", [])
            if not e.get("dir") and not e["name"].endswith((".crdownload", ".part"))]


@app.get("/api/downloads/{name}")
async def download(name: str, request: Request):
    _need_ui(request)
    fname = Path(name).name
    try:
        data = await desk.act("read_file", path=f"Downloads/{fname}")
    except desk.DeskDown:
        raise HTTPException(503, DESK_DOWN) from None
    except desk.DeskError:
        raise HTTPException(404, "That file is no longer in the desk's Downloads folder.") from None
    return Response(
        base64.b64decode(data["data"]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@app.post("/api/restart-browser")
async def restart_browser(request: Request):
    _need_ui(request)
    await desk.act("restart_browser")
    journal.activity("human", "restart browser", "", "Sessions get a new tab on their next call")
    return {"ok": True}


@app.websocket("/vnc")
async def vnc(ws: WebSocket):
    """Bridge noVNC to Xvnc's unix socket. View-only is enforced by the page; the human owns it."""
    if not _ui_ok(ws.cookies, ws.headers):
        await ws.close(code=4401)
        return
    offered = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol="binary" if "binary" in offered else None)
    try:
        reader, writer = await asyncio.open_unix_connection(str(config.RUN_DIR / "vnc.sock"))
    except OSError:
        await ws.close(code=1011)
        return

    async def up() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            writer.write(data)
            await writer.drain()

    async def down() -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        writer.close()
        with contextlib.suppress(Exception):
            await ws.close()


# ---------------------------------------------------------------- hooks


@app.post("/hooks/{event}")
async def hooks(event: str, request: Request):
    """Claude Code hooks (plugin/deskmate/bin/hook). Must answer fast: the hook gives up after 1 s."""
    if not _bearer_ok(request.headers):
        raise HTTPException(401)
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if event == "pre-tool":
        sessions.hook_pre_tool(payload)
    else:
        try:
            from . import secretary

            secretary.hook(event, payload)
        except (ImportError, AttributeError):
            pass
    return {"ok": True}
