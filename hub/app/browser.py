"""The desk's Chromium, driven over CDP with Playwright. Each MCP session works in its own tab.

Two things here exist because of what the desk taught us (design D11):

* Playwright connects with `no_defaults=True`. Without it Playwright turns on focus emulation
  for every page, which Chromium implements by marking the tab as being captured, and a captured
  tab keeps `requestFullscreen()` inside the tab. edm_react's customer display would never go
  fullscreen on monitor 2.
* Openbox ignores the position a page asks for in `window.open(url, name, "left=…,top=…")`, so
  every popup opened on monitor 1. The hub hears the request (`Page.windowOpen` carries the
  features) and moves the new window there with `Browser.setWindowBounds`.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from playwright.async_api import Error as PwError
from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PwTimeout

from . import config, journal

log = logging.getLogger(__name__)

SNAPSHOT_CAP = 6000
DIFF_CAP = 2500
TEXT_CAP = 8000
ACT_TIMEOUT = 10_000


class BrowserError(RuntimeError):
    """A browser action failed; the message says what to do next."""


@dataclass
class PageState:
    owner: str | None
    created: float = field(default_factory=time.time)
    log: deque = field(default_factory=lambda: deque(maxlen=300))
    seq: int = 0
    last_snapshot: str = ""


def _url(raw: str) -> str:
    raw = raw.strip()
    if "://" in raw or raw.startswith(("about:", "data:", "chrome:")):
        return raw
    host = raw.split("/")[0].split(":")[0]
    local = host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".localhost") or host.replace(".", "").isdigit()
    return ("http://" if local else "https://") + raw


def _features_to_bounds(features: list[str]) -> dict | None:
    kv: dict[str, int] = {}
    for f in features or []:
        k, _, v = str(f).partition("=")
        try:
            kv[k.strip().lower()] = int(float(v))
        except ValueError:
            continue
    left = kv.get("left", kv.get("screenx"))
    top = kv.get("top", kv.get("screeny"))
    if left is None or top is None:
        return None
    bounds = {"left": left, "top": top}
    if "width" in kv:
        bounds["width"] = kv["width"]
    if "height" in kv:
        bounds["height"] = kv["height"]
    return bounds


def _cap(text: str, cap: int, hint: str) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n… cut at {cap} of {len(text)} characters. {hint}"


class DeskBrowser:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ctx = None
        self._bcdp = None
        self._lock = asyncio.Lock()
        self._pages: dict[Page, PageState] = {}
        self._current: dict[str, Page] = {}
        self._opens: deque = deque(maxlen=20)

    # ---------- connection ----------

    async def ensure(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return
            if self._pw is None:
                self._pw = await async_playwright().start()
            error: Exception | None = None
            for _ in range(20):
                try:
                    self._browser = await self._pw.chromium.connect_over_cdp(
                        config.CDP_URL, no_defaults=True, timeout=5000
                    )
                    break
                except Exception as exc:  # the browser is restarting
                    error = exc
                    await asyncio.sleep(1)
            else:
                raise BrowserError(f"The desk's browser is not answering on {config.CDP_URL} ({error}). Is the desk running?")
            self._ctx = self._browser.contexts[0]
            self._bcdp = await self._browser.new_browser_cdp_session()
            self._pages.clear()
            self._current.clear()
            self._ctx.on("page", self._on_page)
            self._browser.on("disconnected", lambda _b: log.warning("browser disconnected"))
            for page in self._ctx.pages:
                await self._watch(page, owner=None)
            log.info("connected to the desk's browser, %d tabs", len(self._ctx.pages))

    async def _watch(self, page: Page, owner: str | None) -> PageState:
        if page in self._pages:
            if owner and not self._pages[page].owner:
                self._pages[page].owner = owner
            return self._pages[page]
        st = PageState(owner=owner)
        self._pages[page] = st

        def add(kind: str, text: str) -> None:
            st.seq += 1
            st.log.append((st.seq, kind, text[:500]))

        page.on("console", lambda m: add(f"console.{m.type}", m.text) if m.type in ("error", "warning", "log", "info") else None)
        page.on("pageerror", lambda e: add("pageerror", str(e)))
        page.on("requestfailed", lambda r: add("failed", f"{r.method} {r.url} {r.failure or ''}"))
        page.on("response", lambda r: add(f"http {r.status}", f"{r.request.method} {r.url}") if r.status >= 400 else None)
        page.on("close", lambda p: self._forget(p))
        try:
            cdp = await self._ctx.new_cdp_session(page)
            await cdp.send("Page.enable")
            cdp.on("Page.windowOpen", lambda e, p=page: self._opens.append((time.time(), p, e)))
        except PwError:
            pass
        return st

    def _forget(self, page: Page) -> None:
        self._pages.pop(page, None)
        for sid, p in list(self._current.items()):
            if p is page:
                del self._current[sid]
        journal.publish("tabs", None)

    async def _on_page(self, page: Page) -> None:
        try:
            opener = await page.opener()
        except PwError:
            opener = None
        owner = self._pages[opener].owner if opener in self._pages else None
        await self._watch(page, owner)
        if opener is not None:
            await self._place_popup(page, opener)
        journal.publish("tabs", None)

    async def _place_popup(self, page: Page, opener: Page) -> None:
        """Move a popup to where its opener asked for it (Openbox ignores the request)."""
        await asyncio.sleep(0.2)
        now = time.time()
        for ts, src, event in reversed(self._opens):
            if src is opener and now - ts < 5:
                bounds = _features_to_bounds(event.get("windowFeatures", []))
                if not bounds:
                    return
                try:
                    cdp = await self._ctx.new_cdp_session(page)
                    target = (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
                    await cdp.detach()
                    window = await self._bcdp.send("Browser.getWindowForTarget", {"targetId": target})
                    await self._bcdp.send("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": bounds})
                    log.info("placed popup at %s", bounds)
                except PwError as exc:
                    log.warning("could not place popup: %s", exc)
                return

    # ---------- tabs ----------

    def owned(self, session: str) -> list[Page]:
        pages = [p for p, st in self._pages.items() if st.owner == session and not p.is_closed()]
        return sorted(pages, key=lambda p: self._pages[p].created)

    def current(self, session: str) -> Page:
        page = self._current.get(session)
        if page is None or page.is_closed():
            raise BrowserError("You have no tab yet. Call browser_open with a URL first.")
        return page

    async def _new_tab(self, session: str) -> Page:
        page = await self._ctx.new_page()
        await self._watch(page, session)
        self._pages[page].owner = session
        self._current[session] = page
        return page

    async def front(self, page: Page) -> None:
        try:
            await page.bring_to_front()
        except PwError:
            pass

    # ---------- what the tools call ----------

    async def open(self, session: str, url: str, new_tab: bool = False) -> str:
        await self.ensure()
        page = self._current.get(session)
        if new_tab or page is None or page.is_closed():
            page = await self._new_tab(session)
        await self.front(page)
        target = _url(url)
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        except PwTimeout:
            pass
        except PwError as exc:
            raise BrowserError(f"Could not open {target}: {str(exc).splitlines()[0]}")
        try:
            await page.wait_for_load_state("networkidle", timeout=2500)
        except PwError:
            pass
        snap = await self._snapshot(page)
        return f"{await page.title()}\n{page.url}\n\n{_cap(snap, SNAPSHOT_CAP, 'Call browser_snapshot for the rest, or browser_read for the text.')}"

    async def _snapshot(self, page: Page) -> str:
        try:
            snap = await page.aria_snapshot(mode="ai", timeout=10_000)
        except PwError as exc:
            raise BrowserError(f"Could not read the page: {str(exc).splitlines()[0]}")
        self._pages.setdefault(page, PageState(owner=None)).last_snapshot = snap
        return snap

    async def snapshot(self, session: str, diff: bool = False) -> str:
        await self.ensure()
        page = self.current(session)
        before = self._pages[page].last_snapshot if page in self._pages else ""
        snap = await self._snapshot(page)
        head = f"{await page.title()}\n{page.url}\n\n"
        if diff and before:
            return head + _cap(self._diff(before, snap), DIFF_CAP, "Call browser_snapshot without diff for the whole page.")
        return head + _cap(snap, SNAPSHOT_CAP, "The page is long. Use browser_read for its text.")

    @staticmethod
    def _diff(before: str, after: str) -> str:
        if before == after:
            return "(no change)"
        lines = [
            line
            for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
            if not line.startswith(("---", "+++", "@@"))
        ]
        return "\n".join(lines) or "(no change)"

    async def act(self, session: str, action: str, ref: str | None, value: str | None) -> str:
        await self.ensure()
        page = self.current(session)
        await self.front(page)
        before = self._pages[page].last_snapshot if page in self._pages else ""
        loc = page.locator(f"aria-ref={ref}") if ref else None
        url_before = page.url
        try:
            if action in ("click", "dblclick", "rightclick", "hover", "fill", "type", "select", "check", "uncheck", "focus") and loc is None:
                raise BrowserError(f"{action} needs a ref from browser_snapshot, such as e12.")
            if action == "click":
                await loc.click(timeout=ACT_TIMEOUT)
            elif action == "dblclick":
                await loc.dblclick(timeout=ACT_TIMEOUT)
            elif action == "rightclick":
                await loc.click(button="right", timeout=ACT_TIMEOUT)
            elif action == "hover":
                await loc.hover(timeout=ACT_TIMEOUT)
            elif action == "fill":
                await loc.fill(value or "", timeout=ACT_TIMEOUT)
            elif action == "type":
                await loc.press_sequentially(value or "", delay=20, timeout=ACT_TIMEOUT)
            elif action == "press":
                if loc is not None:
                    await loc.press(value or "Enter", timeout=ACT_TIMEOUT)
                else:
                    await page.keyboard.press(value or "Enter")
            elif action == "select":
                await loc.select_option([v.strip() for v in (value or "").split("|")], timeout=ACT_TIMEOUT)
            elif action == "check":
                await loc.check(timeout=ACT_TIMEOUT)
            elif action == "uncheck":
                await loc.uncheck(timeout=ACT_TIMEOUT)
            elif action == "focus":
                await loc.focus(timeout=ACT_TIMEOUT)
            elif action == "scroll":
                if loc is not None:
                    await loc.scroll_into_view_if_needed(timeout=ACT_TIMEOUT)
                else:
                    await page.mouse.wheel(0, -600 if (value or "").lower() == "up" else 600)
            elif action == "back":
                await page.go_back(timeout=ACT_TIMEOUT)
            elif action == "reload":
                await page.reload(timeout=ACT_TIMEOUT)
            else:
                raise BrowserError(f"Unknown action {action!r}.")
        except PwTimeout:
            raise BrowserError(f"Timed out on {ref or 'the page'}. The page may have changed: take a fresh browser_snapshot.")
        except PwError as exc:
            first = str(exc).splitlines()[0]
            if "aria-ref" in first or "not found" in first.lower():
                raise BrowserError(f"{ref} is not on the page any more. Take a fresh browser_snapshot.")
            raise BrowserError(first)
        await asyncio.sleep(0.3)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PwError:
            pass
        snap = await self._snapshot(page)
        moved = f"\nNow at {page.url}" if page.url != url_before else ""
        return f"done{moved}\n\n" + _cap(self._diff(before, snap) if before else snap, DIFF_CAP, "Call browser_snapshot for the whole page.")

    async def read(self, session: str, what: str, since: int = 0) -> str:
        await self.ensure()
        page = self.current(session)
        if what == "text":
            try:
                text = await page.inner_text("body", timeout=ACT_TIMEOUT)
            except PwError as exc:
                raise BrowserError(str(exc).splitlines()[0])
            return f"{await page.title()}\n{page.url}\n\n" + _cap(text, TEXT_CAP, "")
        st = self._pages.get(page)
        if st is None:
            return "(nothing yet)"
        kinds = ("console", "pageerror") if what == "console" else ("failed", "http")
        rows = [(n, k, t) for n, k, t in st.log if n > since and k.startswith(kinds)]
        if not rows:
            return f"(nothing new) cursor={st.seq}"
        body = "\n".join(f"{k}: {t}" for _n, k, t in rows[-60:])
        return f"{body}\ncursor={st.seq} (pass since={st.seq} next time for only new lines)"

    async def tabs(self, session: str, action: str, index: int | None, url: str | None) -> str:
        await self.ensure()
        mine = self.owned(session)
        if action == "new":
            page = await self._new_tab(session)
            if url:
                return await self.open(session, url)
            await self.front(page)
            return "Opened a new blank tab."
        if action in ("switch", "close"):
            if index is None or not (0 <= index < len(mine)):
                raise BrowserError(f"index must be 0–{len(mine) - 1}. Call browser_tabs to list your tabs.")
            page = mine[index]
            if action == "switch":
                self._current[session] = page
                await self.front(page)
                return f"Switched to {index}: {await page.title()} {page.url}"
            await page.close()
            return f"Closed tab {index}."
        cur = self._current.get(session)
        lines = []
        for i, p in enumerate(mine):
            try:
                title = await p.title()
            except PwError:
                title = "?"
            lines.append(f"{'*' if p is cur else ' '} {i}: {title} — {p.url}")
        others = len([p for p in self._pages if p not in mine and not p.is_closed()])
        return ("\n".join(lines) or "You have no tabs.") + f"\n({others} other tabs belong to other sessions or to {config.OWNER})"

    async def all_tabs(self) -> list[dict]:
        """For the web UI: every open tab with its owner."""
        if self._browser is None or not self._browser.is_connected():
            return []
        out = []
        for page, st in list(self._pages.items()):
            if page.is_closed():
                continue
            try:
                title = await asyncio.wait_for(page.title(), 2)
            except Exception:
                title = ""
            out.append({"owner": st.owner, "title": title, "url": page.url})
        return out

    async def release(self, session: str) -> None:
        self._current.pop(session, None)


browser = DeskBrowser()
