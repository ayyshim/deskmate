"""One SQLite file for everything the hub keeps: activity, sessions, and the secretary's tables.

Small and local, so the standard library driver behind a lock is enough. Every call is short; the
async code calls these directly.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from . import config

SCHEMA = """
create table if not exists activity (
  id integer primary key,
  ts real not null,
  session text,
  tool text not null,
  arg text,
  why text,
  status text,
  note text
);
create index if not exists activity_ts on activity(ts);

create table if not exists mcp_sessions (
  id text primary key,
  number integer,
  label text,
  cwd text,
  claude_session text,
  client text,
  first_seen real,
  last_seen real
);

create table if not exists kv (k text primary key, v text);

-- Secretary: Claude Code sessions it has seen, and how far it has read each transcript.
create table if not exists cc_sessions (
  id text primary key,
  project text,
  cwd text,
  transcript text,
  title text,
  first_ts real,
  last_ts real,
  prompts integer default 0,
  bytes integer default 0,
  read_offset integer default 0,
  digested_offset integer default 0,
  pending_since real,
  ended integer default 0,
  skipped_reason text
);
create table if not exists digests (
  id integer primary key,
  session text not null,
  ts real not null,
  day text not null,
  model text,
  title text,
  repos text,
  summary text,
  shipped text,
  decisions text,
  open_loops text,
  blockers text,
  links text,
  from_offset integer,
  to_offset integer
);
create index if not exists digests_day on digests(day);
create table if not exists hub_entries (
  id text primary key,
  kind text not null,
  repo text,
  day text,
  time text,
  title text,
  body text,
  path text,
  line integer,
  extra text
);
create index if not exists hub_entries_kind on hub_entries(kind, day);
create table if not exists loop_state (id text primary key, state text, until text, ts real);
create table if not exists briefs (day text primary key, ts real, model text, body text, posted integer default 0);
create table if not exists asks (id integer primary key, ts real, origin text, question text, answer text, citations text, ms integer);
create table if not exists notes (id integer primary key, ts real, origin text, kind text, text text, consumed integer default 0);
create table if not exists model_runs (id integer primary key, ts real, day text, kind text, model text, ok integer, ms integer, note text);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _private(path) -> None:
    """The journal holds URLs and what sessions did: 0600, also for a database an older hub made 0644.
    New files are 0600 anyway (main.prepare sets umask 077). Best effort: some file systems ignore modes."""
    try:
        if path.exists() and (path.stat().st_mode & 0o777) != 0o600:
            path.chmod(0o600)
    except OSError:
        pass


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.DATA.mkdir(parents=True, exist_ok=True)
        path = config.DATA / "deskmate.db"
        c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("pragma journal_mode=wal")
        c.executescript(SCHEMA)
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            _private(p)
        _conn = c
    return _conn


def q(sql: str, args: tuple | dict = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def one(sql: str, args: tuple | dict = ()) -> sqlite3.Row | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql: str, args: tuple | dict = ()) -> int:
    """Execute a write; returns lastrowid."""
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid or 0


def many(sql: str, rows: list[tuple]) -> None:
    with _lock:
        c = conn()
        c.execute("begin")
        try:
            c.executemany(sql, rows)
            c.execute("commit")
        except Exception:
            c.execute("rollback")
            raise


def get(key: str, default: str | None = None) -> str | None:
    row = one("select v from kv where k = ?", (key,))
    return row["v"] if row else default


def put(key: str, value: Any) -> None:
    run("insert into kv(k, v) values(?, ?) on conflict(k) do update set v = excluded.v", (key, str(value)))
