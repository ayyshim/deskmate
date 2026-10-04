"""The secretary's side of the hub's SQLite file: migrations, its own connections, kv and live events.

The hub's own connection (db.conn) sits behind one lock that the event loop takes for every activity
row. Transcript parsing writes thousands of rows, so the secretary keeps its own connection per thread
instead: a long parse in a worker thread never makes the desk wait for the lock. WAL mode lets readers
and the one writer run side by side, and writes are committed in short chunks.

migrate() is idempotent: it adds missing columns with ALTER TABLE (CREATE TABLE IF NOT EXISTS never adds
columns, and older hubs already have cc_sessions, digests, briefs, asks and notes), creates the new
tables and the FTS5 index, and sets PRAGMA user_version.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from typing import Any, Iterator

from .. import config, db, journal

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

# (table, column, declaration). Added only when missing.
ADD_COLUMNS: list[tuple[str, str, str]] = [
    # cc_sessions: one row per Claude Code session (main transcript) whose first cwd is in SESSIONS_ROOTS
    ("cc_sessions", "ai_title", "text"),
    ("cc_sessions", "custom_title", "text"),
    ("cc_sessions", "first_prompt", "text"),            # first human prompt, redacted, <= 300 chars
    ("cc_sessions", "model", "text"),                   # most used assistant model
    ("cc_sessions", "branch", "text"),                  # gitBranch of the last record
    ("cc_sessions", "state", "text default 'open'"),
    ("cc_sessions", "digest_state", "text default 'none'"),
    ("cc_sessions", "digest_error", "text"),
    ("cc_sessions", "turns", "integer default 0"),
    ("cc_sessions", "steps", "integer default 0"),
    ("cc_sessions", "errors", "integer default 0"),
    ("cc_sessions", "items", "integer default 0"),
    ("cc_sessions", "repos", "text"),                   # JSON array: files touched, cwd, digests
    ("cc_sessions", "mtime", "real"),
    ("cc_sessions", "compactions", "integer default 0"),
    ("cc_sessions", "subagents", "integer default 0"),
    ("cc_sessions", "parser_version", "integer default 0"),
    ("cc_sessions", "parse_error", "text"),
    # added by the build (not in the research contract)
    ("cc_sessions", "parse_state", "text"),             # JSON: where the incremental parser stopped
    ("cc_sessions", "inode", "integer"),
    ("cc_sessions", "config_dir", "text"),
    ("cc_sessions", "last_hook", "text"),               # stop | session-end | pre-compact
    ("cc_sessions", "last_hook_ts", "real"),
    ("cc_sessions", "assistant_chars", "integer default 0"),
    ("cc_sessions", "digest_checked", "integer"),        # read_offset when the pre-filter last looked
    ("digests", "outcome", "text"),
    ("briefs", "headline", "text"),
    ("briefs", "status", "text default 'ready'"),
    ("briefs", "trigger", "text"),
    ("briefs", "error", "text"),
    ("briefs", "discord_text", "text"),
    ("briefs", "posted_ts", "real"),
    ("asks", "status", "text default 'done'"),
    ("asks", "model", "text"),
    ("asks", "error", "text"),
    ("asks", "warning", "text"),
    ("notes", "repo", "text"),
    ("notes", "area", "text"),                          # JSON field name is "where"
    ("notes", "session", "text"),
    # what each model call cost (llm.py's usage dict) and, when it failed, its code (gate.FAILURES)
    ("model_runs", "error", "text"),
    ("model_runs", "input_tokens", "integer"),
    ("model_runs", "output_tokens", "integer"),
    ("model_runs", "cache_read_tokens", "integer"),
    ("model_runs", "cache_write_tokens", "integer"),
    ("model_runs", "cost_usd", "real"),             # the CLI's list-price estimate; a plan login is not billed by it
]

CREATE = """
create table if not exists cc_session_days (
  session text not null,
  day text not null,
  first_ts real,
  last_ts real,
  prompts integer default 0,
  steps integer default 0,
  primary key (session, day)
) without rowid;
create index if not exists cc_session_days_day on cc_session_days(day);

create table if not exists cc_items (
  session text not null,
  idx integer not null,
  turn integer not null,
  kind text not null,
  ts real,
  ts_end real,
  text text,
  chars integer default 0,
  flags text,
  offset integer,
  uuid text,
  primary key (session, idx)
) without rowid;
create index if not exists cc_items_turn on cc_items(session, turn);

create table if not exists cc_steps (
  session text not null,
  item integer not null,
  n integer not null,
  tool_use_id text,
  tool text not null,
  label text not null,
  arg text,
  status text not null default 'running',
  status_label text,
  exit_code integer,
  ts real,
  ms integer,
  path text,
  primary key (session, item, n)
) without rowid;
create unique index if not exists cc_steps_tool_use on cc_steps(session, tool_use_id) where tool_use_id is not null;

create table if not exists cc_files (
  session text not null,
  path text not null,
  repo text,
  action text not null,
  count integer default 1,
  first_ts real,
  last_ts real,
  last_item integer,
  primary key (session, path)
) without rowid;
create index if not exists cc_files_path on cc_files(path);

create table if not exists cc_commands (
  session text not null,
  tool_use_id text not null,
  item integer,
  n integer,
  turn integer,
  command text,
  description text,
  status text,
  exit_code integer,
  background integer default 0,
  ts real,
  ms integer,
  primary key (session, tool_use_id)
) without rowid;

create table if not exists cc_links (
  session text not null,
  key text not null,
  kind text not null,
  url text,
  title text,
  ts real,
  item integer,
  turn integer,
  extra text,
  primary key (session, key)
) without rowid;

create table if not exists loops (
  id text primary key,
  natural_key text not null unique,
  grp text not null,
  kind text not null,
  text text not null,
  repo text,
  area text,
  origin_day text,
  first_seen real not null,
  last_seen real not null,
  gone integer not null default 0,
  src text not null
);
create index if not exists loops_grp on loops(grp, gone);
create index if not exists hub_entries_repo on hub_entries(kind, repo, day);
create index if not exists digests_session on digests(session, id);
create index if not exists model_runs_day on model_runs(day, kind);

create virtual table if not exists cc_fts using fts5(
  body,
  session unindexed,
  item unindexed,
  turn unindexed,
  step unindexed,
  kind unindexed,
  ts unindexed,
  tokenize = 'unicode61 remove_diacritics 2',
  prefix = '2 3'
);
"""

# Tables that hold anything read from a transcript, for a full re-parse or a purge.
SESSION_TABLES = ("cc_items", "cc_steps", "cc_files", "cc_commands", "cc_links", "cc_session_days")


def _columns(c: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in c.execute(f"pragma table_info({table})")}


def migrate(c: sqlite3.Connection) -> int:
    """Bring the hub database to SCHEMA_VERSION. Safe to call on every start, and twice."""
    for table, column, decl in ADD_COLUMNS:
        if column not in _columns(c, table):
            c.execute(f"alter table {table} add column {column} {decl}")
    c.executescript(CREATE)
    c.execute(f"pragma user_version = {SCHEMA_VERSION}")
    return SCHEMA_VERSION


# ---------------------------------------------------------------- connections

_local = threading.local()
_migrated: set[str] = set()
_mlock = threading.Lock()


def db_path() -> str:
    return str(config.DATA / "deskmate.db")


def conn() -> sqlite3.Connection:
    """This thread's connection to the hub database (opened, and migrated once, on first use)."""
    path = db_path()
    c = getattr(_local, "c", None)
    if c is not None and getattr(_local, "path", None) == path:
        return c
    config.DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=20, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("pragma busy_timeout = 20000")
    with _mlock:
        if path not in _migrated:
            c.execute("pragma journal_mode = wal")
            c.executescript(db.SCHEMA)  # the hub's base tables (idempotent), in case db.conn() has not run yet
            migrate(c)
            _migrated.add(path)
    _local.c, _local.path = c, path
    return c


def ensure() -> None:
    conn()


@contextlib.contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """One short write transaction on this thread's connection."""
    c = conn()
    c.execute("begin immediate")
    try:
        yield c
    except BaseException:
        c.execute("rollback")
        raise
    c.execute("commit")


def q(sql: str, args: tuple | dict | list = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()


def one(sql: str, args: tuple | dict | list = ()) -> sqlite3.Row | None:
    return conn().execute(sql, args).fetchone()


def val(sql: str, args: tuple | dict | list = (), default: Any = None) -> Any:
    row = one(sql, args)
    return default if row is None or row[0] is None else row[0]


def run(sql: str, args: tuple | dict | list = ()) -> int:
    cur = conn().execute(sql, args)
    return cur.lastrowid or 0


def kv_get(key: str, default: str | None = None) -> str | None:
    row = one("select v from kv where k = ?", (key,))
    return row["v"] if row else default


def kv_put(key: str, value: Any) -> None:
    run("insert into kv(k, v) values(?, ?) on conflict(k) do update set v = excluded.v", (key, str(value)))


def kv_json(key: str, default: Any = None) -> Any:
    raw = kv_get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def purge_session(c: sqlite3.Connection, sid: str) -> None:
    """Forget everything read from one transcript (a re-parse, or the session left the roots)."""
    for t in SESSION_TABLES:
        c.execute(f"delete from {t} where session = ?", (sid,))
    c.execute("delete from cc_fts where session = ?", (sid,))


# ---------------------------------------------------------------- live events (journal.publish is loop-only)

_loop: asyncio.AbstractEventLoop | None = None
_last_session_pub: dict[str, float] = {}
_session_pending: set[str] = set()
_SESSION_EVERY = 2.0


def set_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _loop
    _loop = loop


def _on_loop() -> bool:
    try:
        return asyncio.get_running_loop() is _loop
    except RuntimeError:
        return False


def publish(kind: str, data: Any) -> None:
    """journal.publish from any thread: it puts onto asyncio queues, so it must run on the loop."""
    if _loop is None or _loop.is_closed():
        return
    if _on_loop():
        journal.publish(kind, data)
    else:
        _loop.call_soon_threadsafe(journal.publish, kind, data)


def publish_session(sid: str, state: str, digest_state: str) -> None:
    """sec_session, at most one per 2 s per session; the last one in a burst is delivered late."""
    if _loop is None or _loop.is_closed():
        return

    def go() -> None:
        now = time.monotonic()
        wait = _SESSION_EVERY - (now - _last_session_pub.get(sid, 0.0))
        if wait > 0:
            if sid not in _session_pending:
                _session_pending.add(sid)

                def later() -> None:
                    _session_pending.discard(sid)
                    _last_session_pub[sid] = time.monotonic()
                    row = one("select state, digest_state from cc_sessions where id = ?", (sid,))
                    journal.publish("sec_session", {"id": sid, "state": row["state"] if row else state,
                                                    "digest_state": row["digest_state"] if row else digest_state})

                _loop.call_later(wait, later)
            return
        _last_session_pub[sid] = now
        journal.publish("sec_session", {"id": sid, "state": state, "digest_state": digest_state})

    if _on_loop():
        go()
    else:
        _loop.call_soon_threadsafe(go)
