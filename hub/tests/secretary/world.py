"""A small fake world for the secretary tests: Alex's home with Claude Code transcripts, a docs hub,
memory notes and git repos. Every secret in here is fake and planted on purpose: the tests check that
none of them reaches SQLite or a model call.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = "America/St_Johns"  # an odd offset (-03:30 / -02:30) catches day-boundary mistakes

SECRETS = [
    "FAKEfake0123456789secretTOKEN",                                  # API_TOKEN=… in a prompt
    "000000000000000000/FAKEfakeFAKEfakeFAKEfakeFAKE01",                 # webhook path in a Bash command
    "sk-ant-oat01-FAKEfakeFAKEfakeFAKEfakeFAKEfake0123",                # a token in assistant text
    "FakePg456word",                                                    # PGPASSWORD=… in assistant text
    "FakeUrlPass789",                                                   # postgres://u:…@db in a command
    "FakeMemoryPw4567",                                                 # in a memory note
    "FakeChangelogPw987",                                               # in a changelog entry
]
NEVER_READ = ["OUTSIDE_SECRET_FAKE_123456", "SUBAGENT_SECRET_FAKE_998877", "PRIVATE_MEMORY_FAKE_445566",
              "WORKOLD_SECRET_FAKE_112233"]


def slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def noon_today() -> float:
    """Local noon of today: the world's events then fall inside today whatever the time of the run."""
    d = dt.datetime.now(ZoneInfo(TZ)).date()
    return dt.datetime.combine(d, dt.time(12, 0), ZoneInfo(TZ)).timestamp()


def hhmm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, ZoneInfo(TZ)).strftime("%H:%M")


def local_day(ts: float, delta: int = 0) -> str:
    return (dt.datetime.fromtimestamp(ts, ZoneInfo(TZ)).date() + dt.timedelta(days=delta)).isoformat()


class Transcript:
    """Builds Claude Code transcript records the way Claude Code 2.1.28x writes them."""

    def __init__(self, sid: str, cwd: str, t0: float) -> None:
        self.sid, self.cwd, self.t = sid, cwd, t0
        self.records: list[dict] = []

    def tick(self, s: float = 20.0) -> float:
        self.t += s
        return self.t

    def _env(self, typ: str, **kw) -> dict:
        r = {"parentUuid": None, "isSidechain": False, "type": typ, "uuid": str(uuid.uuid4()),
             "timestamp": iso(self.tick(kw.pop("dt", 20.0))), "userType": "external", "entrypoint": "claude-vscode",
             "cwd": kw.pop("cwd", self.cwd), "sessionId": self.sid, "version": "2.1.287", "gitBranch": "main"}
        r.update(kw)
        self.records.append(r)
        return r

    def prompt(self, text: str, images: int = 0):
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}] * images
        content = content + [{"type": "text", "text": text}]
        return self._env("user", message={"role": "user", "content": content}, origin={"kind": "human"},
                         promptId=str(uuid.uuid4()))

    def slash(self, name: str, args: str = ""):
        text = f"<command-name>/{name}</command-name>\n<command-message>{name}</command-message>\n<command-args>{args}</command-args>"
        return self._env("user", message={"role": "user", "content": text}, origin={"kind": "human"})

    def command_record(self, name: str):  # older form: no origin
        text = f"<command-name>/{name}</command-name>\n<command-message>{name}</command-message>\n<command-args></command-args>"
        return self._env("user", message={"role": "user", "content": text})

    def text(self, msg: str, body: str, model: str = "claude-opus-5-5", stop: str = "end_turn"):
        return self._env("assistant", message={"model": model, "id": msg, "type": "message", "role": "assistant",
                                               "content": [{"type": "text", "text": body}], "stop_reason": stop})

    def thinking(self, msg: str):
        return self._env("assistant", message={"model": "claude-opus-5-5", "id": msg, "type": "message",
                                               "role": "assistant", "content": [{"type": "thinking", "thinking": "",
                                                                                 "signature": "c2ln"}],
                                               "stop_reason": "tool_use"})

    def tool(self, msg: str, tid: str, name: str, inp: dict):
        return self._env("assistant", message={"model": "claude-opus-5-5", "id": msg, "type": "message",
                                               "role": "assistant", "content": [{"type": "tool_use", "id": tid,
                                                                                 "name": name, "input": inp}],
                                               "stop_reason": "tool_use"})

    def result(self, tid: str, content: str, is_error: bool = False, tur=None, denial: str | None = None, dt_s=5.0):
        kw = {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content,
                                                       "is_error": is_error}]}, "dt": dt_s}
        if tur is not None:
            kw["toolUseResult"] = tur
        if denial:
            kw["toolDenialKind"] = denial
        return self._env("user", **kw)

    def queued(self, text: str):
        return self._env("attachment", attachment={"type": "queued_command", "prompt": [{"type": "text", "text": text}],
                                                   "commandMode": "prompt", "origin": {"kind": "human"},
                                                   "humanTurn": True, "timestamp": iso(self.t)})

    def notification(self, summary: str):
        text = (f"<task-notification><task-id>b1</task-id><tool-use-id>t8</tool-use-id><status>completed</status>"
                f"<summary>{summary}</summary></task-notification>")
        return self._env("user", message={"role": "user", "content": text}, origin={"kind": "task-notification"})

    def compact(self, trigger: str, pre: int, post: int):
        r = self._env("system", subtype="compact_boundary", content="Conversation compacted", level="info",
                      compactMetadata={"trigger": trigger, "preTokens": pre, "postTokens": post})
        self._env("user", message={"role": "user", "content": "This session is being continued…"},
                  isCompactSummary=True, isVisibleInTranscriptOnly=True)
        return r

    def interrupt(self):
        return self._env("user", message={"role": "user", "content": [{"type": "text",
                                                                        "text": "[Request interrupted by user]"}]})

    def synthetic(self, body: str, error: bool):
        kw = {"message": {"model": "<synthetic>", "id": f"syn-{len(self.records)}", "type": "message",
                          "role": "assistant", "content": [{"type": "text", "text": body}], "stop_reason": "stop_sequence"}}
        if error:
            kw.update(isApiErrorMessage=True, error="rate_limit")
        return self._env("assistant", **kw)

    def meta(self, rtype: str, **kw):
        r = {"type": rtype, "sessionId": self.sid, **kw}
        self.records.append(r)
        return r

    def sidechain_prompt(self, text: str):
        r = self._env("user", message={"role": "user", "content": text}, origin={"kind": "human"})
        r["isSidechain"] = True
        return r

    def write(self, path: Path, partial_tail: bool = False) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(json.dumps(r) + "\n" for r in self.records)
        if partial_tail:
            data += '{"type": "assistant", "message": {"content": [{"type": "text", "text": "half writ'
        path.write_text(data)
        return path


def git(cwd: Path, *args: str, when: float | None = None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="Alex", GIT_AUTHOR_EMAIL="alex@example.com", GIT_COMMITTER_NAME="Alex",
               GIT_COMMITTER_EMAIL="alex@example.com", GIT_CONFIG_NOSYSTEM="0", HOME=str(cwd))
    if when is not None:
        stamp = f"@{int(when)} +0000"
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    r = subprocess.run(["git", "-c", "user.name=Alex", "-c", "user.email=alex@example.com", "-c", "init.defaultBranch=main",
                        "-c", "safe.directory=*", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def make_repo(path: Path, files: dict[str, str], when: float, message: str = "init") -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "alex@example.com")
    for name, body in files.items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message, when=when)


class World:
    def __init__(self, base: Path, now: float | None = None) -> None:
        self.base = base
        self.now = now or noon_today()
        self.home = base / "home" / "alex"
        self.config_dir = self.home / ".claude"
        self.root = self.home / "work"
        self.docs = self.root / "notes"
        self.shop = self.root / "shop"
        self.tools = self.root / "tools"
        self.lib = self.root / "lib"
        self.today = local_day(self.now)
        self.yesterday = local_day(self.now, -1)
        self.sid_a = "aaaaaaaa-1111-4222-8333-444444444444"
        self.sid_b = "bbbbbbbb-1111-4222-8333-444444444444"
        self.sid_c = "cccccccc-1111-4222-8333-444444444444"
        self.sid_d = "dddddddd-1111-4222-8333-444444444444"
        self.paths: dict[str, Path] = {}

    # ----------------------------------------------------------------- transcripts

    def session_a(self) -> Transcript:
        """The main fixture session in ~/work: every item, step and link kind the parser knows."""
        root = str(self.root)
        t = Transcript(self.sid_a, root, self.now - 3 * 3600)
        t.meta("ai-title", aiTitle="Shop checkout fixes")
        t.prompt("Fix the checkout total. My token is API_TOKEN=FAKEfake0123456789secretTOKEN and post to "
                 "https://discord.com/api/webhooks/000000000000000000/FAKEfakeFAKEfakeFAKEfakeFAKE01 when done.")
        t.thinking("m1")
        t.text("m1", "Looking at the checkout code first.", stop="tool_use")
        t.tool("m1", "t1", "Read", {"file_path": f"{root}/shop/src/total.py"})
        t.result("t1", "def total(): ...", tur={"type": "text", "file": {"filePath": f"{root}/shop/src/total.py"}})
        t.tool("m2", "t2", "Edit", {"file_path": f"{root}/shop/src/total.py", "old_string": "a", "new_string": "b"})
        t.result("t2", "The file has been updated.", tur={"filePath": f"{root}/shop/src/total.py"})
        t.queued("also check the tax rounding")
        t.tool("m3", "t3", "Bash", {"command": "cd shop && pytest -q", "description": "Run the shop tests"})
        t.result("t3", "Exit code 1\nFAILED tests/test_total.py::test_tax", is_error=True,
                 tur="Error: Exit code 1\nFAILED tests/test_total.py::test_tax", dt_s=14.0)
        t.tool("m4", "t4", "Bash", {"command": f"git -C {root}/shop commit -q -m 'fix: checkout total' && "
                                               f"git -C {root}/shop log --oneline -1", "description": "Commit the fix"})
        t.result("t4", "abc1234 fix: checkout total", tur={"stdout": "abc1234 fix: checkout total"})
        t.tool("m5", "t5", "Bash", {"command": '~/.config/claude-notify/notify.sh "Checkout total fixed"'})
        t.result("t5", "204", tur={"stdout": "204"})
        t.tool("m6", "t6", "Bash", {"command": "psql postgres://shop:FakeUrlPass789@db:5432/shop -c 'select 1'",
                                    "description": "Check the database"})
        t.result("t6", "1", tur={"stdout": "1"})
        t.text("m7", "Fixed the total; one tax test still fails.")
        t.text("m7", "Run it with PGPASSWORD=FakePg456word and the key sk-ant-oat01-FAKEfakeFAKEfakeFAKEfakeFAKEfake0123.")
        t.slash("model", "opus")
        t.compact("manual", 148000, 12000)
        t.command_record("compact")
        t.prompt("Publish the prototype and ask me about the tax rule", images=1)
        t.tool("m8", "t7", "Artifact", {"action": "publish", "file_path": f"{root}/shop/proto.html",
                                        "label": "Checkout prototype"})
        t.result("t7", "Published https://claude.ai/artifact/FAKEartifact0000000001",
                 tur={"url": "https://claude.ai/artifact/FAKEartifact0000000001", "title": "Checkout prototype"})
        t.tool("m8", "t8", "AskUserQuestion", {"questions": [{"question": "Round per line?"}]})
        t.result("t8", "Per line", tur={"questions": [], "answers": {"Round per line?": "yes"}})
        t.tool("m9", "t9", "Agent", {"subagent_type": "general-purpose", "description": "Scan the logs",
                                     "prompt": "scan", "run_in_background": True})
        t.result("t9", "Async agent launched. agentId: a1", tur={"status": "async_launched", "agentId": "a1"})
        t.tool("m9", "t10", "Bash", {"command": "rm -rf /tmp/shop-cache", "description": "Clear the cache"})
        t.result("t10", "The user doesn't want to proceed with this tool use.", is_error=True, denial="user-rejected")
        t.tool("m9", "t11", "Bash", {"command": "sleep 100", "description": "Wait"})
        t.result("t11", "Interrupted", tur={"interrupted": True, "stdout": ""})
        t.notification("Scan the logs finished")
        t.interrupt()
        t.synthetic("You've hit your session limit · resets 4pm", error=True)
        t.synthetic("No response requested.", error=False)
        t.meta("frame-link", frameUrl="https://claude.ai/artifact/FAKEframe00000000000001", title="Tax table")
        t.meta("pr-link", prUrl="https://github.com/example/shop/pull/7", prNumber=7, prRepository="example/shop")
        t.prompt("Push it")
        t.tool("m10", "t12", "Bash", {"command": f"git -C {root}/shop push", "description": "Push main"})
        t.result("t12", "To example\n   abc1234..def5678  main -> main",
                 tur={"stdout": "", "gitOperation": {"push": {"branch": "main"}}})
        t.tool("m10", "t13", "Write", {"file_path": f"{self.docs}/changelog/shop/{self.today}.md", "content": "x"})
        t.result("t13", "File created", tur={"type": "update", "filePath": f"{self.docs}/changelog/shop/{self.today}.md"})
        t.tool("m10", "t14", "server_tool", {"query": "deferred tools"})
        t.records[-1]["message"]["content"][0]["type"] = "server_tool_use"
        t.sidechain_prompt("A sidechain prompt that must not show")
        t.text("m11", "Pushed main and wrote the changelog entry. " + "Details follow. " * 120)
        t.meta("custom-title", customTitle="Checkout total and prototype")
        return t

    def session_b(self) -> Transcript:
        """A small session in ~/work/shop: under the pre-filter's threshold."""
        t = Transcript(self.sid_b, str(self.shop), self.now - 2 * 3600)
        t.prompt("What does total() return?")
        t.text("n1", "It returns the order total in cents.")
        return t

    def session_c(self) -> Transcript:
        """Started in the home folder, outside the roots: never read."""
        t = Transcript(self.sid_c, str(self.home), self.now - 3600)
        t.prompt("Remember OUTSIDE_SECRET_FAKE_123456 for me")
        t.text("o1", "Noted OUTSIDE_SECRET_FAKE_123456.")
        return t

    def session_d(self) -> Transcript:
        """Started in ~/work-old: its folder name starts like ~/work's, but it is outside the roots."""
        t = Transcript(self.sid_d, str(self.home / "work-old"), self.now - 3600)
        t.prompt("WORKOLD_SECRET_FAKE_112233 is the code")
        t.text("w1", "Fine.")
        return t

    def transcript_path(self, cwd: Path | str, sid: str) -> Path:
        return self.config_dir / "projects" / slug(str(cwd)) / f"{sid}.jsonl"

    def write_transcripts(self) -> None:
        a = self.session_a()
        self.paths["a"] = a.write(self.transcript_path(self.root, self.sid_a), partial_tail=True)
        self.paths["b"] = self.session_b().write(self.transcript_path(self.shop, self.sid_b))
        self.paths["c"] = self.session_c().write(self.transcript_path(self.home, self.sid_c))
        self.paths["d"] = self.session_d().write(self.transcript_path(self.home / "work-old", self.sid_d))
        sub = self.transcript_path(self.root, self.sid_a).with_suffix("") / "subagents" / "agent-a1.jsonl"
        sub.parent.mkdir(parents=True, exist_ok=True)
        sub.write_text(json.dumps({"type": "user", "isSidechain": True, "sessionId": self.sid_a, "cwd": str(self.root),
                                   "message": {"role": "user", "content": "SUBAGENT_SECRET_FAKE_998877"}}) + "\n")

    # ----------------------------------------------------------------- memory

    def write_memory(self) -> None:
        mem = self.config_dir / "projects" / slug(str(self.root)) / "memory"
        mem.mkdir(parents=True, exist_ok=True)
        old = dt.datetime.fromtimestamp(self.now - 10 * 86400, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        (mem / "MEMORY.md").write_text("- [Ship status](ship-status.md) — what is built and what is not pushed\n")
        (mem / "ship-status.md").write_text(
            "---\nname: ship-status\ndescription: The checkout fix is built but not pushed yet; it waits on review.\n"
            f"metadata:\n  type: project\n  originSessionId: {self.sid_a}\n  modified: {old}\n---\n\n"
            "The checkout design (design/checkout) is built. Nothing is pushed. The staging password is "
            "DB_PASSWORD=FakeMemoryPw4567 for now.\n")
        (mem / "loose.md").write_text("---\nname: loose\ndescription: A note that MEMORY.md does not list.\n---\nBody.\n")
        private = self.config_dir / "projects" / slug(str(self.home)) / "memory"
        private.mkdir(parents=True, exist_ok=True)
        (private / "MEMORY.md").write_text("- [p](private.md)\n")
        (private / "private.md").write_text("---\nname: private\ndescription: PRIVATE_MEMORY_FAKE_445566 not pushed\n---\n")

    # ----------------------------------------------------------------- docs hub and repos

    def write_docs(self) -> None:
        d = self.docs
        mon_day = dt.date.fromisoformat(self.today)
        when = f"{mon_day:%b} {mon_day.day}"
        files = {
            "README.md": "# Notes\n",
            "changelog/README.md": "# Changelog\n\n| Folder | Repo |\n|---|---|\n| shop | `work/shop` |\n| tools | `work/tools` |\n",
            f"changelog/shop/{self.today}.md": (
                f"# {self.today}\n\n## {hhmm(self.now - 3 * 3600 + 600)} — Checkout total no longer rounds twice\n\n**Type:** fix\n"
                "**Scope:** shop backend\n**Design doc:** design/checkout/00-overview.md\n"
                "**Branch / commit:** main abc1234, deployed\n\n### What changed\n- Totals in cents.\n"
                "- The test database used DB_PASSWORD=FakeChangelogPw987 while debugging.\n\n"
                "### Follow-ups / risks\n- Check the tax rounding on a device.\n- ~~Old idea~~ Withdrawn: see below.\n"
                f"- None.\n\n## {hhmm(self.now - 3 * 3600 + 600)} — Same minute, second entry\n\n**Type:** chore\n\n### What changed\n- Tidy.\n\n"
                "## An untimed entry: with a colon\n\n**Type:** feature\n\n### Follow-ups\n- Nothing pushed yet for the "
                "admin side.\n"),
            f"changelog/shop/{self.yesterday}.md": (
                f"# {self.yesterday}\n\n## 23:10 — Late fix\n\n**Type:** fix\n\n### Follow-ups / risks\n- None.\n\n"
                f"## 00:30 ({when}) — After midnight\n\n**Type:** fix\n**Branch:** main, not pushed\n\n"
                "### Follow-ups / risks\n- Push main.\n"),
            "changelog/tools/2020-01-01.md": "# 2020-01-01\n\n## 09:00 — Start\n\n**Type:** chore\n",
            "design/README.md": (
                "# Design\n\n## Index\n\n| Feature / module | Status | Projects touched |\n|---|---|---|\n"
                f"| [Checkout](checkout/00-overview.md) | built {self.yesterday}, not pushed | shop, reads notes |\n"
                "| [Gone feature](gone/00-overview.md) | draft | tools |\n"),
            "design/checkout/00-overview.md": (
                "# Checkout: totals and tax\n\n"
                f"**Status:** in progress ({self.yesterday}). Totals built\n"
                "**Prototype:** https://claude.ai/artifact/FAKEproto000000000001\n\n## 1. Problem\n\nText.\n\n"
                "## 7. Decisions and open questions\n\n| Date | Decision / question | Outcome |\n|---|---|---|\n"
                f"| {self.today} | **D1.** Totals are computed in cents | Decided. |\n"
                "| | ❓ **Q1.** Should tax round per line? | Open |\n"),
            "design/orphan/00-overview.md": "# Orphan idea\n\n**Status:** draft\n",
        }
        make_repo(d, files, self.now - 600, "docs")

    def write_repos(self) -> None:
        make_repo(self.shop, {"src/total.py": "def total():\n    return 0\n"}, self.now - 1800, "fix: checkout total")
        remote = self.home / "remotes" / "shop.git"
        remote.mkdir(parents=True, exist_ok=True)
        git(remote, "init", "-q", "--bare")
        git(self.shop, "remote", "add", "origin", str(remote))
        git(self.shop, "push", "-q", "origin", "main")
        git(self.shop, "checkout", "-q", "-b", "feature/tax")
        (self.shop / "src" / "tax.py").write_text("RATE = 13\n")
        git(self.shop, "add", "-A")
        git(self.shop, "commit", "-q", "-m", "feat: tax rounding", when=self.now - 900)
        make_repo(self.tools, {"run.sh": "echo hi\n"}, self.now - 1200, "feat: a tool")
        make_repo(self.lib, {"lib.py": "X = 1\n"}, self.now - 1300, "feat: a lib")
        wt = self.root / "shop-wt" / "tax"
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(self.shop, "worktree", "add", "-q", str(wt), "main")

    def build(self) -> "World":
        self.root.mkdir(parents=True, exist_ok=True)
        self.write_docs()
        self.write_repos()
        self.write_memory()
        self.write_transcripts()
        return self
