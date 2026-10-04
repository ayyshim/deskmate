"""The MCP server every Claude Code session on this machine connects to (§3.1 of the design)."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from . import auth, config, desk, journal, knock, notify, sessions, shots
from .browser import BrowserError, browser
from .lease import Busy, lease

OWNER = config.OWNER
EXCHANGE = config.tilde(config.EXCHANGE_HOST_DIR)  # where the user finds the exchange folder on this machine


def _layout() -> str:
    count, (w, h) = config.DESK_MONITORS, config.DESK_MONITOR_SIZE
    return f"one {w}x{h} monitor" if count == 1 else f"{count} monitors of {w}x{h}"


def _monitor_choice(all_of_them: bool) -> str:
    count = config.DESK_MONITORS
    if count == 1:
        return "1 (the desk has one monitor)"
    choice = "1 or 2" if count == 2 else f"1 to {count}"
    return choice + ("; 0 for all of them side by side (scaled)" if all_of_them else "")


def network_text() -> str:
    """What the desk's browser can reach, which depends on the network mode setup chose."""
    if config.NETWORK == "isolated":
        return (
            "The desk's browser reaches the internet only, not this machine: a dev server on localhost does not open "
            "there, and neither does a page whose code calls an API on localhost or 127.0.0.1."
        )
    if config.NETWORK == "host-access":
        return (
            "In the desk's browser localhost, *.localhost, 127.0.0.1 and [::1] lead to this machine, so dev servers work, "
            "also when a page calls them from its code; a server that listens only on ::1 may not answer: start it on 127.0.0.1."
        )
    return "The desk's localhost is this machine's localhost, so dev servers work."


INSTRUCTIONS = (
    f"Deskmate is one shared Linux desk ({_layout()}, Chromium, a terminal) that every Claude Code session on this "
    f"machine uses and that {OWNER} watches live. Use it for every browser need instead of Playwright, Puppeteer or a "
    "local browser. browser_open gives you your own tab; read pages with browser_snapshot (an outline with refs such as "
    "e12, far cheaper than a screenshot) and act with browser_act. Use desk_screenshot and desk_input only for what is "
    "not a web page; desk_input shares one mouse and keyboard and may answer busy. Pass why on every call: one line "
    f"{OWNER} reads. For a login, one-time code, CAPTCHA or payment, call desk_ask_human instead of guessing. "
    f"{network_text()} Files cross through {EXCHANGE} with desk_files."
    + (f" notify sends {OWNER} a short message, for a milestone or a long job finished." if notify.kind() != "none" else "")
)

mcp = FastMCP(
    "deskmate",
    instructions=INSTRUCTIONS,
    host="127.0.0.1",
    port=config.PORT,
    streamable_http_path="/mcp",
    session_idle_timeout=None,
    # DNS-rebinding protection with the hub's own addresses. FastMCP turns it on by itself only when its host is
    # a loopback address; spelled out, it also holds in the bridge modes, where the hub listens on 0.0.0.0.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(auth.ALLOWED_HOSTS),
        allowed_origins=sorted(auth.ALLOWED_ORIGINS),
    ),
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


TYPED = ("fill", "type", "press", "select")


def act_text(action: str, ref: str | None, value: str | None) -> str:
    """A browser_act call as the journal shows it: typed text appears as its length, never itself."""
    parts = [action, ref or ""]
    if value and action in TYPED:
        parts.append(journal.mask(value))
    return " ".join(p for p in parts if p)


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
    shown = act_text(action, ref, value)
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
    monitor: Annotated[int, Field(description=_monitor_choice(all_of_them=True))] = 1,
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
        return "type_text", {"text": text}, f"type ({journal.chars(text)})"  # the length only, never the text
    if "key" in step:
        keys = step["key"] if isinstance(step["key"], list) else [step["key"]]
        return "type_keys", {"keys": [str(k) for k in keys]}, f"key {'+'.join(map(str, keys)) if len(keys) == 1 else ' '.join(map(str, keys))}"
    if "wait" in step:
        return "wait", {"duration": min(10_000, int(step["wait"]))}, f"wait {step['wait']} ms"
    # Name the keys, not the values: a misspelt {"typ": ...} step may hold a password.
    keys = ", ".join(sorted(map(str, step))) if isinstance(step, dict) else type(step).__name__
    raise ValueError(f"unknown step ({keys}): use click, move, drag, scroll, type, key or wait")


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
    monitor: Annotated[int, Field(description=_monitor_choice(all_of_them=False))] = 1,
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


def _exchange_path(fname: str) -> str:
    """Where a file in the exchange folder is on this machine, absolute so a session can read it."""
    if config.DATA_DIR_HOST:
        return f"{config.EXCHANGE_HOST_DIR}/{fname}"
    return f"{fname} in the exchange folder"


@mcp.tool(annotations=ACT, structured_output=False)
async def desk_files(
    op: Literal["put", "get", "list"],
    name: Annotated[str | None, Field(description=f"File name. put: the file must be in {EXCHANGE} on this machine")] = None,
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
                raise ValueError(f"{_exchange_path(fname)} does not exist. Copy the file there first.")
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
        return f"Saved to {_exchange_path(fname)} ({len(raw)} bytes)"
    except (ValueError, desk.DeskError, OSError) as exc:
        raise _fail(sid, "desk_files", f"{op} {name or ''}", why, exc)


@mcp.tool(annotations=READ, structured_output=False)
async def desk_status(ctx: Context = None) -> str:
    """Who watches the desk and where, who holds the mouse and keyboard, which tabs belong to which session,
    whether a knock is waiting, and whether notifications are on."""
    sid = _begin(ctx, "desk_status", {}, "")
    st = lease.state()
    if st["human"]:
        holder = f"{OWNER} has the desk"
    elif st["paused"]:
        holder = f"{OWNER} paused the agents"
    elif st["holder"]:
        holder = f"input held by {sessions.label(st['holder'])} for {int(time.time() - st['since'])} s"
    else:
        holder = "input free"
    count, w, h = config.monitors()
    kind = notify.kind()
    lines = [
        f"{OWNER} watches this desk live at {config.HUB_URL}.",
        f"{holder[0].upper() + holder[1:]}. {count} monitor{'' if count == 1 else 's'} of {w}x{h}. You are {sessions.label(sid)}.",
        network_text(),
        f"Notifications: on, through {notify.NAMES[kind]} (the notify tool)." if kind != "none" else "Notifications: off.",
    ]
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


@mcp.tool(
    annotations=ACT,
    structured_output=False,
    description=(
        f"Knock on the glass: a banner on the live view, and a notification if {OWNER} has them set up; then wait for "
        f"{OWNER}'s reply or Resume. Use for logins, one-time codes, CAPTCHAs, payments, or a decision only they can make."
    ),
)
async def desk_ask_human(
    question: Annotated[str, Field(description=f"What you need from {OWNER}, e.g. 'Enter the one-time code sent to your phone, then press Resume'")],
    wait_minutes: Annotated[int, Field(description="How long to wait, 1–30")] = 15,
    why: Why = "",
    ctx: Context = None,
) -> str:
    sid = _begin(ctx, "desk_ask_human", {"question": question, "wait_minutes": wait_minutes, "why": why}, why)
    minutes = max(1, min(30, wait_minutes))
    k = await knock.post(sid, sessions.label(sid), question)
    journal.activity(sid, "desk_ask_human", "", why or question[:120], "knock", f"On the live view; {k.notified}")
    answer = await knock.wait(k, minutes * 60)
    if answer is None:
        journal.activity(sid, "desk_ask_human", "", "No answer", "error", f"Waited {minutes} min")
        return f"No answer within {minutes} minutes. Carry on without it, or ask again later."
    # The reply may be a one-time code: the journal keeps its length only.
    journal.activity(sid, "desk_ask_human", "", "Answered", "ok", f"Reply of {journal.chars(answer)}" if answer else "Resume without a reply")
    return f"{OWNER} answered: {answer}" if answer else f"{OWNER} pressed Resume without a reply."


@mcp.tool(
    name="notify",
    annotations=ACT,
    structured_output=False,
    description=(
        f"Send {OWNER} a short notification: a milestone reached, a long job finished, something they should see soon. "
        "It goes to the channel set up in Deskmate, with your session's name in front. Not for questions: desk_ask_human "
        f"waits for an answer. At most one message per {int(notify.SESSION_GAP // 60)} minutes per session and "
        f"{notify.DAILY_MAX} a day in all. Says whether it was sent and never fails; if notifications are off, tell "
        f"{OWNER} in your reply instead."
    ),
)
async def send_notification(
    text: Annotated[str, Field(description="The message: one to three short lines of plain text")],
    images: Annotated[
        list[str] | None,
        Field(description=f"Optional: up to {notify.MAX_IMAGES} images (png, jpg, gif, webp) by file name, put in {EXCHANGE} first"),
    ] = None,
    why: Why = "",
    ctx: Context = None,
) -> str:
    sid = _begin(ctx, "notify", {"text": text, "images": images, "why": why}, why)
    lines = (text or "").strip().splitlines()
    shown = lines[0][:120] if lines else ""
    try:
        if not shown:
            return "Not sent: the text is empty."
        if notify.kind() == "none":
            journal.activity(sid, "notify", shown, why, "refused", "notifications are off")
            return f"Not sent: notifications are off. Tell {OWNER} in your reply instead."
        reason = notify.limit(sid)
        if reason:
            journal.activity(sid, "notify", shown, why, "refused", reason)
            return f"Not sent: {reason}."
        files, skipped = notify.load_images(images, config.EXCHANGE_DIR)
        notify.count(sid)
        result = await notify.send(text, title=f"Deskmate · {sessions.label(sid)}", images=files)
        detail = "; ".join([result["detail"], *skipped])
        journal.activity(sid, "notify", shown, why, "ok" if result["ok"] else "error", detail)
        return (detail[0].upper() + detail[1:] if result["ok"] else f"Not sent: {detail}") + "."
    except Exception as exc:  # this tool reports; it never fails the session's turn
        return f"Not sent: {exc.__class__.__name__}."
