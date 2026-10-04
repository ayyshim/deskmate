"""The model layer (llm.py, usage.py, prompts.py, digest.py, brief.py, ask.py) with a stubbed SDK.

No network and no CLI: llm._client is replaced by a fake that plays back SDK messages, and for ask it
calls the real PreToolUse/PostToolUse hooks the way the CLI would. Every value here is fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# The app is imported per test, not at collection: the hub's own test modules set the environment
# app.config reads at import, and this folder is collected before them.
config = ask_mod = llm = prompts = usage_mod = None


@pytest.fixture(autouse=True)
def _app():
    global config, ask_mod, llm, prompts, usage_mod
    from app import config
    from app.secretary import ask as ask_mod
    from app.secretary import llm, prompts
    from app.secretary import usage as usage_mod

FAKE_TOKEN = "fake-oauth-token-not-real-000"

MATERIAL = {
    "kind": "session_digest", "version": 1, "session": "s-1", "owner": "Alex", "tz": "UTC",
    "title_hint": "fix checkout", "cwd": "~/work", "repos": ["shop"], "repo_labels": {"shop": "shop"},
    "day": "2026-10-04", "window": {"from": "10:00", "to": "11:00"}, "model": "claude-opus-5-5", "previous": None,
    "delta": {"from_offset": 0, "to_offset": 100, "turns_dropped": 0,
              "turns": [{"turn": 1, "time": "10:00", "prompt": "fix the checkout total", "mid_turn": [],
                         "answer": "Fixed it in shop/cart.py.", "interrupted": False, "compacted": False}],
              "files": [{"path": "shop/cart.py", "action": "edit", "count": 2}], "files_read": 3, "commands": [],
              "commands_total": 0, "commands_failed": 0, "commits": [], "pushes": [], "artifacts": [],
              "notifications": [], "prs": []},
    "stats": {},
}
DIGEST = {"title": "Checkout total fixed", "repos": ["shop", "invented"], "outcome": "shipped",
          "summary": "Fixed the total in shop/cart.py.", "shipped": ["Totals in cents"], "decisions": [],
          "open_loops": [{"text": "Review the tax rule", "owner": "you"}], "blockers": []}


def result(**kw) -> ResultMessage:
    base = dict(subtype="success", duration_ms=1200, duration_api_ms=1000, is_error=False, num_turns=2,
                session_id="x", total_cost_usd=0.0012,
                usage={"input_tokens": 800, "output_tokens": 100, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0})
    base.update(kw)
    return ResultMessage(**base)


class FakeClient:
    """Plays a script: a list of SDK messages, or an async function (options) -> list of messages."""

    def __init__(self, options, script, seen):
        self.options, self.script, self.seen = options, script, seen

    async def __aenter__(self):
        self.seen.append(self.options)
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self.seen.append(prompt)

    async def receive_response(self):
        msgs = await self.script(self.options) if callable(self.script) else self.script
        for m in msgs:
            if isinstance(m, BaseException):
                raise m
            if m == "sleep":
                await asyncio.sleep(5)
                continue
            yield m


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    """A stub SDK; returns (set_script, seen). Token present, data folder in tmp."""
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(usage_mod, "oauth_token", lambda: FAKE_TOKEN)
    usage_mod.reset()
    state = {"script": [result(structured_output=DIGEST)]}
    seen: list = []
    monkeypatch.setattr(llm, "_client", lambda options: FakeClient(options, state["script"], seen))

    def set_script(s):
        state["script"] = s

    yield set_script, seen
    usage_mod.reset()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- schemas and prompts


def test_schemas_are_strict_friendly_and_match_the_contract():
    assert set(prompts.DIGEST_SCHEMA["required"]) == {"title", "repos", "outcome", "summary", "shipped", "decisions",
                                                      "open_loops", "blockers"}
    assert set(prompts.BRIEF_SCHEMA["required"]) == {"headline", "lede", "discord_text"}
    loop = prompts.DIGEST_SCHEMA["properties"]["open_loops"]["items"]
    assert loop["properties"]["owner"]["enum"] == ["you", "claude"]
    blob = json.dumps([prompts.DIGEST_SCHEMA, prompts.BRIEF_SCHEMA, prompts.ASK_SCHEMA])
    for kw in ("maxLength", "maxItems", "minLength", "pattern"):  # checked after the fact by the CLI: a retry
        assert kw not in blob
    import jsonschema

    jsonschema.validate(DIGEST, prompts.DIGEST_SCHEMA)


def test_prompts_name_the_owner_and_carry_the_rules():
    s = prompts.digest_system("Alex")
    assert "Alex" in s and "Never guess" in s and "No hype" in s and "starting with a verb" in s
    assert " he " not in s and " she " not in s
    assert "the user" in prompts.brief_system("")
    assert len(prompts.digest_system("Alex")) < 4000 and len(prompts.brief_system("Alex")) < 4000  # argv


# ---------------------------------------------------------------- isolation


def test_env_carries_the_token_only_to_the_child(sdk, monkeypatch):
    monkeypatch.setenv("DESKMATE_TOKEN", "fake-hub-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-api-key")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    env = llm.sdk_env(FAKE_TOKEN)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN
    assert env["DESKMATE_TOKEN"] == "" and env["ANTHROPIC_API_KEY"] == ""
    assert env["HOME"].startswith(str(config.DATA)) and env["CLAUDE_CONFIG_DIR"].startswith(env["HOME"])
    assert os.stat(env["HOME"]).st_mode & 0o077 == 0
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def test_structured_options_are_isolated(sdk):
    _, seen = sdk
    run(llm.digest_session(MATERIAL))
    opts = seen[0]
    assert opts.setting_sources == [] and opts.strict_mcp_config and opts.mcp_servers == {}
    assert opts.tools == [] and opts.plugins == [] and opts.skills == [] and not opts.hooks
    assert opts.permission_mode == "dontAsk" and "no-session-persistence" in opts.extra_args
    assert opts.model == config.DIGEST_MODEL and opts.output_format["schema"] == prompts.DIGEST_SCHEMA
    assert isinstance(opts.system_prompt, str) and "bare" not in json.dumps(opts.extra_args)
    assert str(opts.cwd).startswith(str(config.DATA))
    assert "shop/cart.py" in seen[1]  # the material went in the user message


def test_thinking_is_off_only_where_the_model_allows_it(sdk, monkeypatch):
    """Claude Sonnet 5.5 (the brief's default) answers a disabled thinking config with a 400."""
    _, seen = sdk
    monkeypatch.setattr(config, "DIGEST_MODEL", "claude-haiku-4-5-20251001")
    run(llm.digest_session(MATERIAL))
    assert seen[-2].thinking == {"type": "disabled"} and seen[-2].effort is None
    for model in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"):
        monkeypatch.setattr(config, "DIGEST_MODEL", model)
        run(llm.digest_session(MATERIAL))
        assert seen[-2].thinking == {"type": "adaptive"} and seen[-2].effort == "low", model


# ---------------------------------------------------------------- digest and brief


def test_digest_is_cleaned(sdk):
    d = run(llm.digest_session(MATERIAL))
    assert d["repos"] == ["shop"]  # "invented" was not in the material
    assert d["outcome"] == "shipped" and d["open_loops"] == [{"text": "Review the tax rule", "owner": "you"}]
    assert d["usage"]["input_tokens"] == 800 and d["usage"]["cost_usd"] == 0.0012 and d["usage"]["outcome"] == "ok"


def test_digest_unclear_outcome_is_none(sdk):
    set_script, _ = sdk
    set_script([result(structured_output=dict(DIGEST, outcome="unclear"))])
    assert run(llm.digest_session(MATERIAL))["outcome"] is None


def test_long_material_is_cut_oldest_first():
    from app.secretary import digest

    big = json.loads(json.dumps(MATERIAL))
    big["delta"]["turns"] = [{"turn": i, "prompt": "p" * 3000, "answer": "a" * 5000, "mid_turn": []}
                             for i in range(40)]
    out = digest.fit(big, limit=60_000)
    assert len(json.dumps(out)) <= 60_000
    assert out["delta"]["turns"][-1]["answer"] == "a" * 5000  # the newest turn kept whole
    assert big["delta"]["turns"][0]["answer"] == "a" * 5000  # the caller's dict untouched


def test_brief(sdk):
    set_script, seen = sdk
    set_script([result(structured_output={"headline": "Checkout fixed.", "lede": "Shipped the total.",
                                          "discord_text": "Sun 4 Oct: checkout fixed\n• shipped @everyone"})])
    b = run(llm.daily_brief({"day": "2026-10-04", "owner": "Alex", "sessions": []}))
    assert b["headline"] == "Checkout fixed" and "@everyone" not in b["discord_text"]
    assert seen[0].model == config.BRIEF_MODEL and b["usage"]["model"] == config.BRIEF_MODEL


@pytest.mark.parametrize("script,exc", [
    ([result(subtype="error_max_turns", is_error=True, structured_output=None)], "ModelMaxTurns"),
    ([result(subtype="success", is_error=True, api_error_status=429, structured_output=None)], "ModelRateLimited"),
    ([result(subtype="success", is_error=True, api_error_status=401, structured_output=None)], "ModelAuthFailed"),
    ([result(subtype="error_during_execution", is_error=True, errors=["boom"])], "ModelFailed"),
    ([result(subtype="error_max_structured_output_retries", is_error=True)], "ModelBadOutput"),
    ([result(structured_output=None, result="plain text")], "ModelBadOutput"),
    ([result(structured_output={"title": "x"})], "ModelBadOutput"),  # fails the local schema check
    ([RuntimeError("cli died")], "ModelFailed"),
    ([], "ModelFailed"),
])
def test_errors_map_to_named_model_errors(sdk, script, exc):
    set_script, _ = sdk
    set_script(script)
    with pytest.raises(getattr(llm, exc)) as e:
        run(llm.digest_session(MATERIAL))
    assert isinstance(e.value, llm.ModelError) and FAKE_TOKEN not in str(e.value)


def test_timeout_is_clean(sdk, monkeypatch):
    set_script, _ = sdk
    set_script(["sleep"])
    monkeypatch.setattr(llm, "DIGEST_TIMEOUT_S", 0.2)
    t0 = time.monotonic()
    with pytest.raises(llm.ModelTimeout):
        run(llm.digest_session(MATERIAL))
    assert time.monotonic() - t0 < 3


def test_no_login(sdk, monkeypatch):
    monkeypatch.setattr(usage_mod, "oauth_token", lambda: "")
    assert llm.available() is False
    with pytest.raises(llm.ModelUnavailable):
        run(llm.digest_session(MATERIAL))


def test_rate_limit_event_feeds_the_usage_state(sdk, monkeypatch):
    set_script, _ = sdk
    reset = int(time.time()) + 3600
    ev = RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", resets_at=reset, rate_limit_type="five_hour"),
                        uuid="u", session_id="x")
    set_script([ev, result(subtype="success", is_error=True, api_error_status=429)])
    with pytest.raises(llm.ModelRateLimited):
        run(llm.digest_session(MATERIAL))
    u = llm.usage_state()
    assert u["paused"] and u["five_hour"]["utilization"] >= 1.0 and "5-hour window is full" in u["reason"]


# ---------------------------------------------------------------- usage


class _Resp:
    def __init__(self, status, headers):
        self.status_code, self.headers = status, headers


def _fake_httpx(monkeypatch, resp, calls):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append((url, headers, json))
            if isinstance(resp, Exception):
                raise resp
            return resp

    monkeypatch.setattr(usage_mod.httpx, "AsyncClient", C)


def _hdrs(u5, u7, status5="allowed"):
    now = int(time.time())
    return {"anthropic-ratelimit-unified-5h-utilization": str(u5), "anthropic-ratelimit-unified-5h-reset": str(now + 3600),
            "anthropic-ratelimit-unified-5h-status": status5,
            "anthropic-ratelimit-unified-7d-utilization": str(u7), "anthropic-ratelimit-unified-7d-reset": str(now + 86400)}


def test_probe_reads_headers_once_a_minute(sdk, monkeypatch):
    calls: list = []
    _fake_httpx(monkeypatch, _Resp(200, _hdrs(0.34, 0.12)), calls)
    run(llm.probe_usage())
    run(llm.probe_usage())
    assert len(calls) == 1
    url, headers, body = calls[0]
    assert headers["anthropic-beta"] == "oauth-2025-04-20" and body["max_tokens"] == 1
    assert body["model"].startswith("claude-haiku")
    u = llm.usage_state()
    assert u["five_hour"]["utilization"] == 0.34 and u["seven_day"]["utilization"] == 0.12 and not u["paused"]


def test_pause_on_either_window(sdk, monkeypatch):
    monkeypatch.setattr(config, "PAUSE_AT", 0.6)
    monkeypatch.setattr(config, "PAUSE_AT_WEEK", 0.85, raising=False)
    _fake_httpx(monkeypatch, _Resp(200, _hdrs(0.61, 0.2)), [])
    run(llm.probe_usage())
    u = llm.usage_state()
    assert u["paused"] and "5-hour window is at 61%" in u["reason"] and "resets at" in u["reason"]
    usage_mod.reset()
    _fake_httpx(monkeypatch, _Resp(200, _hdrs(0.1, 0.86)), [])
    run(llm.probe_usage())
    u = llm.usage_state()
    assert u["paused"] and u["reason"].startswith("The 7-day window is at 86%")


def test_429_still_reads_and_errors_never_raise(sdk, monkeypatch):
    _fake_httpx(monkeypatch, _Resp(429, _hdrs(1.0, 0.5, "rejected")), [])
    run(llm.probe_usage())
    assert llm.usage_state()["paused"]
    usage_mod.reset()
    _fake_httpx(monkeypatch, _Resp(401, {}), [])
    run(llm.probe_usage())
    u = llm.usage_state()
    assert u["five_hour"] is None and u["seven_day"] is None and not u["paused"] and "fail" in u["reason"]
    usage_mod.reset()
    import httpx

    _fake_httpx(monkeypatch, httpx.ConnectError("no network"), [])
    run(llm.probe_usage())
    assert "fail" in llm.usage_state()["reason"]
    usage_mod.reset()
    monkeypatch.setattr(usage_mod, "_window", lambda *a: 1 / 0)  # even a broken module
    assert llm.usage_state()["paused"] is False


def test_unknown_and_reset_windows_are_none(sdk):
    u = llm.usage_state()
    assert u["five_hour"] is None and u["seven_day"] is None and not u["paused"]
    usage_mod._state["five_hour"] = {"utilization": 0.9, "resets_at": int(time.time()) - 5, "status": ""}
    assert llm.usage_state()["five_hour"] is None and not llm.usage_state()["paused"]


# ---------------------------------------------------------------- ask: the guard


@pytest.fixture
def askroot(tmp_path, monkeypatch):
    root = tmp_path / "work"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "notes.md").write_text("The deploy runs at 18:00.\nFAKE appears here too.\n")
    (root / "secret.env").write_text("API_TOKEN=FAKE-not-a-real-secret-000000\n")
    (root / "PROD.ENV").write_text("X=FAKE\n")
    (root / "secrets").mkdir()
    (root / "secrets" / "plan.md").write_text("FAKE\n")
    (root / "secretary").mkdir()
    (root / "secretary" / "digest.py").write_text("# FAKE digest code\n")
    (root / "credentials.py").write_text("# loads credentials from the env\n")
    (root / "id_ed25519").write_text("FAKE KEY\n")
    (root / "link-to-notes2.md").symlink_to(root / "secret.env")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("FAKE outside\n")
    (root / "escape.md").symlink_to(outside / "x.md")
    monkeypatch.setattr(config, "SESSIONS_ROOTS", [str(root)])
    monkeypatch.setattr(config, "CLAUDE_CONFIG_DIRS", [])
    monkeypatch.setattr(config, "DOCS_DIR", "")
    return root


def _pre(guard, tool, **ti):
    return run(guard.pre_tool_use({"tool_name": tool, "tool_input": ti, "cwd": guard.roots[0]}, "t", None))


def _denied(out) -> bool:
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_guard_verdicts(askroot):
    g = ask_mod.PathGuard([str(askroot)], [".env", "*.jsonl", "secrets/**"])
    r = str(askroot)
    allow = [("Read", {"file_path": f"{r}/notes/notes.md"}), ("Read", {"file_path": "notes/notes.md"}),
             ("Read", {"file_path": f"{r}/secretary/digest.py"}), ("Read", {"file_path": f"{r}/credentials.py"}),
             ("Glob", {"pattern": "**/*.md"}), ("StructuredOutput", {"answer_md": "x", "citations": []})]
    deny = [("Read", {"file_path": f"{r}/secret.env"}), ("Read", {"file_path": f"{r}/PROD.ENV"}),
            ("Read", {"file_path": f"{r}/secrets/plan.md"}), ("Read", {"file_path": f"{r}/id_ed25519"}),
            ("Read", {"file_path": f"{r}/link-to-notes2.md"}),  # symlink to secret.env
            ("Read", {"file_path": f"{r}/escape.md"}),  # symlink out of the roots
            ("Read", {"file_path": f"{r}/../outside/x.md"}), ("Read", {"file_path": "/etc/passwd"}),
            ("Read", {"file_path": "/proc/self/environ"}), ("Read", {}),
            ("Grep", {"pattern": "FAKE", "path": f"{r}/secret.env"}), ("Grep", {"pattern": "x", "path": "/etc"}),
            ("Glob", {"pattern": "/etc/*"}), ("Glob", {"pattern": "../*"}), ("Glob", {"pattern": "**/*.env"}),
            ("Bash", {"command": "cat secret.env"}), ("Write", {"file_path": f"{r}/x.md"})]
    for tool, ti in allow:
        assert not _denied(_pre(g, tool, **ti)), (tool, ti)
    for tool, ti in deny:
        assert _denied(_pre(g, tool, **ti)), (tool, ti)
    reasons = [d["reason"] for d in g.denied]
    assert any("outside the shared folders" in x for x in reasons) and any("*.env" in x for x in reasons)


def test_guard_fails_closed(askroot, monkeypatch):
    g = ask_mod.PathGuard([str(askroot)])
    monkeypatch.setattr(g, "check_path", lambda *a: 1 / 0)
    out = _pre(g, "Read", file_path=f"{askroot}/notes/notes.md")
    assert _denied(out) and "guard error" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_grep_gets_the_exclusions_and_ripgrep_honours_them(askroot):
    g = ask_mod.PathGuard([str(askroot)], [".env"])
    out = _pre(g, "Grep", pattern="FAKE", glob="*.md")
    glob = out["hookSpecificOutput"]["updatedInput"]["glob"]
    assert glob.startswith("*.md ") and "!*.[eE][nN][vV]" in glob
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed")
    args = [a for x in glob.split()[1:] for a in ("--glob", x)]
    found = subprocess.run(["rg", "--files", "--hidden", "--no-ignore", *args, str(askroot)],
                           capture_output=True, text=True).stdout
    names = {os.path.relpath(p, askroot) for p in found.split()}
    assert "notes/notes.md" in names and "secretary/digest.py" in names
    for bad in ("secret.env", "PROD.ENV", "secrets/plan.md", "id_ed25519", "credentials.py"):
        assert bad not in names, bad


def test_cli_rules_and_roots_inside_a_claude_folder(tmp_path):
    mem = tmp_path / "home" / ".claude" / "projects" / "-work" / "memory"
    mem.mkdir(parents=True)
    g = ask_mod.PathGuard([str(mem)], ["secrets/**"])
    rules = g.cli_rules()
    assert "Read(**/*.env)" in rules and "Read(**/secrets/**)" in rules and "Read(**/secret.*)" in rules
    assert not any(".claude/" in r or r == "Read(**/.claude)" for r in rules)  # would refuse the root itself
    assert "Read(**/secret*)" not in rules  # the broad one would refuse secretary/
    assert _denied(_pre(g, "Read", file_path=str(mem / ".claude" / "x.md")))  # below the root it still applies
    (mem / "note.md").write_text("x")
    assert not _denied(_pre(g, "Read", file_path=str(mem / "note.md")))


def test_shared_roots_are_confined(askroot, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert ask_mod.shared_roots([str(askroot), str(other), "/etc", "", str(askroot / "missing")]) == [str(askroot)]


def test_citations_are_relative_to_their_root(askroot):
    g = ask_mod.PathGuard([str(askroot)])
    c = g.cite(f"{askroot}/notes/notes.md", 2)
    assert c == {"path": "notes/notes.md", "line": 2, "root": str(askroot), "abs": f"{askroot}/notes/notes.md"}
    assert g.cite("notes/notes.md", 0)["line"] is None
    assert g.cite(f"{askroot}/secret.env", 1) is None and g.cite("/etc/passwd", 1) is None
    assert g.cite(f"{askroot}/link-to-notes2.md", 1) is None


# ---------------------------------------------------------------- ask end to end (stubbed CLI)


def _cli(calls, answer=None, final=None, leak_response=None):
    """A script that drives the guard's hooks like the CLI does, then answers."""

    async def script(options):
        pre = options.hooks["PreToolUse"][0].hooks[0]
        post = options.hooks["PostToolUse"][0].hooks[0]
        msgs = []
        for i, (tool, ti) in enumerate(calls):
            data = {"tool_name": tool, "tool_input": ti, "cwd": options.cwd}
            out = await pre(data, f"t{i}", None)
            denied = out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
            msgs.append(AssistantMessage(content=[ToolUseBlock(id=f"t{i}", name=tool, input=ti)], model="m"))
            if denied:
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"t{i}", content=reason, is_error=True)]))
                continue
            resp = leak_response if (tool == "Grep" and leak_response) else {"filenames": []}
            p = await post(dict(data, tool_response=resp), f"t{i}", None)
            if p.get("continue_") is False:
                return msgs + [result(subtype="success", structured_output=None, result="")]
            msgs.append(UserMessage(content=[ToolResultBlock(tool_use_id=f"t{i}", content="ok", is_error=False)]))
        msgs.append(final or result(structured_output=answer))
        return msgs

    return script


def test_ask_answers_with_citations_and_refusals(sdk, askroot):
    set_script, seen = sdk
    r = str(askroot)
    set_script(_cli([("Read", {"file_path": f"{r}/notes/notes.md"}), ("Read", {"file_path": f"{r}/secret.env"}),
                     ("Read", {"file_path": f"{r}/link-to-notes2.md"})],
                    answer={"answer_md": "The deploy runs at **18:00**.",
                            "citations": [{"path": f"{r}/notes/notes.md", "line": 1},
                                          {"path": f"{r}/secret.env", "line": 1}, {"path": "/etc/passwd", "line": 1}]}))
    out = run(llm.ask("When does the deploy run?", [r], [".env"], max_turns=8))
    assert out["ok"] and out["error"] is None and "18:00" in out["answer_md"]
    assert [c["path"] for c in out["citations"]] == ["notes/notes.md"]
    assert out["files_read"] == ["notes/notes.md"]
    assert {x["path"] for x in out["refused"]} >= {f"{r}/secret.env", f"{r}/link-to-notes2.md"}
    opts = seen[0]
    assert opts.tools == ["Read", "Grep", "Glob"] and opts.model == config.ASK_MODEL and opts.max_turns == 9
    assert opts.cwd == r and "Read(**/*.env)" in json.loads(opts.settings)["permissions"]["deny"]
    assert opts.setting_sources == [] and opts.strict_mcp_config and opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN


def test_ask_stops_when_grep_names_a_refused_file(sdk, askroot):
    set_script, _ = sdk
    set_script(_cli([("Grep", {"pattern": "FAKE"})], leak_response={"mode": "content", "filenames": [],
                                                                     "content": "secret.env:1:API_TOKEN=FAKE"}))
    out = run(llm.ask("find FAKE", [str(askroot)], []))
    assert not out["ok"] and out["error"] == "refused_output" and "FAKE" not in out["answer_md"]


@pytest.mark.parametrize("final,code", [
    (result(subtype="error_max_turns", is_error=True, structured_output=None), "max_turns"),
    (result(subtype="success", is_error=True, api_error_status=429), "rate_limited"),
    (result(subtype="success", is_error=True, api_error_status=401), "auth"),
    (result(structured_output=None, result=""), "bad_output"),
])
def test_ask_errors_are_clean_answers(sdk, askroot, final, code):
    set_script, _ = sdk
    set_script(_cli([("Read", {"file_path": f"{askroot}/notes/notes.md"})], final=final))
    out = run(llm.ask("q", [str(askroot)], []))
    assert out["ok"] is False and out["error"] == code and out["answer_md"] and out["citations"] == []


def test_ask_without_login_roots_or_question(sdk, askroot, monkeypatch):
    assert run(llm.ask("q", ["/etc"], []))["error"] == "no_roots"
    assert run(llm.ask("  ", [str(askroot)], []))["error"] == "empty"
    monkeypatch.setattr(usage_mod, "oauth_token", lambda: "")
    assert run(llm.ask("q", [str(askroot)], []))["error"] == "no_login"


def test_ask_timeout(sdk, askroot, monkeypatch):
    set_script, _ = sdk
    set_script(["sleep"])
    monkeypatch.setattr(llm, "ASK_TIMEOUT_S", 0.2)
    out = run(llm.ask("q", [str(askroot)], []))
    assert out["error"] == "timeout" and not out["ok"]


def test_text_answer_without_structured_output_still_counts(sdk, askroot):
    set_script, _ = sdk
    set_script([AssistantMessage(content=[TextBlock(text="partial")], model="m"),
                result(structured_output=None, result="The deploy runs at 18:00.")])
    out = run(llm.ask("q", [str(askroot)], []))
    assert out["ok"] and out["answer_md"] == "The deploy runs at 18:00."
