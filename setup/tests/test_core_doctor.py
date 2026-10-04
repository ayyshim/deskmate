"""doctor: the pre-install checks and the ones that need Deskmate running (hub, /mcp, files, listeners)."""

from __future__ import annotations

import io
import os
import types
import unittest

from test_core_support import FAKE_TOKEN, CoreCase

from deskmate_setup import compose, detect, doctor, settings


def ids(checks):
    return {c["id"]: c for c in checks}


class Preinstall(CoreCase):
    def test_not_set_up(self):
        checks = doctor.run()
        by = ids(checks)
        for k in ("platform", "docker", "compose", "memory", "disk", "claude", "ports", "data", "existing", "setup"):
            self.assertIn(k, by)
        self.assertEqual(by["docker"]["status"], "ok")
        self.assertEqual(by["claude"]["status"], "warn")
        self.assertEqual(by["setup"]["status"], "warn")
        for c in checks:
            self.assertEqual(set(c), {"id", "label", "status", "detail", "fix"})
            self.assertIn(c["status"], ("ok", "warn", "fail"))

    def test_docker_problems_block(self):
        self.docker = dict(self.docker, running=False, sudo_needed=True, label="Docker needs sudo here",
                           fix="sudo usermod -aG docker $USER, then log out and in again")
        checks = doctor.preinstall()
        self.assertEqual([c["id"] for c in doctor.blocking(checks)], ["docker"])
        self.docker = dict(self.docker, running=True, sudo_needed=False, compose_version="2.20.0")
        self.assertEqual(ids(doctor.preinstall())["compose"]["status"], "fail")

    def test_data_dir_rules(self):
        self.assertIn("spaces", doctor.data_dir_problem("/home/alex/my data"))
        self.assertIn("Windows drive", doctor.data_dir_problem("/mnt/c/Users/alex/deskmate"))
        self.assertIn("full path", doctor.data_dir_problem("relative"))
        self.assertEqual(doctor.data_dir_problem(str(self.home / ".local" / "share" / "deskmate")), "")

    def test_busy_port_fails(self):
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            port = s.getsockname()[1]
            c = doctor.ports_check({"HUB_PORT": str(port), "DESK_NETWORK": "isolated"})
            self.assertEqual(c["status"], "fail")
            self.assertIn(str(port), c["label"])
        finally:
            s.close()

    def test_summary_and_report(self):
        checks = [{"id": "a", "label": "A", "status": "ok", "detail": "", "fix": ""},
                  {"id": "b", "label": "B", "status": "warn", "detail": "why", "fix": "do this"}]
        self.assertEqual(doctor.summary(checks), "All checks passed, 1 needs a look.")
        self.assertEqual(doctor.summary(checks[:1]), "All 1 checks passed.")
        out = io.StringIO()
        doctor.print_report(checks, out)
        self.assertIn("! B\n      why\n      -> do this", out.getvalue().replace("→", "->"))


class Postinstall(CoreCase):
    def setUp(self):
        super().setUp()
        settings.save({"claude-token": FAKE_TOKEN, "HUB_PORT": "7810", "DESK_NETWORK": "isolated"})
        compose.prepare(settings.load())
        self.patch(compose, "ps", lambda values=None: [
            {"service": "desk", "name": "deskmate-desk", "state": "running", "status": "Up 1 minute", "health": ""},
            {"service": "hub", "name": "deskmate-hub", "state": "running", "status": "Up 1 minute", "health": ""}])
        self.http = {}

        def fake_http(port, path="/", method="GET", headers=None, body=None, timeout=5.0):
            auth = (headers or {}).get("Authorization", "")
            if path == "/":
                return self.http.get("/", 200), {}, b""
            if path == "/mcp" and method == "POST":
                if not auth:
                    return self.http.get("no_token", 401), {}, b""
                if b'"tools/list"' in (body or b""):
                    return 200, {}, b'data: {"result": {"tools": [{"name": "a"}, {"name": "b"}]}}\n'
                return 200, {"mcp-session-id": "s1"}, b"{}"
            return 200, {}, b""

        self.patch(compose, "http", fake_http)
        self.patch(compose, "capture", lambda args, timeout=60.0, values=None: (0, "hub: connected to the desk's browser\n", ""))
        self.patch(doctor, "_listeners", lambda port: ["127.0.0.1"])

    def test_all_good(self):
        by = ids(doctor.run())
        for k in ("env", "private", "compose-local", "containers", "hub", "mcp", "browser", "listen", "login"):
            self.assertEqual(by[k]["status"], "ok", by[k])
        self.assertIn("2 tools", by["mcp"]["label"])
        self.assertIn("sk-ant-oat…Qx7A", by["login"]["label"])
        self.assertNotIn(FAKE_TOKEN, repr(by))

    def test_problems(self):
        os.chmod(str(settings.data_dir() / "secrets" / "claude-token"), 0o644)
        os.chmod(str(settings.env_path()), 0o644)
        self.http["no_token"] = 200
        self.patch(doctor, "_listeners", lambda port: ["0.0.0.0"])
        by = ids(doctor.run())
        self.assertEqual(by["private"]["status"], "warn")
        self.assertEqual(by["env"]["status"], "warn")
        self.assertEqual(by["mcp-auth"]["status"], "fail")
        self.assertEqual(by["listen"]["status"], "fail")
        settings.save({"DESK_MONITORS": "1"})
        (settings.repo_dir() / "compose.local.yaml").write_text("# stale\n")
        self.assertEqual(ids(doctor.run())["compose-local"]["status"], "warn")

    def test_hub_down(self):
        self.http["/"] = 0
        by = ids(doctor.run())
        self.assertEqual(by["hub"]["status"], "fail")
        self.assertNotIn("mcp", by)

    def test_claude_connection_and_habits(self):
        self.claude = dict(self.claude, installed=True, path="/nonexistent/claude", version="2.1.278")
        self.fake_module("claude_connect", types.SimpleNamespace(status=lambda: {"mcp": "legacy"}))
        self.fake_module("habits", types.SimpleNamespace(status=lambda: {"installed": True, "problems": ["the hub is gone"]}))
        settings.save({"HABITS": "on"})
        by = ids(doctor.run())
        self.assertEqual(by["connect"]["status"], "warn")
        self.assertEqual(by["habits"]["status"], "warn")
        self.assertIn("the hub is gone", by["habits"]["detail"])


if __name__ == "__main__":
    unittest.main()
