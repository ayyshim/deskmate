"""Habits pack behind the wizard: the answers as steps.habits_values() passes them (the real keys) reach
detect, preview and apply as meant (HABITS_HUB auto | existing | none, HABITS_AGENTS, HABITS_CHECK)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_habits_support import Sandbox, habits  # noqa: E402

try:
    from deskmate_setup import steps
except Exception:  # noqa: BLE001 - the core role's module; these tests need it
    steps = None

AGENT = "---\nname: {name}\ndescription: Work in the {name} repo. Use for any change there.\n---\n\nYou work on {name}.\n"


@unittest.skipUnless(steps is not None and hasattr(steps, "habits_values"), "steps.habits_values is not there")
class WizardAnswers(Sandbox):
    def answers(self, root: Path, **kw) -> dict:
        v = {"HABITS": "on", "HABITS_FOLDERS": str(root), "HABITS_HUB": "auto", "HABITS_HUB_PATH": "",
             "HABITS_CHECK": "remind", "HABITS_AGENTS": "", "HABITS_ALLOW_NOTIFY": "off", "HABITS_MEMORY_ON": "off",
             "DESKMATE_OWNER": "Alex", "NOTIFY_KIND": "none", "HABITS_PLUGIN": "off"}
        v.update(kw)
        return steps.habits_values(v)

    def workspace(self) -> Path:
        root = self.folder("work")
        self.repo(root / "api")
        return root

    def test_auto_makes_a_new_hub(self):
        root = self.workspace()
        v = self.answers(root)
        det = habits.detect(v)
        self.assertTrue(det["ok"], det)
        self.assertEqual(det["folders"][0]["hub"]["kind"], "new")
        res = habits.apply(v, self.emit)
        self.assertTrue(res["ok"], res)
        self.assertTrue((root / "claude" / "changelog" / "README.md").is_file())
        self.assertIn("`claude/` is the docs hub", self.read(root / "CLAUDE.md"))

    def test_auto_keeps_a_hub_the_folder_already_has(self):
        root = self.workspace()
        hub = root / "docs"
        (hub / "changelog").mkdir(parents=True)
        (hub / "changelog" / "README.md").write_text("# Ours\n")
        (hub / "design").mkdir()
        (hub / "design" / "_template.md").write_text("# Ours\n")
        det = habits.detect(self.answers(root))
        h = det["folders"][0]["hub"]
        self.assertEqual((h["kind"], h["path"]), ("existing", str(hub)))
        self.assertFalse((root / "claude").exists())

    def test_existing_hub_by_path(self):
        root = self.workspace()
        hub = self.folder("elsewhere/hub")
        (hub / "changelog").mkdir()
        (hub / "changelog" / "README.md").write_text("# Ours\n")
        v = self.answers(root, HABITS_HUB="existing", HABITS_HUB_PATH=str(hub))
        prev = habits.preview(v)
        self.assertTrue(prev["ok"], prev)
        self.assertTrue(any("additionalDirectories" in c["detail"] for c in prev["changes"]),
                        "a hub outside the folder needs write access")
        res = habits.apply(v, self.emit)
        self.assertTrue(res["ok"], res)
        self.assertIn(f"`{hub}/` is the docs hub", self.read(root / "CLAUDE.md"))
        self.assertEqual(sorted(p.name for p in hub.iterdir()), ["changelog"], "nothing added to an existing hub")
        habits.remove(self.emit)
        settings = json.loads(self.read(habits.claude_dir() / "settings.json"))
        self.assertNotIn("permissions", settings)

    def test_existing_hub_missing_path_is_an_error(self):
        root = self.workspace()
        prev = habits.preview(self.answers(root, HABITS_HUB="existing", HABITS_HUB_PATH=""))
        self.assertFalse(prev["ok"])
        self.assertTrue(any("hub" in e for e in prev["errors"]))

    def test_no_hub(self):
        root = self.workspace()
        res = habits.apply(self.answers(root, HABITS_HUB="none", HABITS_CHECK="require"), self.emit)
        self.assertTrue(res["ok"], res)
        self.assertFalse((root / "claude").exists())
        cfg = json.loads(self.read(habits.config_path()))
        self.assertEqual((cfg["mode"], cfg["roots"]), ("require", []))

    def test_agents_from_a_folder_wait_for_the_lint(self):
        root = self.workspace()
        src = self.folder("team-agents")
        (src / "api.md").write_text(AGENT.format(name="api"))
        (src / "web.md").write_text(AGENT.format(name="web") + "Logs are on build.lan.\n")
        v = self.answers(root, HABITS_AGENTS=str(src))
        prev = habits.preview(v)
        self.assertEqual([x["kind"] for x in prev["lint"]], ["local host name"])
        res = habits.apply(v, self.emit)
        self.assertFalse((root / ".claude" / "agents" / "api.md").exists(), "nothing is copied before a yes")
        self.assertEqual(len(res["lint"]), 1)
        (src / "web.md").write_text(AGENT.format(name="web"))
        res = habits.apply(v, self.emit)
        self.assertTrue(res["ok"], res)
        self.assertTrue((root / ".claude" / "agents" / "web.md").exists())
        self.assertIn("## Agents", self.read(root / "CLAUDE.md"))

    def test_notify_rule_names_the_tool(self):
        root = self.workspace()
        habits.apply(self.answers(root, NOTIFY_KIND="discord", HABITS_ALLOW_NOTIFY="on"), self.emit)
        text = self.read(root / "CLAUDE.md")
        self.assertIn("`mcp__deskmate__notify`", text)
        settings = json.loads(self.read(habits.claude_dir() / "settings.json"))
        self.assertEqual(settings["permissions"]["allow"], ["mcp__deskmate__notify"])


if __name__ == "__main__":
    unittest.main()
