"""Fixtures for the secretary tests: a fake world per test, the hub config pointed at it, and a stub
model layer (the real one, llm.py, is tested on its own in test_llm*.py)."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from world import TZ, World  # noqa: E402


class FakeLLM:
    """Stands in for app.secretary.llm: records what it was given, answers with fixed shapes."""

    def __init__(self, available: bool = True, util5: float = 0.2) -> None:
        self._available = available
        self.util5 = util5
        self.calls: list[tuple] = []
        self.fail_digest = False

    def available(self) -> bool:
        return self._available

    def usage_state(self) -> dict:
        now = int(time.time())
        return {"five_hour": {"utilization": self.util5, "resets_at": now + 3600},
                "seven_day": {"utilization": 0.1, "resets_at": now + 86400 * 3},
                "paused": False, "reason": "", "ts": time.time()}

    async def probe_usage(self) -> None:
        self.calls.append(("probe",))

    async def digest_session(self, condensed: dict) -> dict:
        self.calls.append(("digest", condensed))
        if self.fail_digest:
            raise RuntimeError("model said no")
        return {"title": "Checkout total fixed", "repos": ["shop", "made-up-repo"], "outcome": "shipped",
                "summary": "Fixed the checkout total. One tax test still fails.",
                "shipped": ["Checkout total in cents"], "decisions": ["Totals are kept in cents"],
                "open_loops": [{"text": "Fix the tax rounding test", "owner": "claude"},
                               {"text": "Review the tax rule", "owner": "you"}],
                "blockers": [], "links": ["https://claude.ai/artifact/FAKEartifact0000000001", "https://evil.example/x"],
                "usage": {"input_tokens": 1}}

    async def daily_brief(self, material: dict) -> dict:
        self.calls.append(("brief", material))
        return {"headline": "Checkout fixed; tax still open", "lede": "The checkout total ships in cents.",
                "discord_text": "Yesterday: checkout total fixed. 1 thing waits on you.", "usage": {}}

    async def ask(self, question: str, roots: list[str], deny_globs: list[str], max_turns: int = 8) -> dict:
        """The real llm.ask's shape: citations relative to their root, with the root and the absolute path."""
        self.calls.append(("ask", question, roots, deny_globs, max_turns))
        cites = []
        cite = getattr(self, "cite", None)
        if cite:
            root = max((r for r in roots if cite.startswith(r.rstrip("/") + "/")), key=len)
            cites.append({"path": os.path.relpath(cite, root), "line": 3, "root": root, "abs": cite})
        return {"answer_md": "Three things.\n\n1. Push **feature/tax**.\n2. Fix the test.\n3. Deploy.",
                "citations": cites, "files_read": [c["path"] for c in cites], "refused": [], "usage": {},
                "ok": True, "error": None, "partial_md": ""}


@pytest.fixture
def world(tmp_path, monkeypatch):
    from app import config
    from app.secretary import gate, gitlog, hubdocs, store, util

    w = World(tmp_path).build()
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "TZ", TZ)
    monkeypatch.setattr(config, "SESSIONS_ROOTS", [str(w.root)])
    monkeypatch.setattr(config, "CLAUDE_CONFIG_DIRS", [str(w.config_dir)])
    monkeypatch.setattr(config, "DOCS_DIR", str(w.docs))
    monkeypatch.setattr(config, "READ_MEMORY", True)
    monkeypatch.setattr(config, "READ_GIT", True)
    monkeypatch.setattr(config, "SECRETARY", True)
    monkeypatch.setattr(config, "TOKEN", "fake-hub-token-for-tests")
    monkeypatch.setattr(config, "OWNER", "Alex")
    monkeypatch.setattr(config, "HOST_HOME", str(w.home))
    monkeypatch.setattr(config, "MAX_DIGESTS", 40)
    monkeypatch.setattr(config, "PAUSE_AT", 0.6)
    monkeypatch.setattr(config, "NOTIFY_KIND", "none")
    monkeypatch.setattr(store, "_local", threading.local())
    monkeypatch.setattr(store, "_loop", None)
    gitlog.forget_cache()
    hubdocs._cache.clear()
    util._zone_cache.clear()
    fake = FakeLLM(available=False)
    monkeypatch.setitem(gate._mod, "m", fake)
    w.llm = fake
    yield w


@pytest.fixture
def llm(world):
    world.llm._available = True
    return world.llm


def ingest_all(w) -> list:
    from app.secretary import transcripts

    out = []
    for d, slug, path in transcripts.main_transcripts():
        out.append(transcripts.ingest(path, d, slug))
    return out


@pytest.fixture
def loaded(world):
    """The world read in: transcripts ingested and one sweep run."""
    from app.secretary import sweep

    ingest_all(world)
    world.sweep = sweep.run_sweep()
    return world


def ui_client():
    """A TestClient with the secretary's router and a signed-in cookie."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import auth
    from app.secretary.api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    client.cookies.set(auth.COOKIE, auth.ui_secret())
    return client
