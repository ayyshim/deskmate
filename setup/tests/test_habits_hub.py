"""Habits pack: the docs hub (new from the skeleton, existing, none) and the team agents (lint, import)."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_habits_support import GIT, Sandbox, habits  # noqa: E402

FAKE_GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2  # fake, 40 characters after the prefix

AGENT = """---
name: {name}
description: {desc}
tools: Read, Edit, Bash
---

You work on the {name} repo.
"""


def agent(folder: Path, name: str, desc: str = "Backend work in the api repo. Use for any change there.",
          body: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.md"
    p.write_text(AGENT.format(name=name, desc=desc) + body)
    return p


class Hub(Sandbox):
    def workspace(self, name="work") -> Path:
        root = self.folder(name)
        self.repo(root / "api")
        self.repo(root / "web")
        return root

    def test_new_hub_from_the_skeleton(self):
        root = self.workspace()
        res = habits.apply(self.values([root]), self.emit)
        self.assertTrue(res["ok"], res)
        hub = root / "claude"
        files = sorted(str(p.relative_to(hub)) for p in hub.rglob("*") if p.is_file() and ".git" not in p.parts)
        self.assertEqual(files, [".gitattributes", "README.md", "changelog/README.md", "design/README.md",
                                 "design/_template.md", "knowledge-base/README.md", "projects/README.md"])
        for f in files:
            text = (hub / f).read_text()
            self.assertIsNone(re.search(r"\{[?/]?\w+\}", text), f)
        self.assertIn("| api | [api/](api/) |", (hub / "changelog" / "README.md").read_text())
        self.assertIn("- `web`: `projects/web/INDEX.md`", (hub / "README.md").read_text())
        self.assertIn("changelog/**/*.md merge=union", (hub / ".gitattributes").read_text())
        rec = self.record()["habits"]["folders"][0]
        self.assertEqual(rec["hub"]["kind"], "new")
        self.assertTrue(rec["hub"]["created"])
        self.assertEqual(rec["options"]["hub"], "existing", "a re-run uses the hub as it is now")
        if GIT:
            self.assertEqual(self.git("log", "--oneline", cwd=hub).count("\n"), 1)
            self.assertEqual(self.git("status", "--porcelain", cwd=hub), "")

    def test_new_hub_never_overwrites(self):
        root = self.workspace()
        hub = root / "claude"
        (hub / "changelog").mkdir(parents=True)
        (hub / "changelog" / "README.md").write_text("# Our own format\n")
        (hub / "design").mkdir()
        (hub / "design" / "README.md").write_text("# Our designs\n")
        # It looks like a hub now, so "new" fills in only what is missing.
        res = habits.apply(self.values([root], **self.options(root, hub="new", fill_missing=True)), self.emit)
        self.assertTrue(res["ok"], res)
        self.assertEqual((hub / "changelog" / "README.md").read_text(), "# Our own format\n")
        self.assertEqual((hub / "design" / "README.md").read_text(), "# Our designs\n")
        self.assertTrue((hub / "design" / "_template.md").exists())

    def test_new_hub_refuses_a_folder_that_is_something_else(self):
        root = self.workspace()
        (root / "claude").mkdir()
        (root / "claude" / "notes.txt").write_text("mine")
        res = habits.apply(self.values([root]), self.emit)
        self.assertFalse(res["ok"])
        self.assertTrue(any("not a docs hub" in e for e in res["errors"]))
        self.assertEqual(sorted(p.name for p in (root / "claude").iterdir()), ["notes.txt"])

    @unittest.skipUnless(GIT, "git is not installed")
    def test_hub_inside_a_git_work_tree_gets_no_git_init(self):
        root = self.repo(self.folder("single"))
        res = habits.apply(self.values([root], **self.options(root, hub="new", hub_name="docs")), self.emit)
        self.assertTrue(res["ok"], res)
        self.assertTrue((root / "docs" / "README.md").exists())
        self.assertFalse((root / "docs" / ".git").exists())
        self.assertIn("`docs/` is the docs hub", self.read(root / "CLAUDE.local.md"))

    def test_existing_hub_fill_missing_only_when_asked(self):
        root = self.workspace()
        hub = root / "docs"
        (hub / "changelog").mkdir(parents=True)
        (hub / "changelog" / "README.md").write_text("# Changelogs\n")
        (hub / "design").mkdir()
        (hub / "design" / "README.md").write_text("# Designs\n")
        det = habits.detect(self.values([root]))
        h = det["folders"][0]["hub"]
        self.assertEqual((h["kind"], h["path"]), ("existing", str(hub)))
        self.assertIn("design/_template.md", h["missing"])
        habits.apply(self.values([root]), self.emit)
        self.assertFalse((hub / "design" / "_template.md").exists())
        habits.apply(self.values([root], **self.options(root, fill_missing=True)), self.emit)
        self.assertTrue((hub / "design" / "_template.md").exists())
        self.assertEqual((hub / "changelog" / "README.md").read_text(), "# Changelogs\n")

    def test_existing_hub_with_a_team_manifest(self):
        # A hub the team already keeps: its manifest names the changelog folder, repo names, repos to skip
        # and the default check mode. A RULES.md in it is not copied into the block (no team rules).
        root = self.workspace()
        hub = root / "claude"
        (hub / "changelog").mkdir(parents=True)
        (hub / "changelog" / "README.md").write_text("# Changelogs\n")
        (hub / "design").mkdir()
        (hub / "design" / "_template.md").write_text("# Template\n")
        (hub / "RULES.md").write_text("# Acme rules\n\n- Conventional Commits.\n")
        (hub / "deskmate-team.json").write_text(json.dumps({
            "version": 1, "changelog_dir": "log", "repos": [{"name": "website", "folder": "web"}],
            "skip_repos": ["api"], "check_mode": "require"}))
        det = habits.detect(self.values([root]))
        self.assertEqual(det["folders"][0]["hub"]["kind"], "existing")
        self.assertTrue(det["folders"][0]["manifest"])
        res = habits.apply(self.values([root]), self.emit)
        self.assertTrue(res["ok"], res)
        text = self.read(root / "CLAUDE.md")
        self.assertNotIn("Acme rules", text)
        self.assertNotIn("Team rules", text)
        self.assertNotIn("rules=", text.split("\n")[0])
        self.assertIn("`claude/log/<repo>/YYYY-MM-DD.md`", text)
        self.assertIn("(logged as `website`)", text)
        self.assertNotIn("`api/`", text, "skip_repos from the manifest leaves api out")
        cfg = json.loads(self.read(habits.config_path()))
        self.assertEqual(cfg["mode"], "require", "the manifest's check_mode is the default")
        self.assertEqual(cfg["roots"][0]["changelog_dir"], "log")
        self.assertEqual(cfg["roots"][0]["repo_names"], {"web": "website"})
        self.assertNotIn("rules", habits.status()["folders"][0])
        habits.remove(self.emit)
        self.assertTrue((hub / "RULES.md").exists(), "an existing hub is never touched")

    def test_there_is_no_clone(self):
        # D16: no hub from a URL. An old "clone" choice falls back to a new hub; nothing is fetched.
        self.assertNotIn("hub_url", habits.FOLDER_OPTIONS)
        root = self.workspace()
        v = self.values([root], **self.options(root, hub="clone", hub_url="https://git.example.com/hub.git"))
        det = habits.detect(v)
        self.assertEqual(det["folders"][0]["hub"]["kind"], "new")
        self.assertNotIn("url", det["folders"][0]["hub"])
        prev = habits.preview(v)
        self.assertTrue(prev["ok"], prev)
        self.assertFalse(any("lone" in c["what"] for c in prev["changes"]))
        res = habits.apply(v, self.emit)
        self.assertTrue(res["ok"], res)
        self.assertTrue((root / "claude" / "README.md").exists())
        self.assertFalse(any("lone" in t for t in self.texts()))

    def test_no_hub(self):
        root = self.workspace()
        res = habits.apply(self.values([root], **self.options(root, hub="none")), self.emit)
        self.assertTrue(res["ok"])
        text = self.read(root / "CLAUDE.md")
        self.assertNotIn("docs hub", text)
        self.assertNotIn("End-of-turn check", text)
        self.assertIn("in your summary and the commit message", text)
        self.assertEqual(json.loads(self.read(habits.config_path()))["roots"], [])


class Agents(Sandbox):
    def setUp(self):
        super().setUp()
        self.root = self.folder("work")
        self.repo(self.root / "api")
        self.src = self.folder("team-agents")

    def test_lint_finds_what_does_not_belong(self):
        agent(self.src, "clean", body="Docs are in /home/alex/work/claude and ~/.local/share/deskmate.\n")
        agent(self.src, "pathy", body="Read /home/bob/Projects/creds.env first.\nOr /Users/carol/x.\n")
        agent(self.src, "hosty", body="The dev server is http://dev.box.lan:8080 and printer.local.\n")
        agent(self.src, "secret", body=f"Use token {FAKE_GITHUB_TOKEN} to push.\n")
        agent(self.src, "denied", body="Deploy to staging.acme.internal.\n")
        found = habits.lint_agents(habits.read_agents(self.src), deny=["staging.acme.internal"], user="alex")
        by = {(f["file"], f["kind"]) for f in found}
        self.assertEqual(by, {("pathy.md", "home path"), ("hosty.md", "local host name"), ("secret.md", "secret"),
                              ("denied.md", "team lint word")})
        self.assertEqual(sorted(f["text"] for f in found if f["kind"] == "home path"), ["/Users/carol/", "/home/bob/"])
        self.assertEqual(sorted(f["text"] for f in found if f["kind"] == "local host name"), ["dev.box.lan", "printer.local"])
        self.assertNotIn(FAKE_GITHUB_TOKEN[4:12], json.dumps(found), "a secret is never shown")

    def test_import_waits_for_a_yes(self):
        agent(self.src, "backend")
        agent(self.src, "mobile", body="Run it on /home/bob/phone.\n")
        v = self.values([self.root], **self.options(self.root, hub="none", agents="folder", agents_source=str(self.src)))
        prev = habits.preview(v)
        self.assertEqual([x["file"] for x in prev["lint"]], ["mobile.md"])
        res = habits.apply(v, self.emit)
        self.assertTrue(res["ok"])
        self.assertEqual([x["file"] for x in res["lint"]], ["mobile.md"])
        self.assertFalse((self.root / ".claude" / "agents").exists())
        self.assertNotIn("## Agents", self.read(self.root / "CLAUDE.md"))
        v = self.values([self.root], **self.options(self.root, hub="none", agents="folder", agents_source=str(self.src),
                                                     agents_accept=True))
        res = habits.apply(v, self.emit)
        target = self.root / ".claude" / "agents"
        self.assertEqual(sorted(p.name for p in target.iterdir()), [".deskmate.json", "backend.md", "mobile.md"])
        man = json.loads((target / ".deskmate.json").read_text())
        self.assertEqual(sorted(man["files"]), ["backend.md", "mobile.md"])
        text = self.read(self.root / "CLAUDE.md")
        self.assertIn("## Agents", text)
        self.assertIn("- `backend` — Backend work in the api repo.", text)

    def test_update_and_remove_keep_local_edits(self):
        a = agent(self.src, "backend")
        agent(self.src, "frontend", desc="Frontend work.")
        agent(self.src, "old", desc="Retired.")
        v = self.values([self.root], **self.options(self.root, hub="none", agents="folder", agents_source=str(self.src)))
        habits.apply(v, self.emit)
        target = self.root / ".claude" / "agents"
        (target / "frontend.md").write_text((target / "frontend.md").read_text() + "\nLocal note.\n")
        a.write_text(a.read_text() + "\nTeam update.\n")
        (self.src / "old.md").unlink()
        self.events.clear()
        habits.apply(v, self.emit)
        self.assertIn("Team update.", (target / "backend.md").read_text())
        self.assertIn("Local note.", (target / "frontend.md").read_text())
        self.assertFalse((target / "old.md").exists(), "an unchanged file the team dropped goes")
        self.assertTrue(any("frontend.md was edited here" in t for t in self.texts("warn")))
        habits.remove(self.emit)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["frontend.md"])
        self.assertTrue(any("Kept" in t and "frontend.md" in t for t in self.texts("warn")))

    def test_never_overwrites_an_agent_that_is_not_deskmates(self):
        agent(self.src, "backend")
        target = self.root / ".claude" / "agents"
        mine = agent(target, "backend", desc="My own backend agent.")
        before = mine.read_text()
        v = self.values([self.root], **self.options(self.root, hub="none", agents="folder", agents_source=str(self.src)))
        prev = habits.preview(v)
        self.assertTrue(any("is not Deskmate's" in w for w in prev["warnings"]))
        habits.apply(v, self.emit)
        self.assertEqual(mine.read_text(), before)
        habits.remove(self.emit)
        self.assertEqual(mine.read_text(), before)

    def test_source_that_is_the_target_is_used_in_place(self):
        target = self.root / ".claude" / "agents"
        agent(target, "backend", body="Uses /home/bob/x but it is already here.\n")
        v = self.values([self.root], **self.options(self.root, hub="none", agents="folder", agents_source=str(target)))
        res = habits.apply(v, self.emit)
        self.assertEqual(res["lint"], [])
        self.assertFalse((target / ".deskmate.json").exists())
        self.assertIn("- `backend` —", self.read(self.root / "CLAUDE.md"))
        habits.remove(self.emit)
        self.assertTrue((target / "backend.md").exists())

    @unittest.skipUnless(GIT, "git is not installed")
    def test_agents_from_a_git_url_and_the_team_manifest(self):
        agent(self.src / "agents", "backend")
        self.git("init", "-q", cwd=self.src)
        self.git("add", "-A", cwd=self.src)
        self.git("commit", "-q", "-m", "agents", cwd=self.src)
        bare = self.tmp / "agents.git"
        self.git("clone", "-q", "--bare", str(self.src), str(bare))
        hub = self.root / "claude"
        habits._fill_hub(habits.Run({}), {"path": str(self.root), "repos": [], "options": {"skip_repos": []}}, hub)
        (hub / "deskmate-team.json").write_text(json.dumps({"agents": {"git": str(bare), "path": "agents"}}))
        res = habits.apply(self.values([self.root]), self.emit)
        self.assertTrue(res["ok"], res)
        target = self.root / ".claude" / "agents"
        self.assertTrue((target / "backend.md").exists())
        man = json.loads((target / ".deskmate.json").read_text())
        self.assertEqual(len(man["commit"]), 40)
        self.assertEqual(man["source"]["url"], str(bare))
        packs = self.tmp / "data" / "packs"
        self.assertEqual(len(list(packs.iterdir())), 1, "git sources are fetched into the data folder")

    @unittest.skipUnless(GIT, "git is not installed")
    def test_agents_inside_a_git_work_tree_are_excluded(self):
        root = self.repo(self.folder("single"))
        agent(self.src, "backend")
        v = self.values([root], **self.options(root, hub="none", agents="folder", agents_source=str(self.src)))
        habits.apply(v, self.emit)
        self.assertEqual(self.git("status", "--porcelain", cwd=root), "")
        habits.remove(self.emit)
        exclude = (root / ".git" / "info" / "exclude").read_text()
        self.assertNotIn(".claude/agents", exclude)
        self.assertNotIn("CLAUDE.local.md", exclude)


if __name__ == "__main__":
    unittest.main()
