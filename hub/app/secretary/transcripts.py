"""Claude Code transcripts -> the conversation the Session page shows, the counts lists show, and search.

Claude Code writes one append-only JSONL file per session at <config dir>/projects/<slug>/<uuid>.jsonl.
Only those main files are read: never <uuid>/subagents/*, never records marked isSidechain. A session
counts when its first cwd lies inside SESSIONS_ROOTS (config.in_sessions_roots); others are never parsed
and only a "skipped" marker without content is kept, so they are not opened again.

Parsing is incremental. A per-session state (JSON in cc_sessions.parse_state) says where the parser
stopped: the next item index, the turn, the item that can still grow (a step group, or an answer whose
API message continues) and the tool calls still waiting for a result. Only complete lines are consumed
(the last one may be half written), and rows are committed in short chunks so the desk never waits.

Items, in file order (research contract §2.3):
  prompt     a user record with origin.kind "human" (or a user-typed slash command); a queued_command
             attachment typed while a turn runs is a prompt with mid_turn true in the same turn
  assistant  text blocks; consecutive blocks of one API message merge; '<synthetic>' only when it is an
             API error; thinking is never stored
  steps      consecutive tool calls until the next other item; results match by tool_use_id
  compact    a compact_boundary; interrupt  "[Request interrupted by user…"
Every stored text goes through redact.scrub() first. Tool outputs are never stored, only their status.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from collections import Counter
from typing import Iterator, NamedTuple
from urllib.parse import urlsplit

from .. import config
from . import gitlog, store, util
from .redact import scrub

log = logging.getLogger(__name__)

# Bump when what is stored changes (the parser or the redactor): every in-scope transcript is then read again
# from the start at the next scan, so old rows are replaced; digests keep what they cover.
# 2: the redactor treats quoted wordy literals on a secret's own key as secrets; "HEAD" is not a branch.
PARSER_VERSION = 2
SKIP_OUTSIDE = "outside the folders it may read"
MAX_TEXT = 200_000          # longest prompt or answer kept (the API cuts at 20,000 and serves the rest on demand)
MAX_COMMAND = 2000
MAX_ARG = 160
MAX_PENDING = 1000
CHUNK_ROWS = 1500           # rows per write transaction
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# ---------------------------------------------------------------------------------------------- reading
MAX_LINE = 256 << 20


class Rec(NamedTuple):
    start: int
    end: int
    obj: dict


def iter_records(path: str, offset: int = 0, stats: dict | None = None) -> Iterator[Rec]:
    """Every complete JSONL record at or after byte `offset` (from the transcripts research, unchanged).

    An offset inside a line resyncs to the next line; a last line without '\\n' is still being written and
    is not yielded; invalid lines are counted in stats['bad']; an offset past the end restarts at 0.
    stats['next_offset'] is where to resume.
    """
    st = {} if stats is None else stats
    st.update(bad=0, reset=False, next_offset=offset)
    if offset > os.path.getsize(path):
        offset = 0
        st.update(reset=True, next_offset=0)
    with open(path, "rb") as f:
        pos = offset
        if pos:
            f.seek(pos - 1)
            if f.read(1) != b"\n":
                tail = f.readline()
                if not tail.endswith(b"\n"):
                    return
                pos += len(tail)
                st["next_offset"] = pos
        while True:
            line = f.readline(MAX_LINE)
            if not line.endswith(b"\n"):
                if len(line) < MAX_LINE:
                    return
                n = len(line)
                while not line.endswith(b"\n"):
                    line = f.readline(MAX_LINE)
                    if not line:
                        return
                    n += len(line)
                pos += n
                st["bad"] += 1
                st["next_offset"] = pos
                continue
            start, pos = pos, pos + len(line)
            st["next_offset"] = pos
            try:
                obj = json.loads(line)
            except ValueError:
                st["bad"] += 1
                continue
            if isinstance(obj, dict):
                yield Rec(start, pos, obj)


# ---------------------------------------------------------------------------------------------- helpers
SYSREM = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
IDE_PREFIXES = ("<ide_opened_file>", "<ide_selection>")
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+")
EXIT_RE = re.compile(r"\s*Exit code (\d+)")
HTTP_RE = re.compile(r"^\s*([1-5]\d\d)\s*$", re.M)
GIT_COMMIT = re.compile(
    r"(?:^|[;&|(\s])git(?:[ \t]+(?:-[cC][ \t]+\S+|--[\w-]+(?:=\S+)?|-[A-Za-z]))*[ \t]+commit\b", re.M)
GIT_PUSH = re.compile(r"(?:^|[;&|(\s])git(?:[ \t]+(?:-[cC][ \t]+\S+|--[\w-]+(?:=\S+)?|-[A-Za-z]))*[ \t]+push\b", re.M)
GIT_DASH_C = re.compile(r"\bgit[ \t]+-C[ \t]+(\"[^\"]+\"|'[^']+'|\S+)")
COMMIT_LINE = re.compile(
    r"^\[(?P<branch>[^\]\n]*?) (?:\(root-commit\) )?(?P<sha>[0-9a-f]{7,40})\] (?P<subject>[^\n]*)$", re.M)
HEX_LINE = re.compile(r"^(?P<sha>[0-9a-f]{7,40})(?:[ \t]+(?P<subject>[^\n]*))?$", re.M)
# A personal notification script some people keep (claude-notify/notify.sh): its posts show as "Discord".
NOTIFY = re.compile(
    r"(?:^|[;&|(]|\b(?:then|do|else)\b)[ \t]*(?:[A-Za-z_]\w*=\S*[ \t]+)*(?:(?:ba)?sh[ \t]+)?"
    r"(?:~|\$HOME|\$\{HOME\}|/[^\s;&|]*)/\.config/claude-notify/notify\.sh(?=[ \t;&|)]|$)(?P<args>[^\n]*)",
    re.M)
DESKMATE_NOTIFY = "mcp__deskmate__notify"


def blocks_of(o: dict) -> list[dict]:
    c = (o.get("message") or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c or () if isinstance(b, dict)]


def text_of(blocks: list[dict], human: bool = False) -> str:
    parts = [b.get("text") or "" for b in blocks if b.get("type") == "text"]
    if human:
        parts = [p for p in parts if not p.lstrip().startswith(IDE_PREFIXES)]
    return SYSREM.sub("", "\n".join(parts)).strip()


def first_text(blocks: list[dict]) -> str:
    return next((b.get("text") or "" for b in blocks if b.get("type") == "text"), "").lstrip()


def result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text") or "" for x in c or () if isinstance(x, dict) and x.get("type") == "text")


def classify_user(o: dict) -> str:
    """tool_result | human | command | bash_input | interrupt | task_notification | peer | coordinator |
    compact_summary | meta | command_output | other (transcripts research §2)."""
    bl = blocks_of(o)
    if any(b.get("type") == "tool_result" for b in bl):
        return "tool_result"
    if o.get("isCompactSummary"):
        return "compact_summary"
    origin = (o.get("origin") or {}).get("kind")
    raw = first_text(bl)
    if origin == "task-notification" or raw.startswith("<task-notification>"):
        return "task_notification"
    if origin in ("peer", "coordinator"):
        return origin
    if o.get("isMeta"):
        return "meta"
    if raw.startswith("[Request interrupted by user"):
        return "interrupt"
    if origin == "human":
        return "human"
    if raw.startswith(("<command-name>", "<command-message>")):
        return "command"
    if raw.startswith("<bash-input>"):
        return "bash_input"
    if raw.startswith(("<local-command-", "<bash-stdout>", "<bash-stderr>", "Caveat:")):
        return "command_output"
    if origin or raw.startswith(("<system-reminder>", "[Image")):
        return "other"
    if raw or any(b.get("type") == "image" for b in bl):
        return "human"
    return "other"


def _tag(s: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", s, re.S)
    return m.group(1).strip() if m else None


def command_text(raw: str) -> str:
    if raw.startswith("<bash-input>"):
        return "!" + (_tag(raw, "bash-input") or "")
    name = _tag(raw, "command-name") or _tag(raw, "command-message") or ""
    if name and not name.startswith("/"):
        name = "/" + name
    return f"{name} {_tag(raw, 'command-args') or ''}".strip()


def notify_message(args: str) -> str:
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = args.split()
    return parts[0] if parts and parts[0] not in ("&&", "||", ";", "|", ">", "2>&1") else ""


def subject_from_cmd(cmd: str) -> str | None:
    m = re.search(r"""\bcommit\b[^\n]*?[ \t]-m[ \t]*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""", cmd)
    if m:
        s = m.group(1) if m.group(1) is not None else m.group(2)
        if not s.startswith("$(") and s.strip():
            return s.strip().splitlines()[0]
    m = re.search(r"""\bcommit\b[^\n]*<<-?[ \t]*['"]?(\w+)['"]?[^\n]*\n(.*?)^[ \t]*\1\b""", cmd, re.S | re.M)
    if m:
        return next((ln.strip() for ln in m.group(2).splitlines() if ln.strip()), None)
    return None


def one_line(s: str | None, limit: int = MAX_ARG) -> str:
    s = (s or "").strip()
    s = s.splitlines()[0].strip() if s else ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _first_str(inp: dict, prefer: tuple[str, ...] = ()) -> str:
    for k in prefer:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return next((v for v in inp.values() if isinstance(v, str) and v.strip()), "")


def describe_tool(name: str, inp: dict) -> tuple[str, str, str | None]:
    """(label, arg, host path) for a tool call (research contract §2.3, rule 4). arg is not yet redacted."""
    g = inp.get
    path = None
    label = name
    if name == "Bash":
        cmd = g("command") or ""
        m = NOTIFY.search(cmd) if "claude-notify/notify.sh" in cmd else None
        if m:
            return "Discord", notify_message(m.group("args")), None
        arg = g("description") or cmd
    elif name in EDIT_TOOLS or name == "Read":
        path = g("file_path") or g("notebook_path")
        arg = util.rel(path) if isinstance(path, str) and path.startswith("/") else (path or "")
        path = path if isinstance(path, str) and path.startswith("/") else None
    elif name == "Grep":
        where = g("path") or "."
        arg = f"{g('pattern') or ''} in {util.rel(where) if str(where).startswith('/') else where}"
    elif name in ("Glob", "LS"):
        arg = g("pattern") or g("path") or ""
    elif name == "WebFetch":
        u = urlsplit(str(g("url") or ""))
        arg = (u.netloc + u.path) if u.netloc else str(g("url") or "")
    elif name in ("WebSearch", "ToolSearch"):
        arg = g("query") or ""
    elif name in ("Agent", "Task"):
        arg = " · ".join(x for x in (g("subagent_type") or "general-purpose", g("description") or "") if x)
    elif name == "Workflow":
        arg = g("scriptPath") or g("resumeFromRunId") or "inline script"
    elif name == "Skill":
        arg = g("skill") or g("command") or ""
    elif name == "AskUserQuestion":
        qs = g("questions") or []
        n = len(qs) if isinstance(qs, list) else 1
        arg = f"{n} question" + ("" if n == 1 else "s")
    elif name == "Artifact":
        fp = g("file_path") or ""
        arg = g("label") or g("title") or (os.path.basename(fp) if fp else (g("url") or ""))
    elif name.startswith("mcp__"):
        label = name.rsplit("__", 1)[-1] or name
        arg = _first_str(inp, ("url", "query", "text", "path", "file_path"))
    else:
        arg = _first_str(inp)
    return label, str(arg or ""), path


def _dur_label(ms: int | None) -> str:
    if ms is None or ms < 10000:
        return "ok"
    s = ms // 1000
    return f"{s} s" if s < 120 else f"{s // 60} min"


def _ascii_enc(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path).lower()


def slug_may_be_in_roots(slug: str) -> bool:
    """A cheap test on the project folder name, so sessions started elsewhere are never opened.

    Claude Code names the folder after the launch cwd with separators replaced by '-'. That is lossy, so
    a match only means "maybe" and the transcript's first cwd decides; no match means "no" — except for
    names this encoding cannot judge (non-ASCII, or long names Claude Code shortens).
    """
    if not slug.isascii() or len(slug) >= 150:
        return True
    s = re.sub(r"[^A-Za-z0-9]", "-", slug).lower()
    for r in config.SESSIONS_ROOTS:
        e = _ascii_enc(r)
        if s == e or s.startswith(e + "-"):
            return True
    return False


def first_cwd(path: str, max_records: int = 400) -> str | None:
    """The cwd of the first record that has one: the folder the session started in.

    None: no record with a cwd yet (decide later). '': gave up after max_records or 8 MB without one.
    """
    n = 0
    try:
        for rec in iter_records(path):
            cwd = rec.obj.get("cwd")
            if isinstance(cwd, str) and cwd.startswith("/") and not rec.obj.get("isSidechain"):
                return os.path.normpath(cwd)
            n += 1
            if n >= max_records or rec.end > (8 << 20):
                return ""
    except OSError:
        return None
    return None


def main_transcripts() -> list[tuple[str, str, str]]:
    """(config dir, project folder, path) of every main transcript in CLAUDE_CONFIG_DIRS."""
    out = []
    for d in config.CLAUDE_CONFIG_DIRS:
        base = os.path.join(d, "projects")
        try:
            slugs = sorted(os.listdir(base))
        except OSError:
            continue
        for slug in slugs:
            pd = os.path.join(base, slug)
            try:
                names = os.listdir(pd)
            except OSError:
                continue
            for n in names:
                if n.endswith(".jsonl") and UUID_RE.match(n[:-6]):
                    out.append((d, slug, os.path.join(pd, n)))
    return out


def transcript_from_hook(path: str | None) -> tuple[str, str, str] | None:
    """Accept a hook's transcript_path only if it is a main transcript inside CLAUDE_CONFIG_DIRS."""
    if not path or not isinstance(path, str):
        return None
    p = os.path.normpath(path)
    for d in config.CLAUDE_CONFIG_DIRS:
        base = os.path.join(d, "projects") + "/"
        if p.startswith(base):
            parts = p[len(base):].split("/")
            if len(parts) == 2 and parts[1].endswith(".jsonl") and UUID_RE.match(parts[1][:-6]):
                return d, parts[0], p
    return None


def roots_signature() -> str:
    return util.sha10(json.dumps([sorted(config.SESSIONS_ROOTS), sorted(config.CLAUDE_CONFIG_DIRS)]))


# ---------------------------------------------------------------------------------------------- parser

def new_state() -> dict:
    return {"v": PARSER_VERSION, "idx": 0, "turn": 0, "open": None, "pending": {}, "models": {},
            "absorb_compact": False, "prompts": 0, "turns": 0, "steps": 0, "errors": 0, "compactions": 0,
            "subagents": 0, "assistant_chars": 0, "first_ts": None, "last_ts": None, "cwd": None,
            "rec_cwd": None, "branch": None, "ai_title": None, "custom_title": None, "first_prompt": None}


class _Batch:
    """Rows waiting for the next write transaction."""

    def __init__(self) -> None:
        self.items: dict[int, dict] = {}
        self.steps: dict[tuple[int, int], dict] = {}
        self.step_by_tid: dict[str, dict] = {}
        self.step_updates: list[tuple] = []
        self.cmds: dict[str, dict] = {}
        self.cmd_updates: list[tuple] = []
        self.links: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self.days: dict[str, list] = {}
        self.fts: list[tuple] = []
        self.assist_fts: dict[int, int | None] = {}   # item -> FTS rowid to replace (or None)
        self.dirty_steps: set[int] = set()

    def size(self) -> int:
        return (len(self.items) + len(self.steps) + len(self.step_updates) + len(self.cmds) + len(self.links)
                + len(self.files) + len(self.fts))


class Parser:
    """Turns records into cc_* rows for one session, continuing from a saved state."""

    def __init__(self, sid: str, state: dict, c) -> None:
        self.sid, self.s, self.c = sid, state, c
        self.b = _Batch()
        self.files_known: dict[str, str] = {r[0]: r[1] for r in c.execute(
            "select path, action from cc_files where session = ?", (sid,))}
        self.new_items = 0

    # ----------------------------------------------------------------- items

    def _day(self, ts: float | None, prompts: int = 0, steps: int = 0) -> None:
        if not ts:
            return
        d = util.day_of(ts)
        row = self.b.days.get(d)
        if row is None:
            self.b.days[d] = [ts, ts, prompts, steps]
        else:
            row[0], row[1] = min(row[0], ts), max(row[1], ts)
            row[2] += prompts
            row[3] += steps

    def _load_item(self, i: int) -> dict | None:
        row = self.b.items.get(i)
        if row is not None:
            return row
        r = self.c.execute("select * from cc_items where session = ? and idx = ?", (self.sid, i)).fetchone()
        if r is None:
            return None
        row = dict(r)
        row["flags"] = json.loads(row["flags"] or "{}")
        self.b.items[i] = row
        return row

    def _new_item(self, kind: str, ts: float | None, rec: Rec, text: str = "", flags: dict | None = None,
                  chars: int | None = None) -> dict:
        i = self.s["idx"]
        self.s["idx"] += 1
        row = {"session": self.sid, "idx": i, "turn": self.s["turn"], "kind": kind, "ts": ts, "ts_end": ts,
               "text": text, "chars": len(text) if chars is None else chars, "flags": flags or {},
               "offset": rec.start, "uuid": rec.obj.get("uuid")}
        self.b.items[i] = row
        self.new_items += 1
        return row

    def _close_open(self) -> None:
        self.s["open"] = None

    def _fts(self, body: str, item: int, turn: int, step: int | None, kind: str, ts: float | None) -> None:
        body = util.clean_for_index(body)
        if body.strip():
            self.b.fts.append((body, self.sid, item, turn, step, kind, ts))

    # ----------------------------------------------------------------- records

    def feed(self, rec: Rec) -> None:
        o = rec.obj
        if o.get("isSidechain"):
            return
        t = o.get("type")
        ts = util.parse_ts(o.get("timestamp"))
        if ts:
            self.s["first_ts"] = ts if self.s["first_ts"] is None else min(self.s["first_ts"], ts)
            self.s["last_ts"] = ts if self.s["last_ts"] is None else max(self.s["last_ts"], ts)
        cwd = o.get("cwd")
        if isinstance(cwd, str) and cwd.startswith("/"):
            self.s["rec_cwd"] = cwd
            if self.s["cwd"] is None:
                self.s["cwd"] = os.path.normpath(cwd)
        if isinstance(o.get("gitBranch"), str) and o["gitBranch"] and o["gitBranch"] != "HEAD":
            self.s["branch"] = o["gitBranch"]  # "HEAD": a detached checkout, or a cwd that is no repo at all
        if t == "user":
            self._user(rec, ts)
        elif t == "assistant":
            self._assistant(rec, ts)
        elif t == "attachment":
            self._attachment(rec, ts)
        elif t == "system" and o.get("subtype") == "compact_boundary":
            self._compact(rec, ts)
        elif t == "ai-title" and isinstance(o.get("aiTitle"), str) and o["aiTitle"].strip():
            self.s["ai_title"] = one_line(scrub(o["aiTitle"]), 200)
        elif t == "custom-title" and isinstance(o.get("customTitle"), str) and o["customTitle"].strip():
            self.s["custom_title"] = one_line(scrub(o["customTitle"]), 200)
        elif t == "frame-link" and isinstance(o.get("frameUrl"), str):
            self._link("artifact", o["frameUrl"], o["frameUrl"], o.get("title") or "Artifact",
                       ts, None, {"via": "frame-link"})
        elif t == "pr-link" and isinstance(o.get("prUrl"), str):
            n, repo = o.get("prNumber"), o.get("prRepository") or ""
            self._link("pr", o["prUrl"], o["prUrl"], f"#{n} {repo}".strip() if n else (repo or "Pull request"),
                       ts, None, {"number": n, "repo": repo})

    def _user(self, rec: Rec, ts: float | None) -> None:
        o = rec.obj
        k = classify_user(o)
        bl = blocks_of(o)
        if k == "tool_result":
            trs = [b for b in bl if b.get("type") == "tool_result"]
            tur = o.get("toolUseResult") if len(trs) == 1 else None
            for b in trs:
                self._result(b, o, tur, ts)
        elif k == "human":
            raw = first_text(bl)
            if raw.startswith(("<command-name>", "<command-message>")):
                self._slash(rec, ts, command_text(raw))
            else:
                self._prompt(rec, ts, text_of(bl, human=True), sum(1 for b in bl if b.get("type") == "image"), None)
        elif k in ("command", "bash_input"):
            self._slash(rec, ts, command_text(first_text(bl)))
        elif k == "interrupt":
            self._close_open()
            self._new_item("interrupt", ts, rec, "You interrupted")
            self._close_open()
        elif k == "task_notification":
            self._notification(rec, ts, first_text(bl))
        elif k in ("peer", "coordinator"):
            self._notification(rec, ts, None, peer=text_of(bl))

    def _slash(self, rec: Rec, ts: float | None, cmd: str) -> None:
        if self.s.get("absorb_compact") and cmd.startswith("/compact"):
            self.s["absorb_compact"] = False   # the divider already shows the manual compaction
            return
        name = cmd.split(" ", 1)[0] if cmd else None
        self._prompt(rec, ts, cmd, 0, name)

    def _prompt(self, rec: Rec, ts: float | None, text: str, images: int, slash: str | None,
                mid_turn: bool = False) -> None:
        self.s["absorb_compact"] = False
        if not mid_turn:
            self.s["turn"] += 1
            self.s["turns"] += 1
        self._close_open()
        clean = scrub(text)
        stored = clean[:MAX_TEXT]
        row = self._new_item("prompt", ts, rec, stored, {"mid_turn": mid_turn, "slash": slash, "images": images},
                             chars=len(clean))
        self._close_open()
        self.s["prompts"] += 1
        if self.s["first_prompt"] is None and clean.strip() and not slash:
            self.s["first_prompt"] = re.sub(r"\s+", " ", clean).strip()[:300]
        self._fts(stored, row["idx"], row["turn"], None, "prompt", ts)
        self._day(ts, prompts=1)

    def _assistant(self, rec: Rec, ts: float | None) -> None:
        o = rec.obj
        m = o.get("message") or {}
        model = m.get("model")
        if model == "<synthetic>":
            if o.get("isApiErrorMessage") or o.get("error"):
                self._close_open()
                text = scrub(text_of(blocks_of(o)))[:MAX_TEXT]
                row = self._new_item("assistant", ts, rec, text, {"model": None, "error": True})
                self.b.assist_fts[row["idx"]] = None
            return
        if model:
            self.s["models"][model] = self.s["models"].get(model, 0) + 1
        mid = m.get("id")
        for b in blocks_of(o):
            bt = b.get("type")
            if bt == "text":
                tx = b.get("text") or ""
                if not tx.strip():
                    continue
                part = scrub(tx)
                self.s["assistant_chars"] += len(part)
                op = self.s["open"]
                row = None
                if op and op.get("kind") == "assistant" and mid and op.get("msg") == mid:
                    row = self._load_item(op["i"])
                if row is not None:
                    row["text"] = (row["text"] + "\n\n" + part)[:MAX_TEXT]
                    row["chars"] = len(row["text"])
                    row["ts_end"] = max(row["ts_end"] or ts or 0, ts or 0) or None
                    self.b.assist_fts[row["idx"]] = op.get("fts")
                else:
                    self._close_open()
                    row = self._new_item("assistant", ts, rec, part[:MAX_TEXT], {"model": model, "error": False})
                    self.s["open"] = {"kind": "assistant", "i": row["idx"], "msg": mid, "fts": None}
                    self.b.assist_fts[row["idx"]] = None
            elif bt in ("tool_use", "server_tool_use"):
                self._tool_use(b, rec, ts, server=bt == "server_tool_use")

    def _attachment(self, rec: Rec, ts: float | None) -> None:
        a = rec.obj.get("attachment") or {}
        if a.get("type") != "queued_command":
            return
        mode, origin = a.get("commandMode"), (a.get("origin") or {}).get("kind")
        p = a.get("prompt")
        bl = [{"type": "text", "text": p}] if isinstance(p, str) else [x for x in p or () if isinstance(x, dict)]
        tx = text_of(bl)
        ats = util.parse_ts(a.get("timestamp")) or ts
        if mode == "task-notification" or origin == "task-notification" or tx.lstrip().startswith("<task-notification>"):
            self._notification(rec, ats, tx)
        elif origin in ("peer", "coordinator"):
            self._notification(rec, ats, None, peer=tx)
        elif mode == "prompt" and origin == "human" and not a.get("isMeta"):
            self._prompt(rec, ats, text_of(bl, human=True), sum(1 for b in bl if b.get("type") == "image"), None,
                         mid_turn=True)

    def _compact(self, rec: Rec, ts: float | None) -> None:
        cm = rec.obj.get("compactMetadata") or {}
        trig = cm.get("trigger") if cm.get("trigger") in ("auto", "manual") else None

        def k(n):
            return f"{round(n / 1000)}k" if isinstance(n, (int, float)) else "?"

        text = "Conversation compacted" + (f" · {trig}" if trig else "")
        if cm.get("preTokens") is not None or cm.get("postTokens") is not None:
            text += f" · {k(cm.get('preTokens'))} → {k(cm.get('postTokens'))} tokens"
        self._close_open()
        self._new_item("compact", ts, rec, text, {"trigger": trig})
        self._close_open()
        self.s["compactions"] += 1
        self.s["absorb_compact"] = trig == "manual"

    # ----------------------------------------------------------------- steps

    def _steps_item(self, ts: float | None, rec: Rec) -> dict:
        op = self.s["open"]
        if op and op.get("kind") == "steps":
            row = self._load_item(op["i"])
            if row is not None:
                return row
        self._close_open()
        row = self._new_item("steps", ts, rec, "")
        self.s["open"] = {"kind": "steps", "i": row["idx"], "n": 0}
        return row

    def _add_step(self, rec: Rec, ts: float | None, tool: str, label: str, arg: str, tid: str | None,
                  path: str | None, status: str = "running", status_label: str | None = None,
                  extra: dict | None = None) -> dict:
        item = self._steps_item(ts, rec)
        n = self.s["open"]["n"]
        self.s["open"]["n"] = n + 1
        step = {"session": self.sid, "item": item["idx"], "n": n, "tool_use_id": tid, "tool": tool, "label": label,
                "arg": one_line(scrub(arg)), "status": status, "status_label": status_label, "exit_code": None,
                "ts": ts, "ms": None, "path": path}
        self.b.steps[(item["idx"], n)] = step
        if tid:
            self.b.step_by_tid[tid] = step
            pend = self.s["pending"]
            pend[tid] = [item["idx"], n, tool, ts, extra or {}, item["turn"]]
            if len(pend) > MAX_PENDING:
                for old in list(pend)[: len(pend) - MAX_PENDING]:
                    pend.pop(old, None)
        if ts:
            item["ts_end"] = max(item["ts_end"] or ts, ts)
        self.b.dirty_steps.add(item["idx"])
        self.s["steps"] += 1
        self._fts(f"{label} {step['arg']}".strip(), item["idx"], item["turn"], n, "step", ts)
        self._day(ts, steps=1)
        return step

    def _tool_use(self, b: dict, rec: Rec, ts: float | None, server: bool = False) -> None:
        name = b.get("name") or "?"
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        tid = b.get("id") if isinstance(b.get("id"), str) else None
        label, arg, path = describe_tool(name, inp)
        extra: dict = {"path": path} if path else {}
        if name == "Bash":
            cmd = str(inp.get("command") or "")
            if re.search(r"\bgit\b", cmd) and re.search(r"\b(?:commit|push|cherry-pick|merge)\b", cmd):
                extra["git"] = scrub(cmd[:1000])
                extra["cwd"] = self.s.get("rec_cwd")
            if "claude-notify/notify.sh" in cmd:
                extra["notify"] = [one_line(scrub(notify_message(m.group("args"))), 300)
                                   for m in NOTIFY.finditer(cmd)]
        elif name in ("Agent", "Task", "Workflow"):
            self.s["subagents"] += 1
        elif name == "Artifact":
            extra["action"] = inp.get("action") or "publish"
        elif name == DESKMATE_NOTIFY:
            extra["text"] = one_line(scrub(str(inp.get("text") or inp.get("message") or "")), 300)
        if server:
            step = self._add_step(rec, ts, name, label, arg, None, path, "ok", "ok")
            return
        step = self._add_step(rec, ts, name, label, arg, tid, path, extra=extra)
        if name == "Bash" and tid:
            cmd = scrub(str(inp.get("command") or "")[:MAX_COMMAND])
            self.b.cmds[tid] = {"session": self.sid, "tool_use_id": tid, "item": step["item"], "n": step["n"],
                                "turn": self.s["turn"], "command": cmd,
                                "description": one_line(scrub(str(inp.get("description") or "")), 300) or None,
                                "status": "running", "exit_code": None,
                                "background": 1 if inp.get("run_in_background") else 0, "ts": ts, "ms": None}
            self._fts(cmd, step["item"], self.s["turn"], step["n"], "command", ts)

    def _notification(self, rec: Rec, ts: float | None, task_text: str | None, peer: str | None = None) -> None:
        if peer is not None:
            arg = "Message from another session: " + one_line(peer, 120)
        else:
            st = _tag(task_text or "", "status")
            summ = _tag(task_text or "", "summary") or _tag(task_text or "", "task-type") or ""
            arg = " · ".join(x for x in (st, one_line(summ, 140)) if x) or "Background task"
        self._add_step(rec, ts, "Notification", "Notification", arg, None, None, "ok", "ok")

    def _result(self, b: dict, o: dict, tur, ts: float | None) -> None:
        tid = b.get("tool_use_id")
        p = self.s["pending"].pop(tid, None) if isinstance(tid, str) else None
        if p is None:
            return
        item, n, tool, t0, extra, turn = p
        err = bool(b.get("is_error"))
        text = result_text(b)
        tud = tur if isinstance(tur, dict) else {}
        ms = int((ts - t0) * 1000) if ts and t0 and ts >= t0 else None
        exit_code = None
        if o.get("toolDenialKind"):
            status, label = "denied", "denied"
        elif tool == "Bash" and err and EXIT_RE.match(text):
            exit_code = int(EXIT_RE.match(text).group(1))
            status, label = "error", f"exit {exit_code}"
        elif err:
            status, label = "error", "error"
        elif tud.get("interrupted"):
            status, label = "interrupted", "interrupted"
        elif tud.get("backgroundTaskId") or tud.get("status") == "async_launched":
            status, label = "background", "started"
        elif tool == "AskUserQuestion" and tud.get("answers"):
            status, label = "ok", "answered"
        elif extra.get("notify"):
            codes = HTTP_RE.findall(text)
            code = codes[-1] if codes else None
            status = "ok" if code is None or code.startswith("2") else "error"
            label = code or _dur_label(ms)
        else:
            status, label = "ok", _dur_label(ms)
        if tool == "Bash" and exit_code is None and status == "ok":
            exit_code = 0
        if status == "error":
            self.s["errors"] += 1
        step = self.b.step_by_tid.get(tid)
        if step is not None:
            step.update(status=status, status_label=label, exit_code=exit_code, ms=ms)
        else:
            self.b.step_updates.append((status, label, exit_code, ms, self.sid, tid))
        self.b.dirty_steps.add(item)
        if tool == "Bash":
            cmd = self.b.cmds.get(tid)
            bg = 1 if (tud.get("backgroundTaskId") or status == "background") else None
            if cmd is not None:
                cmd.update(status=status, exit_code=exit_code, ms=ms)
                if bg:
                    cmd["background"] = 1
            else:
                self.b.cmd_updates.append((status, exit_code, ms, bg, self.sid, tid))
            for f in (tud.get("bashEditDiff") or {}).get("changedFiles") or ():
                if isinstance(f, str):
                    self._file(f, "edit", ts, item)
            if extra.get("git") and not err:
                self._git(extra, text, tud, ts, item, turn, tid)
            for k, msg in enumerate(extra.get("notify") or ()):
                codes = HTTP_RE.findall(text)
                self._link("discord", f"discord:{tid}:{k}", None, msg or "Discord post", ts, item,
                           {"status": codes[-1] if codes else None}, turn)
        elif tool in EDIT_TOOLS and not err:
            p = extra.get("path") or tud.get("filePath")
            if isinstance(p, str):
                action = "edit"
                if tool == "Write":
                    action = "create" if tud.get("type") == "create" else "write"
                self._file(p, action, ts, item)
        elif tool == "Read" and not err:
            f = tud.get("file") if isinstance(tud.get("file"), dict) else {}
            p = extra.get("path") or f.get("filePath")
            if isinstance(p, str):
                self._file(p, "read", ts, item)
        elif tool == "Artifact" and not err and extra.get("action") not in ("read", "quickstart"):
            if isinstance(tud.get("url"), str):
                self._link("artifact", tud["url"], tud["url"], tud.get("title") or "Artifact", ts, item, {}, turn)
            for u in ARTIFACT_URL.findall(text):
                self._link("artifact", u, u, "Artifact", ts, item, {}, turn)
        elif tool == DESKMATE_NOTIFY and not err:
            self._link("notify", f"notify:{tid}", None, extra.get("text") or "Notification", ts, item,
                       {"status": one_line(text, 40) or None}, turn)
        elif tool.startswith("mcp__") and not err:
            for u in ARTIFACT_URL.findall(text):
                self._link("artifact", u, u, "Artifact", ts, item, {}, turn)

    def _git(self, extra: dict, text: str, tud: dict, ts: float | None, item: int, turn: int, tid: str) -> None:
        cmd = extra.get("git") or ""
        m = GIT_DASH_C.search(cmd)
        where = m.group(1).strip("'\"") if m else extra.get("cwd")
        repo = gitlog.repo_of(where) if where else None
        found = [{"sha": c["sha"], "subject": c["subject"].strip(), "branch": c["branch"]}
                 for c in COMMIT_LINE.finditer(text)]
        go = (tud.get("gitOperation") or {})
        gc = go.get("commit") or {}
        if gc.get("sha") and not any(f["sha"][:7] == str(gc["sha"])[:7] for f in found):
            found.append({"sha": gc["sha"], "subject": subject_from_cmd(cmd), "branch": gc.get("branch")})
        if not found and GIT_COMMIT.search(cmd):
            hm = HEX_LINE.search(text)
            found.append({"sha": hm["sha"] if hm else None,
                          "subject": (hm["subject"] if hm and hm["subject"] else None) or subject_from_cmd(cmd),
                          "branch": None})
        for k, f in enumerate(found):
            sha = (f["sha"] or "")[:12] or None
            key = f"commit:{repo or '?'}@{sha}" if sha else f"commit:{repo or '?'}@{tid}:{k}"
            self._link("commit", key, None, one_line(scrub(f["subject"] or "commit"), 200), ts, item,
                       {"sha": sha[:7] if sha else None, "branch": f.get("branch"), "repo": repo}, turn)
        push = go.get("push") or {}
        if push or GIT_PUSH.search(cmd):
            branch = push.get("branch") if isinstance(push, dict) else None
            self._link("push", f"push:{repo or '?'}@{branch or '?'}@{int(ts or 0)}", None,
                       f"Pushed {branch}" if branch else "Pushed", ts, item, {"branch": branch, "repo": repo}, turn)

    def _file(self, path: str, action: str, ts: float | None, item: int) -> None:
        if not path.startswith("/"):
            return
        path = os.path.normpath(path)
        rank = {"create": 0, "write": 1, "edit": 2, "read": 3}
        known = self.files_known.get(path)
        row = self.b.files.get(path)
        best = action
        for a in (known, row["action"] if row else None):
            if a and rank.get(a, 9) < rank[best]:
                best = a
        if row is None:
            row = {"session": self.sid, "path": path, "repo": gitlog.repo_of(path), "action": best, "count": 0,
                   "first_ts": ts, "last_ts": ts, "last_item": item}
            self.b.files[path] = row
            if known is None:
                self._fts(path, item, self.s["turn"], None, "file", ts)
        row["action"] = best
        row["count"] += 1
        row["last_ts"] = max(row["last_ts"] or 0, ts or 0) or None
        row["last_item"] = item
        self.files_known[path] = best

    def _link(self, kind: str, key: str, url: str | None, title: str, ts: float | None, item: int | None,
              extra: dict | None = None, turn: int | None = None) -> None:
        if url and not re.match(r"https?://", url):
            url = None
        prev = self.b.links.get(key)
        if prev is not None:
            if prev["title"] in ("Artifact", "") and title:
                prev["title"] = one_line(scrub(title), 200)
            return
        if self.c.execute("select 1 from cc_links where session = ? and key = ?", (self.sid, key)).fetchone():
            return
        self.b.links[key] = {"session": self.sid, "key": key[:500], "kind": kind, "url": url,
                             "title": one_line(scrub(title or ""), 200), "ts": ts, "item": item,
                             "turn": self.s["turn"] if turn is None else turn,
                             "extra": json.dumps(extra or {})}

    # ----------------------------------------------------------------- writing

    def flush(self, read_offset: int, finish: dict | None = None) -> None:
        """Write the batch and the parser position in one transaction."""
        b, sid = self.b, self.sid
        with store.tx() as c:
            if b.items:
                c.executemany(
                    """insert into cc_items(session, idx, turn, kind, ts, ts_end, text, chars, flags, offset, uuid)
                       values(:session, :idx, :turn, :kind, :ts, :ts_end, :text, :chars, :flags, :offset, :uuid)
                       on conflict(session, idx) do update set turn = excluded.turn, kind = excluded.kind,
                         ts = excluded.ts, ts_end = excluded.ts_end, text = excluded.text, chars = excluded.chars,
                         flags = excluded.flags, offset = excluded.offset, uuid = excluded.uuid""",
                    [dict(r, flags=json.dumps(r["flags"])) for r in b.items.values()])
            if b.steps:
                c.executemany(
                    """insert or replace into cc_steps(session, item, n, tool_use_id, tool, label, arg, status,
                       status_label, exit_code, ts, ms, path) values(:session, :item, :n, :tool_use_id, :tool, :label,
                       :arg, :status, :status_label, :exit_code, :ts, :ms, :path)""", list(b.steps.values()))
            if b.step_updates:
                c.executemany("""update cc_steps set status = ?, status_label = ?, exit_code = ?, ms = ?
                                 where session = ? and tool_use_id = ?""", b.step_updates)
            if b.cmds:
                c.executemany(
                    """insert or replace into cc_commands(session, tool_use_id, item, n, turn, command, description,
                       status, exit_code, background, ts, ms) values(:session, :tool_use_id, :item, :n, :turn,
                       :command, :description, :status, :exit_code, :background, :ts, :ms)""", list(b.cmds.values()))
            if b.cmd_updates:
                c.executemany("""update cc_commands set status = ?, exit_code = ?, ms = ?,
                                 background = coalesce(?, background) where session = ? and tool_use_id = ?""",
                              b.cmd_updates)
            if b.links:
                c.executemany(
                    """insert or ignore into cc_links(session, key, kind, url, title, ts, item, turn, extra)
                       values(:session, :key, :kind, :url, :title, :ts, :item, :turn, :extra)""", list(b.links.values()))
            if b.files:
                c.executemany(
                    """insert into cc_files(session, path, repo, action, count, first_ts, last_ts, last_item)
                       values(:session, :path, :repo, :action, :count, :first_ts, :last_ts, :last_item)
                       on conflict(session, path) do update set count = cc_files.count + excluded.count,
                         action = excluded.action, last_ts = max(coalesce(cc_files.last_ts, 0), coalesce(excluded.last_ts, 0)),
                         last_item = excluded.last_item, repo = excluded.repo""", list(b.files.values()))
            if b.days:
                c.executemany(
                    """insert into cc_session_days(session, day, first_ts, last_ts, prompts, steps) values(?, ?, ?, ?, ?, ?)
                       on conflict(session, day) do update set first_ts = min(first_ts, excluded.first_ts),
                         last_ts = max(last_ts, excluded.last_ts), prompts = prompts + excluded.prompts,
                         steps = steps + excluded.steps""",
                    [(sid, d, v[0], v[1], v[2], v[3]) for d, v in b.days.items()])
            if b.fts:
                c.executemany("insert into cc_fts(body, session, item, turn, step, kind, ts) values(?, ?, ?, ?, ?, ?, ?)",
                              b.fts)
            op = self.s.get("open")
            for i, old in b.assist_fts.items():
                row = b.items.get(i)
                if row is None:
                    continue
                if old:
                    c.execute("delete from cc_fts where rowid = ?", (old,))
                body = util.clean_for_index(row["text"])
                if not body.strip():
                    continue
                cur = c.execute("insert into cc_fts(body, session, item, turn, step, kind, ts) values(?, ?, ?, ?, ?, ?, ?)",
                                (body, sid, i, row["turn"], None, "assistant", row["ts"]))
                if op and op.get("kind") == "assistant" and op.get("i") == i:
                    op["fts"] = cur.lastrowid
            for i in b.dirty_steps:
                rows = c.execute("select label, status, ts from cc_steps where session = ? and item = ? order by n",
                                 (sid, i)).fetchall()
                summary = util.steps_summary([(r[0], r[1]) for r in rows])
                ends = [r[2] for r in rows if r[2]]
                c.execute("update cc_items set text = ?, chars = ?, ts_end = max(coalesce(ts_end, 0), ?) "
                          "where session = ? and idx = ?", (summary, len(summary), max(ends) if ends else 0, sid, i))
            s = self.s
            models = Counter(s["models"])
            c.execute(
                """update cc_sessions set read_offset = ?, parse_state = ?, prompts = ?, turns = ?, steps = ?,
                   errors = ?, items = ?, compactions = ?, subagents = ?, first_ts = ?, last_ts = ?,
                   cwd = coalesce(cwd, ?), branch = ?, model = ?, ai_title = ?, custom_title = ?,
                   first_prompt = ?, assistant_chars = ?, parser_version = ?, parse_error = null where id = ?""",
                (read_offset, json.dumps(s), s["prompts"], s["turns"], s["steps"], s["errors"], s["idx"],
                 s["compactions"], s["subagents"], s["first_ts"], s["last_ts"], s["cwd"], s["branch"],
                 models.most_common(1)[0][0] if models else None, s["ai_title"], s["custom_title"],
                 s["first_prompt"], s["assistant_chars"], PARSER_VERSION, sid))
            if finish:
                c.execute("update cc_sessions set bytes = ?, mtime = ?, inode = ? where id = ?",
                          (finish["bytes"], finish["mtime"], finish["inode"], sid))
        self.b = _Batch()


# ---------------------------------------------------------------------------------------------- ingest

def _session_repos(c, sid: str, cwd: str | None) -> list[str]:
    """Repos of the files the session changed (most first), its cwd's repo, then its digest's repos."""
    out: list[str] = []
    for r in c.execute("""select repo, sum(count) n from cc_files where session = ? and repo is not null
                          and action != 'read' group by repo order by n desc""", (sid,)):
        out.append(r[0])
    if cwd and not any(cwd == root for root in config.SESSIONS_ROOTS):
        rp = gitlog.repo_of(cwd)
        if rp and rp not in out:
            out.append(rp)
    if not out:
        for r in c.execute("""select repo, sum(count) n from cc_files where session = ? and repo is not null
                              group by repo order by n desc limit 2""", (sid,)):
            out.append(r[0])
    d = c.execute("select repos from digests where session = ? order by id desc limit 1", (sid,)).fetchone()
    if d and d[0]:
        try:
            for rp in json.loads(d[0]):
                if isinstance(rp, str) and rp not in out:
                    out.append(rp)
        except ValueError:
            pass
    return out


def refresh_repos(sid: str) -> None:
    c = store.conn()
    row = c.execute("select cwd from cc_sessions where id = ?", (sid,)).fetchone()
    if row:
        c.execute("update cc_sessions set repos = ? where id = ?", (json.dumps(_session_repos(c, sid, row[0])), sid))


def ingest(path: str, config_dir: str | None = None, slug: str | None = None) -> dict | None:
    """Read what is new in one transcript. Runs in a worker thread; returns a small summary or None."""
    sid = os.path.basename(path)[:-6]
    try:
        st = os.stat(path)
    except OSError:
        return None
    c = store.conn()
    row = c.execute("select * from cc_sessions where id = ?", (sid,)).fetchone()
    if row is not None and row["skipped_reason"] == SKIP_OUTSIDE:
        return None
    if row is not None and row["transcript"] and row["transcript"] != path and os.path.exists(row["transcript"]):
        return None  # the same session id in another config dir: keep the first one
    if row is None:
        slug = slug or os.path.basename(os.path.dirname(path))
        maybe = slug_may_be_in_roots(slug)
        cwd = first_cwd(path) if maybe else ""
        if cwd is None:
            return None  # no record with a cwd yet: decide later
        if not cwd or not config.in_sessions_roots(cwd):
            c.execute("insert or replace into cc_sessions(id, skipped_reason, bytes, mtime) values(?, ?, ?, ?)",
                      (sid, SKIP_OUTSIDE, st.st_size, st.st_mtime))
            return None
        c.execute("""insert into cc_sessions(id, project, cwd, transcript, config_dir, read_offset, bytes, mtime, inode,
                     parser_version, state, digest_state) values(?, ?, ?, ?, ?, 0, 0, ?, ?, ?, 'open', 'none')""",
                  (sid, slug, cwd, path, config_dir, st.st_mtime, st.st_ino, PARSER_VERSION))
        row = c.execute("select * from cc_sessions where id = ?", (sid,)).fetchone()
    offset = row["read_offset"] or 0
    state = None
    if row["parse_state"]:
        try:
            state = json.loads(row["parse_state"])
        except ValueError:
            state = None
    replaced = bool(row["inode"] and row["inode"] != st.st_ino) or st.st_size < offset
    reset = replaced or (state is None and offset > 0) or (state is not None and state.get("v") != PARSER_VERSION) \
        or (row["parser_version"] or 0) != PARSER_VERSION
    if reset:
        # A new parser re-reads the same bytes, so digests still cover what they covered; a replaced file does not.
        with store.tx() as tc:
            store.purge_session(tc, sid)
            # The version is set here, not after the parse: a transcript this parser cannot read is not tried
            # again on every scan (changed_transcripts), only when it changes.
            tc.execute("update cc_sessions set read_offset = 0, parse_state = null, parser_version = ? where id = ?",
                       (PARSER_VERSION, sid))
            if replaced:
                tc.execute("update cc_sessions set digested_offset = 0, digest_state = 'none' where id = ?", (sid,))
        offset, state = 0, None
    if state is None:
        state = new_state()
    finish = {"bytes": st.st_size, "mtime": st.st_mtime, "inode": st.st_ino}
    if st.st_size == offset and not reset:
        c.execute("update cc_sessions set bytes = ?, mtime = ?, inode = ? where id = ?",
                  (st.st_size, st.st_mtime, st.st_ino, sid))
        return {"id": sid, "new_items": 0, "prompts": state["prompts"]}
    t0 = time.monotonic()
    parser = Parser(sid, state, c)
    stats: dict = {}
    last_end = offset
    try:
        for rec in iter_records(path, offset, stats):
            parser.feed(rec)
            last_end = rec.end
            if parser.b.size() >= CHUNK_ROWS:
                parser.flush(last_end)
        parser.flush(stats.get("next_offset", last_end), finish)
    except Exception as exc:  # a bad transcript must not stop the others
        log.warning("parse failed for %s: %s", sid, exc.__class__.__name__)
        c.execute("update cc_sessions set parse_error = ? where id = ?", (exc.__class__.__name__, sid))
        return None
    refresh_repos(sid)
    if row["ended"] and row["last_hook_ts"] and st.st_mtime > row["last_hook_ts"] + 5:
        c.execute("update cc_sessions set ended = 0 where id = ?", (sid,))  # resumed in the same file
    return {"id": sid, "new_items": parser.new_items, "prompts": state["prompts"], "ms": util.monotonic_ms(t0)}


def forget_outside_roots() -> int:
    """After SESSIONS_ROOTS or CLAUDE_CONFIG_DIRS change: drop sessions no longer in scope, re-check skipped ones."""
    c = store.conn()
    sig = roots_signature()
    if store.kv_get("sec.roots_sig") == sig:
        return 0
    gone = 0
    for r in c.execute("select id, cwd, transcript from cc_sessions where skipped_reason is null").fetchall():
        inside = config.in_sessions_roots(r["cwd"]) and r["transcript"] and transcript_from_hook(r["transcript"])
        if not inside:
            with store.tx() as tc:
                store.purge_session(tc, r["id"])
                tc.execute("delete from digests where session = ?", (r["id"],))
                tc.execute("delete from cc_sessions where id = ?", (r["id"],))
            gone += 1
    c.execute("delete from cc_sessions where skipped_reason = ?", (SKIP_OUTSIDE,))
    store.kv_put("sec.roots_sig", sig)
    return gone


def changed_transcripts() -> list[tuple[str, str, str]]:
    """Main transcripts that are new or grew since they were last read (stat only), and those an older parser
    read (PARSER_VERSION): ingest() starts those again from the beginning."""
    known = {r["id"]: r for r in store.q("select id, bytes, mtime, inode, read_offset, skipped_reason, transcript, "
                                          "parser_version from cc_sessions")}
    out = []
    for d, slug, path in main_transcripts():
        sid = os.path.basename(path)[:-6]
        try:
            st = os.stat(path)
        except OSError:
            continue
        r = known.get(sid)
        if r is None:
            out.append((d, slug, path))
        elif r["skipped_reason"] == SKIP_OUTSIDE:
            continue
        elif (r["bytes"] or 0) != st.st_size or (r["inode"] and r["inode"] != st.st_ino) \
                or (r["read_offset"] or 0) > st.st_size or (r["parser_version"] or 0) != PARSER_VERSION:
            out.append((d, slug, path))
    return out
