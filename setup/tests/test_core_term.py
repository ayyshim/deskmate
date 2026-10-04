"""The terminal wizard driven through stdin, the non-interactive setup (--defaults, --answers), and the CLI."""

from __future__ import annotations

import io
import json
import os
import types
import unittest

from test_core_support import FAKE_TOKEN, FAKE_WEBHOOK, CoreCase

from deskmate_setup import compose, doctor, settings, steps, term


class TermBase(CoreCase):
    def setUp(self):
        super().setUp()
        self.applied = []

        def fake_apply(values, emit, start=None, until=None):
            self.applied.append({"values": dict(values), "start": start, "until": until})
            emit({"phase": "Save settings", "level": "ok", "text": "Saved"})
            return {"ok": True, "signin_url": "http://127.0.0.1:7800/login?t=fake", "failed_phase": None, "hint": ""}

        self.patch(steps, "apply", fake_apply)
        self.patch(steps, "check_token", lambda tok: {"ok": True, "message": "**Works.** 5-hour window 34% used.", "data": {}})

    def wizard(self, lines, **kw):
        out = io.StringIO()
        code = term.run(inp=io.StringIO("".join(x + "\n" for x in lines)), out=out, **kw)
        return code, out.getvalue()


class Interactive(TermBase):
    def test_walks_every_step_and_installs(self):
        projects = self.projects()
        self.add_session(str(projects / "app"))
        lines = ["Alex Doe", "",           # you: name, time zone
                 "3", "", "", "",          # desk: monitors, size, network, advanced? no
                 "", FAKE_TOKEN, "",       # secretary: on, token, advanced? no
                 ""]                       # sessions: accept the ticked folders
        code, out = self.wizard(lines + [""] * 30)
        self.assertEqual(code, 0, out)
        for n, title in enumerate(["Check", "You", "Desk", "Secretary", "Sessions", "Notes & docs", "Notifications",
                                   "Working habits", "Connect Claude Code", "Review & install"], 1):
            self.assertIn(f"{n}/10 {title}", out)
        v = self.applied[0]["values"]
        self.assertEqual(v["DESKMATE_OWNER"], "Alex Doe")
        self.assertEqual(v["DESK_MONITORS"], "3")
        self.assertEqual(v["TZ"], "Europe/London")
        self.assertEqual(v["claude-token"], FAKE_TOKEN)
        self.assertEqual(v["SESSIONS_ROOTS"], str(projects))
        self.assertEqual(v["HABITS"], "off")
        self.assertNotIn(FAKE_TOKEN, out, "the token is never echoed")
        self.assertIn("sk-ant-oat…Qx7A", out)
        self.assertIn("Works. 5-hour window 34% used.", out)
        self.assertIn("http://127.0.0.1:7800/login?t=fake", out)
        self.assertIn("Skipped: no Claude Code", out)

    def test_bad_answer_is_asked_again(self):
        lines = ["", "Not/AZone", "Asia/Tokyo"]
        code, out = self.wizard(lines + [""] * 40)
        self.assertEqual(code, 0, out)
        self.assertIn("That isn't in the list", out)
        self.assertEqual(self.applied[0]["values"]["TZ"], "Asia/Tokyo")

    def test_choices_toggles_and_paths(self):
        projects = self.projects()
        self.add_session(str(projects / "app"))
        extra = self.home / "work"
        extra.mkdir()
        lines = ["", "",                     # you
                 "", "", "2", "3", "",       # desk: the disabled choice 2 is refused, then 3 (internet only)
                 "n", ""]                    # secretary off; advanced? no
        code, out = self.wizard(lines + [""] * 40)
        self.assertIn("can't be used here", out)
        v = self.applied[0]["values"]
        self.assertEqual(v["DESK_NETWORK"], "isolated")
        self.assertEqual(v["SECRETARY"], "off")
        self.assertIn("Skipped: the secretary is off", out)

    def test_folder_checklist(self):
        projects = self.projects()
        self.add_session(str(projects / "app"))
        work = self.home / "work"
        work.mkdir()
        lines = ["", "", "", "", "", "", "", "", "",   # you, desk, secretary (on, no token, no advanced)
                 "1", str(work), ""]                    # sessions: untick ~/Projects, add ~/work, accept
        code, out = self.wizard(lines + [""] * 40)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.applied[0]["values"]["SESSIONS_ROOTS"], str(work))
        self.assertIn("[x]  1 ~/Projects", out)

    def test_ctrl_c_before_install_changes_nothing(self):
        code, out = self.wizard(["Alex"])
        self.assertEqual(code, 130)
        self.assertIn("Nothing changed.", out)
        self.assertEqual(self.applied, [])
        self.assertFalse((self.repo / ".env").exists())

    def test_saying_no_to_install(self):
        out = io.StringIO()
        code = term.run(inp=Scripted(out, {"Install?": "n"}), out=out)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("Nothing changed.", out.getvalue())
        self.assertEqual(self.applied, [])

    def test_edit_mode_menu(self):
        settings.save({"DESK_MONITORS": "2"})
        lines = ["2", "4", "", "", "", "", "y"]   # section Desk, monitors 4, size, network, advanced no; done; apply
        code, out = self.wizard(lines + [""] * 10)
        self.assertEqual(code, 0, out)
        self.assertIn("Settings", out)
        self.assertIn("Monitors: 2 → 4 (restarts the desk)", out)
        self.assertEqual(self.applied[0]["values"]["DESK_MONITORS"], "4")

    def test_failed_phase_offers_retry(self):
        calls = []

        def flaky(values, emit, start=None, until=None):
            calls.append(start)
            if len(calls) == 1:
                emit({"phase": "Build images", "level": "error", "text": "network timeout"})
                return {"ok": False, "signin_url": "", "failed_phase": "Build images", "hint": "Check your internet."}
            return {"ok": True, "signin_url": "http://127.0.0.1:7800/login?t=fake", "failed_phase": None, "hint": ""}

        self.patch(steps, "apply", flaky)
        out = io.StringIO()
        code = term.run(inp=Scripted(out, {"r to retry": "r"}), out=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(calls, [None, "Build images"])
        self.assertIn("Build images: Check your internet.", out.getvalue())


class Scripted(io.StringIO):
    """Enter for every prompt, except the given answers, each given once when its marker is in the prompt."""

    def __init__(self, out, answers: dict, limit: int = 300):
        super().__init__()
        self.out, self.answers, self.seen, self.n, self.limit = out, dict(answers), 0, 0, limit

    def readline(self, *a):
        self.n += 1
        if self.n > self.limit:
            return ""
        text = self.out.getvalue()
        prompt = text[self.seen:].rsplit("\n", 1)[-1]
        self.seen = len(text)
        for marker, answer in list(self.answers.items()):
            if marker in prompt:
                del self.answers[marker]
                return answer + "\n"
        return "\n"


class NonInteractive(TermBase):
    def test_parse_answers(self):
        answers, errors = term.parse_answers("# team\nDESK_MONITORS=1\nTZ='Asia/Tokyo'\n\nBOGUS=1\nnot a line\n"
                                             "CLAUDE_CODE_OAUTH_TOKEN=x\nclaude-token=y\nCLAUDE_CODE_OAUTH_TOKEN_FILE=/x\n")
        self.assertEqual(answers, {"DESK_MONITORS": "1", "TZ": "Asia/Tokyo", "CLAUDE_CODE_OAUTH_TOKEN_FILE": "/x"})
        self.assertEqual(len(errors), 4)
        self.assertTrue(any("unknown key BOGUS" in e for e in errors))
        self.assertTrue(any("secret" in e for e in errors))
        self.assertTrue(any("not KEY=value" in e for e in errors))

    def test_unknown_key_exits_2(self):
        f = self.tmp / "answers.env"
        f.write_text("DESK_MONITORS=1\nNOPE=2\n")
        out = io.StringIO()
        self.assertEqual(term.noninteractive(answers_file=str(f), yes=True, out=out, env={}), 2)
        self.assertIn("unknown key NOPE", out.getvalue())
        self.assertEqual(self.applied, [])

    def test_secret_in_answers_exits_2(self):
        f = self.tmp / "answers.env"
        f.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_TOKEN}\n")
        out = io.StringIO()
        self.assertEqual(term.noninteractive(answers_file=str(f), yes=True, out=out, env={}), 2)
        self.assertNotIn(FAKE_TOKEN, out.getvalue())

    def test_secrets_from_env_and_files(self):
        tok = self.tmp / "token"
        tok.write_text(FAKE_TOKEN + "\n")
        hook = self.tmp / "hook.url"
        hook.write_text(FAKE_WEBHOOK)
        f = self.tmp / "answers.env"
        f.write_text(f"DESK_MONITORS=1\nCLAUDE_CODE_OAUTH_TOKEN_FILE={tok}\nNOTIFY_KIND=discord\n")
        out = io.StringIO()
        code = term.noninteractive(answers_file=str(f), yes=True, out=out, env={"NOTIFY_WEBHOOK_FILE": str(hook)})
        self.assertEqual(code, 0, out.getvalue())
        v = self.applied[0]["values"]
        self.assertEqual(v["claude-token"], FAKE_TOKEN)
        self.assertEqual(v["NOTIFY_URL_FILE"], str(hook), "a webhook file is pointed at, never copied")
        self.assertNotIn("notify-url", v)
        self.assertEqual(v["DESK_MONITORS"], "1")
        self.assertNotIn(FAKE_TOKEN, out.getvalue())
        out = io.StringIO()
        self.applied.clear()
        code = term.noninteractive(defaults=True, yes=True, out=out,
                                   env={"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN, "NOTIFY_WEBHOOK_URL": FAKE_WEBHOOK})
        self.assertEqual(code, 0, out.getvalue())
        v = self.applied[0]["values"]
        self.assertEqual(v["notify-url"], FAKE_WEBHOOK)
        self.assertEqual(v["NOTIFY_KIND"], "discord")

    def test_bad_answer_exits_2_and_blocking_exits_3(self):
        f = self.tmp / "answers.env"
        f.write_text("DESK_MONITORS=9\n")
        out = io.StringIO()
        self.assertEqual(term.noninteractive(answers_file=str(f), yes=True, out=out, env={}), 2)
        self.assertIn("DESK_MONITORS", out.getvalue())
        self.docker = dict(self.docker, running=False, label="Docker isn't running", fix="Start Docker")
        out = io.StringIO()
        self.assertEqual(term.noninteractive(defaults=True, yes=True, out=out, env={}), 3)
        self.assertIn("Docker isn't running", out.getvalue())
        self.assertEqual(self.applied, [])

    def test_confirmation_without_yes(self):
        out = io.StringIO()
        code = term.noninteractive(defaults=True, out=out, env={}, inp=io.StringIO(""))
        self.assertEqual(code, 1)
        self.assertIn("--yes", out.getvalue())
        out = io.StringIO()
        self.assertEqual(term.noninteractive(defaults=True, out=out, env={}, inp=io.StringIO("y\n")), 0)

    def test_update_flag_and_dry_run(self):
        out = io.StringIO()
        term.noninteractive(defaults=True, yes=True, dry_run=True, update=True, out=out, env={})
        self.assertEqual(self.applied[0]["until"], "Prepare folders")
        self.assertEqual(self.applied[0]["values"]["HABITS_UPDATE"], "on")


class RealDryRun(CoreCase):
    """--defaults --dry-run for real (no mocks of apply): writes .env and compose.local.yaml, runs no docker."""

    def test_writes_settings_only(self):
        self.add_session(str(self.projects() / "app"))
        self.patch(compose, "run", lambda *a, **k: self.fail("docker compose must not run in a dry run"))
        code, out, err = self.run_cli(["setup", "--defaults", "--yes", "--dry-run"])
        self.assertEqual(code, 0, out + err)
        self.assertTrue((self.repo / ".env").exists())
        self.assertTrue((self.repo / "compose.local.yaml").exists())
        env = settings.read_env()
        self.assertEqual(env["DESKMATE_OWNER"], "Alex")
        self.assertEqual(env["SESSIONS_ROOTS"], str(self.home / "Projects"))
        self.assertEqual(env["HABITS"], "off")
        self.assertIn("nothing was built or started", out)


class Cli(CoreCase):
    def test_usage_errors(self):
        self.assertEqual(self.run_cli([])[0], 2)
        self.assertEqual(self.run_cli(["nope"])[0], 2)
        self.assertEqual(self.run_cli(["setup", "--terminal", "--web"])[0], 2)
        self.assertEqual(self.run_cli(["setup", "--dry-run", "--terminal"])[0], 2)
        code, out, _ = self.run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("Deskmate", out)

    def test_commands_need_setup(self):
        for cmd in (["up"], ["down"], ["open"], ["status"], ["logs"], ["connect"]):
            code, _, err = self.run_cli(cmd)
            self.assertEqual(code, 1, cmd)
            self.assertIn("./deskmate setup", err)

    def test_config(self):
        settings.save({})
        code, out, _ = self.run_cli(["config", "set", "DESK_MONITORS", "4"])
        self.assertEqual(code, 0, out)
        self.assertEqual(settings.read_env()["DESK_MONITORS"], "4")
        code, _, err = self.run_cli(["config", "set", "DESK_MONITORS", "9"])
        self.assertEqual(code, 2)
        self.assertIn("1 to 4", err)
        self.assertEqual(self.run_cli(["config", "set", "claude-token", FAKE_TOKEN])[0], 2)
        self.assertEqual(self.run_cli(["config", "set", "NOPE", "1"])[0], 2)
        self.assertEqual(self.run_cli(["config", "get", "DESK_MONITORS"])[1].strip(), "4")
        code, out, _ = self.run_cli(["config", "set-secret", "claude-token"], stdin=FAKE_TOKEN + "\n")
        self.assertEqual(code, 0, out)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertEqual(settings.read_secret("claude-token"), FAKE_TOKEN)
        self.assertEqual(self.run_cli(["config", "set-secret", "claude-token"], stdin="sk-ant-api03-nope\n")[0], 2)
        code, out, _ = self.run_cli(["config", "list"])
        self.assertIn("claude-token=sk-ant-oat…Qx7A", out)
        self.assertIn("DESK_MONITORS=4", out)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn(settings.read_secret("hub-token"), out)
        old = settings.read_secret("hub-token")
        self.assertEqual(self.run_cli(["config", "set-secret", "hub-token"], stdin="\n")[0], 0)
        self.assertNotEqual(settings.read_secret("hub-token"), old)

    def test_open_prints_the_link(self):
        settings.save({"HUB_PORT": "7810"})
        code, out, _ = self.run_cli(["open", "--no-open"])
        self.assertEqual(code, 0)
        self.assertIn("http://127.0.0.1:7810/login?t=" + settings.read_secret("hub-token"), out)
        self.assertEqual(self.opened, [])

    def test_doctor_exit_codes(self):
        code, out, _ = self.run_cli(["doctor", "--json"])
        data = json.loads(out)
        self.assertIn("Deskmate isn't set up yet", [c["label"] for c in data["checks"]])
        self.assertEqual(code, 0)
        self.docker = dict(self.docker, running=False, label="Docker isn't running", fix="Start it")
        self.assertEqual(self.run_cli(["doctor"])[0], 1)

    def test_connect_and_habits_use_the_other_parts(self):
        settings.save({})
        got = {}
        self.claude = dict(self.claude, installed=True, path="/nonexistent/claude", version="2.1.278")
        self.fake_module("claude_connect", types.SimpleNamespace(
            connect=lambda v, emit: got.update(connect=v) or {"ok": True},
            disconnect=lambda emit, remove_plugins=True: got.update(disconnect=remove_plugins) or {"ok": True}))
        self.fake_module("habits", types.SimpleNamespace(
            apply=lambda v, emit: got.update(habits=v) or {"ok": True},
            remove=lambda emit, folder=None: got.update(removed=folder) or {"ok": True},
            status=lambda: {"installed": True, "problems": []}))
        self.assertEqual(self.run_cli(["connect", "--remove", "mcp:playwright"])[0], 0)
        self.assertEqual(got["connect"]["CONNECT_REMOVE"], "mcp:playwright")
        self.assertFalse(any(isinstance(v, dict) for v in got["connect"].values()), "no secret states passed on")
        self.assertEqual(self.run_cli(["disconnect", "--keep-plugins"])[0], 0)
        self.assertFalse(got["disconnect"])
        folder = self.projects()
        self.assertEqual(self.run_cli(["habits", "apply", "--folder", str(folder)])[0], 0)
        self.assertEqual(got["habits"]["HABITS_FOLDER"], str(folder))
        self.assertEqual(settings.read_env()["HABITS"], "on")
        self.assertEqual(self.run_cli(["habits", "remove", "--folder", str(folder)])[0], 0)
        self.assertEqual(got["removed"], str(folder))
        self.assertEqual(self.run_cli(["habits", "status"])[0], 0)

    def test_update_pulls_then_reruns_setup(self):
        from deskmate_setup import cli, util

        settings.save({})
        (self.repo / ".git").mkdir()
        ran, called = [], []
        self.patch(util, "run", lambda cmd, timeout=30.0, env=None, cwd=None, input_text=None:
                   ran.append(cmd) or (0, "Already up to date.", ""))
        self.patch(cli.subprocess, "call", lambda cmd, cwd=None: called.append(cmd) or 0)
        code, out, _ = self.run_cli(["update", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertEqual(ran[0][-2:], ["pull", "--ff-only"])
        self.assertEqual(called[0][1:], [str(self.repo / "deskmate"), "setup", "--defaults", "--update", "--yes"])
        self.patch(util, "run", lambda *a, **k: (1, "", "error: local changes"))
        self.assertEqual(self.run_cli(["update"])[0], 1)

    def test_uninstall(self):
        settings.save({"claude-token": FAKE_TOKEN})
        compose.prepare(settings.load())
        data = settings.data_dir()
        calls = []
        self.patch(compose, "run", lambda args, emit=None, phase="", values=None: calls.append(args) or 0)
        self.fake_module("claude_connect", types.SimpleNamespace(disconnect=lambda emit, remove_plugins=True: {"ok": True}))
        self.fake_module("habits", types.SimpleNamespace(remove=lambda emit, folder=None: {"ok": True}))
        code, out, _ = self.run_cli(["uninstall"], stdin="n\n")
        self.assertEqual(code, 1)
        self.assertTrue(data.exists())
        code, out, _ = self.run_cli(["uninstall", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertEqual(calls, [["down", "--rmi", "all", "--remove-orphans", "-v"]])
        self.assertFalse(data.exists())
        self.assertFalse((self.repo / "compose.local.yaml").exists())
        self.assertTrue((self.repo / ".env").exists(), ".env is kept without --purge")

    def test_uninstall_never_deletes_a_foreign_folder(self):
        settings.save({})
        data = settings.data_dir()
        (data / "my-own-file.txt").write_text("keep me")
        self.patch(compose, "run", lambda *a, **k: 0)
        self.fake_module("claude_connect", types.SimpleNamespace(disconnect=lambda emit, remove_plugins=True: {"ok": True}))
        self.fake_module("habits", types.SimpleNamespace(remove=lambda emit, folder=None: {"ok": True}))
        self.run_cli(["uninstall", "--yes", "--purge"])
        self.assertTrue((data / "my-own-file.txt").exists())
        self.assertFalse((self.repo / ".env").exists())


if __name__ == "__main__":
    unittest.main()
