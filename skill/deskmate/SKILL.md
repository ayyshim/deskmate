---
name: deskmate
description: Use the shared Deskmate desk for anything that needs a browser or a screen — opening a web page or a local dev server (sageweb web/admin, edm_react POS, Fleet, localhost:5173), checking a UI change, logging in, filling a form, downloading a file, or a desktop app. Use it instead of Playwright, Puppeteer, viway-browser or the system browser. Ashim watches it live at http://127.0.0.1:7800.
---

# Deskmate

One Linux desk shared by every Claude Code session on this machine: Chromium, xterm, and **two
1280×800 monitors**. Ashim watches it live and can take over at any moment. Its `localhost` is this
machine's `localhost`, so dev servers are reachable as they are.

## How to work on it

1. **Web pages: outline first.** `browser_open(url, why)` opens your own tab and returns an outline
   with refs (`e12`). Act with `browser_act(action, ref, value)`; it returns what changed. Use
   `browser_snapshot(diff=true)` to see changes since your last look, `browser_read("text")` for long
   text, `browser_read("console")` / `("network")` for errors. An outline costs a fraction of a
   screenshot: only take `desk_screenshot` when layout or pixels matter.
2. **Anything that is not a web page** (a native dialog, the terminal, a canvas, the second monitor):
   `desk_screenshot(monitor)` then `desk_input(steps, monitor)`. Coordinates are relative to the
   monitor you name. `desk_input` shares one mouse and keyboard; if it answers *busy*, another
   session is typing — wait or carry on in your own tab.
3. **Always pass `why`**: one plain line Ashim reads in the activity feed.
4. **Knock instead of guessing.** For a login, a one-time code, a CAPTCHA, a payment, or a decision
   that is Ashim's, call `desk_ask_human(question)`. It shows a banner on the live view, posts to his
   Discord and waits for his reply. Never type passwords you were not given.
5. **Two-window apps.** edm_react's customer display opens on monitor 2 by itself (the desk honours
   the position a page asks for). Check it with `browser_tabs` (popups your page opened are your
   tabs) or `desk_screenshot(monitor=2)`.
6. **Files.** To give the desk a file, copy it to `~/.local/share/deskmate/exchange/` and call
   `desk_files("put", name)`; it lands in the desk's `~/Uploads`. Downloads come back with
   `desk_files("get", name)` into the same exchange folder.
7. **Leave it tidy.** Close tabs you no longer need with `browser_tabs("close", index)`. Do not close
   tabs that are not yours.

If Ashim has taken over or paused the agents, every action is refused with a message saying so:
wait, or ask with `desk_ask_human`.
