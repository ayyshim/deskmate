"""Habits pack: rendering the rules block, editing CLAUDE.md around the user's text, and apply/remove."""

from __future__ import annotations

import itertools
import json
import os
import re
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_habits_support import GIT, Sandbox, habits  # noqa: E402

LEFTOVER = re.compile(r"\{[?/]?\w+\}")


def sample_values(**kw) -> dict:
    v = {"version": 1, "date": "2026-10-05", "hub": "claude", "changelog_dir": "changelog",
         "repo_rows": "| `api/` | not documented yet |", "owner": "Alex", "skill_prefix": "habits:",
         "check_mode": "remind", "agent_rows": "- `backend` — Backend work."}
    v.update(kw)
    return v


class Render(unittest.TestCase):
    def test_all_flag_combinations_render_cleanly(self):
        tmpl = habits.rules_template()
        names, flags = habits.template_names(tmpl)
        self.assertEqual(sorted(flags), sorted(["multi", "single", "hub", "nohub", "notify", "check", "codegraph",
                                                "agents"]))
        inner_names = set(names) - {"version", "date", "block_sha"}
        self.assertTrue(inner_names <= set(sample_values()), inner_names - set(sample_values()))
        count = 0
        for combo in itertools.product([False, True], repeat=len(flags)):
            fl = dict(zip(flags, combo))
            lines, sha = habits.render_block(sample_values(), fl)
            text = "\n".join(lines)
            self.assertIsNone(LEFTOVER.search(text), (fl, LEFTOVER.findall(text)))
            self.assertTrue(habits.BEGIN_RE.match(lines[0]) and habits.END_RE.match(lines[-1]))
            self.assertEqual(sum(1 for ln in lines if habits.BEGIN_RE.match(ln) or habits.END_RE.match(ln)), 2)
            self.assertEqual(habits.block_info(text + "\n")["state"], "current", fl)
            self.assertIn(f"block={sha}", lines[0])
            self.assertNotIn("\n\n\n", text)
            self.assertNotIn("None", text)
            count += 1
        self.assertEqual(count, 2 ** len(flags))

    def test_values_keep_their_braces(self):
        lines, _ = habits.render_block(sample_values(agent_rows="- `api` — GET /api/items/{id} returns {name}."),
                                       {f: True for f in habits.template_names(habits.rules_template())[1]})
        self.assertIn("GET /api/items/{id} returns {name}", "\n".join(lines))

    def test_template_errors(self):
        with self.assertRaises(habits.TemplateError):
            habits.render("hello {who}", {})
        with self.assertRaises(habits.TemplateError):
            habits.render("{?a}\nx\n", {}, {"a": True})
        with self.assertRaises(habits.TemplateError):
            habits.render("x\n{/a}\n", {}, {"a": True})
        with self.assertRaises(habits.TemplateError):
            habits.render("{?b}\nx\n{/b}\n", {}, {})
        with self.assertRaises(habits.TemplateError):
            habits.render("text {?a} inline\n", {}, {"a": True})

    def test_skeleton_templates_render(self):
        values = {"title": "work docs hub", "repo_list": "- `api`", "changelog_rows": "| api | [api/](api/) |",
                  "today": "2026-10-05"}
        for src, rel in habits._skeleton_files():
            text = src.read_text(encoding="utf-8")
            out = habits.render(text, values) if src.suffix == ".tmpl" else text
            if src.suffix == ".tmpl":
                self.assertIsNone(LEFTOVER.search(out), rel)


class BlockSurgery(unittest.TestCase):
    BLOCK = ["<!-- deskmate:habits:begin v1 · test block=x -->", "# Working habits", "", "- a rule",
             "<!-- deskmate:habits:end -->"]

    def test_append_and_strip_round_trip(self):
        for original in ["", "# Mine\n", "# Mine\n\n", "a\nb\n\n\nc\n", "x\r\ny\r\n"]:
            placed = habits.place_block(original, self.BLOCK)
            self.assertTrue(placed.startswith(original.rstrip("\n") if not original.endswith("\n") else original))
            back, found = habits.strip_block(placed)
            self.assertTrue(found)
            self.assertEqual(back, original, repr(original))

    def test_replace_in_place_keeps_text_around(self):
        above, below = "# Mine\n\nabove\n\n", "\nbelow\n"
        text = above + "\n".join(self.BLOCK) + "\n" + below
        new_block = self.BLOCK[:3] + ["- another rule"] + self.BLOCK[3:]
        out = habits.place_block(text, new_block)
        self.assertTrue(out.startswith(above) and out.endswith(below))
        self.assertIn("- another rule", out)
        self.assertEqual(habits.strip_block(out)[0], above.rstrip("\n") + "\n" + below)

    def test_crlf_is_kept(self):
        out = habits.place_block("one\r\ntwo\r\n", self.BLOCK)
        self.assertNotIn("\n", out.replace("\r\n", ""))

    def test_agents_prefix_on_new_file(self):
        out = habits.place_block("", self.BLOCK, ["@AGENTS.md"])
        self.assertTrue(out.startswith("@AGENTS.md\n\n<!-- deskmate:habits:begin"))

    def test_broken_markers(self):
        twice = "\n".join(self.BLOCK + self.BLOCK) + "\n"
        with self.assertRaises(habits.BlockError):
            habits.place_block(twice, self.BLOCK)
        backwards = self.BLOCK[-1] + "\n" + self.BLOCK[0] + "\n"
        self.assertEqual(habits.block_info(backwards)["state"], "broken")


class ApplyRemove(Sandbox):
    def workspace(self, name="work") -> Path:
        root = self.folder(name)
        self.repo(root / "api")
        self.repo(root / "web")
        return root

    def test_paths_stay_in_the_sandbox(self):
        for p in (habits.config_path(), habits.state_dir(), habits.claude_dir(), habits.data_dir(),
                  habits.installed_path()):
            self.assertTrue(str(p).startswith(str(self.tmp)), p)

    def test_plain_folder_round_trip(self):
        root = self.workspace()
        res = habits.apply(self.values([root]), self.emit)
        self.assertTrue(res["ok"], res)
        claude_md = root / "CLAUDE.md"
        text = self.read(claude_md)
        self.assertTrue(text.startswith("<!-- deskmate:habits:begin v1"))
        self.assertIn("| `api/` | not documented yet |", text)
        self.assertIn("`claude/` is the docs hub", text)
        self.assertIn("Memory is Alex's", text)
        self.assertNotIn("Post milestones", text)  # notifications are off
        cfg = json.loads(self.read(habits.config_path()))
        self.assertEqual(cfg["mode"], "remind")
        self.assertEqual(cfg["roots"][0]["path"], str(root))
        self.assertEqual(cfg["roots"][0]["hub"], str(root / "claude"))
        self.assertEqual(stat.S_IMODE(habits.config_path().stat().st_mode), 0o600)
        rec = self.record()["habits"]
        self.assertEqual(rec["folders"][0]["block"]["created"], True)
        self.assertEqual(stat.S_IMODE(habits.installed_path().stat().st_mode), 0o600)
        out = habits.remove(self.emit)
        self.assertTrue(out["ok"], out)
        self.assertFalse(claude_md.exists())
        self.assertFalse(habits.config_path().exists())
        self.assertTrue((root / "claude" / "README.md").exists(), "the hub is the user's data and stays")
        self.assertNotIn("habits", self.record())

    @unittest.skipUnless(GIT, "git is not installed")
    def test_folder_that_is_a_git_repo_uses_claude_local_md(self):
        root = self.repo(self.folder("single"))
        (root / "CLAUDE.md").write_text("# Team instructions\n")
        self.git("add", "CLAUDE.md", cwd=root)
        self.git("commit", "-q", "-m", "init", cwd=root)
        exclude = root / ".git" / "info" / "exclude"
        before_exclude = exclude.read_text() if exclude.exists() else None
        res = habits.apply(self.values([root], **self.options(root, hub="none")), self.emit)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.read(root / "CLAUDE.md"), "# Team instructions\n")
        local = self.read(root / "CLAUDE.local.md")
        self.assertIn("This folder is a git repo", local)
        self.assertIn(f"| `{root.name}` (this folder) | — |", local)
        self.assertIn("/CLAUDE.local.md", exclude.read_text().splitlines())
        self.assertEqual(self.git("status", "--porcelain", cwd=root), "")
        habits.remove(self.emit)
        self.assertFalse((root / "CLAUDE.local.md").exists())
        self.assertEqual(exclude.read_text() if exclude.exists() else None, before_exclude)

    def test_folder_with_agents_md_only(self):
        root = self.workspace("agentsmd")
        (root / "AGENTS.md").write_text("# Instructions for every agent\n")
        habits.apply(self.values([root], **self.options(root, hub="none")), self.emit)
        text = self.read(root / "CLAUDE.md")
        self.assertTrue(text.startswith("@AGENTS.md\n\n<!-- deskmate:habits:begin"), text[:80])
        self.assertTrue(any("imports AGENTS.md" in t for t in self.texts("ok")))
        habits.remove(self.emit)
        self.assertFalse((root / "CLAUDE.md").exists())
        self.assertEqual(self.read(root / "AGENTS.md"), "# Instructions for every agent\n")

    def test_user_text_above_and_below_is_untouched(self):
        root = self.workspace()
        above = "# My own notes\n\nKeep answers short.\n"
        (root / "CLAUDE.md").write_text(above)
        habits.apply(self.values([root]), self.emit)
        below = "\n## Later notes\n\nRun the linters.\n"
        with open(root / "CLAUDE.md", "a") as f:
            f.write(below)
        text1 = self.read(root / "CLAUDE.md")
        habits.apply(self.values([root], NOTIFY_KIND="discord"), self.emit)  # the block changes
        text2 = self.read(root / "CLAUDE.md")
        self.assertNotEqual(text1, text2)
        self.assertIn("Post milestones", text2)
        self.assertTrue(text2.startswith(above + "\n<!-- deskmate:habits:begin"))
        self.assertTrue(text2.endswith("<!-- deskmate:habits:end -->\n" + below))
        habits.remove(self.emit)
        self.assertEqual(self.read(root / "CLAUDE.md"), above + below)

    def test_reapply_is_idempotent(self):
        root = self.workspace()
        (root / "CLAUDE.md").write_text("# Mine\n")
        habits.apply(self.values([root]), self.emit)
        path = root / "CLAUDE.md"
        first, mtime = path.read_bytes(), path.stat().st_mtime_ns
        cfg_mtime = habits.config_path().stat().st_mtime_ns
        backups = sorted((self.tmp / "data" / "backups").iterdir())
        self.events.clear()
        res = habits.apply(self.values([root]), self.emit)
        self.assertTrue(res["ok"])
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(habits.config_path().stat().st_mtime_ns, cfg_mtime)
        self.assertEqual(sorted((self.tmp / "data" / "backups").iterdir()), backups, "no new backup on a no-op")
        self.assertEqual(self.texts("ok"), ["Working habits are set up"])

    def test_backup_before_edit(self):
        root = self.workspace()
        (root / "CLAUDE.md").write_text("# Mine\n")
        res = habits.apply(self.values([root]), self.emit)
        backup = Path(res["backups"]) / str(root / "CLAUDE.md").lstrip("/")
        self.assertEqual(backup.read_text(), "# Mine\n")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_edits_inside_the_block_are_detected(self):
        root = self.workspace()
        habits.apply(self.values([root]), self.emit)
        path = root / "CLAUDE.md"
        edited = self.read(path).replace("Keep the knowledge base current.", "Keep the KB current!")
        path.write_text(edited)
        det = habits.detect(self.values([root]))
        self.assertEqual(det["folders"][0]["block_now"]["state"], "edited")
        prev = habits.preview(self.values([root]))
        self.assertEqual(prev["folders"][0]["block"], "conflict")
        self.assertIn("Keep the KB current!", prev["folders"][0]["diff"])
        # Default: leave it alone and say so.
        self.events.clear()
        habits.apply(self.values([root]), self.emit)
        self.assertEqual(self.read(path), edited)
        self.assertTrue(any("edited by hand" in t for t in self.texts("warn")))
        # Overwrite: Deskmate's text comes back.
        habits.apply(self.values([root], **self.options(root, on_edit="overwrite")), self.emit)
        self.assertIn("Keep the knowledge base current.", self.read(path))
        self.assertEqual(habits.block_info(self.read(path))["state"], "current")
        # Keep: the markers go, the edited text stays and is the user's.
        path.write_text(self.read(path).replace("Keep the knowledge base current.", "Keep the KB current!"))
        habits.apply(self.values([root], **self.options(root, on_edit="keep")), self.emit)
        kept = self.read(path)
        self.assertIn("Keep the KB current!", kept)
        self.assertNotIn("deskmate:habits", kept)
        self.assertIsNone(self.record()["habits"]["folders"][0].get("block"))
        habits.remove(self.emit)
        self.assertEqual(self.read(path), kept)

    def test_broken_markers_change_nothing(self):
        root = self.workspace()
        habits.apply(self.values([root]), self.emit)
        path = root / "CLAUDE.md"
        broken = self.read(path) + "\n<!-- deskmate:habits:end -->\n"
        path.write_text(broken)
        res = habits.apply(self.values([root], NOTIFY_KIND="slack"), self.emit)
        self.assertFalse(res["ok"])
        self.assertEqual(self.read(path), broken)
        self.assertTrue(any("markers are broken" in e for e in res["errors"]))

    def test_folder_with_its_own_rules_defaults_off(self):
        root = self.workspace()
        own = ("# Workspace\n\nAfter each change append to `docs/changelog/<repo>/YYYY-MM-DD.md`.\n"
               "Design docs live in `docs/design/`.\n")
        (root / "CLAUDE.md").write_text(own)
        det = habits.detect(self.values([root]))
        f = det["folders"][0]
        self.assertFalse(f["block_default"])
        self.assertIn("already has its own rules", f["block_reason"])
        res = habits.apply(self.values([root]), self.emit)
        self.assertTrue(res["ok"])
        self.assertEqual(self.read(root / "CLAUDE.md"), own)
        self.assertTrue(any("already has its own rules" in t for t in self.texts("info")))
        # The check still covers the folder (it has a hub now).
        self.assertEqual(json.loads(self.read(habits.config_path()))["roots"][0]["path"], str(root))
        # The user can still ask for the block.
        habits.apply(self.values([root], **self.options(root, block=True)), self.emit)
        self.assertIn("deskmate:habits:begin", self.read(root / "CLAUDE.md"))

    def test_nested_folders_are_refused(self):
        outer = self.workspace()
        inner = self.repo(outer / "api")
        prev = habits.preview(self.values([outer, inner]))
        self.assertFalse(prev["ok"])
        self.assertTrue(any("is inside" in e for e in prev["errors"]))
        res = habits.apply(self.values([outer, inner]), self.emit)
        self.assertFalse(res["ok"])
        self.assertFalse((inner / "CLAUDE.local.md").exists() or (inner / "CLAUDE.md").exists())
        self.assertTrue((outer / "CLAUDE.md").exists())

    def test_file_mode_and_symlink_are_kept(self):
        root = self.workspace()
        real = self.tmp / "dotfiles" / "CLAUDE.md"
        real.parent.mkdir()
        real.write_text("# Mine\n")
        os.chmod(real, 0o600)
        (root / "CLAUDE.md").symlink_to(real)
        habits.apply(self.values([root]), self.emit)
        self.assertTrue((root / "CLAUDE.md").is_symlink())
        self.assertIn("deskmate:habits:begin", real.read_text())
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)
        habits.remove(self.emit)
        self.assertTrue((root / "CLAUDE.md").is_symlink())
        self.assertEqual(real.read_text(), "# Mine\n")

    def test_settings_are_merged_and_put_back(self):
        root = self.workspace()
        hub = self.folder("shared-docs")
        habits._fill_hub(habits.Run({}), {"path": str(root), "repos": [], "options": {"skip_repos": []}}, hub)
        settings = habits.claude_dir() / "settings.json"
        settings.parent.mkdir(parents=True)
        original = {"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]}, "autoMemoryEnabled": False}
        settings.write_text(json.dumps(original, indent=2) + "\n")
        v = self.values([root], NOTIFY_KIND="ntfy", HABITS_MEMORY_ON="on",
                        **self.options(root, hub="existing", hub_path=str(hub)))
        res = habits.apply(v, self.emit)
        self.assertTrue(res["ok"], res)
        now = json.loads(settings.read_text())
        self.assertEqual(now["model"], "opus")
        self.assertEqual(now["permissions"]["allow"], ["Bash(ls:*)", habits.NOTIFY_TOOL])
        self.assertEqual(now["permissions"]["additionalDirectories"], [str(hub)])
        self.assertIs(now["autoMemoryEnabled"], True)
        block = self.read(root / "CLAUDE.md")
        self.assertIn(f"`{hub}/` is the docs hub", block, "a hub outside the folder is named by its full path")
        self.assertIn("Post milestones", block)
        habits.remove(self.emit)
        self.assertEqual(json.loads(settings.read_text()), original)

    def test_plugin_is_installed_and_removed(self):
        root = self.workspace()
        habits.apply(self.values([root], HABITS_PLUGIN="on"), self.emit)
        self.assertEqual(self.plugin_calls, [("install", "habits")])
        habits.remove(self.emit)
        self.assertEqual(self.plugin_calls, [("install", "habits"), ("uninstall", "habits")])

    def test_an_older_plugin_is_updated(self):
        root = self.workspace()
        plugins = habits.claude_dir() / "plugins"
        plugins.mkdir(parents=True)
        listed = {"version": 2, "plugins": {"habits@deskmate": [{"scope": "user", "version": "0.0.1"}]}}
        (plugins / "installed_plugins.json").write_text(json.dumps(listed))
        habits.apply(self.values([root], HABITS_PLUGIN="on"), self.emit)
        self.assertEqual(self.plugin_calls, [("install", "habits")])
        st = habits.status()
        self.assertTrue(any("this clone has" in p for p in st["problems"]))
        listed["plugins"]["habits@deskmate"][0]["version"] = habits.plugin_version()
        (plugins / "installed_plugins.json").write_text(json.dumps(listed))
        habits.apply(self.values([root], HABITS_PLUGIN="on"), self.emit)
        self.assertEqual(self.plugin_calls, [("install", "habits")], "an up-to-date plugin is left alone")
        self.assertEqual(habits.status()["problems"], [])

    def test_habits_off_removes_everything(self):
        root = self.workspace()
        habits.apply(self.values([root]), self.emit)
        self.assertTrue((root / "CLAUDE.md").exists())
        res = habits.apply(self.values([root], HABITS="off"), self.emit)
        self.assertTrue(res["ok"])
        self.assertFalse((root / "CLAUDE.md").exists())
        self.assertNotIn("habits", self.record())

    def test_one_folder_at_a_time(self):
        a, b = self.workspace("a"), self.workspace("b")
        habits.apply(self.values([a, b]), self.emit)
        b_text = self.read(b / "CLAUDE.md")
        res = habits.apply({"HABITS": "on", "HABITS_FOLDER": str(a), "DESKMATE_OWNER": "Alex", "HABITS_PLUGIN": "off",
                            "HABITS_CHECK": "require"}, self.emit)
        self.assertTrue(res["ok"], res)
        self.assertIn("End-of-turn check (require)", self.read(a / "CLAUDE.md"))
        self.assertEqual(self.read(b / "CLAUDE.md"), b_text)
        cfg = json.loads(self.read(habits.config_path()))
        self.assertEqual(sorted(r["path"] for r in cfg["roots"]), sorted([str(a), str(b)]))
        habits.remove(self.emit, folder=str(a))
        self.assertFalse((a / "CLAUDE.md").exists())
        self.assertTrue((b / "CLAUDE.md").exists())
        cfg = json.loads(self.read(habits.config_path()))
        self.assertEqual([r["path"] for r in cfg["roots"]], [str(b)])
        self.assertEqual(habits.status()["folders"][0]["path"], str(b))

    def test_dropping_a_folder_from_the_plan_removes_its_block(self):
        a, b = self.workspace("a"), self.workspace("b")
        habits.apply(self.values([a, b]), self.emit)
        habits.apply(self.values([a]), self.emit)
        self.assertTrue((a / "CLAUDE.md").exists())
        self.assertFalse((b / "CLAUDE.md").exists())
        self.assertTrue((b / "claude").is_dir())

    def test_status_reports_problems(self):
        root = self.workspace()
        habits.apply(self.values([root]), self.emit)
        st = habits.status()
        self.assertEqual(st["folders"][0]["block"], "current")
        self.assertEqual(st["problems"], [])
        (root / "CLAUDE.md").unlink()
        st = habits.status()
        self.assertEqual(st["folders"][0]["block"], "missing")
        self.assertTrue(st["problems"])
        self.assertIn("block missing", habits.format_status(st))

    def test_detect_never_raises(self):
        res = habits.detect({"HABITS_FOLDERS": [str(self.tmp / "nope")]})
        self.assertTrue(res["ok"])
        self.assertTrue(res["folders"][0]["errors"])
        res = habits.detect({"HABITS_FOLDER_OPTIONS": "{not json"})
        self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main()
