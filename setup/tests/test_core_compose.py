"""compose: compose.local.yaml (identity binds, spaces), prepare() (folders and secret files), running
docker compose from the repo with its output streamed and scrubbed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

from test_core_support import FAKE_TOKEN, REAL_REPO, CoreCase, mode

from deskmate_setup import compose, settings


class LocalFile(CoreCase):
    def values(self, **kw):
        v = {"SECRETARY": "on", "CLAUDE_CONFIG_DIRS": str(self.claude_dir), "SESSIONS_ROOTS": "", "DOCS_DIR": ""}
        v.update(kw)
        return v

    def test_binds_with_a_space(self):
        spaced = self.home / "My Projects"
        (spaced / "app").mkdir(parents=True)
        docs = self.home / "notes hub"
        docs.mkdir()
        text = compose.render_local(self.values(SESSIONS_ROOTS=f"{spaced}:{spaced / 'app'}", DOCS_DIR=str(docs)))
        self.assertIn(f'        source: "{spaced}"', text)
        self.assertIn(f'        target: "{spaced}"', text)
        self.assertIn(f'        source: "{self.claude_dir}/projects"', text)
        self.assertIn(f'        source: "{docs}"', text)
        self.assertNotIn(f'"{spaced}/app"', text, "a folder inside another root is not mounted twice")
        self.assertNotIn(f'source: "{self.claude_dir}"\n', text, "never the whole Claude Code folder")
        self.assertEqual(text.count("read_only: true"), 3)
        self.assertEqual(text.count("create_host_path: false"), 3)

    def test_missing_off_and_forbidden(self):
        text = compose.render_local(self.values(SESSIONS_ROOTS=str(self.home / "gone")))
        self.assertIn("# Left out, the folder doesn't exist: " + str(self.home / "gone"), text)
        self.assertIn("services:\n  hub:\n", text)
        off = compose.render_local(self.values(SECRETARY="off", SESSIONS_ROOTS=str(self.home)))
        self.assertTrue(off.rstrip().endswith("hub: {}"))
        self.assertTrue(compose.collides("/"))
        self.assertTrue(compose.collides("/data"))
        self.assertTrue(compose.collides("relative/path"))
        self.assertTrue(compose.collides("/a:b"))
        self.assertEqual(compose.collides(str(self.home / "Projects")), "")

    def test_dollar_is_escaped(self):
        odd = self.home / "a$b"
        odd.mkdir()
        self.assertIn('"' + str(self.home) + '/a$$b"', compose.render_local(self.values(SESSIONS_ROOTS=str(odd))))

    def test_compose_file_line(self):
        self.assertEqual(compose.compose_file_line({"DESK_NETWORK": "host-access"}),
                         "COMPOSE_FILE=compose.yaml:deploy/net-host-access.yaml:compose.local.yaml")

    @unittest.skipUnless(shutil.which("docker"), "docker CLI not installed")
    def test_docker_compose_reads_it(self):
        """`docker compose config` (no daemon needed) accepts the generated file with a spaced path."""
        for name in ("compose.yaml", "deploy"):
            src = REAL_REPO / name
            if not src.exists():
                self.skipTest(f"{name} is not in this clone yet")
            if src.is_dir():
                shutil.copytree(str(src), str(self.repo / name))
            else:
                shutil.copy(str(src), str(self.repo / name))
        spaced = self.home / "My Projects"
        spaced.mkdir()
        (self.repo / "desk").mkdir()
        (self.repo / "hub").mkdir()
        self.patch(compose, "_docker_path", lambda: shutil.which("docker"))
        settings.save({"SESSIONS_ROOTS": str(spaced), "DESK_NETWORK": "isolated", "COMPOSE_PROJECT_NAME": "dmt-a1-test"})
        compose.prepare(settings.load())
        code, out, err = compose.capture(["config", "--format", "json"], timeout=60, values=settings.load())
        self.assertEqual(code, 0, err[-800:])
        cfg = json.loads(out)
        binds = [v for v in cfg["services"]["hub"]["volumes"] if v.get("type") == "bind"]
        sources = {v["source"]: v for v in binds}
        self.assertIn(str(spaced), sources)
        self.assertTrue(sources[str(spaced)].get("read_only"))
        self.assertEqual(sources[str(spaced)]["target"], str(spaced))
        self.assertIn(str(self.claude_dir / "projects"), sources)
        self.assertEqual(cfg["name"], "dmt-a1-test")


class Prepare(CoreCase):
    def test_folders_and_secret_files(self):
        settings.save({"claude-token": FAKE_TOKEN})
        events = []
        ok = compose.prepare(settings.load(), events.append)
        self.assertTrue(ok, events)
        data = settings.data_dir()
        for sub in ("", "hub", "exchange", "secrets", "bin"):
            self.assertEqual(mode(data / sub if sub else data), 0o700, sub)
        for name in settings.SECRET_NAMES:
            self.assertEqual(mode(data / "secrets" / name), 0o600, name)
        self.assertEqual((data / "secrets" / "notify-url").read_text(), "")
        self.assertTrue(all(e["phase"] == "Prepare folders" for e in events))
        self.assertNotIn(FAKE_TOKEN, json.dumps(events))
        self.assertTrue(compose.in_step(settings.load()))

    def test_missing_pointed_webhook_file_fails(self):
        settings.save({})
        v = settings.load()
        v["NOTIFY_URL_FILE"] = str(self.home / "gone.url")
        events = []
        self.assertFalse(compose.prepare(v, events.append))
        self.assertEqual(events[-1]["level"], "error")

    def test_tightens_loose_modes(self):
        settings.save({})
        data = settings.data_dir()
        os.chmod(str(data), 0o755)
        os.chmod(str(data / "secrets" / "hub-token"), 0o644)
        compose.prepare(settings.load())
        self.assertEqual(mode(data), 0o700)
        self.assertEqual(mode(data / "secrets" / "hub-token"), 0o600)


class Running(CoreCase):
    def fake_docker(self, body: str) -> str:
        path = self.tmp / "docker"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        self.patch(compose, "_docker_path", lambda: str(path))
        return str(path)

    def test_streams_lines_from_the_repo_scrubbed(self):
        settings.save({"claude-token": FAKE_TOKEN})
        self.fake_docker(f'pwd\necho "args: $*"\necho "error: leaked {FAKE_TOKEN}"\necho "file $COMPOSE_FILE"\nexit 3\n')
        events = []
        code = compose.run(["build"], events.append, phase="Build images", values=settings.load())
        self.assertEqual(code, 3)
        texts = [e["text"] for e in events]
        self.assertEqual(os.path.realpath(texts[0]), os.path.realpath(str(self.repo)))
        self.assertEqual(texts[1], "args: compose --ansi never build")
        self.assertNotIn(FAKE_TOKEN, "\n".join(texts))
        self.assertEqual(events[2]["level"], "error")
        self.assertEqual(texts[3], "file compose.yaml:deploy/net-host.yaml:compose.local.yaml")
        self.assertTrue(all(e["phase"] == "Build images" for e in events))

    def test_env_file_wins_over_the_shell(self):
        settings.save({"TZ": "Europe/Paris"})
        os.environ["TZ"] = "America/New_York"
        os.environ["COMPOSE_FILE"] = "something-else.yaml"
        env = compose.compose_env()
        self.assertEqual(env["TZ"], "Europe/Paris")
        self.assertTrue(env["COMPOSE_FILE"].startswith("compose.yaml:deploy/net-"))
        os.environ.pop("COMPOSE_FILE")

    def test_signin_url(self):
        settings.save({"HUB_PORT": "7810"})
        url = compose.signin_url(settings.load())
        self.assertTrue(url.startswith("http://127.0.0.1:7810/login?t="))
        self.assertEqual(url.split("t=", 1)[1], settings.read_secret("hub-token"))


if __name__ == "__main__":
    unittest.main()
