---
name: desk
description: Use the shared Deskmate desk for anything that needs a browser or a screen — opening a web page or a local dev server (for example localhost:3000 or localhost:5173), checking a UI change, logging in, filling a form, downloading a file, or a desktop app. Use it instead of Playwright, Puppeteer, other browser tools or the system browser. A person watches the desk live and can take over at any moment.
---

# Deskmate

One Linux desk shared by every Claude Code session on this machine: Chromium, a terminal, and one or
more monitors. A person watches it live and can take over at any moment.

**Start with `desk_status`.** It tells you who is watching (use their name when you mention them),
where they watch the desk, how many monitors it has, which tabs are yours, and whether a knock is
waiting. The desk's `localhost` is normally this machine's `localhost`, so dev servers are reachable
as they are; `desk_status` says so when the desk was set up without that access.

## How to work on it

1. **Web pages: outline first.** `browser_open(url, why)` opens your own tab and returns an outline
   with refs (`e12`). Act with `browser_act(action, ref, value)`; it returns what changed. Use
   `browser_snapshot(diff=true)` to see changes since your last look, `browser_read("text")` for long
   text, `browser_read("console")` / `("network")` for errors. An outline costs a fraction of a
   screenshot: only take `desk_screenshot` when layout or pixels matter.
2. **Anything that is not a web page** (a native dialog, the terminal, a canvas, another monitor):
   `desk_screenshot(monitor)` then `desk_input(steps, monitor)`. Coordinates are relative to the
   monitor you name. `desk_input` shares one mouse and keyboard; if it answers *busy*, another
   session is typing — wait or carry on in your own tab.
3. **Always pass `why`**: one plain line the person watching reads in the activity feed.
4. **Knock instead of guessing.** For a login, a one-time code, a CAPTCHA, a payment, or a decision
   that belongs to the person, call `desk_ask_human(question)`. It shows a banner on the live view,
   sends a notification if they set one up, and waits for the reply. Never type passwords you were
   not given.
5. **Apps with a second window.** A window a page opens (a customer display, a popup) appears where
   the page asks for it, which can be another monitor. Check it with `browser_tabs` (popups your
   page opened are your tabs) or `desk_screenshot(monitor=2)`.
6. **Files.** To give the desk a file, copy it to the exchange folder on this machine (the
   `desk_files` tool names it) and call `desk_files("put", name)`; it lands in the desk's `~/Uploads`.
   Downloads come back with `desk_files("get", name)` into the same exchange folder.
7. **Leave it tidy.** Close tabs you no longer need with `browser_tabs("close", index)`. Do not close
   tabs that are not yours.

If the person has taken over or paused the agents, every action is refused with a message saying so:
wait, or ask with `desk_ask_human`.
