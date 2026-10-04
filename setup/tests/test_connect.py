"""Connecting Claude Code (claude_connect.py and plugin/deskmate): the hook and header scripts, the plugin files,
and connect / disconnect / legacy migration / conflicts against the real `claude` CLI in a sandbox.

Every test that runs `claude` points HOME, CLAUDE_CONFIG_DIR and XDG_* at a temp folder. The module checks the
real ~/.claude/settings.json and the MCP servers in the real ~/.claude.json before and after, and fails if they
changed. (The whole ~/.claude.json is not compared: running Claude Code sessions rewrite it all the time.)

Optional:
  DESKMATE_TEST_CLAUDE=/path/to/claude   the CLI to use (default: `claude` on PATH, resolved past version shims)
  DESKMATE_TEST_HUB_PORT=7820            a running throwaway hub; its token in DESKMATE_TEST_HUB_TOKEN_FILE.
                                         With both, connect() must report "✔ Connected".

Run: python3 -m unittest discover -s setup/tests -p 'test_connect*.py' -v
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "setup"))

from deskmate_setup import claude_connect as cc  # noqa: E402

PLUGIN = REPO / "plugin"
HOOK = PLUGIN / "deskmate" / "bin" / "hook"
HEADERS = PLUGIN / "deskmate" / "bin" / "mcp-headers"
FAKE_TOKEN = "fake-hub-token-for-tests-0123456789abcdefXYZ"
REAL_HOME = Path(os.path.expanduser("~"))


# ---------------------------------------------------------------- the real config must not change


def _real_fingerprint() -> dict:
    """sha256 of the real settings.json and of the mcpServers part of the real .claude.json. No values printed."""
    out = {}
    try:
        out["settings.json"] = hashlib.sha256((REAL_HOME / ".claude" / "settings.json").read_bytes()).hexdigest()
    except OSError:
        out["settings.json"] = "missing"
    try:
        d = json.loads((REAL_HOME / ".claude.json").read_text())
        part = {"user": d.get("mcpServers"),
                "projects": {p: v.get("mcpServers") for p, v in (d.get("projects") or {}).items() if isinstance(v, dict)}}
        out["claude.json mcpServers"] = hashlib.sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()
    except (OSError, ValueError):
        out["claude.json mcpServers"] = "missing"
    for rel in ("skills", "plugins/installed_plugins.json", "plugins/known_marketplaces.json"):
        p = REAL_HOME / ".claude" / rel
        h = hashlib.sha256()
        for f in sorted(p.rglob("*")) if p.is_dir() else [p]:
            h.update(str(f).encode())
            try:
                h.update(f.read_bytes())
            except OSError:
                pass
        out[rel] = h.hexdigest()
    return out


_REAL_BEFORE = {}


def setUpModule():
    _REAL_BEFORE.update(_real_fingerprint())


def tearDownModule():
    after = _real_fingerprint()
    changed = [k for k in _REAL_BEFORE if _REAL_BEFORE[k] != after.get(k)]
    if changed:
        raise AssertionError(f"THE REAL CLAUDE CODE CONFIG CHANGED: {changed}")


# ---------------------------------------------------------------- the claude CLI


def _find_claude() -> str | None:
    """A real binary, not a version-manager shim (shims read config from HOME, which tests replace)."""
    exe = os.environ.get("DESKMATE_TEST_CLAUDE") or shutil.which("claude")
    if not exe:
        return None
    real = os.path.realpath(exe)
    if "/shims/" in real:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout
            m = re.search(r"(\d+\.\d+\.\d+)", out)
            cand = Path(real).parents[1] / "installs" / "claude" / m.group(1) / "claude" if m else None
            return str(cand) if cand and cand.is_file() else None
        except (OSError, subprocess.SubprocessError):
            return None
    return real


CLAUDE = _find_claude()
needs_claude = unittest.skipUnless(CLAUDE, "the claude CLI was not found (set DESKMATE_TEST_CLAUDE)")
HUB_PORT = os.environ.get("DESKMATE_TEST_HUB_PORT", "")
HUB_TOKEN_FILE = os.environ.get("DESKMATE_TEST_HUB_TOKEN_FILE", "")


# ---------------------------------------------------------------- a sandbox and a stub hub


class Sandbox:
    """HOME, CLAUDE_CONFIG_DIR and XDG_* under one temp folder, set in os.environ (claude_env() copies it)."""

    def __init__(self, use_config_dir: bool = True):
        self.root = Path(tempfile.mkdtemp(prefix="dmt-a2-"))
        self.home = self.root / "home"
        self.config = (self.root / "config") if use_config_dir else (self.home / ".claude")
        self.data = self.home / ".local" / "share" / "deskmate"
        for d in (self.home / ".config", self.home / ".local" / "share", self.config):
            d.mkdir(parents=True, exist_ok=True)
        self.env = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"), "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"), "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            "NO_COLOR": "1",
        }
        if use_config_dir:
            self.env["CLAUDE_CONFIG_DIR"] = str(self.config)
        path = os.environ.get("PATH", "/usr/bin:/bin")
        if CLAUDE:
            path = os.path.dirname(CLAUDE) + os.pathsep + path
        self.env["PATH"] = path
        self._saved = None

    def __enter__(self):
        self._saved = dict(os.environ)
        for k in ("CLAUDE_CONFIG_DIR", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_API_KEY"):
            os.environ.pop(k, None)
        os.environ.update(self.env)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self._saved)
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def gconf(self) -> Path:
        return self.config / ".claude.json" if "CLAUDE_CONFIG_DIR" in self.env else self.home / ".claude.json"

    @property
    def settings(self) -> Path:
        return self.config / "settings.json"

    def token(self, value: str = FAKE_TOKEN) -> Path:
        p = self.data / "secrets" / "hub-token"
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(str(p.parent), 0o700)
        p.write_text(value)
        os.chmod(str(p), 0o600)
        return p

    def values(self, **kw) -> dict:
        v = {"HUB_PORT": HUB_PORT or "7829", "DESKMATE_DATA_DIR": str(self.data), "SECRETARY": "on"}
        v.update(kw)
        return v

    def write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text()) if path.exists() else {}

    def snapshot(self) -> dict:
        """Everything a user would care about: settings.json, the MCP servers, skills, and the data and
        ~/.config/deskmate folders. Keys Claude Code itself keeps up to date in .claude.json are left out."""
        g = self.read_json(self.gconf)
        files = {}
        for base in (self.config / "skills", self.data, self.home / ".config" / "deskmate"):
            if base.exists():
                for f in sorted(base.rglob("*")):
                    rel = str(f.relative_to(self.root))
                    files[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "dir"
        return {"settings": self.read_json(self.settings) if self.settings.exists() else None,
                "settings_exists": self.settings.exists(),
                "mcp": g.get("mcpServers") or None,  # `claude mcp remove` leaves {}
                "project_mcp": {p: v.get("mcpServers") for p, v in (g.get("projects") or {}).items()
                                if isinstance(v, dict) and v.get("mcpServers")},
                "files": files}

    def claude(self, *args, timeout=60) -> subprocess.CompletedProcess:
        return subprocess.run([CLAUDE] + list(args), env=dict(os.environ), cwd=str(self.root), capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)


class StubHub:
    """Records POSTs to /hooks/<event>; delay holds each answer open (to look at running processes)."""

    def __init__(self, delay: float = 0.0):
        self.requests = []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                time.sleep(delay)
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                except OSError:  # curl gave up first, as it should
                    pass

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _client_env(home: Path, port: int, token_file: Path, secretary: str = "on") -> dict:
    conf = home / ".config" / "deskmate" / "client.env"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(f"HUB_URL=http://127.0.0.1:{port}\nTOKEN_FILE={token_file}\nSECRETARY={secretary}\nDATA_DIR={home}/data\n")
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
            "TMPDIR": str(home / "tmp")}


# ---------------------------------------------------------------- the scripts, without Claude Code


needs_curl = unittest.skipUnless(shutil.which("curl"), "curl not installed (the hook then does nothing, by design)")


class Scripts(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="dmt-a2-sh-"))
        (self.home / "tmp").mkdir()
        self.token_file = self.home / "hub-token"
        self.token_file.write_text(FAKE_TOKEN + "\n")
        os.chmod(str(self.token_file), 0o600)
        self.hub = StubHub()

    def tearDown(self):
        self.hub.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def run_hook(self, event, env, payload=b'{"tool_name":"mcp__deskmate__desk_status","session_id":"s1"}'):
        t = time.time()
        p = subprocess.run(["sh", str(HOOK), event], input=payload, env=env, capture_output=True, timeout=10)
        return p, time.time() - t

    @needs_curl
    def test_hook_posts_each_event_with_the_token(self):
        env = _client_env(self.home, self.hub.port, self.token_file)
        for event in ("pre-tool", "stop", "session-end", "pre-compact"):
            p, _ = self.run_hook(event, env)
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        self.assertEqual([r["path"] for r in self.hub.requests],
                         ["/hooks/pre-tool", "/hooks/stop", "/hooks/session-end", "/hooks/pre-compact"])
        self.assertTrue(all(r["auth"] == "Bearer " + FAKE_TOKEN for r in self.hub.requests))
        self.assertEqual(json.loads(self.hub.requests[0]["body"])["tool_name"], "mcp__deskmate__desk_status")
        self.assertEqual(list((self.home / "tmp").iterdir()), [], "the header file must be removed")

    @needs_curl
    def test_secretary_off_posts_only_pre_tool(self):
        env = _client_env(self.home, self.hub.port, self.token_file, secretary="off")
        for event in ("stop", "session-end", "pre-compact", "pre-tool"):
            p, _ = self.run_hook(event, env)
            self.assertEqual(p.returncode, 0)
        self.assertEqual([r["path"] for r in self.hub.requests], ["/hooks/pre-tool"])

    def test_hook_quiet_without_setup_or_hub(self):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "none")}
        p, _ = self.run_hook("pre-tool", env)  # no client.env
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        env = _client_env(self.home, self.hub.port, self.home / "missing-token")
        p, _ = self.run_hook("pre-tool", env)  # no token file
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        p, _ = self.run_hook("something-else", _client_env(self.home, self.hub.port, self.token_file))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.hub.requests, [])
        # A hub that does not answer: the hook gives up after about a second, still exit 0.
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            p, took = self.run_hook("pre-tool", _client_env(self.home, s.getsockname()[1], self.token_file))
        finally:
            s.close()
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        self.assertLess(took, 3)

    @needs_curl
    @unittest.skipUnless(Path("/proc/self/cmdline").exists(), "needs /proc")
    def test_token_never_in_a_command_line(self):
        self.hub.close()
        self.hub = StubHub(delay=1.5)  # curl gives up at 1 s; the scan runs while it waits
        env = _client_env(self.home, self.hub.port, self.token_file)
        proc = subprocess.Popen(["sh", str(HOOK), "pre-tool"], stdin=subprocess.PIPE, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.stdin.write(b"{}")
        proc.stdin.close()
        seen_curl, hits = False, []
        deadline = time.time() + 1.2
        while time.time() < deadline:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
                except OSError:
                    continue
                if b"curl" in cmd and b"/hooks/pre-tool" in cmd:
                    seen_curl = True
                if FAKE_TOKEN.encode() in cmd:
                    hits.append(pid)
            ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
            if FAKE_TOKEN in ps:
                hits.append("ps")
            time.sleep(0.05)
        proc.wait(timeout=5)
        self.assertTrue(seen_curl, "curl was not seen running; the scan proved nothing")
        self.assertEqual(hits, [])
        self.assertEqual(proc.returncode, 0)

    def test_mcp_headers(self):
        env = _client_env(self.home, 1, self.token_file)
        p = subprocess.run(["sh", str(HEADERS)], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"Authorization": "Bearer " + FAKE_TOKEN})
        self.token_file.write_text("bad token\n\"x")
        p = subprocess.run(["sh", str(HEADERS)], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertNotIn("bad token", p.stderr)
        self.token_file.unlink()
        p = subprocess.run(["sh", str(HEADERS)], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn("no hub token", p.stderr)

    def test_mcp_headers_installed_copy_finds_the_token_next_to_it(self):
        data = self.home / "data"
        (data / "bin").mkdir(parents=True)
        (data / "secrets").mkdir()
        (data / "secrets" / "hub-token").write_text(FAKE_TOKEN)
        shutil.copy(str(HEADERS), str(data / "bin" / "mcp-headers"))
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "none")}
        p = subprocess.run(["sh", str(data / "bin" / "mcp-headers")], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(p.stdout)["Authorization"], "Bearer " + FAKE_TOKEN)

    @unittest.skipUnless(shutil.which("dash"), "dash not installed")
    def test_scripts_parse_in_dash(self):
        for f in (HOOK, HEADERS):
            p = subprocess.run(["dash", "-n", str(f)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)


# ---------------------------------------------------------------- the plugin files


class PluginFiles(unittest.TestCase):
    def test_marketplace_lists_both_plugins(self):
        m = json.loads((PLUGIN / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(m["name"], "deskmate")
        self.assertEqual([(p["name"], p["source"]) for p in m["plugins"]], [("deskmate", "./deskmate"), ("habits", "./habits")])

    def test_plugin_and_hooks(self):
        p = json.loads((PLUGIN / "deskmate" / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(p["name"], "deskmate")
        self.assertRegex(p["version"], r"^\d+\.\d+\.\d+$")
        h = json.loads((PLUGIN / "deskmate" / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(sorted(h), ["PreCompact", "PreToolUse", "SessionEnd", "Stop"])
        self.assertEqual(h["PreToolUse"][0]["matcher"], cc.MATCHER)
        # The hub links a tool call to its hook by the name after the last "__" (hub/app/sessions.py).
        self.assertTrue(re.fullmatch(cc.MATCHER, "mcp__deskmate__desk_status"))
        args = {"PreToolUse": "pre-tool", "Stop": "stop", "SessionEnd": "session-end", "PreCompact": "pre-compact"}
        for event, arg in args.items():
            cmd = h[event][0]["hooks"][0]["command"]
            self.assertEqual(cmd, f'sh "${{CLAUDE_PLUGIN_ROOT}}/bin/hook" {arg}')
            self.assertLessEqual(h[event][0]["hooks"][0]["timeout"], 10)

    def test_skill_is_generic(self):
        text = (PLUGIN / "deskmate" / "skills" / "desk" / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: desk\ndescription: "))
        for word in ("desk_status", "browser_open", "desk_ask_human", "desk_files", "why"):
            self.assertIn(word, text)
        for bad in ("Ashim", "ashim", "sageflick", "/home/", "/Users/", "7800", "Discord", " his ", " her ", "edm_react",
                    "sageweb", "viway"):
            self.assertNotIn(bad, text)

    def test_nothing_personal_in_plugin_deskmate_or_connect(self):
        files = [f for f in (PLUGIN / "deskmate").rglob("*") if f.is_file()]
        files += [PLUGIN / ".claude-plugin" / "marketplace.json", REPO / "setup" / "deskmate_setup" / "claude_connect.py"]
        for f in files:
            text = f.read_text(errors="replace")
            for bad in ("ashim", "sageflick", "discord.com/api/webhooks", "sk-ant-", "/home/", "/Users/"):
                self.assertNotIn(bad, text.lower() if bad == "ashim" else text, f"{bad} in {f}")

    def test_old_install_is_gone(self):
        self.assertFalse((REPO / "scripts").exists())
        self.assertFalse((REPO / "skill").exists())

    @needs_claude
    def test_claude_validates_the_marketplace(self):
        with Sandbox() as sb:
            p = sb.claude("plugin", "validate", str(PLUGIN))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            p = sb.claude("plugin", "validate", str(PLUGIN / "deskmate"))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


# ---------------------------------------------------------------- pure helpers


class Helpers(unittest.TestCase):
    def test_strip_hooks_keeps_everything_else(self):
        settings = {"model": "x", "hooks": {
            "PreToolUse": [{"matcher": "mcp__deskmate__.*", "hooks": [{"type": "command", "command": "/d/deskmate/hook.sh pre-tool"}]},
                           {"matcher": "Bash", "hooks": [{"type": "command", "command": "mine.sh"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/d/deskmate/hook.sh stop"},
                                {"type": "command", "command": "notify.sh"}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": "/d/deskmate/hook.sh session-end"}]}]}}
        new, removed = cc._strip_hooks(settings, lambda e, h: cc._legacy_hook(h))
        self.assertEqual(len(removed), 3)
        self.assertEqual(new, {"model": "x", "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mine.sh"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}]}})
        self.assertEqual(cc._strip_hooks({"a": 1}, lambda e, h: True), ({"a": 1}, []))

    def test_mcp_kind(self):
        want = cc._want_entry(7800, Path("/x/bin/mcp-headers"))
        self.assertEqual(cc._mcp_kind(None), "none")
        self.assertEqual(cc._mcp_kind({"type": "http", "url": "u", "headers": {"Authorization": "Bearer t"}}), "legacy")
        self.assertEqual(cc._mcp_kind(dict(want), want), "current")
        self.assertEqual(cc._mcp_kind(dict(want, url="http://127.0.0.1:1/mcp"), want), "ours")
        self.assertEqual(cc._mcp_kind({"type": "stdio", "command": "x"}, want), "other")
        self.assertEqual(cc._want_entry(1, Path("/a b/bin/mcp-headers"))["headersHelper"], "'/a b/bin/mcp-headers'")

    def test_mask(self):
        self.assertEqual(cc._mask('headers: {"Authorization": "Bearer abc.def"}'), 'headers: {"Authorization": "Bearer …"}')

    def test_legacy_skill_hashes_include_the_last_shipped_skill(self):
        # The old installer copied skill/deskmate/SKILL.md; both versions it ever shipped are recognised.
        self.assertEqual(len(cc.LEGACY_SKILL_SHA256), 2)


# ---------------------------------------------------------------- with the real claude CLI


def _plugin_cache_skill(sb: Sandbox) -> list:
    return sorted(str(p.relative_to(sb.config)) for p in (sb.config / "plugins").rglob("skills/desk/SKILL.md"))


@needs_claude
class Connect(unittest.TestCase):
    maxDiff = None

    def test_clean_connect_then_disconnect(self):
        with Sandbox() as sb:
            if HUB_PORT and HUB_TOKEN_FILE:
                sb.token(Path(HUB_TOKEN_FILE).read_text().strip())
            else:
                sb.token()
            before = sb.snapshot()
            prev = cc.preview(sb.values())
            whats = [c["what"] for c in prev["changes"]]
            self.assertEqual(len(whats), 5, whats)
            self.assertIn("Add the deskmate MCP server for every session (user scope)", whats)
            self.assertIn("Install the plugin deskmate@deskmate", whats)
            self.assertNotIn(FAKE_TOKEN, json.dumps(prev))

            events = []
            res = cc.connect(sb.values(), events.append)
            self.assertTrue(res["ok"], res)
            self.assertNotIn(FAKE_TOKEN, json.dumps(events) + json.dumps(res))
            self.assertTrue(all(e["phase"] == "Connect Claude Code" for e in events))

            # MCP: headersHelper, no token in Claude Code's config.
            entry = sb.read_json(sb.gconf)["mcpServers"]["deskmate"]
            self.assertEqual(set(entry), {"type", "url", "headersHelper"})
            self.assertEqual(entry["headersHelper"], str(sb.data / "bin" / "mcp-headers"))
            self.assertNotIn(FAKE_TOKEN, sb.gconf.read_text() + sb.settings.read_text())
            self.assertEqual(oct(os.stat(entry["headersHelper"]).st_mode & 0o777), "0o700")
            env_file = sb.home / ".config" / "deskmate" / "client.env"
            self.assertEqual(oct(env_file.stat().st_mode & 0o777), "0o600")
            self.assertNotIn(FAKE_TOKEN, env_file.read_text())
            lst = sb.claude("mcp", "list").stdout
            self.assertIn("deskmate: http://127.0.0.1:", lst)
            if HUB_PORT:
                self.assertIn("✔ Connected", lst)
                self.assertTrue(res["connected"], res)
            else:
                self.assertFalse(res["connected"])

            # Plugin: installed and enabled, hooks with the matcher, skill desk.
            plugins = json.loads(sb.claude("plugin", "list", "--json").stdout)
            mine = [p for p in plugins if p.get("id") == "deskmate@deskmate"]
            self.assertEqual(len(mine), 1, plugins)
            self.assertTrue(mine[0].get("enabled"), mine)
            s = sb.read_json(sb.settings)
            self.assertEqual(s["enabledPlugins"], {"deskmate@deskmate": True})
            self.assertEqual(s["extraKnownMarketplaces"]["deskmate"]["source"],
                             {"source": "directory", "path": str(PLUGIN)})
            self.assertTrue(_plugin_cache_skill(sb), "skills/desk/SKILL.md is not in the plugin cache")
            st = cc.status(sb.values())
            self.assertEqual((st["mcp"], st["hooks"], st["skill"], st["client_env"]), ("ok", "ok", "ok", "ok"), st)
            self.assertTrue(st["marketplace"]["this_clone"])
            rec = json.loads((sb.data / "installed.json").read_text())["claude"]
            self.assertEqual(rec["mcp"]["before"], "none")
            self.assertEqual(rec["plugins"]["deskmate"]["added"], True)

            # A second run changes nothing.
            again = cc.connect(sb.values())
            self.assertTrue(again["ok"], again)
            self.assertEqual(again["changes"][:-1] if again["changes"] and "reaches the hub" in again["changes"][-1]
                             else again["changes"], [])

            # SECRETARY=off rewrites client.env only.
            off = cc.connect(sb.values(SECRETARY="off"))
            self.assertIn("SECRETARY=off", env_file.read_text())
            self.assertEqual(len([c for c in off["changes"] if "client.env" in c]), 1, off["changes"])

            out = cc.disconnect()
            self.assertTrue(out["ok"], out)
            after = sb.snapshot()
            self.assertEqual(after, before)
            st = cc.status(sb.values())
            self.assertEqual((st["mcp"], st["hooks"], st["skill"]), ("missing", "missing", "missing"), st)

    def test_without_claude_config_dir(self):
        # The usual case: no CLAUDE_CONFIG_DIR, so ~/.claude.json and ~/.claude in the sandbox HOME.
        with Sandbox(use_config_dir=False) as sb:
            sb.token()
            sb.write_json(sb.settings, {"theme": "dark"})
            before = sb.snapshot()
            res = cc.connect(sb.values())
            self.assertTrue(res["ok"], res)
            self.assertIn("deskmate", sb.read_json(sb.home / ".claude.json")["mcpServers"])
            self.assertEqual(sb.read_json(sb.settings)["theme"], "dark")
            self.assertTrue(cc.disconnect()["ok"])
            self.assertEqual(sb.snapshot(), before)

    def test_refuses_without_token_or_with_broken_json(self):
        with Sandbox() as sb:
            res = cc.connect(sb.values())
            self.assertFalse(res["ok"])
            self.assertIn("no hub token", res["error"])
            sb.token()
            sb.settings.write_text("{ not json")
            res = cc.connect(sb.values())
            self.assertFalse(res["ok"])
            self.assertIn("is not valid JSON", res["error"])
            self.assertEqual(sb.settings.read_text(), "{ not json")
            self.assertFalse(sb.gconf.exists())

    def test_legacy_migration(self):
        with Sandbox() as sb:
            sb.token()
            old_dir = sb.data
            # What scripts/install.sh left: a static header, the copied skill, hook.sh and a token copy, hooks.
            sb.write_json(sb.gconf, {"numStartups": 3, "mcpServers": {
                "deskmate": {"type": "http", "url": "http://127.0.0.1:7800/mcp", "headers": {"Authorization": "Bearer " + FAKE_TOKEN}},
                "github": {"type": "stdio", "command": "gh-mcp", "args": []}}})
            skill = sb.config / "skills" / "deskmate"
            skill.mkdir(parents=True)
            shutil.copy(_old_skill_file(sb), str(skill / "SKILL.md"))
            (sb.config / "skills" / "my-notes").mkdir()
            (sb.config / "skills" / "my-notes" / "SKILL.md").write_text("---\nname: my-notes\ndescription: x\n---\n")
            (old_dir / "hook.sh").write_text('#!/bin/sh\ncurl -s "http://127.0.0.1:7800/hooks/$1"\n')
            (old_dir / "token").write_text(FAKE_TOKEN)
            hook = f"{old_dir}/hook.sh"
            user_settings = {
                "cleanupPeriodDays": 45,
                "permissions": {"allow": ["Bash(ls:*)"]},
                "hooks": {
                    "PreToolUse": [{"matcher": "mcp__deskmate__.*",
                                    "hooks": [{"type": "command", "command": f"{hook} pre-tool", "timeout": 2}]},
                                   {"matcher": "Bash", "hooks": [{"type": "command", "command": "/home/alex/bin/guard.sh"}]}],
                    "Stop": [{"hooks": [{"type": "command", "command": f"{hook} stop"},
                                        {"type": "command", "command": "/home/alex/bin/notify.sh"}]}],
                    "SessionEnd": [{"hooks": [{"type": "command", "command": f"{hook} session-end"}]}]}}
            sb.write_json(sb.settings, user_settings)

            found = {c["kind"] + ":" + c["name"] for c in cc.conflicts(sb.values())}
            self.assertIn("legacy:deskmate", found)
            self.assertIn("legacy:deskmate skill", found)
            prev = [c["what"] for c in cc.preview(sb.values())["changes"]]
            for w in ("Replace the old deskmate MCP server, which has the token written in it",
                      "Remove the old Deskmate hooks",
                      "Remove the old deskmate skill (the plugin's deskmate:desk replaces it)",
                      "Remove the old hook script and token copy"):
                self.assertIn(w, prev)
            self.assertEqual(cc.status(sb.values())["mcp"], "legacy")

            res = cc.connect(sb.values())
            self.assertTrue(res["ok"], res)
            g = sb.read_json(sb.gconf)
            self.assertEqual(set(g["mcpServers"]["deskmate"]), {"type", "url", "headersHelper"})
            self.assertEqual(g["mcpServers"]["github"], {"type": "stdio", "command": "gh-mcp", "args": []})
            self.assertNotIn(FAKE_TOKEN, sb.gconf.read_text())
            s = sb.read_json(sb.settings)
            expect = dict(user_settings)
            expect["hooks"] = {"PreToolUse": [user_settings["hooks"]["PreToolUse"][1]],
                               "Stop": [{"hooks": [{"type": "command", "command": "/home/alex/bin/notify.sh"}]}]}
            for k in ("enabledPlugins", "extraKnownMarketplaces"):
                s.pop(k)
            self.assertEqual(s, expect)
            self.assertFalse(skill.exists())
            self.assertTrue((sb.config / "skills" / "my-notes" / "SKILL.md").exists())
            self.assertFalse((old_dir / "hook.sh").exists())
            self.assertFalse((old_dir / "token").exists())
            self.assertTrue((sb.data / "secrets" / "hub-token").exists())
            # Backups of what changed, private, in the data folder; the old .claude.json copy is 0600.
            b = Path(res["backup"])
            self.assertEqual(sorted(p.name for p in b.iterdir()), [".claude.json", "hook.sh", "settings.json", "skills-deskmate"])
            self.assertEqual(oct(b.stat().st_mode & 0o777), "0o700")
            self.assertEqual(oct((b / ".claude.json").stat().st_mode & 0o777), "0o600")
            # settings.json is backed up just before the old hooks go, after the plugin went in.
            backed = json.loads((b / "settings.json").read_text())
            self.assertEqual({k: v for k, v in backed.items() if k not in ("enabledPlugins", "extraKnownMarketplaces")},
                             user_settings)
            self.assertEqual(cc.status(sb.values())["mcp"], "ok")
            self.assertEqual(cc.legacy(), {"mcp": False, "hooks": [], "skill": None})
            self.assertEqual({c["kind"] for c in cc.conflicts(sb.values())} & {"legacy"}, set())

            # Disconnect: the user's own things stay.
            self.assertTrue(cc.disconnect()["ok"])
            s = sb.read_json(sb.settings)
            self.assertEqual(s, expect)
            self.assertNotIn("deskmate", sb.read_json(sb.gconf)["mcpServers"])

    def test_conflicts_remove_and_restore(self):
        with Sandbox() as sb:
            sb.token()
            proj = sb.home / "work" / "site"
            proj.mkdir(parents=True)
            sb.write_json(sb.gconf, {"mcpServers": {
                "playwright": {"type": "stdio", "command": "npx", "args": ["@playwright/mcp@latest"]},
                "github": {"type": "stdio", "command": "gh-mcp", "args": []}},
                "projects": {str(proj): {"mcpServers": {"chrome-devtools": {"type": "stdio", "command": "npx",
                                                                          "args": ["chrome-devtools-mcp"]}}}}})
            for name in ("viway-browser", "viway-note", "my-notes"):
                (sb.config / "skills" / name).mkdir(parents=True)
                (sb.config / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n")
            sb.write_json(sb.settings, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/home/alex/.viway/hook stop"},
                                                                      {"type": "command", "command": "mine.sh"}]}]}})
            before = sb.snapshot()
            found = {c["id"]: c for c in cc.conflicts(sb.values())}
            self.assertEqual(sorted(found), sorted(["mcp:playwright", f"mcp:chrome-devtools@{proj}", "skill:viway-browser",
                                                    "skill:viway-note", "hook:viway Stop hook@Stop"]))
            self.assertTrue(all(c["removable"] for c in found.values()))
            self.assertNotIn("@playwright/mcp", json.dumps(found), "conflicts must not show commands")
            self.assertEqual(cc.preview(sb.values())["conflicts"], list(found.values()))

            res = cc.connect(sb.values(CONNECT_REMOVE=",".join(found)))
            self.assertTrue(res["ok"], res)
            self.assertEqual([w for w in res["warnings"] if "Could not" in w], [])
            self.assertEqual(cc.conflicts(sb.values()), [])
            g = sb.read_json(sb.gconf)
            self.assertEqual(sorted(g["mcpServers"]), ["deskmate", "github"])
            self.assertFalse(g["projects"][str(proj)].get("mcpServers"))
            self.assertEqual(sorted(p.name for p in (sb.config / "skills").iterdir()), ["my-notes"])
            self.assertEqual(sb.read_json(sb.settings)["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]})

            out = cc.restore_conflicts()
            self.assertEqual(out["errors"], [])
            self.assertEqual(len(out["restored"]), 5)
            self.assertTrue(cc.disconnect()["ok"])
            after = sb.snapshot()
            # The backups of what was removed stay in the data folder.
            self.assertTrue(any("/backups/" in f for f in after["files"]))
            after["files"] = {f: h for f, h in after["files"].items() if "/backups" not in f}
            self.assertEqual(after, before)

    def test_install_and_uninstall_habits_keeps_marketplace_for_the_other(self):
        with Sandbox() as sb:
            sb.token()
            self.assertTrue(cc.connect(sb.values())["ok"])
            cc.install_plugin("habits")
            ids = {p["id"] for p in json.loads(sb.claude("plugin", "list", "--json").stdout)}
            self.assertEqual(ids, {"deskmate@deskmate", "habits@deskmate"})
            out = cc.disconnect()
            self.assertIn("the plugin marketplace “deskmate” (another Deskmate plugin still uses it)", out["kept"])
            ids = {p["id"] for p in json.loads(sb.claude("plugin", "list", "--json").stdout)}
            self.assertEqual(ids, {"habits@deskmate"})
            cc.uninstall_plugin("habits")
            self.assertEqual(json.loads(sb.claude("plugin", "list", "--json").stdout), [])
            markets = json.loads(sb.claude("plugin", "marketplace", "list", "--json").stdout or "[]")
            self.assertNotIn("deskmate", [m.get("name") for m in markets])
            self.assertFalse(sb.settings.exists())
            self.assertFalse((sb.data / "installed.json").exists())


def _old_skill_file(sb: Sandbox) -> str:
    """The skill text scripts/install.sh copied (from git history; the file itself is gone from the tree)."""
    p = sb.root / "old_skill.md"
    try:
        text = subprocess.run(["git", "-C", str(REPO), "show", "81f44c7:skill/deskmate/SKILL.md"], capture_output=True,
                              timeout=10).stdout
    except OSError:
        text = b""
    if not text:  # no git history: an edited old skill is recognised by its content
        text = b"---\nname: deskmate\ndescription: old\n---\nUse browser_open and desk_ask_human.\n"
    p.write_bytes(text)
    return str(p)


if __name__ == "__main__":
    unittest.main()
