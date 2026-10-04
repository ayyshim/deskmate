"""Every /api/sec route against the fake world: each answer validates against its contract model (the
routes declare response_model, and the tests validate again so a route without one cannot slip by),
errors are one sentence, and the UI cookie is required."""

from __future__ import annotations

import time

import pytest

from conftest import ui_client


def _ok(r, model, status: int = 200):
    from app.secretary import contract as C

    assert r.status_code == status, (r.status_code, r.text[:300])
    getattr(C, model).model_validate(r.json())
    return r.json()


def _wait(client, url: str, done, timeout: float = 10.0):
    end = time.time() + timeout
    while time.time() < end:
        body = client.get(url).json()
        if done(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"{url} did not finish")


def test_needs_the_ui_cookie(loaded):
    from fastapi.testclient import TestClient

    c = ui_client()
    anon = TestClient(c.app)
    assert anon.get("/api/sec/state").status_code == 401
    assert anon.post("/api/sec/sweep").status_code == 401


def test_every_read_route_validates(loaded):
    c = ui_client()
    w = loaded
    st = _ok(c.get("/api/sec/state"), "StateResp")
    assert st["secretary"]["status"] == "no_token"  # the stub model layer has no login in this fixture
    assert st["sources"]["docs"]["kind"] == "hub" and st["sources"]["repos"] >= 3
    assert st["counts"]["sessions"] == 2
    assert {r["key"] for r in st["repos"]} >= {"shop", "tools", "lib"}
    _ok(c.get("/api/sec/usage"), "Usage")
    t = _ok(c.get("/api/sec/today"), "TodayResp")
    assert t["day"] == w.today and t["stats"]["sessions"] >= 1
    _ok(c.get(f"/api/sec/today?day={w.yesterday}"), "TodayResp")
    _ok(c.get("/api/sec/timeline"), "TimelineResp")
    _ok(c.get("/api/sec/timeline?repo=shop&days=3"), "TimelineResp")
    s = _ok(c.get("/api/sec/sessions"), "SessionsResp")
    ids = [x["id"] for x in s["sessions"]]
    assert w.sid_a in ids and w.sid_c not in ids and w.sid_d not in ids
    _ok(c.get("/api/sec/sessions?repo=shop&limit=1"), "SessionsResp")
    _ok(c.get("/api/sec/sessions?q=checkout"), "SessionsResp")
    d = _ok(c.get(f"/api/sec/sessions/{w.sid_a}"), "SessionDetailResp")
    assert d["session"]["id"] == w.sid_a
    it = _ok(c.get(f"/api/sec/sessions/{w.sid_a}/items"), "ItemsResp")
    kinds = {x["kind"] for x in it["items"]}
    assert {"prompt", "assistant", "steps", "compact", "interrupt"} <= kinds, kinds
    _ok(c.get(f"/api/sec/sessions/{w.sid_a}/items?cursor=2&limit=3"), "ItemsResp")
    from pydantic import TypeAdapter

    from app.secretary import contract as C

    one = c.get(f"/api/sec/sessions/{w.sid_a}/items/0")
    assert one.status_code == 200
    TypeAdapter(C.Item).validate_python(one.json())
    f = _ok(c.get(f"/api/sec/sessions/{w.sid_a}/find?q=checkout"), "FindResp")
    assert f["total"] >= 1
    _ok(c.get("/api/sec/board"), "BoardResp")
    lp = _ok(c.get("/api/sec/loops?state=all"), "LoopsResp")
    assert lp["counts"]["open"] >= 1
    _ok(c.get("/api/sec/loops?group=you&repo=shop"), "LoopsResp")
    dec = _ok(c.get("/api/sec/decisions"), "DecisionsResp")
    assert any("cents" in x["text"] for x in dec["rows"])
    _ok(c.get("/api/sec/decisions?q=cents&repo=all"), "DecisionsResp")
    _ok(c.get("/api/sec/asks"), "AsksResp")
    _ok(c.get("/api/sec/notes"), "NotesResp")
    h = _ok(c.get("/api/sec/hygiene"), "HygieneResp")
    assert h["checks"]


def test_bad_parameters_and_unknown_ids(loaded):
    c = ui_client()
    for url in ("/api/sec/sessions/nope", "/api/sec/sessions/nope/items", "/api/sec/asks/999999",
                "/api/sec/brief?day=2001-01-01"):
        r = c.get(url)
        assert r.status_code == 404, (url, r.status_code)
        assert isinstance(r.json()["detail"], str)
    for url in ("/api/sec/today?day=yesterday", "/api/sec/sessions?limit=-1", "/api/sec/loops?state=sideways",
                "/api/sec/brief?day=x"):
        assert c.get(url).status_code in (400, 422), url


def test_loop_actions_and_notes(loaded):
    c = ui_client()
    lp = c.get("/api/sec/loops").json()
    first = (lp["you"] + lp["followup"])[0]
    _ok(c.post(f"/api/sec/loops/{first['id']}/snooze", json={"days": 2}), "LoopActionResp")
    sn = c.get("/api/sec/loops?state=snoozed").json()
    assert [x for x in sn["you"] + sn["followup"] if x["id"] == first["id"]]
    _ok(c.post(f"/api/sec/loops/{first['id']}/reopen"), "LoopActionResp")
    _ok(c.post(f"/api/sec/loops/{first['id']}/done"), "LoopActionResp")
    assert c.post("/api/sec/loops/lp_nope/done").status_code == 404
    n = _ok(c.post("/api/sec/notes", json={"kind": "followup", "text": "Ask about the tax rule TOKEN=FAKEfake0123456789secretTOKEN"}),
            "NoteCreated", 201)
    assert "FAKEfake0123456789secretTOKEN" not in str(n)
    assert c.post("/api/sec/notes", json={"kind": "followup", "text": "  "}).status_code == 400
    _ok(c.post("/api/sec/notes", json={"kind": "decision", "text": "Tax rounds per line", "repo": "shop"}), "NoteCreated", 201)
    assert any("per line" in x["text"] for x in c.get("/api/sec/decisions").json()["rows"])


def test_model_routes_without_a_login(loaded):
    c = ui_client()
    r = c.post("/api/sec/ask", json={"question": "What is open?"})
    assert r.status_code == 503 and "login" in r.json()["detail"]
    assert c.post("/api/sec/brief/run", json={}).status_code == 503
    r = _ok(c.post(f"/api/sec/sessions/{loaded.sid_a}/digest"), "DigestQueued", 202)
    assert r["digest_state"] in ("queued", "no_token")


def test_pause_and_sweep(loaded):
    c = ui_client()
    r = _ok(c.post("/api/sec/pause", json={"paused": True}), "PauseResp")
    assert r["secretary"]["status"] == "paused_user"
    r = _ok(c.post("/api/sec/pause", json={"paused": False}), "PauseResp")
    assert r["secretary"]["status"] != "paused_user"
    _ok(c.post("/api/sec/sweep"), "SweepResp", 202)


def test_secretary_off_says_so(loaded, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "SECRETARY", False)
    c = ui_client()
    st = _ok(c.get("/api/sec/state"), "StateResp")
    assert st["secretary"]["status"] == "off"
    assert _ok(c.get("/api/sec/usage"), "Usage")["error"] == "off"
    r = c.post("/api/sec/ask", json={"question": "x"})
    assert r.status_code == 503 and "off" in r.json()["detail"]
    assert c.post("/api/sec/sweep").json()["started"] is False


def test_no_docs_folder(world, monkeypatch):
    """DOCS_DIR unset: pages that need the hub still answer, and state says there is no docs folder."""
    from app import config
    from app.secretary import sweep

    from conftest import ingest_all

    monkeypatch.setattr(config, "DOCS_DIR", "")
    ingest_all(world)
    sweep.run_sweep()
    c = ui_client()
    st = _ok(c.get("/api/sec/state"), "StateResp")
    assert st["sources"]["docs"] == {"set": False, "found": False, "label": None, "kind": "none",
                                     "changelog": False, "design": False}
    _ok(c.get("/api/sec/board"), "BoardResp")
    _ok(c.get("/api/sec/decisions"), "DecisionsResp")
    _ok(c.get("/api/sec/today"), "TodayResp")
    _ok(c.get("/api/sec/hygiene"), "HygieneResp")


@pytest.mark.parametrize("q", ["x11vnc -unixsockonly", 'say "hi', "C++", "--  !!", "*", "NEAR(a b)", "a:b", "%_", "ü café",
                               "' or 1=1 --", "(", "^", "checkout"])
def test_search_with_odd_characters(loaded, q):
    c = ui_client()
    r = c.get("/api/sec/search", params={"q": q})
    assert r.status_code in (200, 400), (q, r.status_code)
    if r.status_code == 200:
        _ok(r, "SearchResp")
    f = c.get(f"/api/sec/sessions/{loaded.sid_a}/find", params={"q": q})
    assert f.status_code in (200, 400), (q, f.status_code)


def test_search_finds_prompts_commands_and_files(loaded):
    c = ui_client()
    for q in ("checkout", "pytest", "total.py", "tax rounding"):
        res = _ok(c.get("/api/sec/search", params={"q": q}), "SearchResp")
        assert res["total_hits"] >= 1, q
    assert c.get("/api/sec/search", params={"q": "FAKEfake0123456789secretTOKEN"}).json()["total_hits"] == 0


def test_routes_after_a_digest_a_brief_and_an_ask(loaded, llm):
    """The same routes once the stub model layer has written a digest, a brief and an answer."""
    import asyncio

    from app.secretary import store, sweep
    from app.secretary.scheduler import Scheduler

    async def go():
        s = Scheduler()
        store.set_loop(asyncio.get_running_loop())
        s.wake, s.ingest_lock, s.started = asyncio.Event(), asyncio.Lock(), True
        await asyncio.to_thread(s.consider_digest, loaded.sid_a)
        await s._digest_next()
        await s.run_brief(loaded.today, "manual")
        aid = await s.run_ask("What is open?", "ui")
        for _ in range(300):
            if not s.briefing and s.asks == 0:
                break
            await asyncio.sleep(0.02)
        return aid

    aid = asyncio.run(go())
    store.set_loop(None)
    sweep.run_sweep()
    c = ui_client()
    st = _ok(c.get("/api/sec/state"), "StateResp")
    assert st["secretary"]["status"] == "on" and st["caps"]["digests_today"] == 1
    u = _ok(c.get("/api/sec/usage"), "Usage")
    assert u["ok"] and u["five_hour"]["utilization"] == 0.2
    d = _ok(c.get(f"/api/sec/sessions/{loaded.sid_a}"), "SessionDetailResp")
    assert d["digest"] and d["digest"]["title"] == "Checkout total fixed"
    t = _ok(c.get("/api/sec/today"), "TodayResp")
    assert t["brief"] and t["brief"]["status"] == "ready"
    _ok(c.get("/api/sec/brief"), "BriefResp")
    lp = _ok(c.get("/api/sec/loops"), "LoopsResp")
    assert any(x["kind"] == "digest" for x in lp["you"] + lp["followup"])
    _ok(c.get("/api/sec/timeline"), "TimelineResp")
    _ok(c.get("/api/sec/sessions"), "SessionsResp")
    _ok(c.get("/api/sec/decisions"), "DecisionsResp")
    _ok(c.get("/api/sec/board"), "BoardResp")
    a = _ok(c.get(f"/api/sec/asks/{aid}"), "Ask")
    assert a["status"] == "done"
    _ok(c.get("/api/sec/asks"), "AsksResp")
    r = _ok(c.post(f"/api/sec/sessions/{loaded.sid_a}/digest"), "DigestQueued", 202)
    assert r["ok"] is False and r["digest_state"] == "ready"  # nothing new since the digest


def test_usage_refresh_probes_unless_the_user_paused(world, llm, monkeypatch):
    """The meter's refresh is a model request; a secretary the user paused spends none on it."""
    import time

    from app.secretary import views

    old = llm.usage_state
    monkeypatch.setattr(llm, "usage_state", lambda: dict(old(), ts=time.time() - 120))  # an old reading
    c = ui_client()
    _ok(c.get("/api/sec/usage?refresh=1"), "Usage")
    assert llm.calls.count(("probe",)) == 1
    views.set_paused(True)
    _ok(c.get("/api/sec/usage?refresh=1"), "Usage")
    assert llm.calls.count(("probe",)) == 1
