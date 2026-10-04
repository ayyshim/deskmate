"""The habits pack as shipped: plugin layout, Python 3.9 grammar, examples, and no personal data."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_habits_support import HOOK, REPO, Sandbox, habits  # noqa: E402

PLUGIN = REPO / "plugin" / "habits"
PACK = REPO / "pack"

# CONTRACT §7: nothing personal may ship. Paths are matched case-sensitively, words in any case.
PERSONAL_PATHS = re.compile(r"/home/|/Users/")
PERSONAL_WORDS = re.compile(r"ashim|sageflick|discord\.com/api/webhooks|sk-ant-|merge and push|without asking", re.I)
# Stricter for the files this pack owns: no names from the workspace it was generalised from.
OWN_WORDS = re.compile(r"\bsage\b|amphitheater|dolby|sageweb|edm_react|pvapp|\.lan\b|cred\.env", re.I)


def text_files(*roots: Path):
    for root in roots:
        for p in sorted(root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                try:
                    yield p, p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue


class Layout(unittest.TestCase):
    def test_plugin_manifest(self):
        m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(m["name"], "habits")
        self.assertRegex(m["version"], r"^\d+\.\d+\.\d+$")
        self.assertTrue(m["description"])

    def test_stop_hook_registration(self):
        h = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
        self.assertEqual(sorted(h["hooks"]), ["Stop"], "never SubagentStop")
        (group,) = h["hooks"]["Stop"]
        self.assertNotIn("matcher", group)
        (cmd,) = group["hooks"]
        self.assertEqual(cmd["type"], "command")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/bin/habits-stop", cmd["command"])
        self.assertLessEqual(cmd["timeout"], 30)

    def test_hook_script(self):
        self.assertTrue(os.access(HOOK, os.X_OK))
        self.assertEqual(HOOK.read_text().split("\n", 1)[0], "#!/usr/bin/env python3")

    def test_skills(self):
        names = sorted(p.name for p in (PLUGIN / "skills").iterdir())
        self.assertEqual(names, ["changelog-entry", "design-doc", "wrap-up"])
        for name in names:
            text = (PLUGIN / "skills" / name / "SKILL.md").read_text()
            fm = habits._frontmatter(text)
            self.assertEqual(fm.get("name"), name)
            self.assertTrue(20 < len(fm.get("description", "")) < 1024, name)

    def test_marketplace_lists_habits_when_present(self):
        mp = REPO / "plugin" / ".claude-plugin" / "marketplace.json"
        if not mp.exists():
            self.skipTest("plugin/.claude-plugin/marketplace.json is written by the connect role")
        data = json.loads(mp.read_text())
        self.assertEqual(data["name"], "deskmate")
        entry = next(p for p in data["plugins"] if p["name"] == "habits")
        self.assertEqual(os.path.normpath(entry["source"]), "habits")

    def test_python_files_parse_as_python_3_9(self):
        files = [REPO / "setup" / "deskmate_setup" / "habits.py", HOOK] + sorted(Path(__file__).parent.glob("test_habits*.py"))
        for f in files:
            src = f.read_text()
            ast.parse(src, filename=str(f), feature_version=(3, 9))
            self.assertIn("from __future__ import annotations", src, f)
            self.assertNotRegex(src, r"\bimport tomllib\b|\bmatch \w+:\n", f)


class NoPersonalData(unittest.TestCase):
    def test_pack_and_plugins_hold_nothing_personal(self):
        found = []
        for p, text in text_files(PACK, REPO / "plugin"):
            for n, line in enumerate(text.split("\n"), 1):
                for rx in (PERSONAL_PATHS, PERSONAL_WORDS):
                    for m in rx.finditer(line):
                        found.append(f"{p.relative_to(REPO)}:{n}: {m.group(0)}")
        self.assertEqual(found, [])

    def test_habits_files_name_no_workspace(self):
        found = []
        for p, text in text_files(PACK, PLUGIN):
            for n, line in enumerate(text.split("\n"), 1):
                for m in OWN_WORDS.finditer(line):
                    found.append(f"{p.relative_to(REPO)}:{n}: {m.group(0)}")
        self.assertEqual(found, [])

    def test_no_rule_grants_autonomy(self):
        rules = (PACK / "rules.md.tmpl").read_text()
        self.assertIn("Commit, push, merge or deploy only when {owner} asks.", rules)
        for skill in (PLUGIN / "skills").rglob("SKILL.md"):
            self.assertNotRegex(skill.read_text(), r"(?i)\b(push|merge|deploy)\b[^.\n]*\bwithout\b")

    def test_example_agents_pass_the_lint(self):
        agents = habits.read_agents(PACK / "examples" / "agents")
        self.assertTrue(agents)
        self.assertEqual(habits.lint_agents(agents, user="alex"), [])


class Examples(Sandbox):
    def test_team_manifest_example(self):
        m = json.loads((PACK / "examples" / "deskmate-team.json").read_text())
        for key in ("changelog_dir", "agents", "repos", "skip_repos", "check_mode", "lint_deny"):
            self.assertIn(key, m)
        for key in ("team_rules", "hub_folder", "hub_url"):
            self.assertNotIn(key, m, "D16/D17: no team rules, no hub from a URL")
        self.assertIn(m["check_mode"], habits.MODES)
        self.assertIn("git", m["agents"])
        self.assertFalse((PACK / "examples" / "RULES.md").exists(), "D17: no team RULES.md ships")

    def test_team_manifest_example_in_a_hub(self):
        root = self.folder("work")
        self.repo(root / "mobile-app")
        hub = root / "claude"
        habits._fill_hub(habits.Run({}), {"path": str(root), "repos": [], "options": {"skip_repos": []}}, hub)
        m = json.loads((PACK / "examples" / "deskmate-team.json").read_text())
        m.pop("agents")  # a git URL on example.com: nothing to fetch in a test
        (hub / "deskmate-team.json").write_text(json.dumps(m))
        res = habits.apply(self.values([root]), self.emit)
        self.assertTrue(res["ok"], res)
        text = self.read(root / "CLAUDE.md")
        self.assertIn("`mobile-app/` (logged as `mobile`)", text)
        self.assertEqual(habits.block_info(text)["state"], "current")
        self.assertEqual(json.loads(self.read(habits.config_path()))["mode"], m["check_mode"])

    def test_habits_example_config_drives_the_hook(self):
        root = self.home / "work"
        (root / "api-v2" / ".git").mkdir(parents=True)
        (root / "claude" / "changelog").mkdir(parents=True)
        transcript = self.tmp / "t.jsonl"
        entries = [
            {"type": "user", "uuid": str(uuid.uuid4()), "timestamp": "2026-10-05T10:00:00.000Z", "cwd": str(root),
             "message": {"role": "user", "content": "Fix it"}},
            {"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": "2026-10-05T10:00:01.000Z", "cwd": str(root),
             "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                           "input": {"command": "echo 1 > api-v2/a.txt && git -C api-v2 commit -qam x"}}]}},
            {"type": "user", "uuid": str(uuid.uuid4()), "timestamp": "2026-10-05T10:00:02.000Z", "cwd": str(root),
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        ]
        transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
        event = {"session_id": "ex", "transcript_path": str(transcript), "cwd": str(root), "hook_event_name": "Stop",
                 "permission_mode": "default", "stop_hook_active": False}
        env = dict(os.environ, DESKMATE_HABITS_CONFIG=str(PACK / "habits.example.json"),
                   DESKMATE_HABITS_STATE=str(self.tmp / "state"))
        p = subprocess.run(["python3", str(HOOK)], input=json.dumps(event), capture_output=True, text=True, env=env,
                           timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        msg = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("missing for api.", msg, "repo_names maps the api-v2 folder to api")
        self.assertIn(str(root / "claude" / "changelog" / "api"), msg)


if __name__ == "__main__":
    unittest.main()
