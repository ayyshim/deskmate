"""./deskmate doctor: the checks the wizard's Check step runs before installing, plus the ones that need
Deskmate running (the hub answers, /mcp wants the token, the desk's browser is connected, nothing listens
beyond 127.0.0.1, compose.local.yaml matches .env, Claude Code is connected).

Every check is {id, label, status: ok|warn|fail, detail, fix}. Nothing here changes anything, posts
anything or calls a model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import compose, detect, settings, util

MIN_COMPOSE = (2, 24, 0)  # compose.yaml uses env_file `required:` and long-syntax bind options
GOOD_COMPOSE = (2, 24, 6)
MIN_ENGINE = (24, 0, 0)
TESTED_CLAUDE = (2, 1, 278)  # headersHelper and local plugin marketplaces verified on this version
DESKTOP_SHARED = ("/Users", "/Volumes", "/private", "/tmp", "/var/folders")


def _check(cid, label, status="ok", detail="", fix=""):
    return {"id": cid, "label": label, "status": status, "detail": detail, "fix": fix}


def _plural(n, word, many=""):
    return f"{n} {word if n == 1 else (many or word + 's')}"


# ---------------------------------------------------------------- before installing


def platform_check(p: dict) -> dict:
    if p["os"] == "windows":
        return _check("platform", "Windows: run setup inside WSL 2", "fail",
                      "Deskmate's tools are Linux tools. On Windows they run inside WSL 2.",
                      "wsl --install, open the Linux distro, clone Deskmate there (not under /mnt/c) and run ./deskmate setup")
    if sys.version_info < (3, 9):
        return _check("platform", f"Python {p['python']} is too old", "fail", "Setup needs Python 3.9 or newer.",
                      "Install Python 3.9 or newer")
    mem = f" · {p['memory_gb']:g} GB of memory" if p.get("memory_gb") else ""
    return _check("platform", f"{p['label']}{mem}", detail=f"Python {p['python']} runs setup.")


def docker_check(d: dict, p: dict) -> dict:
    if not d["installed"] or d["sudo_needed"] or not d["running"]:
        detail = d.get("error", "")
        if d["sudo_needed"]:
            detail = "Your user can't use Docker's socket. Setup never runs sudo itself."
        return _check("docker", d["label"], "fail", detail, d["fix"])
    v = util.version_tuple(d["version"])
    if v and v < MIN_ENGINE:
        return _check("docker", d["label"], "warn", f"Docker Engine {d['version']} is older than 24.",
                      "Update Docker")
    detail = "Docker works without sudo."
    if d["rootless"] and v < (29, 5, 0):
        detail += " Rootless before 29.5: the desk reaches your localhost through host-access networking."
    return _check("docker", d["label"], detail=detail)


def compose_check(d: dict):
    if not d["installed"]:
        return None
    v = util.version_tuple(d["compose_version"])
    if not v:
        return _check("compose", "Docker Compose isn't installed", "fail", "Setup needs Docker Compose 2.24 or newer.",
                      "Install the Docker Compose plugin (package docker-compose-plugin), 2.24 or newer")
    if v < MIN_COMPOSE:
        return _check("compose", f"Docker Compose {d['compose_version']} is too old", "fail",
                      "Setup needs 2.24 or newer.", "Update the Docker Compose plugin")
    if v < GOOD_COMPOSE:
        return _check("compose", f"Docker Compose {d['compose_version']}", "warn", "2.24.6 or newer is better.",
                      "Update the Docker Compose plugin")
    return _check("compose", f"Docker Compose {d['compose_version']}", detail="Setup needs 2.24 or newer.")


def memory_check(d: dict, p: dict):
    if not d["running"] or not d["memory_gb"]:
        return None
    gb = d["memory_gb"]
    where = ("Docker Desktop: Settings, Resources, Memory" if d["desktop"] else
             "%UserProfile%\\.wslconfig: [wsl2] memory=6GB, then wsl --shutdown" if p["os"] == "wsl" else
             "Add memory to this computer or the VM")
    if gb < 2:
        return _check("memory", f"Docker has {gb:g} GB of memory", "fail", "The desk needs 2 GB at the very least.", where)
    if gb < 4:
        return _check("memory", f"Docker has {gb:g} GB of memory", "warn", "4 GB or more keeps Chromium comfortable.", where)
    return _check("memory", f"Docker has {gb:g} GB of memory", detail="4 GB or more keeps Chromium comfortable.")


def disk_check(values: dict, d: dict, install: bool) -> dict:
    paths = [str(settings.data_dir(values))]
    if d.get("root_dir") and not d.get("desktop") and os.path.isabs(d["root_dir"]):
        paths.append(d["root_dir"])
    free = min(detect.disk_free_gb(p) for p in paths)
    label = f"{free:g} GB free on disk"
    if install and free < 4:
        return _check("disk", label, "fail", "The desk and the hub take about 2.6 GB.", "Free some disk space")
    if free < 10:
        return _check("disk", label, "warn", "The desk and the hub take about 2.6 GB.", "docker system prune frees old images")
    return _check("disk", label, detail="The desk and the hub take about 2.6 GB.")


def claude_check() -> dict:
    c = detect.claude_cli()
    if not c["installed"]:
        return _check("claude", "Claude Code wasn't found", "warn",
                      "Setup skips connecting it; everything else works.",
                      "Install Claude Code (https://code.claude.com), then run ./deskmate connect")
    where = util.tilde(c["path"])
    v = util.version_tuple(c["version"])
    if v and v < TESTED_CLAUDE:
        return _check("claude", f"Claude Code {c['version']} at {where}", "warn",
                      "Connecting was tested on 2.1.278; older versions may lack plugins or headersHelper.",
                      "claude update")
    return _check("claude", f"Claude Code {c['version'] or ''} at {where}".replace("  ", " "),
                  detail=f"Its sessions are in {util.tilde(detect.default_config_dir())}.")


def _ours(values: dict) -> dict:
    """The ports and display this repo's running Deskmate uses (they count as free)."""
    stack = detect.own_stack(settings.repo_dir(), values.get("COMPOSE_PROJECT_NAME") or "deskmate")
    if not stack["running"]:
        return {"ports": set(), "display": set()}
    env = settings.read_env()
    if settings.is_legacy(env) or not env:
        return {"ports": {7800, 7802}, "display": {87}}
    ports = {int(env[k]) for k in ("HUB_PORT", "CDP_PORT") if env.get(k, "").isdigit()}
    disp = {int(env["DESK_DISPLAY"])} if env.get("DESK_DISPLAY", "").isdigit() else set()
    return {"ports": ports, "display": disp}


def ports_check(values: dict) -> dict:
    host = values.get("DESK_NETWORK") == "host"
    names = [("HUB_PORT", "Deskmate's page")] + ([("CDP_PORT", "the desk's browser")] if host else [])
    nums = []
    for key, _ in names:
        try:
            nums.append(int(values.get(key) or 0))
        except ValueError:
            nums.append(0)
    if host and len(nums) == 2 and nums[0] == nums[1]:
        return _check("ports", f"Both ports are {nums[0]}", "fail", "The two ports must differ.",
                      "Change one in Desk, Advanced")
    ours = _ours(values)["ports"]
    found = detect.ports([n for n in nums if n])
    bad = []
    for n in nums:
        info = found.get(n)
        if info and not info["free"] and n not in ours:
            bad.append(f"Port {n} is taken by {info['owner'] or 'another program'}")
    shown = " and ".join(str(n) for n in nums)
    if bad:
        return _check("ports", "; ".join(bad), "fail",
                      "On 127.0.0.1 only: one port for Deskmate's page, tools and hooks"
                      + (", one for the desk's browser." if host else "."),
                      "Stop that program, or pick a free port in Desk, Advanced (./deskmate setup)")
    mine = all(n in ours for n in nums)
    return _check("ports", f"Port{'s' if len(nums) > 1 else ''} {shown} {'are' if len(nums) > 1 else 'is'} "
                  + ("Deskmate's own" if mine else "free"), detail="On 127.0.0.1 only.")


def display_check(values: dict, p: dict):
    if values.get("DESK_NETWORK") != "host" or p["os"] not in ("linux", "wsl"):
        return None
    try:
        n = int(values.get("DESK_DISPLAY") or 87)
    except ValueError:
        return _check("display", "The display number isn't a number", "fail", "", "Use a whole number, like 87")
    if n in _ours(values)["display"] or detect.display_free(n):
        return _check("display", f"Display :{n} is free for the desk",
                      detail="With host networking the desk's X display shares this computer's.")
    return _check("display", f"Display :{n} is in use on this computer", "fail",
                  "Another X server or desk uses it.", f"Pick another, like :{detect.free_display(n + 1)} (Desk, Advanced)")


def data_dir_problem(path: str, p: dict | None = None) -> str:
    """Why a data folder can't be used ('' when it can)."""
    p = p or detect.platform()
    if not path or not os.path.isabs(path):
        return "Use a full path, starting with /."
    if " " in path:
        return "Pick a folder without spaces in its path (the hooks and Compose need plain paths)."
    if re.match(r"^/mnt/[a-zA-Z]/", path):
        return "That is a Windows drive: files there can't be kept private. Use a folder in the Linux filesystem, like ~/.local/share/deskmate."
    if ":" in path:
        return "Folder names with ':' can't be used."
    base = util.nearest_existing(path)
    if not os.access(str(base), os.W_OK):
        return f"You can't write in {util.tilde(str(base))}."
    return ""


def data_dir_check(values: dict, p: dict) -> dict:
    path = str(settings.data_dir(values))
    problem = data_dir_problem(path, p)
    if problem:
        return _check("data", f"Data folder {util.tilde(path)} can't be used", "fail", problem, "Change it in Desk, Advanced")
    if p["os"] == "macos" and not any(path == s or path.startswith(s + "/") for s in DESKTOP_SHARED):
        return _check("data", f"Data folder {util.tilde(path)}", "warn",
                      "Docker Desktop shares only /Users, /Volumes, /private, /tmp and /var/folders at first.",
                      "Add it in Docker Desktop, Settings, Resources, File sharing")
    return _check("data", f"Data folder {util.tilde(path)}", detail="Only you can open it (0700).")


def repo_check(p: dict):
    repo = str(settings.repo_dir())
    if p["os"] == "wsl" and re.match(r"^/mnt/[a-zA-Z]/", repo):
        return _check("repo", "Deskmate is cloned on the Windows drive", "warn",
                      "It is slow there, and .env can't be kept private.",
                      "Clone it into the Linux filesystem, like ~/deskmate, and run setup there")
    return None


def curl_check():
    if shutil.which("curl"):
        return None
    return _check("curl", "curl isn't installed", "warn", "Claude Code's hooks use curl to reach Deskmate.",
                  "Install curl (apt install curl, or your system's package manager)")


def existing_check(values: dict) -> dict:
    path = settings.env_path()
    stack = detect.own_stack(settings.repo_dir(), values.get("COMPOSE_PROJECT_NAME") or "deskmate")
    if stack.get("other_dir"):
        return _check("existing", f"Deskmate also runs from {util.tilde(stack['other_dir'])}", "warn",
                      f"Both use the Compose project '{values.get('COMPOSE_PROJECT_NAME')}'. Installing here replaces "
                      "those containers; the desk's logins are kept.",
                      "Stop it there with ./deskmate down, or pick another project name in Desk, Advanced")
    if not path.is_file():
        return _check("existing", "No earlier install",
                      detail="Running setup again later opens it with your answers, so you can change them.")
    if settings.is_legacy():
        return _check("existing", "An earlier Deskmate install, set up the old way",
                      detail="Setup moves its tokens out of .env into private files, keeps the same sign-in, and keeps the desk's logins.")
    t = time.localtime(path.stat().st_mtime)
    since = f"{t.tm_mday} {time.strftime('%b %Y', t)}"
    return _check("existing", "Deskmate is installed", detail=f"Settings saved {since}, in {util.tilde(str(path))}.")


def preinstall(values: dict | None = None) -> list:
    """The Check step: can Deskmate run here, and is it already installed?"""
    values = settings.normalize(values) if values else settings.load()
    p = detect.platform()
    d = detect.docker()
    install = not settings.env_path().is_file()
    checks = [platform_check(p), docker_check(d, p), compose_check(d), memory_check(d, p),
              disk_check(values, d, install), claude_check(), ports_check(values), display_check(values, p),
              data_dir_check(values, p), repo_check(p), curl_check(), existing_check(values)]
    return [c for c in checks if c]


BLOCKING = ("platform", "docker", "compose", "ports", "display", "data", "memory", "disk")


def blocking(checks: list) -> list:
    """The failed checks that keep Install disabled."""
    return [c for c in checks if c["status"] == "fail" and c["id"] in BLOCKING]


# ---------------------------------------------------------------- with Deskmate running


def _sse_json(body: bytes) -> dict:
    text = body.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            return {}
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                continue
    return {}


def mcp_probe(port, token: str) -> dict:
    """{no_token, initialize, tools}: the HTTP status without a token, with it, and how many tools it lists."""
    base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "deskmate-doctor", "version": "1"}}}).encode()
    no_token, _, _ = compose.http(port, "/mcp", "POST", base, init)
    out = {"no_token": no_token, "initialize": 0, "tools": -1}
    if not token:
        return out
    auth = dict(base, Authorization=f"Bearer {token}")
    status, headers, _ = compose.http(port, "/mcp", "POST", auth, init)
    out["initialize"] = status
    if status != 200:
        return out
    sid = next((v for k, v in headers.items() if k.lower() == "mcp-session-id"), "")
    if sid:
        auth["mcp-session-id"] = sid
    compose.http(port, "/mcp", "POST", auth, json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode())
    status, _, body = compose.http(port, "/mcp", "POST", auth,
                                   json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode())
    if status == 200:
        tools = _sse_json(body).get("result", {}).get("tools")
        out["tools"] = len(tools) if isinstance(tools, list) else -1
    if sid:
        compose.http(port, "/mcp", "DELETE", auth)
    return out


def _listeners(port: int) -> list:
    """Local addresses something listens on for this port ([] when unknown)."""
    addrs = []
    if sys.platform.startswith("linux") and shutil.which("ss"):
        code, out, _ = util.run(["ss", "-Hltn", f"sport = :{port}"], timeout=5)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                addrs.append(parts[3].rsplit(":", 1)[0])
    elif shutil.which("lsof"):
        code, out, _ = util.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=5)
        for line in out.splitlines()[1:]:
            m = re.search(r"\s(\S+):%d\s+\(LISTEN\)" % port, line)
            if m:
                addrs.append(m.group(1))
    return addrs


def _loopback(addr: str) -> bool:
    a = addr.strip("[]").split("%")[0]
    return a.startswith("127.") or a in ("::1", "localhost")


def _plugin_marketplace(values: dict):
    """Claude Code's record of where the deskmate marketplace lives, if it was added."""
    for d in util.split_list(values.get("CLAUDE_CONFIG_DIRS", "")) or [detect.default_config_dir()]:
        try:
            data = json.loads(Path(d, "settings.json").read_text())
        except (OSError, ValueError):
            continue
        src = ((data.get("extraKnownMarketplaces") or {}).get("deskmate") or {}).get("source") or {}
        path = src.get("path") or src.get("url") or ""
        if path:
            return path
    return None


def postinstall(values: dict) -> list:
    out = []
    port = values.get("HUB_PORT") or "7800"
    env = settings.env_path()
    m = util.mode_of(env)
    out.append(_check("env", ".env is private (0600)") if m == 0o600 else
               _check("env", f".env can be read by others ({oct(m)[2:]})", "warn",
                      "It holds no secrets, but your folders and settings.", f"chmod 600 {util.tilde(str(env))}"))
    data = settings.data_dir(values)
    loose = [str(p) for p in (data, data / "secrets") if p.exists() and util.mode_of(p) & 0o077]
    loose += [str(f) for f in sorted((data / "secrets").glob("*")) if f.is_file() and util.mode_of(f) & 0o077]
    out.append(_check("private", "The data folder and secret files are private") if not loose else
               _check("private", "Some of Deskmate's files can be read by others", "warn",
                      ", ".join(util.tilde(p) for p in loose), "./deskmate up fixes the folders; chmod 600 the files"))
    token = settings.read_secret("hub-token", values)
    if not token:
        out.append(_check("token", "The hub token is missing", "fail", "", "./deskmate setup"))
    out.append(_check("compose-local", "compose.local.yaml matches .env") if compose.in_step(values) else
               _check("compose-local", "compose.local.yaml is out of step with .env", "warn",
                      "A setting changed since the last start.", "./deskmate up"))
    _, missing = compose.mounts(values)
    if missing:
        out.append(_check("mounts", f"{_plural(len(missing), 'folder')} the hub should read {'is' if len(missing) == 1 else 'are'} missing",
                          "warn", ", ".join(util.tilde(m) for m in missing), "Fix the folders in ./deskmate setup"))
    d = detect.docker()
    if not d["running"]:
        out.append(_check("containers", "Can't see the containers: Docker isn't running", "fail", "", d["fix"]))
        return out
    state = {c["service"]: c for c in compose.ps(values)}
    down = [s for s in ("desk", "hub") if state.get(s, {}).get("state") != "running"]
    if down:
        out.append(_check("containers", f"The {' and the '.join(down)} {'is' if len(down) == 1 else 'are'} not running", "fail",
                          "; ".join(f"{s}: {state[s]['status']}" for s in down if s in state),
                          "./deskmate up, then ./deskmate logs " + down[0]))
    else:
        out.append(_check("containers", "The desk and the hub are running"))
    status, _, _ = compose.http(port, "/")
    if status != 200:
        out.append(_check("hub", f"Deskmate doesn't answer on 127.0.0.1:{port}", "fail",
                          f"HTTP {status}" if status else "Nothing answers.", "./deskmate logs hub"))
        return out
    out.append(_check("hub", f"Deskmate answers on http://127.0.0.1:{port}"))
    probe = mcp_probe(port, token)
    if probe["no_token"] in (200, 202):
        out.append(_check("mcp-auth", "The MCP server answers without the token", "fail",
                          "Anything on this computer could drive the desk.", "Update Deskmate: git pull, ./deskmate update"))
    elif probe["initialize"] != 200:
        out.append(_check("mcp", "The MCP server refuses the hub token", "fail",
                          f"HTTP {probe['initialize']}", "./deskmate restart; if it persists, ./deskmate setup"))
    else:
        tools = probe["tools"]
        out.append(_check("mcp", f"The MCP server wants the token and lists {_plural(tools, 'tool')}" if tools >= 0 else
                          "The MCP server wants the token and answers with it"))
    code, logs, _ = compose.capture(["logs", "--tail", "400", "hub"], timeout=30, values=values)
    if "connected to the desk's browser" in logs:
        out.append(_check("browser", "The hub is connected to the desk's browser"))
    else:
        out.append(_check("browser", "The hub hasn't reached the desk's browser yet", "warn",
                          "It retries by itself; this can take half a minute after a start.", "./deskmate logs desk"))
    ports = [int(port)] + ([int(values.get("CDP_PORT") or 0)] if values.get("DESK_NETWORK") == "host" else [])
    wide = [f"{a}:{p}" for p in ports if p for a in _listeners(p) if not _loopback(a)]
    out.append(_check("listen", "Nothing of Deskmate's listens beyond 127.0.0.1") if not wide else
               _check("listen", "Deskmate listens beyond 127.0.0.1", "fail", ", ".join(wide),
                      "Check deploy/net-*.yaml and HUB_BIND; then ./deskmate up"))
    out.extend(_secretary_checks(values))
    out.extend(_claude_checks(values))
    return out


def _secretary_checks(values: dict) -> list:
    out = []
    if util.is_on(values.get("SECRETARY", "on")):
        tok = settings.read_secret("claude-token", values)
        out.append(_check("login", f"The secretary has a Claude login ({util.mask_token(tok)})") if tok else
                   _check("login", "The secretary has no Claude login yet", "warn",
                          "It reads, but makes no model calls until it has one.",
                          "claude setup-token, then ./deskmate config set-secret claude-token and paste it"))
    kind = values.get("NOTIFY_KIND") or "none"
    if kind != "none":
        url = settings.read_secret("notify-url", values)
        if not url:
            out.append(_check("notify", f"Notifications go to {kind}, but no URL is set", "warn", "",
                              "./deskmate config set-secret notify-url"))
        elif kind == "discord":
            # A GET on a Discord webhook returns its name and posts nothing.
            import urllib.request

            code = 0
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "deskmate-doctor"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    code = resp.status
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", 0) or 0
            if code == 200:
                out.append(_check("notify", "The Discord webhook answers", detail="Checked without posting anything."))
            else:
                out.append(_check("notify", "The Discord webhook doesn't answer", "warn",
                                  f"HTTP {code}" if code else "No answer.",
                                  "Make a new webhook in the channel's settings, then ./deskmate setup"))
        else:
            out.append(_check("notify", f"Notifications go to {kind}", detail="Use Send a test in ./deskmate setup to try it."))
    return out


def _claude_checks(values: dict) -> list:
    out = []
    if not detect.claude_cli()["installed"]:
        return out
    try:
        from . import claude_connect
    except ImportError:
        return [_check("connect", "Can't check Claude Code's connection", "warn",
                       "This copy of Deskmate has no claude_connect module.", "git pull, then ./deskmate update")]
    try:
        st = claude_connect.status() or {}
    except Exception as exc:  # noqa: BLE001 - a broken check is a warning, not a crash
        return [_check("connect", "Can't check Claude Code's connection", "warn", str(exc)[:200], "./deskmate connect")]
    mcp = st.get("mcp")
    if mcp == "ok":
        out.append(_check("connect", "Claude Code is connected"))
    elif mcp == "legacy":
        out.append(_check("connect", "Claude Code uses the old Deskmate connection", "warn",
                          "Its token sits in Claude Code's config.", "./deskmate connect"))
    else:
        out.append(_check("connect", "Claude Code isn't connected to Deskmate", "warn", f"MCP server: {mcp or 'missing'}",
                          "./deskmate connect"))
    market = _plugin_marketplace(values)
    here = str(settings.repo_dir() / "plugin")
    if market and os.path.normpath(market) != os.path.normpath(here) and not market.startswith("http"):
        out.append(_check("marketplace", "Claude Code looks for Deskmate's plugins in another folder", "warn",
                          util.tilde(market), "./deskmate connect"))
    if util.is_on(values.get("HABITS", "off")):
        try:
            from . import habits

            hs = habits.status() or {}
            problems = [str(x) for x in hs.get("problems") or []]
            if not hs.get("installed", True):
                problems.insert(0, "nothing is set up yet")
            ok = not problems
            out.append(_check("habits", "Working habits are set up" if ok else "Working habits need a look",
                              "ok" if ok else "warn", "; ".join(problems)[:300],
                              "" if ok else "./deskmate habits apply"))
        except Exception as exc:  # noqa: BLE001
            out.append(_check("habits", "Can't check the working habits", "warn", str(exc)[:200], "./deskmate habits status"))
    return out


def run(values: dict | None = None) -> list:
    """All checks: the pre-install ones, plus the running ones once Deskmate is set up."""
    values = settings.merged(values) if values else settings.load()
    values = {k: v for k, v in values.items() if not isinstance(v, dict)}
    checks = preinstall(values)
    if not settings.env_path().is_file():
        checks.append(_check("setup", "Deskmate isn't set up yet", "warn", "", "./deskmate setup"))
        return checks
    if settings.is_legacy():
        checks.append(_check("layout", ".env still holds tokens (the old layout)", "warn",
                             "They move into private files when setup runs.", "./deskmate setup"))
    return checks + postinstall(values)


def summary(checks: list) -> str:
    """'All 9 checks passed.', 'All checks passed, 1 needs a look.' or '2 problems, 1 needs a look.'"""
    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    look = f"{warns} need{'s' if warns == 1 else ''} a look"
    if not fails and not warns:
        return f"All {len(checks)} checks passed."
    if not fails:
        return f"All checks passed, {look}."
    return _plural(fails, "problem") + (f", {look}." if warns else ".")


def print_report(checks: list, out=None, style: util.Style | None = None) -> None:
    out = out or sys.stdout
    style = style or util.Style(out)
    for c in checks:
        out.write(f"  {style.mark(c['status'])} {c['label']}\n")
        if c["status"] != "ok" and c.get("detail"):
            out.write(f"      {style.dim(c['detail'])}\n")
        if c["status"] != "ok" and c.get("fix"):
            out.write(f"      {style.arrow} {c['fix']}\n")
    out.write(summary(checks) + "\n")
