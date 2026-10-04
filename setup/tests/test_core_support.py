"""Shared fixtures for the core tests (settings, detect, compose, steps, term, doctor, cli).

Every test runs against a fake home, fake XDG folders, a fake Claude Code folder and a temporary repo
folder, with Docker and Claude Code detection replaced. Nothing here touches the real .env, the real
~/.claude or a container. Test data uses the name Alex and obviously fake tokens.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deskmate_setup  # noqa: E402
from deskmate_setup import compose, detect, settings, util  # noqa: E402

REAL_REPO = Path(__file__).resolve().parents[2]
FAKE_TOKEN = "sk-ant-oat01-" + "FAKEfakeFAKEfake" * 4 + "Qx7A"
FAKE_HUB_TOKEN = "fake-hub-token-0123456789abcdefghijklmnop"
FAKE_WEBHOOK = "https://discord.com/api/webhooks/123456789/FAKE-webhook-token-abcdef"

DOCKER_OK = {"installed": True, "running": True, "version": "29.1.0", "desktop": False, "rootless": False,
             "compose_version": "2.40.0", "sudo_needed": False, "memory_gb": 8.0, "label": "Docker Engine 29.1.0 is running",
             "fix": "", "path": "/nonexistent/docker", "os_name": "Fake Linux", "context": "default", "root_dir": "",
             "desktop_version": "", "error": ""}
CLAUDE_NONE = {"installed": False, "path": "", "version": ""}
CLAUDE_OK = {"installed": True, "path": "/nonexistent/claude", "version": "2.1.278", "answers": True}


def transcript(cwd: str, extra_lines: int = 0) -> str:
    """A tiny Claude Code transcript whose first record carries the session's working folder."""
    lines = [json.dumps({"type": "summary", "summary": "x"})]
    lines += [json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}})]
    lines += [json.dumps({"type": "assistant", "cwd": cwd})] * extra_lines
    return "\n".join(lines) + "\n"


class CoreCase(unittest.TestCase):
    """A sandbox: fake HOME/XDG/CLAUDE_CONFIG_DIR, a temp repo dir, fake Docker and Claude Code."""

    docker = DOCKER_OK
    claude = CLAUDE_NONE

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-core-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.home = self.tmp / "home" / "alex"
        self.home.mkdir(parents=True)
        (self.home / ".gitconfig").write_text("[user]\n\tname = Alex Doe\n")
        self.repo = self.tmp / "deskmate"
        self.repo.mkdir()
        self.claude_dir = self.home / ".claude"
        (self.claude_dir / "projects").mkdir(parents=True)
        env = {"HOME": str(self.home), "USER": "alex", "LOGNAME": "alex", "TZ": "Europe/London",
               "XDG_DATA_HOME": str(self.home / ".local" / "share"), "XDG_CONFIG_HOME": str(self.home / ".config"),
               "GIT_CONFIG_NOSYSTEM": "1", "NO_COLOR": "1"}
        for k in ("CLAUDE_CONFIG_DIR", "SSH_CONNECTION", "SSH_TTY", "CLAUDE_CODE_OAUTH_TOKEN", "NOTIFY_WEBHOOK_URL",
                  "NOTIFY_URL", "CLAUDE_CODE_OAUTH_TOKEN_FILE", "NOTIFY_WEBHOOK_FILE", "GIT_CONFIG_GLOBAL"):
            env[k] = ""
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ("CLAUDE_CONFIG_DIR", "SSH_CONNECTION", "SSH_TTY", "CLAUDE_CODE_OAUTH_TOKEN", "NOTIFY_WEBHOOK_URL",
                  "NOTIFY_URL", "CLAUDE_CODE_OAUTH_TOKEN_FILE", "NOTIFY_WEBHOOK_FILE", "GIT_CONFIG_GLOBAL"):
            os.environ.pop(k, None)
        self.patch(settings, "_REPO", self.repo)
        detect.clear_cache()
        self.addCleanup(detect.clear_cache)
        self.patch(detect, "docker", lambda timeout=10.0: dict(self.docker))
        self.patch(detect, "claude_cli", lambda: dict(self.claude))
        self.patch(detect, "own_stack", lambda repo, project: {"exists": False, "running": False, "working_dir": "",
                                                                 "containers": [], "other_dir": ""})
        self.patch(detect, "project_name", lambda repo, user="": "deskmate")
        # Never open a real browser from a test.
        self.opened = []
        self.patch(util, "can_open_browser", lambda: False)
        self.patch(util, "open_url", lambda url: self.opened.append(url) or True)
        compose.reset_cancel()

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def fake_module(self, name: str, module):
        """Stand in for another role's module (claude_connect, habits), however it is imported."""
        p1 = mock.patch.dict(sys.modules, {f"deskmate_setup.{name}": module})
        p2 = mock.patch.object(deskmate_setup, name, module, create=True)
        p1.start()
        p2.start()
        self.addCleanup(p2.stop)
        self.addCleanup(p1.stop)

    # ---- fixtures
    def add_session(self, cwd: str, name: str = "s1", folder: str = "-proj", config: Path | None = None):
        d = (config or self.claude_dir) / "projects" / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.jsonl").write_text(transcript(cwd))
        return d / f"{name}.jsonl"

    def projects(self) -> Path:
        p = self.home / "Projects"
        (p / "app" / ".git").mkdir(parents=True, exist_ok=True)
        return p

    def env_text(self) -> str:
        return (self.repo / ".env").read_text()

    def run_cli(self, argv, stdin: str = ""):
        from deskmate_setup import cli

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


def mode(path) -> int:
    return os.stat(str(path)).st_mode & 0o777
