"""The MCP server every Claude Code session on this machine connects to (§3.1 of the design)."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import config, desk, journal, knock, sessions, shots
from .browser import BrowserError, browser
from .lease import Busy, lease

INSTRUCTIONS = f"""Deskmate is one shared Linux desk (two 1280x800 monitors, Chromium, xterm) that every Claude Code \
session on this machine uses and that {config.OWNER} watches live. Use it for every browser need instead of Playwright, \
Puppeteer or a local browser. browser_open gives you your own tab; read pages with browser_snapshot (an outline with refs \
such as e12, far cheaper than a screenshot) and act with browser_act. Use desk_screenshot and desk_input only for what is \
not a web page; desk_input shares one mouse and keyboard and may answer busy. Pass why on every call: one line \
{config.OWNER} reads. For a login, one-time code, CAPTCHA or payment, call desk_ask_human instead of guessing. The desk's \
localhost is this machine's localhost, so dev servers work. Files cross through ~/.local/share/deskmate/exchange with desk_files."""

mcp = FastMCP(
    "deskmate",
    instructions=INSTRUCTIONS,
    host="127.0.0.1",
    port=config.PORT,
    streamable_http_path="/mcp",
    session_idle_timeout=None,
)

Why = Annotated[str, Field(description="One line: why you are doing this. Shown to the human watching the desk.")]
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def _sid(ctx: Context) -> str:
    req = ctx.request_context.request
    sid = req.headers.get("mcp-session-id") if req is not None else None
    return sid or "local"


def _begin(ctx: Context, tool: str, args: dict, why: str) -> str:
    sid = _sid(ctx)
    sessions.touch(sid, tool, args, why)
    return sid


def _refuse_if_blocked() -> None:
    reason = lease.refusal()
    if reason:
        raise ToolError(reason)


def _fail(sid: str, tool: str, arg: str, why: str, exc: Exception) -> ToolError:
    status = "busy" if isinstance(exc, Busy) and str(exc).startswith("busy") else "refused" if isinstance(exc, Busy) else "error"
    journal.activity(sid, tool, arg, why, status, str(exc))
    return ToolError(str(exc))


# ---------------------------------------------------------------- browser


@mcp.tool(annotations=ACT, structured_output=False)
async def browser_open(
    url: Annotated[str, Field(description="Address to open, e.g. localhost:5173/login or https://example.com")],
    why: Why = "",
    new_tab: bool = False,
    ctx: Context = None,
) -> str:
    """Open a URL in your own tab on the desk (created on first use). Returns the title and an outline of the page."""
    sid = _begin(ctx, "browser_open", {"url": url, "why": why, "new_tab": new_tab}, why)
    try:
        _refuse_if_blocked()
        out = await browser.open(sid, url, new_tab)
    except (BrowserError, Busy, ToolError) as exc:
        raise _fail(sid, "browser_open", url, why, exc)
    journal.activity(sid, "browser_open", url, why)
    return out


@mcp.tool(annotations=READ, structured_output=False)
async def browser_snapshot(
    why: Why = "",
    diff: Annotated[bool, Field(description="Only what changed since your last snapshot of this tab")] = False,
    ctx: Context = None,
) -> str:
    """Outline of your current tab: roles, names and refs (e12) to act on. Much cheaper than a screenshot."""
    sid = _begin(ctx, "browser_snapshot", {"why": why, "diff": diff}, why)
    try:
        out = await browser.snapshot(sid, diff)
    except BrowserError as exc:
        raise _fail(sid, "browser_snapshot", "diff" if diff else "", why, exc)
    journal.activity(sid, "browser_snapshot", "diff" if diff else "", why, note=f"{len(out)} characters")
    return out


@mcp.tool(annotations=ACT, structured_output=False)
async def browser_act(
    action: Literal["click", "dblclick", "rightclick", "hover", "fill", "type", "press", "select", "check", "uncheck", "focus", "scroll", "back", "reload"],
    ref: Annotated[str | None, Field(description="Element ref from browser_snapshot, e.g. e12")] = None,
    value: Annotated[str | None, Field(description="Text for fill/type, key for press (Enter, Tab, Control+a), option(s) for select separated by |, 'up' to scroll up")] = None,
    why: Why = "",
    ctx: Context = None,
) -> str:
    """Act on an element of your current tab. Returns what changed on the page."""
    sid = _begin(ctx, "browser_act", {"action": action, "ref": ref, "value": value, "why": why}, why)
    shown = f"{action} {ref or ''} {('“' + value + '”') if value and action in ('fill', 'type', 'press', 'select') else ''}".strip()
    try:
        _refuse_if_blocked()
        out = await browser.act(sid, action, ref, value)
    except (BrowserError, ToolError) as exc:
        raise _fail(sid, "browser_act", shown, why, exc)
    journal.activity(sid, "browser_act", shown, why)
    return out


@mcp.tool(annotations=READ, structured_output=False)
async def browser_read(
    what: Literal["text", "console", "network"] = "text",
    since: Annotated[int, Field(description="For console/network: the cursor from your last read, to get only new lines")] = 0,
    why: Why = "",
    ctx: Context = None,
) -> str:
    """Your current tab's text, its console messages, or its failed requests (4xx/5xx and network errors)."""
    sid = _begin(ctx, "browser_read", {"what": what, "since": since, "why": why}, why)
    try:
        out = await browser.read(sid, what, since)
    except BrowserError as exc:
        raise _fail(sid, "browser_read", what, why, exc)
    journal.activity(sid, "browser_read", what, why)
    return out


@mcp.tool(annotations=ACT, structured_output=False)
async def browser_tabs(
    action: Literal["list", "switch", "close", "new"] = "list",
    index: int | None = None,
    url: str | None = None,
    why: Why = "",
    ctx: Context = None,
) -> str:
    """List your tabs (including popups your pages opened), switch to one, close one, or open a new one."""
    sid = _begin(ctx, "browser_tabs", {"action": action, "index": index, "url": url, "why": why}, why)
    try:
        if action != "list":
            _refuse_if_blocked()
        out = await browser.tabs(sid, action, index, url)
    except (BrowserError, ToolError) as exc:
        raise _fail(sid, "browser_tabs", action, why, exc)
    if action != "list":
        journal.activity(sid, "browser_tabs", f"{action} {index if index is not None else url or ''}".strip(), why)
    return out


# ---------------------------------------------------------------- desk


@mcp.tool(annotations=READ, structured_output=False)
async def desk_screenshot(
    monitor: Annotated[int, Field(description="1 or 2; 0 for both side by side (scaled)")] = 1,
    region: Annotated[list[float] | None, Field(description="[x1, y1, x2, y2] on that monitor to zoom in, with a ruler grid")] = None,
    why: Why = "",
    ctx: Context = None,
):
    """A picture of a desk monitor with the pointer marked. Prefer browser_snapshot for web pages."""
    sid = _begin(ctx, "desk_screenshot", {"monitor": monitor, "region": region, "why": why}, why)
    arg = f"monitor {monitor}" + (f" region {region}" if region else "")
    try:
        jpeg, caption = await shots.capture(monitor, region)
    except (ValueError, desk.DeskError) as exc:
        raise _fail(sid, "desk_screenshot", arg, why, exc)
    journal.activity(sid, "desk_screenshot", arg, why)
    return [Image(data=jpeg, format="jpeg"), caption] if caption else Image(data=jpeg, format="jpeg")


def _step(monitor: int, step: dict) -> tuple[str, dict, str]:
    """One desk_input step → (deskd action, params, text for the activity feed)."""

    def pt(v) -> dict:
        x, y = shots.to_screen(monitor, float(v[0]), float(v[1]))
        return {"x": x, "y": y}

    if "click" in step:
        return "click_mouse", {
            "coordinates": pt(step["click"]),
            "button": step.get("button", "left"),
            "clickCount": int(step.get("count", 1)),
            "holdKeys": step.get("hold") or [],
        }, f"click {step['click']}"
    if "move" in step:
        return "move_mouse", {"coordinates": pt(step["move"])}, f"move {step['move']}"
    if "drag" in step:
        a, b = step["drag"]
        return "drag_mouse", {"path": [pt(a), pt(b)], "button": step.get("button", "left")}, f"drag {a}→{b}"
    if "scroll" in step:
        return "scroll", {
            "coordinates": pt(step["scroll"]),
            "direction": step.get("direction", "down"),
            "scrollCount": int(step.get("amount", 3)),
        }, f"scroll {step.get('direction', 'down')} at {step['scroll']}"
    if "type" in step:
        text = str(step["type"])
        return "type_text", {"text": text}, f"type “{text[:40]}{'…' if len(text) > 40 else ''}”"
    if "key" in step:
        keys = step["key"] if isinstance(step["key"], list) else [step["key"]]
        return "type_keys", {"keys": [str(k) for k in keys]}, f"key {'+'.join(map(str, keys)) if len(keys) == 1 else ' '.join(map(str, keys))}"
    if "wait" in step:
        return "wait", {"duration": min(10_000, int(step["wait"]))}, f"wait {step['wait']} ms"
    raise ValueError(f"unknown step {step}: use click, move, drag, scroll, type, key or wait")


@mcp.tool(annotations=ACT, structured_output=False)
async def desk_input(
    steps: Annotated[
        list[dict],
        Field(
            description='Steps in order, coordinates on the chosen monitor: {"click":[x,y],"button":"left","count":1,"hold":["ctrl"]}, '
            '{"move":[x,y]}, {"drag":[[x1,y1],[x2,y2]]}, {"scroll":[x,y],"direction":"down","amount":3}, {"type":"text"}, '
            '{"key":"ctrl+l"} or {"key":["Tab","Return"]}, {"wait":500}'
        ),
    ],
    monitor: Annotated[int, Field(description="1 or 2")] = 1,
    screenshot: Annotated[bool, Field(description="Return a picture of the monitor afterwards")] = True,
    why: Why = "",
    ctx: Context = None,
):
    """Mouse and keyboard on the desk, for anything that is not a web page (or what browser_act cannot reach).
    Takes the shared input lease: answers busy if another session is typing."""
    sid = _begin(ctx, "desk_input", {"steps": steps, "monitor": monitor, "screenshot": screenshot, "why": why}, why)
    try:
        plan = [_step(monitor, s) for s in steps]
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise _fail(sid, "desk_input", "", why, ValueError(f"Bad steps: {exc}"))
    shown = "; ".join(p[2] for p in plan)[:300]
    try:
        await lease.acquire(sid, sessions.label)
        for action, params, _text in plan:
            if lease.holder != sid:
                raise Busy(lease.refusal() or "The input lease was taken from you mid-way. Check the screen before going on.")
            await desk.act(action, **params)
            lease.touch(sid)
    except (Busy, desk.DeskError) as exc:
        raise _fail(sid, "desk_input", shown, why, exc)
    journal.activity(sid, "desk_input", shown, why)
    if not screenshot:
        return f"done: {len(plan)} steps"
    await desk.act("wait", duration=300)
    jpeg, caption = await shots.capture(monitor)
    return [f"done: {len(plan)} steps", Image(data=jpeg, format="jpeg"), caption]


def _safe_name(name: str) -> str:
    base = Path(str(name)).name
    if not base or base in (".", ".."):
        raise ValueError("name must be a plain file name")
    return base


@mcp.tool(annotations=ACT, structured_output=False)
async def desk_files(
    op: Literal["put", "get", "list"],
    name: Annotated[str | None, Field(description="File name. put: the file must be in ~/.local/share/deskmate/exchange on this machine")] = None,
    why: Why = "",
    ctx: Context = None,
) -> str:
    """Move files between this machine and the desk. put: exchange folder → the desk's ~/Uploads.
    get: the desk's ~/Downloads → exchange folder. list: what is in the desk's Downloads and Uploads."""
    sid = _begin(ctx, "desk_files", {"op": op, "name": name, "why": why}, why)
    try:
        if op == "list":
            out = []
            for folder in ("Downloads", "Uploads"):
                data = await desk.act("list_files", path=folder)
                rows = [f"  {e['name']}  {e['size']} bytes" for e in data.get("entries", []) if not e.get("dir")]
                out.append(f"{folder}:\n" + ("\n".join(rows) if rows else "  (empty)"))
            return "\n".join(out)
        fname = _safe_name(name or "")
        if op == "put":
            src = config.EXCHANGE_DIR / fname
            if not src.is_file():
                raise ValueError(f"{config.EXCHANGE_HOST_DIR}/{fname} does not exist. Copy the file there first.")
            data = src.read_bytes()
            if len(data) > 50 * 1024 * 1024:
                raise ValueError("files over 50 MB are not sent")
            await desk.act("write_file", path=f"Uploads/{fname}", data=base64.b64encode(data).decode())
            journal.activity(sid, "desk_files", f"put {fname}", why, note=f"{len(data)} bytes")
            return f"Sent to the desk: ~/Uploads/{fname} ({len(data)} bytes)"
        data = await desk.act("read_file", path=f"Downloads/{fname}")
        raw = base64.b64decode(data["data"])
        config.EXCHANGE_DIR.mkdir(parents=True, exist_ok=True)
        (config.EXCHANGE_DIR / fname).write_bytes(raw)
        journal.activity(sid, "desk_files", f"get {fname}", why, note=f"{len(raw)} bytes")
        return f"Saved to {config.EXCHANGE_HOST_DIR}/{fname} ({len(raw)} bytes)"
    except (ValueError, desk.DeskError, OSError) as exc:
        raise _fail(sid, "desk_files", f"{op} {name or ''}", why, exc)


@mcp.tool(annotations=READ, structured_output=False)
async def desk_status(ctx: Context = None) -> str:
    """Who holds the mouse and keyboard, which tabs belong to which session, and whether a knock is waiting."""
    sid = _begin(ctx, "desk_status", {}, "")
    st = lease.state()
    if st["human"]:
        holder = f"{config.OWNER} has the desk"
    elif st["paused"]:
        holder = f"{config.OWNER} paused the agents"
    elif st["holder"]:
        holder = f"input held by {sessions.label(st['holder'])} for {int(time.time() - st['since'])} s"
    else:
        holder = "input free"
    count, w, h = config.monitors()
    lines = [f"{holder}. {count} monitors of {w}x{h}. You are {sessions.label(sid)}."]
    tabs = await browser.all_tabs()
    for s in sessions.active():
        mine = [t for t in tabs if t["owner"] == s["id"]]
        if mine:
            lines.append(f"{s['letter']} {s['display']}: " + "; ".join(t["title"] or t["url"] for t in mine))
    unowned = [t for t in tabs if not t["owner"]]
    if unowned:
        lines.append(f"Not owned by a session: {len(unowned)} tabs")
    waiting = knock.waiting()
    if waiting:
        lines.append("Knocks waiting: " + "; ".join(f"{k['label']}: {k['question'][:80]}" for k in waiting))
    return "\n".join(lines)


@mcp.tool(annotations=ACT, structured_output=False)
async def desk_ask_human(
    question: Annotated[str, Field(description=f"What you need from {config.OWNER}, e.g. 'Enter the OTP sent to your phone, then press Resume'")],
    wait_minutes: Annotated[int, Field(description="How long to wait, 1–30")] = 15,
    why: Why = "",
    ctx: Context = None,
) -> str:
    """Knock on the glass: a banner on the live view and a Discord message, then wait for the human's reply or Resume.
    Use for logins, one-time codes, CAPTCHAs, payments, or a decision only they can make."""
    sid = _begin(ctx, "desk_ask_human", {"question": question, "wait_minutes": wait_minutes, "why": why}, why)
    journal.activity(sid, "desk_ask_human", "", why or question[:120], "knock", "Posted to Discord")
    answer = await knock.ask(sid, sessions.label(sid), question, max(1, min(30, wait_minutes)) * 60)
    if answer is None:
        journal.activity(sid, "desk_ask_human", "", "No answer", "error", f"Waited {wait_minutes} min")
        return f"No answer within {wait_minutes} minutes. Carry on without it, or ask again later."
    journal.activity(sid, "desk_ask_human", "", "Answered", "ok", answer[:200])
    return f"{config.OWNER} answered: {answer}" if answer else f"{config.OWNER} pressed Resume without a reply."
