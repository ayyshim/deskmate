"""The scheduler with the REAL model layer: C1's scheduler, gate and views calling C2's llm.py, digest.py,
brief.py, ask.py and usage.py, with only the SDK's client stubbed. Nothing reaches the network: llm._client
plays back SDK messages (for Ask it calls the real PreToolUse/PostToolUse guard hooks the way the CLI does)
and the usage probe's HTTP client answers with fixed headers. Every token and secret here is fake.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from conftest import ui_client
from world import NEVER_READ, SECRETS

FAKE_TOKEN = "fake-oauth-token-not-real-000"


def result(**kw) -> ResultMessage:
    base = dict(subtype="success", duration_ms=1200, duration_api_ms=1000, is_error=False, num_turns=2,
                session_id="x", total_cost_usd=0.0012,
                usage={"input_tokens": 800, "output_tokens": 100, "cache_read_input_tokens": 50,
                       "cache_creation_input_tokens": 0})
    base.update(kw)
    return ResultMessage(**base)


DIGEST = {"title": "Checkout total fixed", "repos": ["shop", "invented"], "outcome": "shipped",
          "summary": "Fixed the checkout total in shop/src/total.py. One tax test still fails.",
          "shipped": ["Totals in cents"], "decisions": ["Totals are kept in cents"],
          "open_loops": [{"text": "Fix the tax rounding test", "owner": "claude"},
                         {"text": "Review the tax rule", "owner": "you"}], "blockers": []}
BRIEF = {"headline": "Checkout fixed; tax still open.", "lede": "The checkout total ships in cents.",
         "discord_text": "Checkout total fixed @everyone. One thing waits on you."}


def hdrs(u5: float, u7: float, status5: str = "allowed", status7: str = "allowed") -> dict:
    now = int(time.time())
    return {"anthropic-ratelimit-unified-5h-utilization": str(u5), "anthropic-ratelimit-unified-5h-reset": str(now + 3600),
            "anthropic-ratelimit-unified-5h-status": status5,
            "anthropic-ratelimit-unified-7d-utilization": str(u7), "anthropic-ratelimit-unified-7d-reset": str(now + 3 * 86400),
            "anthropic-ratelimit-unified-7d-status": status7}


class SDK:
    """Stands in for the claude CLI behind llm._client. It tells a digest, a brief and an ask apart by their
    output schema, plays that kind's script, and records (kind, options, prompt) for every call."""

    def __init__(self) -> None:
        from app.secretary import prompts

        self.kinds = {id(prompts.DIGEST_SCHEMA): "digest", id(prompts.BRIEF_SCHEMA): "brief",
                      id(prompts.ASK_SCHEMA): "ask"}
        self.scripts: dict = {"digest": [result(structured_output=DIGEST)], "brief": [result(structured_output=BRIEF)],
                              "ask": [result(structured_output={"answer_md": "No idea.", "citations": []})]}
        self.calls: list[tuple] = []
        self.decisions: list[tuple] = []  # what the guard hooks said, per tool call

    def client(self, options):
        return _Client(self, options, self.kinds[id((options.output_format or {}).get("schema"))])

    def of(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]


class _Client:
    def __init__(self, sdk: SDK, options, kind: str) -> None:
        self.sdk, self.options, self.kind = sdk, options, kind

    async def __aenter__(self):
        script = self.sdk.scripts[self.kind]
        if isinstance(script, BaseException):  # e.g. the CLI binary is missing: connect() fails
            raise script
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self.sdk.calls.append((self.kind, self.options, prompt))

    async def receive_response(self):
        script = self.sdk.scripts[self.kind]
        msgs = await script(self.options) if callable(script) else script
        for m in msgs:
            if isinstance(m, BaseException):
                raise m
            if m == "sleep":
                await asyncio.sleep(5)
                continue
            yield m


class Probe:
    """The usage probe's httpx.AsyncClient: fixed rate-limit headers, no network."""

    def __init__(self) -> None:
        self.status, self.headers, self.calls = 200, hdrs(0.20, 0.30), 0

    def client_class(self):
        probe = self

        class C:
            def __init__(self, *a, **k) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

            async def post(self, url, headers=None, json=None):
                probe.calls += 1
                assert url.startswith("https://api.anthropic.com/") and json["max_tokens"] == 1
                return SimpleNamespace(status_code=probe.status, headers=probe.headers)

        return C


def guard_script(sdk: SDK, calls, answer=None, final=None, leak=None):
    """An ask script that runs each tool call through the guard's hooks like the CLI, then answers."""

    async def script(options):
        pre = options.hooks["PreToolUse"][0].hooks[0]
        post = options.hooks["PostToolUse"][0].hooks[0]
        msgs = []
        for i, (tool, ti) in enumerate(calls):
            data = {"tool_name": tool, "tool_input": ti, "cwd": options.cwd}
            out = (await pre(data, f"t{i}", None)).get("hookSpecificOutput", {})
            denied = out.get("permissionDecision") == "deny"
            sdk.decisions.append((tool, ti, "deny" if denied else "allow", out))
            msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"t{i}", name=tool, input=ti)], model="m"))
            if denied:
                msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"t{i}",
                                                                 content=out["permissionDecisionReason"], is_error=True)]))
                continue
            resp = leak if (tool == "Grep" and leak) else {"filenames": []}
            p = await post(dict(data, tool_response=resp), f"t{i}", None)
            if p.get("continue_") is False:
                return msgs + [result(structured_output=None, result="")]
            msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"t{i}", content="ok", is_error=False)]))
        msgs.append(final or result(structured_output=answer))
        return msgs

    return script


@pytest.fixture
def real(loaded, monkeypatch):
    """The fixture world, read in, with the real model layer behind gate.llm() and the SDK stubbed."""
    from app.secretary import gate, llm, usage

    sdk, probe = SDK(), Probe()
    monkeypatch.setitem(gate._mod, "m", llm)
    monkeypatch.setattr(usage, "oauth_token", lambda: FAKE_TOKEN)
    monkeypatch.setattr(llm, "_client", sdk.client)
    monkeypatch.setattr(usage.httpx, "AsyncClient", probe.client_class())
    usage.reset()
    loaded.sdk, loaded.probe = sdk, probe
    loaded.memdir = next(p for p in (loaded.config_dir / "projects").glob("*/memory") if (p / "ship-status.md").exists())
    yield loaded
    usage.reset()


def _sched():
    from app.secretary import store
    from app.secretary.scheduler import Scheduler

    s = Scheduler()
    store.set_loop(asyncio.get_running_loop())
    s.wake, s.ingest_lock, s.started = asyncio.Event(), asyncio.Lock(), True
    return s


async def _until(test, tries: int = 300) -> None:
    for _ in range(tries):
        if test():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting")


def _no_secret(blob: str) -> None:
    for x in SECRETS + NEVER_READ:
        assert x not in blob, f"a planted secret reached the model ({x[:6]}…)"


def _runs(kind: str) -> list:
    from app.secretary import store

    return store.q("select * from model_runs where kind = ? order by id", (kind,))


async def _digest(w) -> None:
    s = _sched()
    await asyncio.to_thread(s.consider_digest, w.sid_a)
    await s._digest_next()


async def _brief(w) -> None:
    s = _sched()
    await s.run_brief(w.today, "manual")
    await _until(lambda: w.today not in s.briefing)


async def _ask(question: str) -> int:
    s = _sched()
    aid = await s.run_ask(question, "ui")
    await _until(lambda: s.asks == 0)
    return aid


# ---------------------------------------------------------------- a digest, a brief, an ask


def test_digest_through_the_real_model_layer(real):
    from app import config
    from app.secretary import store, views

    asyncio.run(_digest(real))
    (kind, opts, prompt), = real.sdk.of("digest")
    _no_secret(prompt)  # C1 redacts, C2 builds the prompt: nothing planted gets through
    assert "Fix the checkout total" in prompt and '"session_digest"' in prompt
    assert opts.model == config.DIGEST_MODEL and opts.tools == [] and opts.setting_sources == []
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN
    d = store.one("select * from digests where session = ?", (real.sid_a,))
    assert d["title"] == "Checkout total fixed" and json.loads(d["repos"]) == ["shop"] and d["outcome"] == "shipped"
    assert json.loads(d["open_loops"])[0] == {"text": "Fix the tax rounding test", "owner": "claude"}
    row = store.one("select * from cc_sessions where id = ?", (real.sid_a,))
    assert row["digest_state"] == "ready" and row["digested_offset"] == row["read_offset"]
    run, = _runs("digest")
    assert (run["ok"], run["error"], run["input_tokens"], run["output_tokens"], run["cache_read_tokens"]) == (1, None, 800, 100, 50)
    assert run["cost_usd"] == pytest.approx(0.0012) and run["model"] == config.DIGEST_MODEL
    r = ui_client().get(f"/api/sec/sessions/{real.sid_a}")
    assert r.status_code == 200 and r.json()["digest"]["summary"].startswith("Fixed the checkout total")
    # The transcript ends in a half-written line: that is not "the session went on after this digest".
    assert r.json()["digest"]["stale"] is False and r.json()["session"]["digest_stale"] is False
    assert views.digests_today() == 1


def test_brief_through_the_real_model_layer(real, monkeypatch):
    from app import config, notify
    from app.secretary import store

    asyncio.run(_brief(real))
    (kind, opts, prompt), = real.sdk.of("brief")
    _no_secret(prompt)
    assert opts.model == config.BRIEF_MODEL and real.today in prompt
    b = store.one("select * from briefs where day = ?", (real.today,))
    assert b["status"] == "ready" and b["headline"] == "Checkout fixed; tax still open"  # brief.py drops the full stop
    assert "@everyone" not in b["discord_text"] and b["error"] is None
    run, = _runs("brief")
    assert run["ok"] == 1 and run["input_tokens"] == 800 and run["cost_usd"] == pytest.approx(0.0012)
    r = ui_client().get(f"/api/sec/brief?day={real.today}")
    assert r.status_code == 200 and r.json()["brief"]["status"] == "ready"
    sent = []

    async def fake_send(text, *, title="", images=(), event="notify"):
        sent.append((text, event))
        return {"ok": True}

    monkeypatch.setattr(notify, "kind", lambda: "discord")
    monkeypatch.setattr(notify, "send", fake_send)

    async def post():
        return await _sched().morning_post(real.today)

    assert asyncio.run(post()) is True
    assert sent == [(b["discord_text"], "brief")]


def test_ask_with_citations_refusals_and_the_one_deny_list(real):
    from app import config
    from app.secretary import store, views

    root, docs, mem = real.root, real.docs, real.memdir
    (root / "shop" / "secret.env").write_text("API_TOKEN=FAKE-not-a-real-secret-000000\n")
    (root / "shop" / "deploy.key").write_text("FAKE KEY\n")
    (root / "shop" / "credentials.py").write_text("def load():\n    return {}\n")
    transcript = str(real.paths["a"])
    calls = [("Read", {"file_path": f"{docs}/design/checkout/00-overview.md"}),
             ("Read", {"file_path": f"{root}/shop/secret.env"}),
             ("Read", {"file_path": f"{root}/shop/deploy.key"}),
             ("Read", {"file_path": transcript}),
             ("Grep", {"pattern": "tax", "path": str(root)}),
             ("Read", {"file_path": f"{root}/shop/credentials.py"}),
             ("Read", {"file_path": f"{mem}/ship-status.md"})]
    answer = {"answer_md": "The checkout fix is **built, not pushed**.\n\n- Tax rounding is still open.",
              "citations": [{"path": f"{docs}/design/checkout/00-overview.md", "line": 4},
                            {"path": f"notes/changelog/shop/{real.today}.md", "line": 5},
                            {"path": f"{mem}/ship-status.md", "line": 1},
                            {"path": "shop/credentials.py", "line": 1},
                            {"path": "shop/secret.env", "line": 1},
                            {"path": "/etc/passwd", "line": 1}]}
    real.sdk.scripts["ask"] = guard_script(real.sdk, calls, answer=answer)
    aid = asyncio.run(_ask("What is left on checkout? my key is sk-ant-oat01-FAKEfakeFAKEfakeFAKEfakeFAKEfake0123"))

    (kind, opts, prompt), = real.sdk.of("ask")
    _no_secret(prompt)
    assert opts.model == config.ASK_MODEL and opts.tools == ["Read", "Grep", "Glob"]
    assert str(root) == opts.cwd and str(mem) in opts.add_dirs and all(".jsonl" not in d for d in opts.add_dirs)
    verdict = {(t, (ti.get("file_path") or ti.get("path")).rsplit("/", 1)[-1]): v for t, ti, v, _ in real.sdk.decisions}
    assert verdict == {("Read", "00-overview.md"): "allow", ("Read", "secret.env"): "deny",
                       ("Read", "deploy.key"): "deny", ("Read", transcript.rsplit("/", 1)[-1]): "deny",
                       ("Grep", root.name): "allow", ("Read", "credentials.py"): "allow",
                       ("Read", "ship-status.md"): "allow"}
    grep = next(out for t, _, _, out in real.sdk.decisions if t == "Grep")
    assert "!*.[eE][nN][vV]" in grep["updatedInput"]["glob"]  # ripgrep skips env files inside the root
    # The one deny list: the CLI's own rules refuse env and key files, never a code file named credentials.py.
    rules = json.loads(opts.settings)["permissions"]["deny"]
    globs = [r[len("Read(**/"):-1] for r in rules if r.startswith("Read(**/") and not r.endswith("/**)")]
    assert any(fnmatch.fnmatchcase("secret.env", g) for g in globs)
    assert any(fnmatch.fnmatchcase("deploy.key", g) for g in globs)
    assert any(fnmatch.fnmatchcase("credentials.json", g) for g in globs)
    assert not [g for g in globs if fnmatch.fnmatchcase("credentials.py", g)]

    a = views.get_ask(aid)
    assert a["status"] == "done" and a["error"] is None and "built, not pushed" in a["answer_md"]
    cites = [(c["kind"], c["label"]) for c in a["citations"]]
    entry = store.one("select time from hub_entries where kind = 'changelog' and path = ? order by line limit 1",
                      (f"{docs}/changelog/shop/{real.today}.md",))
    assert cites == [("design", "notes/design/checkout/00-overview.md"),
                     ("changelog", f"shop/{real.today} · {entry['time']}"),
                     ("memory", "memory/ship-status.md"), ("file", "shop/credentials.py")]
    assert a["citations"][0]["href"] == f"vscode://file{docs}/design/checkout/00-overview.md:4"
    assert a["citations"][1]["href"] == f"vscode://file{docs}/changelog/shop/{real.today}.md:5"
    run, = _runs("ask")
    assert run["ok"] == 1 and run["error"] is None and run["output_tokens"] == 100
    r = ui_client().get(f"/api/sec/asks/{aid}")
    assert r.status_code == 200 and r.json()["citations"][3]["label"] == "shop/credentials.py"


def test_ask_refusal_when_a_search_names_a_refused_file(real):
    from app.secretary import gate, views

    (real.root / "shop" / "secret.env").write_text("API_TOKEN=FAKE-not-a-real-secret-000000\n")
    leak = {"mode": "content", "filenames": [], "content": "shop/secret.env:1:API_TOKEN=FAKE-not-a-real-secret-000000"}
    real.sdk.scripts["ask"] = guard_script(real.sdk, [("Grep", {"pattern": "API_TOKEN"})], leak=leak)
    aid = asyncio.run(_ask("Which token does the shop use?"))
    a = views.get_ask(aid)
    assert a["status"] == "failed" and a["error"] == gate.failure_sentence("refused_output")
    assert a["answer_md"] is None and a["citations"] == []  # nothing the model wrote is kept
    run, = _runs("ask")
    assert run["ok"] == 0 and run["error"] == "refused_output"
    assert ui_client().get(f"/api/sec/asks/{aid}").status_code == 200


def test_a_partial_answer_is_kept_above_the_reason(real):
    from app.secretary import gate, views

    final = result(subtype="error_max_turns", is_error=True,
                   structured_output={"answer_md": "So far: the design says built.", "citations": []})
    real.sdk.scripts["ask"] = guard_script(real.sdk, [("Read", {"file_path": f"{real.docs}/design/README.md"})],
                                           final=final)
    aid = asyncio.run(_ask("Is checkout shipped?"))
    a = views.get_ask(aid)
    assert a["status"] == "failed" and a["error"] == gate.failure_sentence("max_turns")
    assert a["answer_md"] == "So far: the design says built."


# ---------------------------------------------------------------- every model error kind


ERRORS = [
    # code, the SDK's answer to a digest or brief, to an ask, the ModelError class digests and briefs raise
    ("max_turns", [result(subtype="error_max_turns", is_error=True, structured_output=None)], None, "ModelMaxTurns"),
    ("rate_limited", [result(is_error=True, api_error_status=429, structured_output=None)], None, "ModelRateLimited"),
    ("auth", [result(is_error=True, api_error_status=401, structured_output=None)], None, "ModelAuthFailed"),
    ("failed", [result(subtype="error_during_execution", is_error=True, errors=["boom"])], None, "ModelFailed"),
    ("bad_output", [result(structured_output=None, result="plain text")],
     [result(structured_output=None, result="")], "ModelBadOutput"),
    ("timeout", ["sleep"], None, "ModelTimeout"),
    ("cli_missing", CLINotFoundError("no claude CLI"), None, "ModelCliMissing"),
    ("cli_error", [RuntimeError("the CLI died")], None, "ModelCliError"),
]


@pytest.mark.parametrize("code,script,ask_script,exc", ERRORS, ids=[e[0] for e in ERRORS])
def test_each_model_error_kind(real, monkeypatch, code, script, ask_script, exc):
    from app.secretary import gate, llm, store, views

    for name in ("DIGEST_TIMEOUT_S", "BRIEF_TIMEOUT_S", "ASK_TIMEOUT_S"):
        monkeypatch.setattr(llm, name, 0.3)
    real.sdk.scripts.update(digest=script, brief=script, ask=ask_script or script)
    asyncio.run(_digest(real))
    asyncio.run(_brief(real))
    aid = asyncio.run(_ask("What is open?"))

    row = store.one("select * from cc_sessions where id = ?", (real.sid_a,))
    assert row["digest_state"] == "failed" and row["digest_error"] == exc
    assert views.digest_label(row) == f"digest failed: {gate.failure_phrase(code)}"
    b = views.brief_dict(views.get_brief(real.today))
    assert b["status"] == "failed" and b["error"] == f"The brief failed: {gate.failure_phrase(code)}."
    a = views.get_ask(aid)
    assert a["status"] == "failed" and a["error"] == gate.failure_sentence(code) and a["answer_md"] is None
    for kind in ("digest", "brief", "ask"):
        run, = _runs(kind)
        assert run["ok"] == 0 and run["error"] == code, (kind, run["error"])
        assert run["input_tokens"] == (800 if isinstance(script, list) and isinstance(script[0], ResultMessage) else 0)
    # Every page that shows these still answers with its contract shape.
    c = ui_client()
    for path in (f"/api/sec/sessions/{real.sid_a}", f"/api/sec/today?day={real.today}", f"/api/sec/asks/{aid}",
                 "/api/sec/timeline", "/api/sec/sessions", "/api/sec/state"):
        assert c.get(path).status_code == 200, path


def test_no_login_holds_digests_and_ask_says_so(real, monkeypatch):
    from app.secretary import store, usage

    monkeypatch.setattr(usage, "oauth_token", lambda: "")
    asyncio.run(_digest(real))
    assert not real.sdk.calls and real.probe.calls == 0
    assert store.val("select digest_state from cc_sessions where id = ?", (real.sid_a,)) == "no_token"
    c = ui_client()
    r = c.post("/api/sec/ask", json={"question": "What is open?"})
    assert r.status_code == 503 and "Claude login" in r.json()["detail"]
    s = c.get("/api/sec/state").json()
    assert s["secretary"]["status"] == "no_token" and s["usage"]["error"] == "no_token"
    assert c.get("/api/sec/asks").json()["disabled_reason"] == "no_token"


# ---------------------------------------------------------------- plan usage pauses, on each window


@pytest.mark.parametrize("window,headers,label", [
    ("5-hour", hdrs(0.61, 0.20), "Paused at 61% of 5\u00a0h"),
    ("7-day", hdrs(0.10, 0.86), "Paused at 86% of the week"),
])
def test_usage_pause_on_each_window(real, monkeypatch, window, headers, label):
    import datetime as dt
    from zoneinfo import ZoneInfo

    from app import config
    from app.secretary import gate, llm, store, util, views

    real.probe.headers = headers

    async def go():
        s = _sched()
        await asyncio.to_thread(s.consider_digest, real.sid_a)
        await s._digest_next()  # probes, finds the window over the limit, holds the digest
        clock = dt.datetime.combine(dt.date.fromisoformat(real.today), dt.time(18, 45), ZoneInfo(config.TZ))
        monkeypatch.setattr(util, "now_local", lambda: clock)
        await s._tick()  # the brief's time has come, but the gate is closed
        assert not s.briefing

    asyncio.run(go())
    assert real.probe.calls == 1 and not real.sdk.calls
    assert store.val("select digest_state from cc_sessions where id = ?", (real.sid_a,)) == "paused"
    snap, st = gate.usage_snapshot(), llm.usage_state()
    assert snap["over_pause"] and st["paused"] and snap["reason"] == st["reason"]  # one rule, one text
    assert snap["reason"].startswith(f"The {window} window is at") and "resets" in snap["reason"]
    assert views.secretary_status()["status"] == "paused_usage" and views.secretary_status()["label"] == label
    row = store.one("select * from cc_sessions where id = ?", (real.sid_a,))
    assert views.digest_label(row) == label.lower()
    c = ui_client()
    assert c.get("/api/sec/usage").json()["reason"] == st["reason"]
    assert c.get("/api/sec/state").json()["secretary"]["label"] == label
    # Ask still runs, and says why digests wait.
    real.sdk.scripts["ask"] = [result(structured_output={"answer_md": "Two things are open.", "citations": []})]
    aid = asyncio.run(_ask("What is open?"))
    a = views.get_ask(aid)
    assert a["status"] == "done" and a["warning"].startswith(st["reason"]) and "Ask still works" in a["warning"]


def test_a_rejected_window_from_the_stream_pauses_until_it_resets(real):
    from app.secretary import gate, llm, store, views

    reset = int(time.time()) + 4 * 86400
    event = RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", resets_at=reset,
                                                         rate_limit_type="seven_day"), uuid="u", session_id="x")
    real.sdk.scripts["digest"] = [event, result(is_error=True, api_error_status=429, structured_output=None)]

    async def go():
        s = _sched()
        await asyncio.to_thread(s.consider_digest, real.sid_a)
        await s._digest_next()  # the probe reads 20% / 30%, the call is rejected
        assert await s.gate_state() == "paused"  # the stream said the week is full

    asyncio.run(go())
    assert store.val("select digest_error from cc_sessions where id = ?", (real.sid_a,)) == "ModelRateLimited"
    st = llm.usage_state()
    assert st["paused"] and st["reason"].startswith("The 7-day window is full") and gate.usage_snapshot()["reason"] == st["reason"]
    assert views.secretary_status()["label"] == "Paused at 100% of the week"
    assert "Ask fails too" in views.ask_warning()


@pytest.mark.parametrize("five,seven,paused", [
    (None, None, False),
    ({"utilization": 0.59, "resets_at": 1, "status": "allowed"}, None, False),
    ({"utilization": 0.60, "resets_at": 1, "status": "allowed"}, None, True),
    ({"utilization": 0.10, "resets_at": 1, "status": "rejected"}, None, True),
    (None, {"utilization": 0.84, "resets_at": 1, "status": ""}, False),
    (None, {"utilization": 0.85, "resets_at": 1, "status": ""}, True),
    ({"utilization": 0.70, "resets_at": 1, "status": ""}, {"utilization": 0.90, "resets_at": 1, "status": ""}, True),
])
def test_gate_and_usage_state_agree(real, five, seven, paused):
    from app.secretary import gate, llm, usage

    later = int(time.time()) + 3600
    for name, w in (("five_hour", five), ("seven_day", seven)):
        usage._state[name] = dict(w, resets_at=later) if w else None
    usage._state["ts"] = time.time()
    snap, st = gate.usage_snapshot(), llm.usage_state()
    assert snap["over_pause"] is st["paused"] is paused
    assert snap["reason"] == (st["reason"] if paused else None)
    if five and seven and paused:  # both full: both said, the 5-hour one first
        assert st["reason"].index("5-hour") < st["reason"].index("7-day")
