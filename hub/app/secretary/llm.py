"""The secretary's model layer: every model call goes through here (CONTRACT §10, C1 ↔ C2).

    await digest_session(condensed) -> {title, repos, outcome, summary, shipped, decisions, open_loops, blockers, usage}
    await daily_brief(material)     -> {headline, lede, discord_text, usage}
    await ask(question, roots, deny_globs, max_turns=8) -> {answer_md, citations, files_read, refused, usage, ok, error}
    usage_state() / await probe_usage() / available()

Calls run the Claude Agent SDK, which spawns its bundled `claude` CLI and talks stream-json to it. The
recipe is research agent_sdk.json's, measured in this image:
- a short system prompt that REPLACES Claude Code's (12k tokens → about 600), no tools but the CLI's
  StructuredOutput for digests and briefs, JSON-schema output, thinking off (low-effort adaptive thinking on
  the models that refuse "disabled", see thinking_options);
- isolation: setting_sources=[] (no settings files, CLAUDE.md, hooks, skills, agents or MCP servers of
  anyone's), strict MCP config with none, no plugins, no session file (--no-session-persistence), no
  telemetry; HOME and CLAUDE_CONFIG_DIR are a folder under the hub's data folder, so nothing the CLI
  writes lands in the user's ~/.claude;
- the Claude login travels only in the CLI's env (options.env), never in this process's os.environ;
  other secret-looking variables the hub has are blanked for the child (the SDK passes the whole
  environment on, and options.env can only override). Never --bare: it refuses OAuth tokens.

digest_session and daily_brief raise a ModelError subclass on failure (the class name is what the
scheduler records); ask returns an answer that says what went wrong instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .. import config
from . import usage as _usage

try:  # comes with mcp; a second check of the CLI's own schema validation
    import jsonschema
except Exception:  # pragma: no cover
    jsonschema = None

log = logging.getLogger(__name__)

# Read by the SDK from this process's env (not options.env): skips a `claude -v` spawn per call.
os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")

MAX_PROCESSES = 2  # each call is a ~300 MB CLI process
DIGEST_TIMEOUT_S = 180.0  # below the scheduler's own limits (240 / 420 / 420 s), so ours fire first
BRIEF_TIMEOUT_S = 300.0
ASK_TIMEOUT_S = 360.0

# Inherited variables whose value the CLI must not see (the hub token, webhook paths, API keys).
_SECRETISH = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|WEBHOOK|CREDENTIAL|PRIVATE|_KEY$|NOTIFY_URL", re.I)


# ---------------------------------------------------------------- errors


class ModelError(RuntimeError):
    """A model call did not give a usable result. `kind` is a short code, `usage` what it cost."""

    kind = "failed"

    def __init__(self, message: str, usage: dict | None = None):
        super().__init__(message)
        self.usage = usage or {}


class ModelUnavailable(ModelError):
    kind = "no_login"


class ModelTimeout(ModelError):
    kind = "timeout"


class ModelMaxTurns(ModelError):
    kind = "max_turns"


class ModelRateLimited(ModelError):
    kind = "rate_limited"


class ModelAuthFailed(ModelError):
    kind = "auth"


class ModelBadOutput(ModelError):
    kind = "bad_output"


class ModelFailed(ModelError):
    kind = "failed"


class ModelCliMissing(ModelFailed):
    kind = "cli_missing"  # the image has no claude CLI


class ModelCliError(ModelFailed):
    kind = "cli_error"  # the CLI process broke before it gave a result


# ---------------------------------------------------------------- isolation


def claude_home() -> Path:
    """HOME for the CLI: <data>/secretary/claude-home (0700). Its state dir is .claude inside it."""
    home = Path(config.DATA) / "secretary" / "claude-home"
    for d in (home, home / ".claude", home / "work"):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return home


def sdk_env(token: str) -> dict:
    """The env the CLI child gets on top of the hub's own. The only place the token goes."""
    home = claude_home()
    env = {k: "" for k in os.environ if _SECRETISH.search(k)}
    env.update({
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "CLAUDE_CODE_OAUTH_TOKEN": token,
        "ANTHROPIC_API_KEY": "",  # would outrank the OAuth token
        "ANTHROPIC_AUTH_TOKEN": "",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_AGENT_SDK_CLIENT_APP": "deskmate-hub/1.0",
    })
    return env


def base_options(model: str, system: str, cwd: str, token: str, **more: Any) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=model,
        system_prompt=system,  # a plain string replaces Claude Code's prompt
        setting_sources=[],  # no user/project/local settings, CLAUDE.md, hooks, skills, agents, MCP
        strict_mcp_config=True,
        mcp_servers={},
        skills=[],
        plugins=[],
        permission_mode="dontAsk",  # nothing prompts; whatever is not allowed is refused
        cwd=cwd,
        env=sdk_env(token),
        extra_args={"no-session-persistence": None},  # no transcript of our own calls
        **more,
    )


# Models that refuse a disabled thinking config with a 400 (Claude Sonnet 5.5, Opus 5.5, Fable 5/5.1, Mythos):
# thinking cannot be switched off there, only kept short with a low effort.
NO_THINKING_OFF = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-", "claude-mythos-")


def thinking_options(model: str) -> dict:
    """Thinking off where the model allows it (Haiku 4.5 and the older models); on the newest models,
    adaptive thinking at low effort, which a summary needs and which they accept."""
    if str(model or "").startswith(NO_THINKING_OFF):
        return {"thinking": {"type": "adaptive"}, "effort": "low"}
    return {"thinking": {"type": "disabled"}}


def available() -> bool:
    """A Claude login exists (config.OAUTH_TOKEN, read fresh from the secret file)."""
    try:
        return bool(_usage.oauth_token())
    except Exception:
        return False


def token_or_raise() -> str:
    tok = _usage.oauth_token()
    if not tok:
        raise ModelUnavailable("The secretary has no Claude login.")
    return tok


# ---------------------------------------------------------------- the message stream


@dataclass
class Collector:
    """Turns the SDK's message stream into texts, tool errors, rate-limit events and usage."""

    model: str
    result: Any = None
    texts: list = field(default_factory=list)
    tool_errors: list = field(default_factory=list)
    tool_uses: dict = field(default_factory=dict)
    rate_limits: list = field(default_factory=list)
    assistant_errors: list = field(default_factory=list)
    api_retries: int = 0
    failure: str | None = None  # timeout | cli_missing | cli_error (the run itself broke)
    detail: str = ""

    def feed(self, msg: Any) -> None:
        if isinstance(msg, AssistantMessage):
            if getattr(msg, "error", None):
                self.assistant_errors.append(str(msg.error))
            for b in msg.content or []:
                if isinstance(b, TextBlock) and b.text.strip():
                    self.texts.append(b.text)
                elif isinstance(b, ToolUseBlock):
                    self.tool_uses[b.id] = (b.name, b.input or {})
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock) and b.is_error:
                    name, ti = self.tool_uses.get(b.tool_use_id, ("?", {}))
                    text = b.content if isinstance(b.content, str) else json.dumps(b.content, default=str)
                    self.tool_errors.append({"tool": name, "error": (text or "")[:300],
                                             "input": {k: ti[k] for k in ("file_path", "path", "pattern", "glob")
                                                       if k in ti}})
        elif isinstance(msg, RateLimitEvent):
            i = msg.rate_limit_info
            self.rate_limits.append({"status": i.status, "rate_limit_type": i.rate_limit_type,
                                     "resets_at": i.resets_at, "utilization": i.utilization})
        elif isinstance(msg, SystemMessage) and msg.subtype == "api_retry":
            self.api_retries += 1
        elif isinstance(msg, ResultMessage):
            self.result = msg

    def usage(self) -> dict:
        """Tokens and the CLI's list-price estimate (on a plan token it is not billed; the plan meter is)."""
        r = self.result
        tok = (getattr(r, "usage", None) or {}) if r is not None else {}
        return {
            "model": self.model,
            "input_tokens": int(tok.get("input_tokens") or 0),
            "output_tokens": int(tok.get("output_tokens") or 0),
            "cache_read_tokens": int(tok.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(tok.get("cache_creation_input_tokens") or 0),
            "cost_usd": round(float(getattr(r, "total_cost_usd", 0) or 0), 6) if r is not None else 0.0,
            "turns": int(getattr(r, "num_turns", 0) or 0) if r is not None else 0,
            "ms": int(getattr(r, "duration_ms", 0) or 0) if r is not None else 0,
            "outcome": self.outcome(),
        }

    def outcome(self) -> str:
        """ok | timeout | cli_missing | cli_error | no_result | max_turns | bad_output | auth | rate_limited | failed"""
        if self.failure:
            return self.failure
        r = self.result
        if r is None:
            return "no_result"
        sub = str(getattr(r, "subtype", "") or "")
        status = getattr(r, "api_error_status", None)
        errs = " ".join(self.assistant_errors).lower()
        if sub == "error_max_turns":
            return "max_turns"
        if sub == "error_max_structured_output_retries":
            return "bad_output"
        if not getattr(r, "is_error", False) and sub == "success":
            return "ok"
        if status in (401, 403) or "auth" in errs:
            return "auth"
        if status == 429 or "rate_limit" in errs or any(e.get("status") == "rejected" for e in self.rate_limits):
            return "rate_limited"
        return "failed"

    def why(self) -> str:
        """A short, readable reason (never the prompt, never a token)."""
        r = self.result
        bits = [self.outcome()]
        status = getattr(r, "api_error_status", None) if r is not None else None
        if status:
            bits.append(f"HTTP {status}")
        if self.detail:
            bits.append(self.detail)
        elif r is not None and getattr(r, "is_error", False):
            text = "; ".join(str(e) for e in (getattr(r, "errors", None) or [])) or str(getattr(r, "result", "") or "")
            if text:
                bits.append(text[:200])
        return ": ".join(bits[:1]) + (f" ({', '.join(bits[1:])})" if bits[1:] else "")


_slots: dict = {}


def _slot() -> asyncio.Semaphore:
    """One semaphore per event loop (tests run several loops; the hub runs one)."""
    loop = asyncio.get_running_loop()
    sem = _slots.get(id(loop))
    if sem is None:
        _slots.clear()
        sem = _slots[id(loop)] = asyncio.Semaphore(MAX_PROCESSES)
    return sem


def _client(options: ClaudeAgentOptions):
    """The SDK client (a seam for the tests' stub)."""
    return ClaudeSDKClient(options=options)


async def run(options: ClaudeAgentOptions, prompt: str, timeout_s: float) -> Collector:
    """One prompt, read up to and including the result. Never raises for the run's own failures.

    anyio.fail_after, not asyncio.wait_for: the SDK's close() is shielded only against anyio
    cancellation, so a plain asyncio cancel can leave the CLI child running."""
    col = Collector(model=str(options.model or ""))
    async with _slot():
        try:
            with anyio.fail_after(timeout_s):
                async with _client(options) as client:
                    await client.query(prompt)
                    async for msg in client.receive_response():
                        col.feed(msg)
        except TimeoutError:
            col.failure, col.detail = "timeout", f"no answer within {int(timeout_s)} s"
        except CLINotFoundError:
            col.failure, col.detail = "cli_missing", "the claude CLI is missing"
        except Exception as exc:
            # The CLI exits non-zero after an error result; the result we already have says more.
            if col.result is None:
                col.failure, col.detail = "cli_error", exc.__class__.__name__
    _usage.note_rate_limits(col.rate_limits)
    return col


_ERRORS = {"timeout": ModelTimeout, "max_turns": ModelMaxTurns, "rate_limited": ModelRateLimited,
           "auth": ModelAuthFailed, "bad_output": ModelBadOutput, "cli_missing": ModelCliMissing,
           "cli_error": ModelCliError}


def raise_for(col: Collector, what: str) -> None:
    out = col.outcome()
    if out != "ok":
        cls = _ERRORS.get(out, ModelFailed)
        raise cls(f"{what} failed: {col.why()}", col.usage())


async def structured(model: str, system: str, prompt: str, schema: dict, *, timeout_s: float,
                     what: str) -> tuple[dict, dict]:
    """Recipe A: one request, no tools, a schema-checked object back. Returns (data, usage); raises ModelError."""
    token = token_or_raise()
    with tempfile.TemporaryDirectory(prefix="call-", dir=claude_home() / "work") as scratch:
        opts = base_options(model, system, scratch, token,
                            tools=[],  # the CLI still adds its StructuredOutput tool
                            output_format={"type": "json_schema", "schema": schema},
                            max_turns=3,  # one request already counts 2; one schema retry fits
                            **thinking_options(model))
        col = await run(opts, prompt, timeout_s)
    raise_for(col, what)
    data = getattr(col.result, "structured_output", None)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise ModelBadOutput(f"{what} failed: no structured answer", col.usage())
    if jsonschema is not None:
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as exc:
            raise ModelBadOutput(f"{what} failed: answer does not match the schema ({exc.message[:120]})",
                                 col.usage()) from None
    return data, col.usage()


# ---------------------------------------------------------------- the interface C1 calls


async def digest_session(condensed: dict) -> dict:
    from .digest import digest_session as _f

    return await _f(condensed)


async def daily_brief(material: dict) -> dict:
    from .brief import daily_brief as _f

    return await _f(material)


async def ask(question: str, roots: list[str], deny_globs: list[str], max_turns: int = 8) -> dict:
    from .ask import ask as _f

    return await _f(question, roots, deny_globs, max_turns=max_turns)


def usage_state() -> dict:
    return _usage.usage_state()


async def probe_usage() -> None:
    await _usage.probe_usage()
