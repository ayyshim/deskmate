"""steps: the wizard model (install and edit mode), validate() errors, actions and apply() with Docker,
Claude Code and the habits part replaced."""

from __future__ import annotations

import json
import types
import unittest

from test_core_support import CLAUDE_OK, FAKE_TOKEN, FAKE_WEBHOOK, CoreCase

from deskmate_setup import compose, doctor, settings, steps


class Model(CoreCase):
    def test_install_mode(self):
        self.add_session(str(self.projects() / "app"))
        m = steps.model()
        self.assertEqual(m["version"], 1)
        self.assertEqual(m["mode"], "install")
        self.assertEqual([s["id"] for s in m["steps"]], steps.STEP_IDS)
        for s in m["steps"]:
            for k in ("id", "title", "summary", "status", "intro", "fields", "checks", "info"):
                self.assertIn(k, s, s["id"])
            for f in s["fields"]:
                for k in ("key", "label", "type", "value", "default", "help", "advanced", "error", "readonly"):
                    self.assertIn(k, f, f.get("key"))
        by = {s["id"]: s for s in m["steps"]}
        self.assertEqual(by["you"]["summary"], "Alex · Europe/London")
        self.assertEqual(m["values"]["SESSIONS_ROOTS"], str(self.home / "Projects"))
        folders = by["sessions"]["info"]["folders"]
        self.assertTrue(any(r["path"] == str(self.home / "Projects") and r["selected"] for r in folders))
        self.assertTrue(by["connect"].get("skip"), "no Claude Code: the connect step is skipped")
        self.assertEqual(m["values"]["claude-token"], {"set": False, "masked": ""})
        self.assertFalse((self.repo / ".env").exists(), "model() writes nothing")

    def test_secrets_never_in_the_model(self):
        m = steps.model({"claude-token": FAKE_TOKEN, "notify-url": FAKE_WEBHOOK, "NOTIFY_KIND": "discord"})
        blob = json.dumps(m)
        self.assertNotIn(FAKE_TOKEN, blob)
        self.assertNotIn("FAKE-webhook-token", blob)
        self.assertEqual(m["values"]["claude-token"]["masked"], "sk-ant-oat…Qx7A")

    def test_edit_mode_lists_changes(self):
        settings.save({"DESK_MONITORS": "2", "claude-token": FAKE_TOKEN})
        m = steps.model({"DESK_MONITORS": "3", "claude-token": "sk-ant-oat01-" + "B" * 40})
        self.assertEqual(m["mode"], "edit")
        review = {s["id"]: s for s in m["steps"]}["review"]
        self.assertEqual(review["title"], "Review & apply")
        changes = {c["key"]: c for c in review["info"]["changes"]}
        self.assertEqual((changes["DESK_MONITORS"]["old"], changes["DESK_MONITORS"]["new"]), ("2", "3"))
        self.assertEqual(changes["DESK_MONITORS"]["tag"], "restarts the desk")
        self.assertEqual(changes["claude-token"]["old"], "sk-ant-oat…Qx7A")
        self.assertNotIn("B" * 20, json.dumps(m))

    def test_habits_step_hub_options(self):
        m = steps.model({"HABITS": "on"})
        habits = {s["id"]: s for s in m["steps"]}["habits"]
        keys = [f["key"] for f in habits["fields"]]
        self.assertIn("HABITS_HUB_PATH", keys)
        hub = next(f for f in habits["fields"] if f["key"] == "HABITS_HUB")
        self.assertEqual([c["value"] for c in hub["choices"]], ["auto", "existing", "none"])
        self.assertEqual(hub["value"], "auto")
        check = next(f for f in habits["fields"] if f["key"] == "HABITS_CHECK")
        self.assertEqual(check["value"], "remind")

    def test_habits_values_per_folder(self):
        a, b = str(self.home / "a"), str(self.home / "b")
        v = steps.habits_values({"HABITS_FOLDERS": f"{a}:{b}", "HABITS_HUB": "existing", "HABITS_HUB_PATH": "/srv/hub",
                                 "HABITS_AGENTS": "https://git.example.com/agents.git", "claude-token": FAKE_TOKEN})
        self.assertNotIn("claude-token", v)
        self.assertEqual(v["HABITS_FOLDER_OPTIONS"][a], {"hub": "existing", "hub_path": "/srv/hub", "agents": "git",
                                                         "agents_source": "https://git.example.com/agents.git"})
        auto = steps.habits_values({"HABITS_FOLDERS": a, "HABITS_HUB": "auto"})
        self.assertEqual(auto["HABITS_FOLDER_OPTIONS"], {})


class Validate(CoreCase):
    def errors(self, step, **values):
        return steps.validate(step, values)["errors"]

    def test_you(self):
        self.assertIn("DESKMATE_OWNER", self.errors("you", DESKMATE_OWNER=""))
        self.assertIn("DESKMATE_OWNER", self.errors("you", DESKMATE_OWNER="<b>"))
        self.assertIn("TZ", self.errors("you", TZ="Mars/Olympus"))
        self.assertEqual(self.errors("you", DESKMATE_OWNER="Alex", TZ="Asia/Tokyo"), {})

    def test_desk(self):
        e = self.errors("desk", DESK_MONITORS="5", DESK_MONITOR_SIZE="999x9", HUB_PORT="80", DESK_DISPLAY="0",
                        COMPOSE_PROJECT_NAME="Bad Name", DESKMATE_DATA_DIR="/mnt/c/Users/alex/dm")
        for k in ("DESK_MONITORS", "DESK_MONITOR_SIZE", "HUB_PORT", "DESK_DISPLAY", "COMPOSE_PROJECT_NAME", "DESKMATE_DATA_DIR"):
            self.assertIn(k, e)
        e = self.errors("desk", HUB_PORT="7810", CDP_PORT="7810")
        self.assertEqual(e.get("CDP_PORT"), "The two ports must differ.")
        e = self.errors("desk", DESK_NETWORK="host-access")
        self.assertIn("DESK_NETWORK", e, "host-access needs Docker Desktop")
        self.assertIn("spaces", self.errors("desk", DESKMATE_DATA_DIR=str(self.home / "my data"))["DESKMATE_DATA_DIR"])

    def test_secretary(self):
        e = self.errors("secretary", **{"claude-token": "sk-ant-api03-" + "x" * 40})
        self.assertIn("API key", e["claude-token"])
        self.assertIn("claude-token", self.errors("secretary", **{"claude-token": "sk-ant-oat01-short"}))
        self.assertEqual(self.errors("secretary", **{"claude-token": FAKE_TOKEN}), {})
        e = self.errors("secretary", BRIEF_AT="25:00", SECRETARY_PAUSE_AT="2", SECRETARY_DIGEST_MODEL="gpt-4")
        self.assertEqual(set(e), {"BRIEF_AT", "SECRETARY_PAUSE_AT", "SECRETARY_DIGEST_MODEL"})
        w = steps.validate("secretary", {})["warnings"]
        self.assertIn("claude-token", w)

    def test_sessions_and_docs(self):
        self.assertIn("SESSIONS_ROOTS", self.errors("sessions", SESSIONS_ROOTS="/data"))
        self.assertIn("DOCS_DIR", self.errors("docs", DOCS_DIR=str(self.home)))
        self.assertIn("DOCS_DIR", self.errors("docs", DOCS_DIR=str(self.home / "nope")))
        self.assertEqual(self.errors("sessions", SECRETARY="off", SESSIONS_ROOTS="/data"), {})

    def test_notify(self):
        e = self.errors("notify", NOTIFY_KIND="discord", **{"notify-url": "https://example.com/hook"})
        self.assertIn("Discord", e["notify-url"])
        self.assertIn("notify-url", self.errors("notify", NOTIFY_KIND="slack"))
        self.assertEqual(self.errors("notify", NOTIFY_KIND="discord", **{"notify-url": FAKE_WEBHOOK}), {})
        self.assertIn("NOTIFY_URL_FILE", self.errors("notify", NOTIFY_URL_FILE=str(self.home / "nope.url")))

    def test_habits(self):
        self.patch(steps, "_habits_detect", lambda v: {"folders": []})
        self.assertIn("HABITS_FOLDERS", self.errors("habits", HABITS="on", HABITS_FOLDERS=""))
        a = self.home / "a"
        (a / "inner").mkdir(parents=True)
        e = self.errors("habits", HABITS="on", HABITS_FOLDERS=f"{a}:{a / 'inner'}")
        self.assertIn("inside another", e["HABITS_FOLDERS"])
        e = self.errors("habits", HABITS="on", HABITS_FOLDERS=str(a), HABITS_HUB="existing", HABITS_HUB_PATH="")
        self.assertIn("HABITS_HUB_PATH", e)
        e = self.errors("habits", HABITS="on", HABITS_FOLDERS=str(a), HABITS_HUB="clone")
        self.assertIn("HABITS_HUB", e, "no clone option (D16)")
        self.assertIn("HABITS_CHECK", self.errors("habits", HABITS="on", HABITS_FOLDERS=str(a), HABITS_CHECK="always"))
        self.assertEqual(self.errors("habits", HABITS="on", HABITS_FOLDERS=str(a)), {})
        self.assertEqual(self.errors("habits", HABITS="off", HABITS_FOLDERS=""), {})

    def test_review_collects_and_unknown_step(self):
        res = steps.validate("review", {"DESKMATE_OWNER": "", "DESK_MONITORS": "9"})
        self.assertIn("DESKMATE_OWNER", res["errors"])
        self.assertIn("DESK_MONITORS", res["errors"])
        self.assertEqual(res["step"]["id"], "review")
        self.assertIn("step", steps.validate("nope", {})["errors"])


class Actions(CoreCase):
    def test_check_token_offline_and_shapes(self):
        self.assertFalse(steps.action("check_token", {})["ok"])
        calls = []

        def fake_post(url, payload, headers=None, timeout=10.0, raw=None):
            calls.append((url, headers))
            return 200, {"anthropic-ratelimit-unified-5h-utilization": "0.34"}, b"{}"

        self.patch(steps, "_post_json", fake_post)
        res = steps.action("check_token", {"claude-token": FAKE_TOKEN})
        self.assertTrue(res["ok"])
        self.assertIn("34%", res["message"])
        self.assertNotIn(FAKE_TOKEN, res["message"])
        self.assertEqual(calls[0][0], "https://api.anthropic.com/v1/messages")

    def test_test_notify(self):
        sent = []
        self.patch(steps, "_post_json", lambda url, payload, headers=None, timeout=10.0, raw=None:
                   (sent.append(payload) or 204, {}, b""))
        res = steps.action("test_notify", {"NOTIFY_KIND": "discord", "notify-url": FAKE_WEBHOOK, "DESKMATE_OWNER": "Alex"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(sent[0]["allowed_mentions"], {"parse": []})
        self.assertIn("Alex", sent[0]["content"])

    def test_previews_and_recheck(self):
        fake = types.SimpleNamespace(preview=lambda v: {"changes": [{"what": "Add the MCP server", "where": "x", "detail": ""}],
                                                        "conflicts": []},
                                     conflicts=lambda v: [])
        self.fake_module("claude_connect", fake)
        res = steps.action("preview_connect", {})
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["changes"][0]["what"], "Add the MCP server")
        self.assertEqual(steps.action("preview_habits", {"HABITS": "off"})["data"], {"changes": []})
        res = steps.action("recheck", {})
        self.assertEqual(res["data"]["step"]["id"], "check")
        self.assertFalse(steps.action("nope", {})["ok"])


class Apply(CoreCase):
    claude = CLAUDE_OK

    def setUp(self):
        super().setUp()
        self.compose_calls = []

        def fake_run(args, emit=None, phase="Docker", values=None):
            self.compose_calls.append((list(args), phase, dict(values or {})))
            if emit:
                emit({"phase": phase, "level": "info", "text": f"docker compose {' '.join(args)} with {FAKE_TOKEN}"})
            return self.codes.pop(0) if self.codes else 0

        self.codes = []
        self.patch(compose, "run", fake_run)
        self.patch(compose, "wait_for_hub", lambda port, timeout=90.0, emit=None, phase="": True)
        self.patch(doctor, "run", lambda values=None: [{"id": "hub", "label": "Deskmate answers", "status": "ok",
                                                         "detail": "", "fix": ""}])
        self.connected = []
        self.habits_applied = []
        self.fake_module("claude_connect", types.SimpleNamespace(
            connect=lambda v, emit: (self.connected.append(dict(v)), emit({"level": "ok", "text": "Connected"}))
            and {"ok": True},
            conflicts=lambda v: [], preview=lambda v: {"changes": []}))
        self.fake_module("habits", types.SimpleNamespace(
            apply=lambda v, emit: (self.habits_applied.append(dict(v)), emit({"level": "ok", "text": "Rules written"}),
                                   {"ok": True})[2],
            detect=lambda v: {"folders": []}, preview=lambda v: {"changes": []}))

    def run_apply(self, values, **kw):
        events = []
        res = steps.apply(values, events.append, **kw)
        return res, events

    def test_install_runs_every_phase(self):
        folder = self.projects()
        res, events = self.run_apply({"DESKMATE_OWNER": "Alex", "claude-token": FAKE_TOKEN, "HABITS": "on",
                                      "HABITS_FOLDERS": str(folder)})
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["signin_url"].startswith("http://127.0.0.1:"))
        phases = []
        for e in events:
            if e["phase"] not in phases:
                phases.append(e["phase"])
        self.assertEqual(phases, steps.PHASES)
        self.assertEqual([e["n"] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(all("ts" in e for e in events))
        blob = json.dumps(events)
        self.assertNotIn(FAKE_TOKEN, blob)
        self.assertEqual([c[0] for c in self.compose_calls], [["build"], ["up", "-d", "--remove-orphans"]])
        for _, _, values in self.compose_calls:
            self.assertNotIn("claude-token", values)
        self.assertEqual(len(self.connected), 1)
        self.assertNotIn("claude-token", self.connected[0])
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.connected))
        self.assertEqual(self.habits_applied[0]["HABITS_FOLDERS"], str(folder))
        self.assertEqual(settings.read_secret("claude-token"), FAKE_TOKEN)
        self.assertTrue((self.repo / "compose.local.yaml").exists())

    def test_build_failure_then_retry(self):
        self.codes = [1]
        res, events = self.run_apply({})
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_phase"], "Build images")
        self.assertTrue(res["hint"])
        self.assertNotIn("Start Deskmate", {e["phase"] for e in events})
        res, events = self.run_apply({}, start="Build images")
        self.assertTrue(res["ok"], res)
        self.assertEqual(events[0]["phase"], "Build images")
        self.assertNotIn("Working habits", {e["phase"] for e in events}, "habits off and never on: no phase")

    def test_bad_answers_change_nothing(self):
        res, events = self.run_apply({"DESK_MONITORS": "9"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_phase"], "Save settings")
        self.assertFalse((self.repo / ".env").exists())
        self.assertEqual(self.compose_calls, [])

    def test_until_prepare(self):
        res, events = self.run_apply({}, until="Prepare folders")
        self.assertTrue(res["ok"])
        self.assertEqual(res["stopped_after"], "Prepare folders")
        self.assertEqual(self.compose_calls, [])
        self.assertTrue((self.repo / ".env").exists())

    def test_connect_failure_names_the_phase(self):
        self.fake_module("claude_connect", types.SimpleNamespace(
            connect=lambda v, emit: {"ok": False, "error": "claude mcp add failed"}, conflicts=lambda v: []))
        res, _ = self.run_apply({})
        self.assertEqual(res["failed_phase"], "Connect Claude Code")
        self.assertIn("claude mcp add failed", res["hint"])

    def test_crash_in_a_phase_is_that_phase_failing(self):
        def boom(args, emit=None, phase="", values=None):
            raise RuntimeError("no docker here")

        self.patch(compose, "run", boom)
        res, events = self.run_apply({})
        self.assertEqual(res["failed_phase"], "Build images")
        self.assertIn("no docker here", events[-1]["text"])


if __name__ == "__main__":
    unittest.main()
