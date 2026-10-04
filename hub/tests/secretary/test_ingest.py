"""Reading transcripts: what counts, what is stored, and that nothing secret or out of scope reaches SQLite.

Runs in the hub image (pytest added), from the hub folder:
    python -m pytest -q tests/secretary
"""

from __future__ import annotations

import json
import sqlite3

from conftest import ingest_all
from world import NEVER_READ, SECRETS


def _dump(path) -> str:
    """Every text in the database file, for the never-stored checks (the WAL is checkpointed first)."""
    c = sqlite3.connect(path)
    c.execute("pragma wal_checkpoint(truncate)")
    out = []
    for (name,) in c.execute("select name from sqlite_master where type = 'table'"):
        try:
            for row in c.execute(f"select * from {name}"):
                out.extend(str(v) for v in row if v is not None)
        except sqlite3.OperationalError:
            pass
    c.close()
    raw = "\n".join(out)
    return raw + "\n" + path.read_bytes().decode("latin-1")


def test_migrations_run_twice_and_keep_rows(world):
    from app import db
    from app.secretary import store

    c = sqlite3.connect(":memory:", isolation_level=None)
    c.executescript(db.SCHEMA)
    c.execute("insert into cc_sessions(id, project, cwd, transcript, title) values('old', 'p', '/home/alex/work', 't', 'kept')")
    assert store.migrate(c) == store.SCHEMA_VERSION
    assert store.migrate(c) == store.SCHEMA_VERSION
    assert c.execute("pragma user_version").fetchone()[0] == store.SCHEMA_VERSION
    row = c.execute("select title, digest_state, state from cc_sessions where id = 'old'").fetchone()
    assert row == ("kept", "none", "open")
    tables = {r[0] for r in c.execute("select name from sqlite_master where type = 'table'")}
    assert {"cc_items", "cc_steps", "cc_files", "cc_commands", "cc_links", "cc_session_days", "loops", "cc_fts"} <= tables
    # and the real file, through the secretary's own connection, twice
    store.ensure()
    store.migrate(store.conn())
    assert store.val("pragma user_version") == store.SCHEMA_VERSION


def test_only_sessions_inside_the_roots_are_read(world):
    res = [r for r in ingest_all(world) if r]
    from app.secretary import store

    rows = {r["id"]: r for r in store.q("select * from cc_sessions")}
    assert world.sid_a in rows and world.sid_b in rows
    for sid in (world.sid_c, world.sid_d):  # the home folder, and ~/work-old (a prefix of ~/work, but outside)
        if sid in rows:
            assert rows[sid]["skipped_reason"]
            assert store.val("select count(*) from cc_items where session = ?", (sid,)) == 0
    a = rows[world.sid_a]
    assert a["prompts"] >= 3 and a["turns"] >= 3 and a["steps"] >= 10
    assert a["custom_title"] == "Checkout total and prototype"
    assert res


def test_no_secret_and_nothing_out_of_scope_reaches_sqlite(loaded):
    from app.secretary import store

    store.conn().execute("pragma wal_checkpoint(truncate)")
    blob = _dump(store.config.DATA / "deskmate.db")
    for s in SECRETS + NEVER_READ:
        assert s not in blob, f"a planted secret reached SQLite ({s[:6]}…)"
    assert "A sidechain prompt that must not show" not in blob
    assert "half writ" not in blob  # the half-written last line waits for the rest


def test_incremental_parse_appends_without_duplicates(world):
    from app.secretary import store, transcripts

    ingest_all(world)
    n_items = store.val("select count(*) from cc_items where session = ?", (world.sid_b,))
    path = world.paths["b"]
    t = world.session_b()
    t.t = world.now - 3600
    t.prompt("And with tax?")
    t.text("n2", "Tax is added per line.")
    with open(path, "a") as f:
        for r in t.records[-2:]:
            f.write(json.dumps(r) + "\n")
    res = transcripts.ingest(str(path), str(world.config_dir), path.parent.name)
    assert res and res.get("new_items")
    assert store.val("select count(*) from cc_items where session = ?", (world.sid_b,)) == n_items + 2
    again = transcripts.ingest(str(path), str(world.config_dir), path.parent.name)
    assert not (again or {}).get("new_items")
    assert store.val("select count(*) from cc_items where session = ?", (world.sid_b,)) == n_items + 2


def test_changed_transcripts_sees_only_main_files_in_scope(world):
    from app.secretary import transcripts

    found = {p for _, _, p in transcripts.changed_transcripts()}
    assert str(world.paths["a"]) in found and str(world.paths["b"]) in found
    assert not any("/subagents/" in p for p in found)
    ingest_all(world)
    again = {p for _, _, p in transcripts.changed_transcripts()}
    assert str(world.paths["b"]) not in again


def test_links_files_and_commands(loaded):
    from app.secretary import store

    kinds = {r["kind"] for r in store.q("select kind from cc_links where session = ?", (loaded.sid_a,))}
    assert {"commit", "push", "artifact"} <= kinds, kinds
    files = {r["path"]: r["action"] for r in store.q("select path, action from cc_files where session = ?", (loaded.sid_a,))}
    assert files.get(f"{loaded.shop}/src/total.py") in ("edit", "write", "edited")
    statuses = {r["status"] for r in store.q("select status from cc_steps where session = ?", (loaded.sid_a,))}
    assert {"ok", "error", "denied"} <= statuses, statuses


def test_forget_outside_roots(loaded, monkeypatch):
    from app import config
    from app.secretary import store, transcripts

    monkeypatch.setattr(config, "SESSIONS_ROOTS", [str(loaded.shop)])
    gone = transcripts.forget_outside_roots()
    assert gone >= 1
    assert store.val("select count(*) from cc_items where session = ?", (loaded.sid_a,)) == 0


def test_repo_of_places_removed_worktrees_and_spare_clones(loaded):
    """Worktrees are often removed once merged; their files still belong to the repo they sit next to."""
    from app.secretary import gitlog

    root = loaded.root
    assert gitlog.repo_of(f"{root}/shop-wt/tax/src/a.py") == "shop"        # a live worktree (discovered)
    assert gitlog.repo_of(f"{root}/shop-wt/gone/src/a.py") == "shop"       # removed: only the folder name is left
    assert gitlog.repo_of(f"{root}/shop-hotfix/src/a.py") == "shop"        # a spare clone next to the repo
    assert gitlog.repo_of(f"{root}/shopping/src/a.py") is None             # another name, not shop + separator
    assert gitlog.repo_of(f"{root}/shop-notes.md") is None                 # a file in the root, not a folder
    assert gitlog.repo_of(f"{root}/elsewhere/x.py") is None


def test_a_new_parser_reads_old_sessions_again(world):
    """After PARSER_VERSION changes (parser or redactor), finished sessions are read again, not only growing ones."""
    from app.secretary import store, transcripts

    ingest_all(world)
    sid = world.sid_b
    items = store.val("select count(*) from cc_items where session = ?", (sid,))
    assert str(world.paths["b"]) not in {p for _, _, p in transcripts.changed_transcripts()}
    store.run("update cc_sessions set parser_version = ?, branch = 'HEAD' where id = ?",
              (transcripts.PARSER_VERSION - 1, sid))
    assert str(world.paths["b"]) in {p for _, _, p in transcripts.changed_transcripts()}
    ingest_all(world)
    row = store.one("select parser_version, branch from cc_sessions where id = ?", (sid,))
    assert row["parser_version"] == transcripts.PARSER_VERSION
    assert row["branch"] != "HEAD"
    assert store.val("select count(*) from cc_items where session = ?", (sid,)) == items
    assert str(world.paths["b"]) not in {p for _, _, p in transcripts.changed_transcripts()}
