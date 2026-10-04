"""Ask: a question answered from the shared folders with Read, Grep and Glob only (contract §1 A).

Two locks, each enough on its own (research agent_sdk.json, proof runs B and B_cli):
1. The CLI's own permission rules: `Read(**/<glob>)` deny rules passed as flag settings. The CLI applies
   them to Read (the literal path, and again the symlink target) and turns them into ripgrep exclusions,
   so Glob and Grep never list or search those files.
2. Our PreToolUse hook (PathGuard), which runs on every tool call that passes input validation and which
   permission modes cannot skip: it confines every path to the roots after resolving symlinks, applies the
   deny list per path component and case-insensitively, appends the deny list to Grep's own globs, refuses
   any other tool, and fails closed on any error. A PostToolUse check stops the run if a Grep result still
   names a refused file.

The deny list is the redaction research's (redaction/deny_paths.py) plus the contract's credential globs
(*.env, *.cred.env, .env*, id_*, *.pem, *.key, credentials*, secret*, *.p12, *.keystore,
service-account*.json, anything under a secrets folder), plus whatever the caller adds. It is the one deny
list for Ask: the scheduler adds nothing of its own. Two exceptions keep ordinary work readable:
`secretar*` (the secretary's own code and docs) is not a secret, and code files named credentials*.py or
id_*.ts are code, not credentials: Read may open them and Glob may list them, Grep never searches them.
An env or key file is refused under any name.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from typing import Any

from claude_agent_sdk import HookMatcher

from .. import config
from . import gate, llm, prompts
from . import usage as _usage

log = logging.getLogger(__name__)

TOOLS = ("Read", "Grep", "Glob")
SUBMIT = "StructuredOutput"  # the CLI's own answer tool when an output schema is set

DENY_DIRS = (
    ".git", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", "gcloud",
    ".claude",  # a Claude config folder: transcripts, history, credentials (memory roots are below it, allowed)
    ".viway", ".auth", ".config", "keyrings", "secrets", ".secrets",
)
DENY_FILES = (
    # env files
    ".env", ".env*", "*.env", "*.env.*", ".envrc", "*.cred", "*.cred.*",
    # keys, keystores, certificates
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.p8", "*.ppk", "*.der", "*.kdbx", "*.age",
    "*.gpg", "*.asc", "*.ovpn", "*.mobileprovision", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "key.properties", "*.tfvars", "*.tfstate", "*.tfstate.*",
    # tool credential stores
    ".netrc", "_netrc", ".pgpass", ".my.cnf", ".git-credentials", ".npmrc", ".pypirc", ".yarnrc.yml", ".htpasswd",
    ".dockercfg", ".boto", ".s3cfg", "rclone.conf", ".vault-token", "kubeconfig*", "*.kubeconfig",
    ".credentials.json", ".claude.json", ".mcp.json", "settings.local.json",
    # credential and secret documents
    "client_secret*.json", "service-account*.json", "*service-account*.json", "*serviceaccount*.json",
    "firebase-adminsdk*.json", "google-services.json", "googleservice-info.plist",
    "*.secret", "*.secrets", "*-secret.*", "*_secret.*", "*-secrets.*", "*_secrets.*", "*-secret-*", "*_secret_*",
    # tokens, TOTP, webhooks, browser sessions
    "token", "*token*.txt", "*.token", "*_token", "*-token", "*.jwt", "*totp*", "*otp_secret*", "*.otpauth",
    "*webhook*.url", "cookies", "cookies.sqlite", "cookies-journal", "login data", "login data-journal",
    "web data", "web data-journal", "local state", "*storage_state*.json", "*storagestate*.json",
    # databases, dumps, transcripts
    "*.sqlite", "*.sqlite3", "*.db", "*.db-wal", "*.db-shm", "dump.rdb", "*.dump", "*.sql.gz", "*.bak", "*.jsonl",
)
# The contract's broad name globs, with their exceptions (see _broad): only the CLI cannot express those,
# so its rules use these narrower spellings and the hook applies the broad ones. credentials* is spelt out
# per data extension: a rule like credentials.* would also refuse code such as credentials.py, which the
# deny list allows (an env or key file stays refused under any name: DENY_FILES).
CRED_EXT = ("json", "yaml", "yml", "toml", "ini", "cfg", "conf", "txt", "xml", "properties", "plist", "csv", "env",
            "db", "sqlite", "bak", "enc", "gpg", "dat", "key", "pem", "p12", "pfx", "jks")
CLI_EXTRA = ("secret", "secret.*", "secret_*", "secret-*", "secrets.*", "secrets_*", "secrets-*", "credentials",
             *(f"credentials{sep}{ext}" for sep in (".", "_*.", "-*.") for ext in CRED_EXT))
CODE_EXT = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".dart", ".go", ".rs", ".java", ".kt",
            ".kts", ".rb", ".php", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".scala", ".ex", ".exs",
            ".vue", ".svelte"}


def _broad(name: str, last: bool) -> str | None:
    """secret* (not secretar*), credentials* (not code), id_* (files only, not code)."""
    ext = os.path.splitext(name)[1]
    if name.startswith("secret") and not name.startswith("secretar"):
        return "secret*"
    if name.startswith("credentials") and not (last and ext in CODE_EXT):
        return "credentials*"
    if last and name.startswith("id_") and ext not in CODE_EXT:
        return "id_*"
    return None


def split_globs(globs) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A caller's globs as (per-component globs, whole-path globs), lower-case. 'secrets/**' is the folder."""
    comp, path = [], []
    for g in globs or ():
        g = str(g).strip().lower()
        while g.startswith("**/"):
            g = g[3:]
        if g.endswith("/**"):
            g = g[:-3]
        g = g.strip("/")
        if not g or g in ("*", "**"):
            continue  # "deny everything" is not a glob anyone means; the roots decide what is shared
        (path if "/" in g else comp).append(g)
    return tuple(comp), tuple(path)


def ci_glob(g: str) -> str:
    """A ripgrep glob matching either case: '*.env' → '*.[eE][nN][vV]'. Spaces become '?' (Grep splits on them)."""
    out, in_class = [], False
    for ch in g:
        if in_class:
            out.append(ch + ch.upper() if ch.isalpha() and ch.islower() else ch)
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
            out.append(ch)
        elif ch.isalpha():
            out.append(f"[{ch.lower()}{ch.upper()}]")
        elif ch == " ":
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


class PathGuard:
    """The SDK-side gate (PreToolUse on every tool call, PostToolUse on Read and Grep)."""

    def __init__(self, roots: list[str], deny_globs=()):
        self.roots = [os.path.realpath(r) for r in roots]
        self._by_len = sorted(self.roots, key=len, reverse=True)  # the most specific root wins
        self.extra_comp, self.extra_path = split_globs(deny_globs)
        self.comp = tuple(dict.fromkeys(DENY_DIRS + DENY_FILES + self.extra_comp))
        self.denied: list[dict] = []
        self.files_read: list[str] = []
        self.leak = False

    # ------------------------------------------------------------ verdicts

    def root_of(self, p: str) -> str | None:
        return next((r for r in self._by_len if p == r or p.startswith(r.rstrip("/") + "/")), None)

    def deny_reason(self, rel: str) -> str | None:
        """The glob a root-relative path hits, or None. Every component is checked, lower-case."""
        parts = [x.lower() for x in rel.split("/") if x not in ("", ".")]
        for i, part in enumerate(parts):
            for g in self.comp:
                if fnmatch.fnmatchcase(part, g):
                    return g
            b = _broad(part, i == len(parts) - 1)
            if b:
                return b
        low = "/".join(parts)
        for g in self.extra_path:
            if fnmatch.fnmatchcase(low, g) or fnmatch.fnmatchcase(low, "*/" + g):
                return g
        return None

    def check_path(self, raw: str | None, cwd: str) -> str | None:
        """None when allowed, else the reason. Both the path as written and its symlink target must pass."""
        raw = raw or cwd
        if "\x00" in raw:
            return "the path contains NUL"
        p = os.path.expanduser(raw)
        p = p if os.path.isabs(p) else os.path.join(cwd, p)
        for cand in (os.path.normpath(p), os.path.realpath(p)):
            root = self.root_of(cand)
            if root is None:
                return f"{raw} is outside the shared folders"
            g = self.deny_reason(os.path.relpath(cand, root))
            if g:
                return f"{raw} matches the refused pattern {g}"
        return None

    def _check_glob_pattern(self, pattern: str, base: str) -> str | None:
        if ".." in pattern.replace("\\", "/").split("/"):
            return "'..' is not allowed in patterns"
        if os.path.isabs(pattern):
            cut = min([i for i, c in enumerate(pattern) if c in "*?[{"] or [len(pattern)])
            return self.check_path(os.path.dirname(pattern[:cut]) or "/", base)
        g = self.deny_reason(pattern.replace("**/", "").replace("*", "x"))
        return f"the pattern {pattern} asks for refused files ({g})" if g else None

    # ------------------------------------------------------------ what the CLI and ripgrep get

    def _skip(self, g: str) -> bool:
        """A glob that matches a part of a root's own path (e.g. '.claude' for a memory folder) is left to
        the hook, which applies it below the root only; the CLI would refuse the whole root."""
        return any(fnmatch.fnmatchcase(part.lower(), g) for r in self.roots for part in r.split("/") if part)

    def cli_rules(self) -> list[str]:
        rules: list[str] = []
        for g in dict.fromkeys(self.comp + CLI_EXTRA):
            if not self._skip(g):
                rules += [f"Read(**/{g})", f"Read(**/{g}/**)"]
        rules += [f"Read(**/{g})" for g in self.extra_path]
        return rules

    def rg_excludes(self) -> str:
        globs = list(self.comp) + ["secret", "secret[!a]*", "secreta", "secreta[!r]*", "credentials*", "id_*"]
        return " ".join(f"!{ci_glob(g)}" for g in dict.fromkeys(globs) if not self._skip(g))

    # ------------------------------------------------------------ hooks

    def _deny(self, tool: str, ti: dict, reason: str) -> dict:
        self.denied.append({"tool": tool, "reason": reason,
                            "input": {k: str(ti[k])[:300] for k in ("file_path", "path", "pattern", "glob")
                                      if k in ti and k != "glob"}})
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"[deskmate] refused: {reason}. Do not retry it; continue without it.",
        }}

    async def pre_tool_use(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        name = str((input_data or {}).get("tool_name", ""))
        ti = dict((input_data or {}).get("tool_input") or {})
        try:
            if name == SUBMIT:
                return {}  # the answer itself, no file access
            cwd = input_data.get("cwd") or self.roots[0]
            if name not in TOOLS:
                return self._deny(name, ti, f"the tool {name} is not available")
            if name == "Read":
                fp = ti.get("file_path")
                reason = self.check_path(str(fp), cwd) if fp else "file_path is required"
            elif name == "Glob":
                base = str(ti.get("path") or cwd)
                reason = self.check_path(base, cwd) or self._check_glob_pattern(str(ti.get("pattern", "")), base)
            else:  # Grep
                reason = self.check_path(str(ti["path"]) if ti.get("path") else None, cwd)
                if reason is None:
                    # ripgrep skips refused files inside an allowed folder: the CLI splits `glob` on spaces
                    # into --glob arguments, and a later '!x' excludes. No decision here: the CLI applies
                    # updatedInput and still runs its own checks.
                    excl = self.rg_excludes()
                    ti["glob"] = f"{ti['glob']} {excl}" if ti.get("glob") else excl
                    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": ti}}
            return self._deny(name, ti, reason) if reason else {}
        except Exception as exc:  # fail closed
            return self._deny(name, ti, f"guard error ({exc.__class__.__name__})")

    def _paths_in(self, resp: Any) -> list[str]:
        if isinstance(resp, dict):
            names = [str(x) for x in (resp.get("filenames") or []) if isinstance(x, str)]
            text = resp.get("content")
        else:
            names, text = [], resp
        if isinstance(text, str):
            for line in text.splitlines():
                head = line.split(":", 1)[0].strip()
                if head and ("/" in head or "." in head) and " " not in head:
                    names.append(head)
        return names

    async def post_tool_use(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        try:
            name = (input_data or {}).get("tool_name")
            ti = (input_data or {}).get("tool_input") or {}
            cwd = input_data.get("cwd") or self.roots[0]
            if name == "Read" and ti.get("file_path"):
                p = ti["file_path"]
                p = p if os.path.isabs(p) else os.path.join(cwd, p)
                self.files_read.append(os.path.realpath(p))
            elif name == "Grep":
                base = str(ti.get("path") or cwd)
                base = base if os.path.isdir(base) else os.path.dirname(base)
                for n in self._paths_in(input_data.get("tool_response")):
                    # Relative names may be relative to the search folder or to the working folder.
                    for p in ([n] if os.path.isabs(n) else [os.path.join(base, n), os.path.join(cwd, n)]):
                        p = os.path.normpath(p)
                        root = self.root_of(p)
                        if root and self.deny_reason(os.path.relpath(p, root)):
                            self.leak = True
                            self.denied.append({"tool": "Grep", "reason": "a result named a refused file",
                                                "input": {"path": n[:300]}})
                            return {"continue_": False, "stopReason": "A search result named a refused file."}
            return {}
        except Exception as exc:
            log.info("ask post-tool check: %s", exc.__class__.__name__)
            self.leak = True
            return {"continue_": False, "stopReason": "The file check failed."}

    # ------------------------------------------------------------ citations

    def cite(self, path: str, line) -> dict | None:
        """{path relative to its root, line, root, abs} for a readable file, else None."""
        if not isinstance(path, str) or not path.strip():
            return None
        raw = path.strip()
        cands = [raw] if os.path.isabs(raw) else [os.path.join(r, raw) for r in self.roots]
        for c in cands:
            c = os.path.normpath(c)
            if self.check_path(c, self.roots[0]) is None and os.path.isfile(c):
                root = self.root_of(c)
                try:
                    ln = int(line) if line is not None else None
                except (TypeError, ValueError):
                    ln = None
                return {"path": os.path.relpath(c, root), "line": ln if ln and ln > 0 else None,
                        "root": root, "abs": c}
        return None


def shared_roots(roots) -> list[str]:
    """The caller's roots that exist and lie inside what the hub may read (config.readable_roots())."""
    out: list[str] = []
    for r in roots or []:
        if not isinstance(r, str) or not r.strip():
            continue
        real = os.path.realpath(r)
        if os.path.isdir(real) and config.is_readable(real) and real not in out:
            out.append(real)
    return out


def ask_options(guard: PathGuard, system: str, token: str, max_turns: int):
    return llm.base_options(
        config.ASK_MODEL, system, guard.roots[0], token,
        tools=list(TOOLS),  # the only tools the model sees
        add_dirs=guard.roots[1:],
        settings=json.dumps({"permissions": {"deny": guard.cli_rules()}}),
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard.pre_tool_use], timeout=30)],
               "PostToolUse": [HookMatcher(matcher="Read|Grep", hooks=[guard.post_tool_use], timeout=30)]},
        output_format={"type": "json_schema", "schema": prompts.ASK_SCHEMA},
        max_turns=max_turns + 1,  # the answer itself is one more turn
        max_buffer_size=8 * 1024 * 1024,  # one tool result is one stdout frame
    )


def _says(code: str) -> str:
    """One sentence per failure, the same table the scheduler shows on the Ask page (gate.FAILURES)."""
    return gate.failure_sentence(code)


def _result(answer: str, *, ok: bool, error: str | None, usage: dict | None = None, citations=(), files_read=(),
            refused=(), partial: str = "") -> dict:
    return {"answer_md": answer, "citations": list(citations), "files_read": list(files_read),
            "refused": list(refused), "usage": usage or {}, "ok": ok, "error": error, "partial_md": partial}


async def ask(question: str, roots: list[str], deny_globs: list[str], max_turns: int = 8) -> dict:
    """{answer_md, citations: [{path, line, root, abs}], files_read: [relative paths], refused, usage, ok, error,
    partial_md}.

    Never raises for the call's own failures: the answer says what went wrong (error holds the code, one of
    gate.FAILURES; partial_md holds what the model had written before it failed, else "")."""
    q = str(question or "").strip()
    if not q:
        return _result(_says("empty"), ok=False, error="empty")
    token = _usage.oauth_token()
    if not token:
        return _result(_says("no_login"), ok=False, error="no_login")
    shared = shared_roots(roots)
    if not shared:
        return _result(_says("no_roots"), ok=False, error="no_roots")
    guard = PathGuard(shared, deny_globs)
    system = prompts.ask_system(config.OWNER, guard.roots)
    turns = max(1, min(int(max_turns or 8), 20))
    col = await llm.run(ask_options(guard, system, token, turns), q, llm.ASK_TIMEOUT_S)

    out = col.outcome()
    data = getattr(col.result, "structured_output", None) if col.result is not None else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    answer = ""
    cites: list[dict] = []
    if isinstance(data, dict):
        answer = str(data.get("answer_md") or "").strip()
        for c in data.get("citations") or []:
            if isinstance(c, dict):
                x = guard.cite(c.get("path"), c.get("line"))
                if x and not any(y["abs"] == x["abs"] and y["line"] == x["line"] for y in cites):
                    cites.append(x)
    if not answer and col.result is not None and out == "ok":
        answer = str(getattr(col.result, "result", "") or "").strip() or "\n\n".join(col.texts).strip()
    read = []
    for p in dict.fromkeys(guard.files_read):
        x = guard.cite(p, None)
        if x:
            read.append(x["path"])
            if not cites and out == "ok" and answer:
                cites.append(x)
    refused = [{"tool": d["tool"], "path": d["input"].get("file_path") or d["input"].get("path")
                or d["input"].get("pattern"), "reason": d["reason"]} for d in guard.denied]
    refused += [{"tool": e["tool"], "path": e["input"].get("file_path") or e["input"].get("path"),
                 "reason": e["error"][:200]} for e in col.tool_errors
                if "denied" in e["error"].lower() and not e["error"].startswith("[deskmate]")]
    use = col.usage()
    if guard.leak:  # whatever was written may quote the refused file: nothing of it is kept
        return _result(_says("refused_output"), ok=False, error="refused_output", usage=use, refused=refused)
    if out == "ok" and answer:
        return _result(answer, ok=True, error=None, usage=use, citations=cites, files_read=read, refused=refused)
    code = out if out != "ok" else "bad_output"
    say = _says(code) if code in gate.FAILURES else f"Ask failed ({col.why()})."
    if code not in gate.FAILURES:
        code = "failed"
    if answer:  # a partial answer is worth keeping, with the reason under it
        say = f"{answer}\n\n_{say}_"
    return _result(say, ok=False, error=code, usage=use, citations=cites, files_read=read, refused=refused,
                   partial=answer)
