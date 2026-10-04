"""The hub's guards and settings: Host and Origin checks, bearer auth, typed-value masking, file modes,
config parsing, the DevTools address in the bridge modes, and no personal data in the files role B owns.

Runs with the hub's own dependencies (the hub image), from the hub folder:
    python -m unittest discover -s tests -p 'test_hub*.py'      (pytest works too)
Every value here is fake. Nothing reaches a desk, a browser or the network.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if "HUB_DATA" not in os.environ:  # the first hub test module to load sets up a world for all of them
    _tmp = Path(tempfile.mkdtemp(prefix="deskmate-hub-test-"))
    (_tmp / "secrets").mkdir()
    (_tmp / "secrets" / "hub_token").write_text("fake-hub-token-for-tests\n")
    (_tmp / "exchange").mkdir()
    os.environ.update(
        {
            "HUB_PORT": "7850",
            "HUB_DATA": str(_tmp / "data"),
            "SECRETS_DIR": str(_tmp / "secrets"),
            "RUN_DIR": str(_tmp / "run"),
            "EXCHANGE_DIR": str(_tmp / "exchange"),
            "DESKMATE_OWNER": "Alex",
            "HOST_HOME": "/home/alex",
            "DESKMATE_DATA_DIR": "/home/alex/.local/share/deskmate",
            "DESK_NETWORK": "host",
            "DESK_MONITORS": "2",
            "DESK_MONITOR_SIZE": "1280x800",
            "TZ": "UTC",
            "NOTIFY_KIND": "none",
        }
    )

from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from app import auth, browser, config, db, journal, main, tools, web  # noqa: E402

PORT = config.PORT
TOKEN = "fake-hub-token-for-tests"


def client(host: str = f"127.0.0.1:{PORT}") -> TestClient:
    return TestClient(web.app, base_url=f"http://{host}")


class TrustedHostsTest(unittest.TestCase):
    """Every route answers only to the hub's own address (DNS rebinding; `http://hub:<port>` from the desk)."""

    def test_own_addresses_pass(self):
        for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}", f"LOCALHOST:{PORT}"):
            with self.subTest(host=host):
                r = client().get("/", headers={"host": host})
                self.assertEqual(r.status_code, 200)

    def test_foreign_host_gets_421_everywhere(self):
        c = client()
        for host in ("evil.example", f"evil.example:{PORT}", f"hub:{PORT}", f"127.0.0.1:{PORT + 1}", "127.0.0.1", f"desk:{PORT}"):
            for method, path in (("GET", "/"), ("GET", "/api/state"), ("POST", "/mcp"), ("POST", "/hooks/stop"), ("GET", "/static/app.js"), ("GET", "/login?t=" + TOKEN)):
                with self.subTest(host=host, path=path):
                    r = c.request(method, path, headers={"host": host, "authorization": f"Bearer {TOKEN}"})
                    self.assertEqual(r.status_code, 421)
                    self.assertNotIn(TOKEN, r.text)

    def test_missing_host_header_gets_421(self):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", "http_version": "1.1"}
        asyncio.run(auth.TrustedHosts(web.app)(scope, receive, send))
        self.assertEqual(sent[0]["status"], 421)

    def test_websocket_with_foreign_host_is_refused(self):
        c = client()
        c.cookies.set(auth.COOKIE, auth.ui_secret())
        with self.assertRaises(WebSocketDisconnect) as caught:
            with c.websocket_connect("/vnc", headers={"host": f"evil.example:{PORT}"}):
                pass
        self.assertEqual(caught.exception.code, 1008)

    def test_lifespan_passes_through(self):
        async def run():
            calls = []

            async def inner(scope, receive, send):
                calls.append(scope["type"])

            await auth.TrustedHosts(inner)({"type": "lifespan"}, None, None)
            return calls

        self.assertEqual(asyncio.run(run()), ["lifespan"])


class AuthTest(unittest.TestCase):
    def test_hooks_need_the_bearer(self):
        c = client()
        self.assertEqual(c.post("/hooks/pre-tool", json={}).status_code, 401)
        self.assertEqual(c.post("/hooks/pre-tool", json={}, headers={"authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(c.post("/hooks/pre-tool", json={}, headers={"authorization": f"Bearer {TOKEN}"}).status_code, 200)

    def test_mcp_needs_the_bearer(self):
        r = client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(r.status_code, 401)

    def test_ui_needs_the_cookie_and_its_own_origin(self):
        c = client()
        self.assertEqual(c.get("/api/state").status_code, 401)
        c.cookies.set(auth.COOKIE, auth.ui_secret())
        self.assertEqual(c.post("/api/pause", json={"paused": False}, headers={"origin": "http://evil.example"}).status_code, 401)
        self.assertEqual(c.post("/api/pause", json={"paused": False}, headers={"origin": f"http://hub:{PORT}"}).status_code, 401)
        r = c.post("/api/pause", json={"paused": False}, headers={"origin": f"http://127.0.0.1:{PORT}"})
        self.assertEqual(r.status_code, 200)

    def test_sign_in_link(self):
        c = client()
        self.assertEqual(c.get("/login?t=wrong", follow_redirects=False).status_code, 401)
        r = c.get(f"/login?t={TOKEN}", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("httponly", r.headers["set-cookie"].lower())
        self.assertIn("samesite=strict", r.headers["set-cookie"].lower())

    def test_mcp_transport_security_is_built_from_the_port(self):
        ts = tools.mcp.settings.transport_security
        self.assertTrue(ts.enable_dns_rebinding_protection)
        self.assertEqual(set(ts.allowed_hosts), {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"})
        self.assertEqual(set(ts.allowed_origins), {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", f"http://[::1]:{PORT}"})

    def test_mcp_origin_check(self):
        from mcp.server.transport_security import TransportSecurityMiddleware
        from starlette.requests import Request

        guard = TransportSecurityMiddleware(tools.mcp.settings.transport_security)

        def request(headers: dict) -> Request:
            raw = [(k.encode(), v.encode()) for k, v in headers.items()]
            return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": raw, "query_string": b""})

        base = {"host": f"127.0.0.1:{PORT}", "content-type": "application/json"}
        self.assertIsNone(asyncio.run(guard.validate_request(request(base), is_post=True)))
        bad_origin = asyncio.run(guard.validate_request(request({**base, "origin": "http://evil.example"}), is_post=True))
        self.assertEqual(bad_origin.status_code, 403)
        bad_host = asyncio.run(guard.validate_request(request({**base, "host": f"hub:{PORT}"}), is_post=True))
        self.assertEqual(bad_host.status_code, 421)


class MaskingTest(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(journal.mask("hunter2-secret"), "“…” (14 characters)")
        self.assertEqual(journal.mask("x"), "“…” (1 character)")
        self.assertEqual(journal.mask(None), "“…” (0 characters)")

    def test_browser_act_never_shows_typed_values(self):
        for action in ("fill", "type", "press", "select"):
            with self.subTest(action=action):
                shown = tools.act_text(action, "e12", "fake-password-123")
                self.assertNotIn("fake-password-123", shown)
                self.assertEqual(shown, f"{action} e12 “…” (17 characters)")
        self.assertEqual(tools.act_text("click", "e3", None), "click e3")
        self.assertEqual(tools.act_text("scroll", None, "up"), "scroll")

    def test_desk_input_type_steps_show_the_length_only(self):
        action, params, shown = tools._step(1, {"type": "fake-otp-918273"})
        self.assertEqual((action, params["text"]), ("type_text", "fake-otp-918273"))
        self.assertEqual(shown, "type (15 characters)")
        with self.assertRaises(ValueError) as caught:
            tools._step(1, {"typ": "fake-password-123"})
        self.assertNotIn("fake-password-123", str(caught.exception))

    def test_password_values_are_hidden_in_page_outlines(self):
        snap = "\n".join(
            [
                '- textbox "Email" [ref=e4]: alex@example.com',
                '- textbox "Password" [ref=e5]: fake-pass word',
                '- textbox "PIN" [active] [ref=e6]: "a: b"',
                '- paragraph [ref=e7]: fake-pass word appears in plain text too',
            ]
        )
        out = browser.mask_passwords(snap, ["fake-pass word", "a: b"])
        self.assertIn("alex@example.com", out)
        self.assertIn(f'- textbox "Password" [ref=e5]: {browser.MASKED}', out)
        self.assertIn(f'- textbox "PIN" [active] [ref=e6]: {browser.MASKED}', out)
        self.assertIn("- paragraph [ref=e7]: fake-pass word appears", out)  # only field values are touched
        self.assertEqual(browser.mask_passwords(snap, []), snap)


class DevToolsAddressTest(unittest.TestCase):
    def test_ip_addresses_and_localhost_are_kept(self):
        for url in ("http://127.0.0.1:7802", "http://localhost:7802", "http://[::1]:7802", "http://172.20.0.5:7812"):
            with self.subTest(url=url):
                self.assertEqual(asyncio.run(browser.cdp_endpoint(url)), url)

    def test_a_service_name_is_resolved(self):
        async def fake_getaddrinfo(self, host, port, **kw):
            assert host == "desk" and port == 7812
            return [(2, 1, 6, "", ("172.20.0.5", 7812))]

        with mock.patch("asyncio.base_events.BaseEventLoop.getaddrinfo", fake_getaddrinfo):
            self.assertEqual(asyncio.run(browser.cdp_endpoint("http://desk:7812")), "http://172.20.0.5:7812")


class FileModesTest(unittest.TestCase):
    def test_database_and_folders_are_private(self):
        main.prepare()  # umask 077 for this process, as in the hub
        folder = Path(tempfile.mkdtemp(prefix="deskmate-modes-"))
        old = folder / "old.db"
        old.write_text("")
        old.chmod(0o644)
        db._private(old)
        self.assertEqual(stat.S_IMODE(old.stat().st_mode), 0o600)
        new = folder / "new.txt"
        new.write_text("x")
        self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o600)
        sub = folder / "sub"
        sub.mkdir()
        self.assertEqual(stat.S_IMODE(sub.stat().st_mode), 0o700)
        loose = folder / "loose"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        main._private(loose, 0o700)
        self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o700)

    def test_the_hub_database_is_0600(self):
        main.prepare()
        db.conn()
        path = config.DATA / "deskmate.db"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class ConfigTest(unittest.TestCase):
    def env(self, **values):
        return mock.patch.dict(os.environ, values)

    def test_empty_means_the_default(self):
        with self.env(SOME_KEY="", SOME_SPACES="  "):
            self.assertEqual(config._env("SOME_KEY", "fallback"), "fallback")
            self.assertEqual(config._env("SOME_SPACES", "fallback"), "fallback")
        with self.env(SOME_KEY="value"):
            self.assertEqual(config._env("SOME_KEY", "fallback"), "value")

    def test_lists_switches_numbers_sizes(self):
        with self.env(PATHS="/home/alex/code:/home/alex/My Projects/:/home/alex/code:", ON="On", OFF="off", N="9", BAD="x"):
            self.assertEqual(config._list("PATHS"), ["/home/alex/code", "/home/alex/My Projects"])
            self.assertTrue(config._on("ON"))
            self.assertFalse(config._on("OFF"))
            self.assertTrue(config._on("UNSET_SWITCH"))
            self.assertEqual(config._int("N", 2, 1, 4), 4)
            self.assertEqual(config._int("BAD", 2, 1, 4), 2)
        self.assertEqual(config._size("1440X900"), (1440, 900))
        self.assertEqual(config._size("wide"), (1280, 800))

    def test_secrets_come_from_files(self):
        folder = Path(tempfile.mkdtemp(prefix="deskmate-secrets-"))
        (folder / "claude_token").write_text("  fake-claude-token \n")
        (folder / "empty").write_text("")
        with self.env(SECRETS_DIR=str(folder), LEGACY_FAKE="fake-legacy"):
            self.assertEqual(config.secret("claude_token"), "fake-claude-token")
            self.assertEqual(config.secret("empty", "LEGACY_FAKE"), "fake-legacy")
            self.assertEqual(config.secret("missing"), "")

    def test_hub_settings(self):
        self.assertEqual(config.PORT, PORT)
        self.assertEqual(config.HUB_URL, f"http://127.0.0.1:{PORT}")
        self.assertEqual(config.TOKEN, TOKEN)
        self.assertEqual(config.OWNER, "Alex")
        self.assertEqual(config.tilde("/home/alex/.local/share/deskmate/exchange"), "~/.local/share/deskmate/exchange")
        self.assertEqual(config.tilde("/srv/code"), "/srv/code")

    def test_monitors_fall_back_to_the_configured_layout(self):
        with mock.patch.object(config, "RUN_DIR", Path("/nonexistent-run-dir")):
            self.assertEqual(config.monitors(), (2, 1280, 800))


class ToolTextsTest(unittest.TestCase):
    def tool_list(self):
        return asyncio.run(tools.mcp.list_tools())

    def test_tools_and_their_words(self):
        listed = {t.name: t for t in self.tool_list()}
        self.assertIn("notify", listed)
        self.assertEqual(len(listed), 11)
        everything = tools.INSTRUCTIONS + " ".join((t.description or "") + str(t.inputSchema) for t in listed.values())
        for word in ("Discord", "Slack", "edm_react", "1280x800 monitors", "1 or 2; 0 for both"):
            self.assertNotIn(word, everything)
        self.assertIn("Alex", tools.INSTRUCTIONS)
        self.assertIn("Alex", listed["desk_ask_human"].description)
        self.assertIn("2 monitors of 1280x800", tools.INSTRUCTIONS)
        self.assertIn("~/.local/share/deskmate/exchange", tools.INSTRUCTIONS)

    def test_network_texts(self):
        texts = {}
        for mode in ("host", "host-access", "isolated"):
            with mock.patch.object(config, "NETWORK", mode):
                texts[mode] = tools.network_text()
        self.assertIn("is this machine's localhost", texts["host"])
        self.assertIn("127.0.0.1", texts["host-access"])
        self.assertIn("internet only", texts["isolated"])

    def test_desk_status_names_the_owner_and_the_address(self):
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={"mcp-session-id": "fake-status-session"})))
        with mock.patch.object(browser.browser, "all_tabs", mock.AsyncMock(return_value=[])):
            text = asyncio.run(tools.desk_status(ctx))
        self.assertIn(f"Alex watches this desk live at http://127.0.0.1:{PORT}.", text)
        self.assertIn("Notifications: off.", text)


class NoPersonalDataTest(unittest.TestCase):
    """Role B's files must work on anyone's machine: no owner names, home paths or private hosts."""

    PATTERN = re.compile(r"ashim|/home/(?!alex|bot\b)[a-z]|/Users/(?!alex)|sageflick|\.lan\b|discord\.com/api/webhooks|sk-ant-", re.I)

    def files(self) -> list[Path]:
        hub = Path(__file__).resolve().parents[1]
        repo = hub.parent
        out = [p for p in (hub / "app").glob("*.py") if p.name != "__init__.py"]
        out += [p for p in (hub / "Dockerfile", hub / "requirements.txt") if p.is_file()]  # tests use fake fixtures
        if (repo / "compose.yaml").is_file():  # run from a checkout (not inside the image)
            out += [repo / "compose.yaml", *sorted((repo / "deploy").glob("*.yaml"))]
            out += [p for p in sorted((repo / "desk").rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
        return out

    def test_role_b_files(self):
        secretary_owned = {"llm.py", "usage.py", "prompts.py", "digest.py", "brief.py", "ask.py"}
        for path in self.files():
            if path.parent.name == "secretary" or path.name in secretary_owned:
                continue
            text = path.read_text(errors="replace")
            with self.subTest(path=str(path)):
                self.assertIsNone(self.PATTERN.search(text), f"{path} has personal data")


if __name__ == "__main__":
    unittest.main()
