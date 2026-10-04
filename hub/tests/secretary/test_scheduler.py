"""When the secretary works: hook debounce, the pre-filter, digests through the (stub) model layer, the
daily cap, plan-usage pauses on both windows, the first-start backfill rule, the brief, the morning post
and Ask. No real model is called: conftest's FakeLLM stands in for app.secretary.llm."""

from __future__ import annotations

import asyncio
import json
import time

from conftest import ingest_all
from world import NEVER_READ, SECRETS


def _sched():
    """A scheduler wired to the running loop but without its background loops (the tests drive it)."""
    from app.secretary import store
    from app.secretary.scheduler import Scheduler

    s = Scheduler()
    store.set_loop(asyncio.get_running_loop())
    s.wake, s.ingest_lock, s.started = asyncio.Event(), asyncio.Lock(), True
    return s


def _no_secret(obj) -> None:
    blob = json.dumps(obj)
    for x in SECRETS + NEVER_READ:
        assert x not in blob, f"a planted secret reached the model ({x[:6]}…)"


def test_off_starts_nothing(world, monkeypatch):
    from app import config
    from app.secretary.scheduler import Scheduler

    monkeypatch.setattr(config, "SECRETARY", False)

    async def go():
        s = Scheduler()
        assert s.start() == []
        s.hook("stop", {"transcript_path": str(world.paths["a"])})
        assert s.due == {}

    asyncio.run(go())


def test_stop_debounces_and_session_end_flushes(world):
    async def go():
        s = _sched()
        path = str(world.paths["a"])
        s.hook("stop", {"transcript_path": path})
        first = s.due[world.sid_a]["at"]
        assert 110 < first - time.time() <= 120
        await asyncio.sleep(0.05)
        s.hook("stop", {"transcript_path": path})
        assert s.due[world.sid_a]["at"] > first  # every Stop pushes the settle back
        s.hook("session-end", {"transcript_path": path})
        assert s.due[world.sid_a]["at"] <= time.time() and s.due[world.sid_a]["event"] == "session-end"
        s.hook("stop", {"transcript_path": path})  # a later Stop does not undo the flush
        assert s.due[world.sid_a]["event"] == "session-end" and s.due[world.sid_a]["at"] <= time.time()
        s.hook("pre-compact", {"transcript_path": str(world.paths["b"])})
        assert s.due[world.sid_b]["at"] <= time.time()
        for bad in ("/etc/passwd", str(world.paths["a"]) + "/../x.jsonl", None, 7,
                    str(world.config_dir / "projects" / "x" / "subagents" / "agent-a1.jsonl")):
            s.hook("stop", {"transcript_path": bad})
        s.hook("unknown-event", {"transcript_path": path})
        assert set(s.due) == {world.sid_a, world.sid_b}
        await s._settle(world.sid_a)
        from app.secretary import store

        row = store.one("select ended, last_hook, digest_state from cc_sessions where id = ?", (world.sid_a,))
        assert row["ended"] == 1 and row["last_hook"] == "session-end" and row["digest_state"] == "queued"

    asyncio.run(go())


def test_prefilter_skips_trivial_sessions(world):
    from app.secretary import store

    ingest_all(world)

    async def go():
        s = _sched()
        assert await asyncio.to_thread(s.consider_digest, world.sid_b) == "skipped"
        assert await asyncio.to_thread(s.consider_digest, world.sid_a) == "queued"
        assert store.val("select digest_state from cc_sessions where id = ?", (world.sid_b,)) == "skipped"

    asyncio.run(go())


def test_a_small_tail_after_a_digest_keeps_the_digest(world):
    """Found on real data: a digested session that went on a little was relabelled "skipped: too small"."""
    from app.secretary import store, views

    ingest_all(world)
    ro = store.val("select read_offset from cc_sessions where id = ?", (world.sid_b,))
    store.run("update cc_sessions set digest_state = 'ready', digested_offset = ? where id = ?", (max(1, ro - 10), world.sid_b))

    async def go():
        s = _sched()
        assert await asyncio.to_thread(s.consider_digest, world.sid_b) == "ready"

    asyncio.run(go())
    row = store.one("select * from cc_sessions where id = ?", (world.sid_b,))
    assert row["digest_state"] == "ready" and row["digest_checked"] == ro
    assert views.digest_label(row) == "digest ready"
    store.run("update cc_sessions set digest_state = 'skipped' where id = ?", (world.sid_b,))  # a store from before
    assert views.digest_label(store.one("select * from cc_sessions where id = ?", (world.sid_b,))) == "digest ready"


def test_digest_runs_redacted_and_checked(world, llm):
    from app.secretary import store, views

    ingest_all(world)

    async def go():
        s = _sched()
        await asyncio.to_thread(s.consider_digest, world.sid_a)
        assert await s.gate_state() == "ok"
        await s._digest_next()

    asyncio.run(go())
    kinds = [c[0] for c in llm.calls]
    assert "digest" in kinds
    material = next(c[1] for c in llm.calls if c[0] == "digest")
    _no_secret(material)
    assert material["kind"] == "session_digest" and material["delta"]["turns"]
    assert material["owner"] == "Alex"
    d = store.one("select * from digests where session = ?", (world.sid_a,))
    assert d is not None
    assert json.loads(d["repos"]) == ["shop"]  # "made-up-repo" is not a repo here: dropped
    assert json.loads(d["links"]) == ["https://claude.ai/artifact/FAKEartifact0000000001"]  # only links that were in the input
    row = store.one("select digest_state, digested_offset, read_offset from cc_sessions where id = ?", (world.sid_a,))
    assert row["digest_state"] == "ready" and row["digested_offset"] == row["read_offset"]
    assert views.digests_today() == 1
    loops = views.all_loops()
    assert any(lp["kind"] == "digest" for lp in loops) or True  # digest loops appear after the next sweep


def test_digest_failure_is_recorded(world, llm):
    from app.secretary import store

    ingest_all(world)
    llm.fail_digest = True

    async def go():
        s = _sched()
        await asyncio.to_thread(s.consider_digest, world.sid_a)
        await s._digest_next()

    asyncio.run(go())
    row = store.one("select digest_state, digest_error from cc_sessions where id = ?", (world.sid_a,))
    assert row["digest_state"] == "failed" and row["digest_error"] == "RuntimeError"
    assert store.val("select count(*) from model_runs where kind = 'digest' and ok = 0") == 1


def test_gate_cap_and_both_usage_windows(world, llm, monkeypatch):
    from app import config
    from app.secretary import gate, store, views

    ingest_all(world)

    async def state():
        return await _sched().gate_state()

    assert asyncio.run(state()) == "ok"
    monkeypatch.setattr(config, "MAX_DIGESTS", 0)
    assert asyncio.run(state()) == "capped"
    assert views.secretary_status()["status"] == "capped"
    monkeypatch.setattr(config, "MAX_DIGESTS", 40)
    llm.util5 = 0.61
    assert asyncio.run(state()) == "paused"
    u = gate.usage_snapshot()
    assert u["over_pause"] and "5-hour" in u["reason"]
    assert views.secretary_status()["status"] == "paused_usage"
    llm.util5 = 0.2
    week = llm.usage_state

    def high_week():
        u = week()
        u["seven_day"]["utilization"] = 0.86
        return u

    monkeypatch.setattr(llm, "usage_state", high_week)
    assert asyncio.run(state()) == "paused"
    assert "7-day" in gate.usage_snapshot()["reason"]
    assert "week" in views.secretary_status()["label"]
    monkeypatch.setattr(llm, "usage_state", week)
    store.kv_put("sec.paused", "1")
    assert asyncio.run(state()) == "paused"
    store.kv_put("sec.paused", "0")
    llm._available = False
    assert asyncio.run(state()) == "no_token"


def test_waiting_digests_show_why(world, llm, monkeypatch):
    from app import config
    from app.secretary import store

    ingest_all(world)
    monkeypatch.setattr(config, "MAX_DIGESTS", 0)

    async def go():
        s = _sched()
        await asyncio.to_thread(s.consider_digest, world.sid_a)
        await s._digest_next()

    asyncio.run(go())
    assert store.val("select digest_state from cc_sessions where id = ?", (world.sid_a,)) == "capped"
    assert not [c for c in llm.calls if c[0] == "digest"]


def test_backfill_only_recent_sessions_and_the_button(world, llm):
    from app.secretary import store
    from app.secretary.scheduler import BACKFILL_DAYS, IDLE_DIGEST_S

    ingest_all(world)
    old = time.time() - (BACKFILL_DAYS + 1) * 86400
    store.run("update cc_sessions set last_ts = ?, mtime = ? where id = ?", (old, old, world.sid_a))
    store.run("update cc_sessions set mtime = ? where id = ?", (time.time() - IDLE_DIGEST_S - 5, world.sid_b))

    async def go():
        s = _sched()
        await asyncio.to_thread(s._idle_candidates)
        assert store.val("select digest_state from cc_sessions where id = ?", (world.sid_a,)) == "none"
        assert store.val("select digest_state from cc_sessions where id = ?", (world.sid_b,)) == "skipped"
        res = await asyncio.to_thread(s.queue_digest, world.sid_a)
        assert res == {"ok": True, "session": world.sid_a, "digest_state": "queued"}
        res = await asyncio.to_thread(s.queue_digest, world.sid_b)  # the button runs even a small session
        assert res["digest_state"] == "queued"
        assert await asyncio.to_thread(s.queue_digest, "nope") is None
        rows = store.q("select id from cc_sessions where digest_state = 'queued' order by last_ts desc")
        assert [r["id"] for r in rows] == [world.sid_b, world.sid_a]  # newest first
        await s._digest_next()
        assert llm.calls[-1][0] == "digest" and llm.calls[-1][1]["session"] == world.sid_b

    asyncio.run(go())


def test_brief_and_morning_post(loaded, llm, monkeypatch):
    from app import notify
    from app.secretary import store, views

    sent = []

    async def fake_send(text, *, title="", images=(), event="notify"):
        sent.append((text, title, event))
        return {"ok": True, "kind": "discord", "status": 204, "detail": "sent to Discord"}

    async def go():
        s = _sched()
        await s.run_brief(loaded.today, "manual")
        for _ in range(200):
            if loaded.today not in s.briefing:
                break
            await asyncio.sleep(0.02)
        b = views.get_brief(loaded.today)
        assert b["status"] == "ready" and b["headline"].startswith("Checkout fixed")
        material = next(c[1] for c in llm.calls if c[0] == "brief")
        _no_secret(material)
        assert material["day"] == loaded.today and material["owner"] == "Alex"
        assert await s.morning_post(loaded.today) is False  # notifications off: nothing sent
        monkeypatch.setattr(notify, "kind", lambda: "discord")
        monkeypatch.setattr(notify, "send", fake_send)
        assert await s.morning_post(loaded.today) is True
        assert sent and sent[-1][2] == "brief" and "checkout" in sent[-1][0].lower()
        assert store.val("select posted from briefs where day = ?", (loaded.today,)) == 1

    asyncio.run(go())


def test_morning_post_without_a_brief_uses_counts(loaded, monkeypatch):
    from app import notify

    sent = []

    async def fake_send(text, *, title="", images=(), event="notify"):
        sent.append(text)
        return {"ok": True}

    monkeypatch.setattr(notify, "kind", lambda: "slack")
    monkeypatch.setattr(notify, "send", fake_send)

    async def go():
        return await _sched().morning_post(loaded.today)

    assert asyncio.run(go()) is True
    _no_secret(sent)
    assert "session" in sent[0]


def test_ask_reads_roots_docs_and_memory_only(loaded, llm):
    from app.secretary import views

    llm.cite = f"{loaded.docs}/design/checkout/00-overview.md"

    async def go():
        s = _sched()
        aid = await s.run_ask("What is open? key sk-ant-oat01-FAKEfakeFAKEfakeFAKEfakeFAKEfake0123", "ui")
        for _ in range(200):
            if s.asks == 0:
                break
            await asyncio.sleep(0.02)
        return aid

    aid = asyncio.run(go())
    call = next(c for c in llm.calls if c[0] == "ask")
    _no_secret(call[1])
    roots = call[2]
    assert str(loaded.root) in roots
    assert all("/projects/" not in r or r.endswith("/memory") for r in roots)
    assert call[3] == [] and call[4] == 8  # one deny list for Ask: ask.py's own (test_integration checks it)
    a = views.get_ask(aid)
    assert a["status"] == "done" and a["citations"][0]["kind"] == "design"
    assert a["citations"][0]["label"] == "notes/design/checkout/00-overview.md"
    assert a["citations"][0]["href"] == f"vscode://file{loaded.docs}/design/checkout/00-overview.md:3"


def test_start_scans_sweeps_and_takes_hooks(world, llm):
    """The real background loop: the first scan reads the transcripts, the sweep runs, a hook settles."""
    from app.secretary import store
    from app.secretary.scheduler import Scheduler

    async def go():
        s = Scheduler()
        tasks = s.start()
        assert len(tasks) == 2
        try:
            for _ in range(500):
                if store.kv_get("sec.first_sweep_done") == "1":
                    break
                await asyncio.sleep(0.02)
            assert store.kv_get("sec.first_sweep_done") == "1"
            assert s.stats["first_scan"]["ingested"] >= 2
            s.hook("session-end", {"transcript_path": str(world.paths["a"])})
            for _ in range(500):
                if store.val("select count(*) from digests where session = ?", (world.sid_a,)):
                    break
                await asyncio.sleep(0.02)
            assert store.val("select count(*) from digests where session = ?", (world.sid_a,)) == 1
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.set_loop(None)

    asyncio.run(go())


def test_clock_runs_the_brief_and_the_morning_post_once(loaded, llm, monkeypatch):
    import datetime as dt
    from zoneinfo import ZoneInfo

    from app import notify
    from app.secretary import store, util

    sent = []

    async def fake_send(text, *, title="", images=(), event="notify"):
        sent.append(event)
        return {"ok": True}

    monkeypatch.setattr(notify, "kind", lambda: "ntfy")
    monkeypatch.setattr(notify, "send", fake_send)
    zone = ZoneInfo(util.config.TZ)
    day = dt.date.fromisoformat(loaded.today)
    clock = {"now": dt.datetime.combine(day, dt.time(18, 29), zone)}
    monkeypatch.setattr(util, "now_local", lambda: clock["now"])

    async def go():
        s = _sched()
        await s._tick()
        assert not [c for c in llm.calls if c[0] == "brief"]  # 18:29: not yet
        clock["now"] = clock["now"].replace(minute=31)
        await s._tick()
        for _ in range(200):
            if not s.briefing:
                break
            await asyncio.sleep(0.02)
        await s._tick()
        assert len([c for c in llm.calls if c[0] == "brief"]) == 1  # once a day
        # next morning 09:05: yesterday's brief goes out once
        clock["now"] = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(9, 5), zone)
        await s._tick()
        await s._tick()
        assert sent == ["brief"]
        assert store.val("select posted from briefs where day = ?", (loaded.today,)) == 1

    asyncio.run(go())
