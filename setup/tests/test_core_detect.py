"""detect: Claude Code folders, sessions from each transcript's cwd, repos, docs hubs, conflicts, name, zone."""

from __future__ import annotations

import json
import unittest

from test_core_support import CoreCase, transcript

from deskmate_setup import detect


class Sessions(CoreCase):
    def test_config_dirs_and_profiles(self):
        other = self.home / "work-claude"
        (other / "projects").mkdir(parents=True)
        (self.home / ".zshrc").write_text('export CLAUDE_CONFIG_DIR="$HOME/work-claude"\n')
        self.add_session(str(self.home / "Projects" / "app"))
        dirs = detect.claude_config_dirs()
        paths = [d["path"] for d in dirs]
        self.assertEqual(paths[0], str(self.claude_dir))
        self.assertIn(str(other), paths)
        main = dirs[0]
        self.assertTrue(main["has_projects"])
        self.assertEqual(main["sessions"], 1)
        self.assertEqual([d["source"] for d in dirs if d["path"] == str(other)], ["~/.zshrc"])

    def test_env_config_dir_first(self):
        import os

        alt = self.tmp / "alt-claude"
        (alt / "projects").mkdir(parents=True)
        os.environ["CLAUDE_CONFIG_DIR"] = str(alt)
        detect.clear_cache()
        self.assertEqual(detect.claude_config_dirs()[0]["path"], str(alt))
        self.assertEqual(detect.default_config_dir(), str(alt))

    def test_folders_come_from_cwd_not_the_encoded_name(self):
        p = self.home / "Projects"
        # Two folders that Claude Code encodes to the same name: only cwd tells them apart.
        self.add_session(str(p / "my-app"), "a", folder="-home-alex-Projects-my-app")
        self.add_session(str(p / "my.app"), "b", folder="-home-alex-Projects-my-app")
        self.add_session(str(p / "my.app"), "c", folder="-home-alex-Projects-my-app")
        self.add_session(str(self.home), "d", folder="-home-alex")
        self.add_session("/srv/tools/x", "e", folder="-srv-tools-x")
        # A subagent transcript is not a session; a file without cwd is counted but has no folder.
        sub = self.claude_dir / "projects" / "-home-alex-Projects-my-app" / "a" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text(transcript(str(p / "elsewhere")))
        (self.claude_dir / "projects" / "-x").mkdir()
        (self.claude_dir / "projects" / "-x" / "z.jsonl").write_text('{"type":"summary"}\n')
        rows = detect.session_folders([str(self.claude_dir)])
        by = {r["path"]: r for r in rows}
        self.assertEqual(by[str(p / "my.app")]["sessions"], 2)
        self.assertEqual(by[str(p / "my-app")]["sessions"], 1)
        self.assertEqual(by[str(p / "my-app")]["parent"], str(p))
        self.assertEqual(by[str(self.home)]["parent"], str(self.home))
        self.assertEqual(by["/srv/tools/x"]["parent"], "/srv/tools")
        self.assertNotIn(str(p / "elsewhere"), by)
        ranked = detect.rank_roots(rows)
        self.assertEqual(ranked[0]["path"], str(p))
        self.assertEqual(ranked[0]["sessions"], 3)
        self.assertTrue(any(g["home"] for g in ranked))
        self.assertEqual(detect.session_stats([str(self.claude_dir)])["total"], 6)

    def test_first_cwd(self):
        f = self.tmp / "t.jsonl"
        f.write_text(transcript("/home/alex/a \"q\""))
        self.assertEqual(detect.first_cwd(f), '/home/alex/a "q"')
        f.write_text('{"type":"x"}\n')
        self.assertEqual(detect.first_cwd(f), "")
        self.assertIsNone(detect.first_cwd(self.tmp / "missing.jsonl"))


class ReposAndHubs(CoreCase):
    def test_repos_and_worktrees(self):
        root = self.projects()
        (root / "lib" / ".git").mkdir(parents=True)
        wt = root / "app-wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {root}/app/.git/worktrees/app-wt\n")
        (root / "node_modules" / "dep" / ".git").mkdir(parents=True)
        found = {r["name"]: r for r in detect.repos([str(root)])}
        self.assertEqual(set(found), {"app", "lib", "app-wt"})
        self.assertEqual(found["app-wt"]["worktree_of"], str(root / "app"))

    def test_docs_hubs(self):
        root = self.projects()
        hub = root / "claude"
        (hub / "changelog" / "app").mkdir(parents=True)
        (hub / "changelog" / "README.md").write_text("# Changelog\n")
        (hub / "changelog" / "app" / "2026-10-01.md").write_text("x\n")
        (hub / "design" / "feature").mkdir(parents=True)
        hubs = detect.docs_hubs([str(root)])
        self.assertEqual([h["path"] for h in hubs], [str(hub)])
        self.assertEqual(hubs[0]["changelog_entries"], 1)
        self.assertEqual(hubs[0]["design_docs"], 1)
        self.assertEqual(hubs[0]["label"], "~/Projects/claude")


class Conflicts(CoreCase):
    def test_finds_browser_tools_and_the_old_install(self):
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "playwright": {"command": "npx", "args": ["@playwright/mcp"]},
            "deskmate": {"type": "http", "url": "http://127.0.0.1:7800/mcp", "headers": {"Authorization": "Bearer fake"}},
            "notes": {"command": "notes-mcp"}}}))
        skills = self.claude_dir / "skills"
        (skills / "deskmate").mkdir(parents=True)
        (skills / "deskmate" / "SKILL.md").write_text("---\nname: deskmate\n---\n")
        (skills / "viway-browser").mkdir()
        (self.claude_dir / "settings.json").write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "/x/deskmate/hook.sh stop"}]}]}}))
        found = detect.conflicts([str(self.claude_dir)])
        ids = {c["id"] for c in found}
        self.assertIn("mcp:playwright", ids)
        self.assertIn("legacy:deskmate", ids)
        self.assertIn("legacy:deskmate skill", ids)
        self.assertIn("skill:viway-browser", ids)
        self.assertIn("legacy:deskmate Stop hook@Stop", ids)
        self.assertNotIn("mcp:notes", ids)
        self.assertNotIn("fake", json.dumps(found))


class Machine(CoreCase):
    def test_owner_and_timezone(self):
        self.assertEqual(detect.owner(), "Alex")
        self.assertEqual(detect.timezone(), "Europe/London")
        self.assertTrue(detect.valid_tz("UTC"))
        self.assertFalse(detect.valid_tz("../../etc/passwd"))
        self.assertFalse(detect.valid_tz("Not/AZone"))
        self.assertEqual(detect.clean_name("Al<ex>\n"), "Alex")

    def test_network_default(self):
        linux = {"os": "linux"}
        self.assertEqual(detect.network_default({"desktop": False, "rootless": False, "version": "29.0.0"}, linux), "host")
        self.assertEqual(detect.network_default({"desktop": True}, linux), "host-access")
        self.assertEqual(detect.network_default({"desktop": False}, {"os": "macos"}), "host-access")
        self.assertEqual(detect.network_default({"rootless": True, "version": "28.1.0"}, linux), "host-access")
        self.assertEqual(detect.network_default({"rootless": True, "version": "29.5.0"}, linux), "host")
        choices = {c["value"]: c for c in detect.network_choices({"desktop": True}, {"os": "macos"})}
        self.assertTrue(choices["host"]["disabled"])
        self.assertFalse(choices["host-access"]["disabled"])

    def test_ports_and_display(self):
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        try:
            info = detect.ports([busy])[busy]
            self.assertFalse(info["free"])
            self.assertNotEqual(detect.free_port(busy), busy)
            self.assertEqual(detect.free_port(busy, ours=(busy,)), busy)
        finally:
            s.close()
        self.assertIsInstance(detect.display_free(987), bool)

    def test_platform_shape(self):
        p = detect.platform()
        self.assertIn(p["os"], ("linux", "macos", "wsl"))
        for k in ("arch", "label", "python"):
            self.assertTrue(p[k])


if __name__ == "__main__":
    unittest.main()
