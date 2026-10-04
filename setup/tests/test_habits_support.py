"""Shared fixtures for the habits tests: a sandbox HOME (so nothing real is touched) and fixture folders.

Every path a test may write is under a temp folder: HOME, the XDG folders, CLAUDE_CONFIG_DIR and
DESKMATE_DATA_DIR all point there, and git gets a fake identity and no global config. The fixture
person is Alex (/home/alex style paths only inside the sandbox).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SETUP = Path(__file__).resolve().parents[1]
REPO = SETUP.parent
if str(SETUP) not in sys.path:
    sys.path.insert(0, str(SETUP))

from deskmate_setup import habits  # noqa: E402

GIT = shutil.which("git")
HOOK = REPO / "plugin" / "habits" / "bin" / "habits-stop"


class Sandbox(unittest.TestCase):
    """A TestCase whose environment points every config and data folder into a temp folder."""

    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-habits-"))
        self.home = self.tmp / "home" / "alex"
        self.home.mkdir(parents=True)
        (self.tmp / "gitconfig").write_text("")
        env = {
            "HOME": str(self.home),
            "USER": "alex",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "DESKMATE_DATA_DIR": str(self.tmp / "data"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
            "GIT_AUTHOR_NAME": "Alex",
            "GIT_AUTHOR_EMAIL": "alex@example.com",
            "GIT_COMMITTER_NAME": "Alex",
            "GIT_COMMITTER_EMAIL": "alex@example.com",
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        for key in ("DESKMATE_OWNER", "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "GIT_DIR", "GIT_WORK_TREE"):
            os.environ.pop(key, None)
        # The plugin is installed through claude_connect (another module); tests watch the calls instead.
        self.plugin_calls = []
        self._ops = mock.patch.object(habits, "_plugin_ops", lambda: (
            lambda name, emit: self.plugin_calls.append(("install", name)),
            lambda name, emit: self.plugin_calls.append(("uninstall", name))))
        self._ops.start()
        self.events = []

    def tearDown(self):
        self._ops.stop()
        self._env.stop()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # ------------------------------------------------------------ helpers

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def texts(self, level: str | None = None) -> list:
        return [e["text"] for e in self.events if level is None or e["level"] == level]

    def folder(self, name: str = "work") -> Path:
        p = self.tmp / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def repo(self, path: Path, real_git: bool = True) -> Path:
        """A git repo: `git init` when git is installed (and asked for), else a bare .git folder."""
        path.mkdir(parents=True, exist_ok=True)
        if real_git and GIT:
            subprocess.run([GIT, "init", "-q", str(path)], check=True)
        else:
            (path / ".git").mkdir(exist_ok=True)
        return path

    def git(self, *args, cwd=None) -> str:
        return subprocess.run([GIT] + list(args), cwd=str(cwd) if cwd else None, check=True,
                              capture_output=True, text=True).stdout

    def values(self, folders, **kw) -> dict:
        v = {"HABITS": "on", "HABITS_FOLDERS": [str(f) for f in folders], "DESKMATE_OWNER": "Alex",
             "NOTIFY_KIND": "none", "HABITS_PLUGIN": "off"}
        v.update(kw)
        return v

    def options(self, folder: Path, **opts) -> dict:
        return {"HABITS_FOLDER_OPTIONS": {str(folder): opts}}

    def read(self, path) -> str:
        return Path(path).read_text(encoding="utf-8")

    def record(self) -> dict:
        return habits._read_json(habits.installed_path()) or {}
