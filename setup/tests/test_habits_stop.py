"""The end-of-turn changelog check (plugin/habits/bin/habits-stop) on fixture transcripts.

The transcripts have the shape Claude Code 2.1.278 writes: user / assistant entries with uuid, timestamp,
cwd and isSidechain; tool_use blocks in assistant entries and tool_result blocks in user entries. The
hook runs as a real process, the way Claude Code runs it, with its config and state in the sandbox.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_habits_support import HOOK, Sandbox  # noqa: E402

PYTHON = sys.executable


def ts() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class StopCheck(Sandbox):
    def setUp(self):
        super().setUp()
        self.root = self.folder("work")
        self.hub = self.root / "claude"
        (self.hub / "changelog").mkdir(parents=True)
        for repo in ("api", "fleet"):
            (self.root / repo / ".git").mkdir(parents=True)
        (self.root / "fleet" / ".git" / "worktrees" / "hotfix").mkdir(parents=True)
        self.wt = self.root / "fleet-wt" / "hotfix"
        self.wt.mkdir(parents=True)
        (self.wt / ".git").write_text(f"gitdir: {self.root}/fleet/.git/worktrees/hotfix\n")
        for f in ("api/app/receipt.py", "fleet-wt/hotfix/backend/app/triage.py"):
            (self.root / f).parent.mkdir(parents=True, exist_ok=True)
            (self.root / f).write_text("x = 1\n")
        self.cfg = self.tmp / "habits.json"
        self.state = self.tmp / "state"
        self.today = dt.date.today().isoformat()

    # ------------------------------------------------------------ transcript entries

    def entry(self, kind: str, content, cwd=None, **extra) -> dict:
        e = {"type": kind, "isSidechain": False, "uuid": str(uuid.uuid4()), "timestamp": ts(),
             "cwd": str(cwd or self.root), "sessionId": "s", "message": {"role": kind, "content": content}}
        e.update(extra)
        return e

    def user(self, text: str) -> list:
        return [self.entry("user", text)]

    def tool(self, name: str, inp: dict, cwd=None, error: bool = False, sidechain: bool = False) -> list:
        tid = "toolu_" + uuid.uuid4().hex[:12]
        a = self.entry("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": inp}], cwd)
        r = self.entry("user", [{"type": "tool_result", "tool_use_id": tid, "content": "error" if error else "ok",
                                 "is_error": error}], cwd)
        if sidechain:
            a["isSidechain"] = r["isSidechain"] = True
        return [a, r]

    def say(self, text: str) -> list:
        return [self.entry("assistant", [{"type": "text", "text": text}])]

    def feedback(self, text: str) -> list:
        return [self.entry("user", "Stop hook feedback:\n" + text, isMeta=True)]

    # ------------------------------------------------------------ running the hook

    def config(self, mode: str, **root) -> None:
        r = {"path": str(self.root), "hub": str(self.hub), "changelog_dir": "changelog", "skip_repos": [],
             "repo_names": {}}
        r.update(root)
        self.cfg.write_text(json.dumps({"version": 1, "mode": mode, "max_blocks": 2,
                                        "skill": "habits:changelog-entry", "roots": [r]}))

    def hook(self, event, stdin: str | None = None, env: dict | None = None) -> tuple:
        e = dict(os.environ, DESKMATE_HABITS_CONFIG=str(self.cfg), DESKMATE_HABITS_STATE=str(self.state))
        e.update(env or {})
        t0 = time.monotonic()
        p = subprocess.run([PYTHON, str(HOOK)], input=stdin if stdin is not None else json.dumps(event),
                           capture_output=True, text=True, env=e, timeout=30)
        return p.returncode, p.stdout.strip(), p.stderr, (time.monotonic() - t0) * 1000

    def event(self, sid: str, active: bool = False, **extra) -> dict:
        path = self.tmp / f"{sid}.jsonl"
        last = ""
        if path.exists():
            for line in path.read_text().splitlines():
                e = json.loads(line)
                if e["type"] == "assistant":
                    last = "\n".join(b.get("text", "") for b in e["message"]["content"] if b.get("type") == "text") or last
        ev = {"session_id": sid, "transcript_path": str(path), "cwd": str(self.root), "permission_mode": "acceptEdits",
              "hook_event_name": "Stop", "stop_hook_active": active, "last_assistant_message": last,
              "background_tasks": [], "session_crons": []}
        ev.update(extra)
        return ev

    def stop(self, sid: str, entries: list, active: bool = False, **extra):
        """Append the turn's entries to the session's transcript and run the hook. Returns its JSON or None."""
        with open(self.tmp / f"{sid}.jsonl", "a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        code, out, err, _ = self.hook(self.event(sid, active, **extra))
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        return json.loads(out) if out else None

    def log_file(self, repo: str) -> Path:
        return Path(os.path.realpath(self.hub)) / "changelog" / repo / f"{self.today}.md"

    # ------------------------------------------------------------ the worked examples

    def test_example_1_remind_after_an_edit_and_a_commit(self):
        self.config("remind")
        out = self.stop("e1", self.user("Fix the rounding bug on the receipt")
                        + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py"), "old_string": "x",
                                             "new_string": "y"})
                        + self.tool("Bash", {"command": 'cd api && git commit -am "fix: receipt rounding"'})
                        + self.say("Fixed the rounding and committed it."))
        ctx = out["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "Stop")
        msg = ctx["additionalContext"]
        self.assertTrue(msg.startswith("Deskmate habits: today's changelog entry is missing for api."), msg)
        self.assertIn(f"- api: {self.log_file('api')} (changed: app/receipt.py)", msg)
        self.assertIn("with the habits:changelog-entry skill", msg)
        self.assertIn("say so in one line and carry on", msg)
        # The session writes the entry; the next stop (a continuation) says nothing.
        self.log_file("api").parent.mkdir(parents=True)
        self.log_file("api").write_text(f"# {self.today}\n\n## 10:00 — Receipt rounding\n")
        self.assertIsNone(self.stop("e1", self.tool("Write", {"file_path": str(self.log_file("api")), "content": "…"})
                                    + self.say("Wrote the api changelog entry."), active=True))
        self.assertIsNone(self.stop("e1", self.user("Thanks") + self.say("You are welcome.")))

    def test_remind_waits_until_the_session_moves_on(self):
        self.config("remind")
        self.assertIsNone(self.stop("r", self.user("Start the receipt fix")
                                    + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")})
                                    + self.say("Started; more to do.")))
        out = self.stop("r", self.user("Now what does the README say?")
                        + self.tool("Read", {"file_path": str(self.root / "api/README.md")}) + self.say("It says hi."))
        self.assertIn("missing for api", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.stop("r", self.user("And the other one?") + self.say("Also hi.")), "one reminder per change")

    def test_example_2_read_only_and_docs_only_turns(self):
        self.config("remind")
        self.assertIsNone(self.stop("e2", self.user("Where is the receipt total computed?")
                                    + self.tool("Read", {"file_path": str(self.root / "api/app/receipt.py")})
                                    + self.tool("Bash", {"command": "git -C api log --oneline -5 && git -C api status"})
                                    + self.say("In app/receipt.py.")))
        self.assertIsNone(self.stop("e2", self.user("Write the design doc for receipt rounding")
                                    + self.tool("Write", {"file_path": str(self.hub / "design/receipts/00-overview.md")})
                                    + self.tool("Edit", {"file_path": str(self.hub / "design/README.md")})
                                    + self.tool("Agent", {"description": "implement", "subagent_type": "backend-engineer",
                                                          "prompt": "..."})
                                    + self.say("Design doc written; the backend agent implemented it.")))
        state = json.loads((self.state / "e2.json").read_text())
        self.assertEqual(state["debt"], {})

    def test_example_3_require_sed_in_a_linked_worktree(self):
        self.config("require")
        out = self.stop("e3", self.user("Make triage skip empty clusters")
                        + self.tool("Bash", {"command": f"sed -i 's/y/w/' {self.wt}/backend/app/triage.py && cd {self.wt} && pytest -q"})
                        + self.say("Triage now skips empty clusters; tests pass."))
        self.assertEqual(out["decision"], "block")
        self.assertIn("missing for fleet.", out["reason"])
        self.assertIn(f"- fleet: {self.log_file('fleet')} (changed: backend/app/triage.py)", out["reason"])
        self.assertIn("they can say 'no changelog'", out["reason"])
        # The entry is appended with a heredoc: the parser skips the body, the file's mtime pays the debt.
        log = self.log_file("fleet")
        log.parent.mkdir(parents=True)
        log.write_text(f"# {self.today}\n\n## 15:10 — Triage skips empty clusters\n")
        self.assertIsNone(self.stop("e3", self.feedback(out["reason"])
                                    + self.tool("Bash", {"command": f"cat >> {log} <<'EOF'\n## 15:10 — Triage\nrm -rf {self.root}/api\nEOF"})
                                    + self.say("Wrote the fleet changelog entry."), active=True))

    def test_example_4_the_waiver(self):
        self.config("require")
        self.assertIsNone(self.stop("e4", self.user("Quick experiment, no changelog: bump the log level")
                                    + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")})
                                    + self.say("Done.")))

    # ------------------------------------------------------------ modes and edge cases

    def test_require_gives_up_after_max_blocks(self):
        self.config("require")
        turn = self.user("Change it") + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")}) + self.say("Changed.")
        first = self.stop("m", turn)
        self.assertEqual(first["decision"], "block")
        second = self.stop("m", self.feedback(first["reason"]) + self.say("Still working on it."), active=True)
        self.assertEqual(second["decision"], "block")
        third = self.stop("m", self.feedback(second["reason"]) + self.say("I will skip it."), active=True)
        self.assertEqual(third, {"systemMessage": "Deskmate: still no changelog entry today for api."})

    def test_stop_hook_active_in_remind_mode_says_nothing(self):
        self.config("remind")
        turn = (self.user("Fix it") + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")})
                + self.tool("Bash", {"command": "git -C api commit -qam fix"}) + self.say("Fixed."))
        self.assertIsNone(self.stop("a", turn, active=True))

    def test_subagent_sidechain_is_not_counted(self):
        self.config("require")
        self.assertIsNone(self.stop("sc", self.user("Delegate the fix")
                                    + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")}, sidechain=True)
                                    + self.tool("Bash", {"command": "git -C api commit -qam fix"}, sidechain=True)
                                    + self.say("The subagent fixed it.")))
        turn = self.user("Fix it yourself") + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")}) + self.say("Done.")
        self.assertIsNone(self.stop("sc2", turn, agent_id="agent-1", agent_type="backend-engineer"))

    def test_failed_tool_calls_do_not_count(self):
        self.config("require")
        self.assertIsNone(self.stop("f", self.user("Try to change it")
                                    + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")}, error=True)
                                    + self.say("The edit failed.")))

    def test_skipped_hidden_and_loose_paths(self):
        self.config("require", skip_repos=["api"])
        (self.root / ".claude" / ".git").mkdir(parents=True)
        self.assertIsNone(self.stop("k", self.user("Edit around")
                                    + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")})
                                    + self.tool("Write", {"file_path": str(self.root / ".claude/agents/x.md")})
                                    + self.tool("Write", {"file_path": str(self.root / "CLAUDE.md")})
                                    + self.tool("Write", {"file_path": str(self.tmp / "elsewhere/notes.md")})
                                    + self.say("Done.")))

    def test_bash_commands_that_change_files(self):
        self.config("require")
        cases = {
            "echo hi > api/out.txt": "out.txt",
            "printf x | tee -a api/log.txt >/dev/null": "log.txt",
            "cp /etc/hostname api/copied.txt": "copied.txt",
            "sudo mv api/app/receipt.py api/app/receipt2.py": "app/receipt.py",
            "git -C api stash pop": None,
            "touch 'api/with space.txt'": "with space.txt",
        }
        for i, (command, changed) in enumerate(cases.items()):
            out = self.stop(f"b{i}", self.user("Do it") + self.tool("Bash", {"command": command}) + self.say("Done."))
            self.assertIsNotNone(out, command)
            self.assertIn("missing for api", out["reason"], command)
            if changed:
                self.assertIn(changed, out["reason"], command)
        for i, command in enumerate(["git -C api status && git -C api diff", "ls api > /dev/null 2>&1",
                                     "grep -r 'a > b' api", "cat api/app/receipt.py | wc -l", "cd api && pytest -q",
                                     "git -C api pull --ff-only", "echo $HOME > $OUT"]):
            self.assertIsNone(self.stop(f"n{i}", self.user("Look") + self.tool("Bash", {"command": command})
                                        + self.say("Looked.")), command)

    def test_root_inside_a_bigger_work_tree(self):
        mono = self.folder("mono")
        (mono / ".git").mkdir()
        services = mono / "services"
        (services / "billing").mkdir(parents=True)
        self.root = services
        self.config("require", work_tree=str(mono))
        out = self.stop("w", self.user("Fix billing") + self.tool("Edit", {"file_path": str(services / "billing/x.py")},
                                                                    cwd=services) + self.say("Fixed."))
        self.assertIn("missing for mono", out["reason"])
        self.assertIn("(changed: services/billing/x.py)", out["reason"])

    def test_bail_outs(self):
        self.config("require")
        turn = self.user("Change it") + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")}) + self.say("Changed.")
        self.assertIsNone(self.stop("p", turn, permission_mode="plan"))
        self.assertIsNone(self.stop("p2", turn, hook_event_name="SubagentStop"))
        for stdin in ("", "not json", "[1, 2]", "{}"):
            code, out, err, _ = self.hook(None, stdin=stdin)
            self.assertEqual((code, out), (0, ""), stdin)
        code, out, _, _ = self.hook(dict(self.event("p"), transcript_path=str(self.tmp / "missing.jsonl")))
        self.assertEqual((code, out), (0, ""))
        self.config("off")
        self.assertIsNone(self.stop("o", turn))
        self.cfg.unlink()
        self.assertIsNone(self.stop("o2", turn))
        self.cfg.write_text("{broken")
        self.assertIsNone(self.stop("o3", turn))

    def test_state_files_are_private(self):
        self.config("remind")
        self.stop("priv", self.user("Hi") + self.say("Hello."))
        mode = (self.state / "priv.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_waits_for_a_transcript_written_late(self):
        self.config("require")
        sid = "late"
        path = self.tmp / f"{sid}.jsonl"
        early = self.user("Change it") + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")})
        with open(path, "w") as f:
            for e in early:
                f.write(json.dumps(e) + "\n")
        final = self.say("Changed it, finally.")
        ev = dict(self.event(sid), last_assistant_message="Changed it, finally.")

        def write_late():
            time.sleep(0.4)
            with open(path, "a") as f:
                f.write(json.dumps(final[0]) + "\n")

        t = threading.Thread(target=write_late)
        t.start()
        code, out, err, ms = self.hook(ev)
        t.join()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertGreaterEqual(ms, 350)

    def test_speed_on_a_large_transcript(self):
        self.config("remind")
        sid = "big"
        filler = "y" * 50_000
        with open(self.tmp / f"{sid}.jsonl", "w") as f:
            for i in range(400):  # about 20 MB of earlier turns
                for e in self.user(f"turn {i}") + self.say(filler):
                    f.write(json.dumps(e) + "\n")
        for e in self.user("Fix it") + self.tool("Edit", {"file_path": str(self.root / "api/app/receipt.py")}) + \
                self.tool("Bash", {"command": "git -C api commit -qam fix"}) + self.say("Fixed."):
            with open(self.tmp / f"{sid}.jsonl", "a") as f:
                f.write(json.dumps(e) + "\n")
        size = (self.tmp / f"{sid}.jsonl").stat().st_size
        self.assertGreater(size, 19_000_000)
        times = []
        for _ in range(3):
            (self.state / f"{sid}.json").unlink() if (self.state / f"{sid}.json").exists() else None
            code, out, err, ms = self.hook(self.event(sid))
            self.assertEqual(code, 0, err)
            self.assertIn("missing for api", out)
            times.append(ms)
        print(f"\n  habits-stop on a {size // 1_000_000} MB transcript: {', '.join(f'{t:.0f} ms' for t in times)}",
              file=sys.stderr)
        self.assertLess(min(times), 1000)


if __name__ == "__main__":
    unittest.main()
