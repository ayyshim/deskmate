"""The Secretary's REST API: every /api/sec route (research contract §2), backed by SQLite.

Each route needs the UI cookie (auth.need_ui) and answers with a model from contract.py, so a stray or
missing field fails in the tests instead of reaching the page. Errors are {"detail": "<one sentence>"}:
400 bad parameter, 404 unknown id, 409 already running, 503 no Claude login or the secretary is off.
Reads run in FastAPI's thread pool on the secretary's own connections; the few routes that start work
(Ask, the brief, the sweep, the usage probe) are async and hand it to the scheduler on the event loop.
"""

from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import auth, config
from . import contract as C
from . import gate, store, util, views
from .scheduler import SCHED, Busy, NoLogin, Off


def _guard(request: Request) -> None:
    auth.need_ui(request)


router = APIRouter(prefix="/api/sec", dependencies=[Depends(_guard)])


def _repo(repo: str | None) -> str | None:
    return None if not repo or repo == "all" else repo


def _int(value: str | None, default: int, lo: int, hi: int, name: str) -> int:
    if value is None or value == "":
        return default
    if not re.fullmatch(r"\d{1,9}", str(value)):
        raise HTTPException(400, f"{name} must be a whole number.")
    return max(lo, min(hi, int(value)))


def _call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except views.BadRequest as exc:
        raise HTTPException(400, str(exc)) from None
    except views.NotFound as exc:
        raise HTTPException(404, str(exc)) from None


# ---------------------------------------------------------------- state and usage


@router.get("/state", response_model=C.StateResp)
def state():
    return views.state(SCHED.running)


@router.get("/usage", response_model=C.Usage)
async def usage(refresh: int = 0):
    # The probe is a (1-token) model request. An open tab asks once a minute; a secretary the user paused calls
    # no model, so it does not spend one on the meter either: the meter keeps its last reading.
    if refresh and config.SECRETARY and await asyncio.to_thread(store.kv_get, "sec.paused") != "1":
        u = gate.usage_snapshot()
        if u["ts"] is None or time.time() - u["ts"] > 60:
            await gate.probe()
            store.publish("sec_usage", gate.usage_snapshot())
    return gate.usage_snapshot()


# ---------------------------------------------------------------- today, timeline


@router.get("/today", response_model=C.TodayResp)
def today(day: str | None = None):
    return _call(views.today_page, day)


@router.get("/timeline", response_model=C.TimelineResp)
def timeline(repo: str = "all", before: str | None = None, days: int = 7):
    return _call(views.timeline, _repo(repo), before, days)


# ---------------------------------------------------------------- sessions and search


@router.get("/sessions", response_model=C.SessionsResp)
def sessions(q: str = "", repo: str = "all", cursor: str | None = None, limit: str | None = None):
    return _call(views.sessions_list, q, _repo(repo), _int(cursor, 0, 0, 10**9, "cursor"),
                 _int(limit, 50, 1, 200, "limit"))


@router.get("/search", response_model=C.SearchResp)
def search(q: str = "", repo: str = "all", cursor: str | None = None, limit: str | None = None):
    return _call(views.search, q, _repo(repo), _int(cursor, 0, 0, 10**9, "cursor"), _int(limit, 20, 1, 50, "limit"))


@router.get("/sessions/{sid}", response_model=C.SessionDetailResp)
def session(sid: str):
    return _call(views.session_detail, sid)


@router.get("/sessions/{sid}/items", response_model=C.ItemsResp)
def items(sid: str, cursor: str | None = None, limit: str | None = None):
    return _call(views.items, sid, _int(cursor, 0, 0, 10**9, "cursor"), _int(limit, 500, 1, 2000, "limit"))


@router.get("/sessions/{sid}/items/{i}", response_model=C.Item)
def item(sid: str, i: int):
    return _call(views.item, sid, i)


@router.post("/sessions/{sid}/digest", response_model=C.DigestQueued, status_code=202)
def digest_now(sid: str):
    """Queue a digest for one session (older sessions are not digested on their own, design §13)."""
    if not config.SECRETARY:
        raise HTTPException(503, "The secretary is off.")
    res = SCHED.queue_digest(sid)
    if res is None:
        raise HTTPException(404, "No such session.")
    if res["ok"]:
        store.publish_session(sid, "open", res["digest_state"])
        store.publish("sec_state", None)
    return res


@router.get("/sessions/{sid}/find", response_model=C.FindResp)
def find(sid: str, q: str = ""):
    return _call(views.find, sid, q)


# ---------------------------------------------------------------- board, loops, decisions


@router.get("/board", response_model=C.BoardResp)
def board():
    return views.board()


@router.get("/loops", response_model=C.LoopsResp)
def loops(state: str = "open", group: str = "all", repo: str = "all"):
    if state not in ("open", "snoozed", "done", "all"):
        raise HTTPException(400, "state is one of open, snoozed, done, all.")
    if group not in ("all", "you", "followup"):
        raise HTTPException(400, "group is one of all, you, followup.")
    return views.loops(state, group, _repo(repo))


def _loop_changed(res: dict) -> dict:
    store.publish("sec_loops", {"counts": res["counts"]})
    return res


@router.post("/loops/{loop_id}/done", response_model=C.LoopActionResp)
def loop_done(loop_id: str):
    return _loop_changed(_call(views.loop_action, loop_id, "done"))


@router.post("/loops/{loop_id}/snooze", response_model=C.LoopActionResp)
def loop_snooze(loop_id: str, body: C.SnoozeReq | None = None):
    body = body or C.SnoozeReq()
    return _loop_changed(_call(views.loop_action, loop_id, "snooze", days=body.days, until=body.until))


@router.post("/loops/{loop_id}/reopen", response_model=C.LoopActionResp)
def loop_reopen(loop_id: str):
    return _loop_changed(_call(views.loop_action, loop_id, "reopen"))


@router.get("/decisions", response_model=C.DecisionsResp)
def decisions(q: str = "", repo: str = "all", cursor: str | None = None, limit: str | None = None):
    return views.decisions(q, _repo(repo), _int(cursor, 0, 0, 10**9, "cursor"), _int(limit, 200, 1, 500, "limit"))


# ---------------------------------------------------------------- ask, notes


@router.get("/asks", response_model=C.AsksResp)
def asks(cursor: str | None = None, limit: str | None = None):
    return views.asks(_int(cursor, 0, 0, 10**12, "cursor") or None, _int(limit, 20, 1, 100, "limit"))


@router.post("/ask", response_model=C.AskCreated, status_code=202)
async def ask(body: C.AskReq):
    if not body.question.strip():
        raise HTTPException(400, "Write a question first.")
    try:
        aid = await SCHED.run_ask(body.question.strip(), "ui")
    except Busy as exc:
        raise HTTPException(409, str(exc)) from None
    except (NoLogin, Off) as exc:
        raise HTTPException(503, str(exc)) from None
    store.publish("sec_ask", {"id": aid, "status": "running"})
    return {"ask": views.get_ask(aid)}


@router.get("/asks/{ask_id}", response_model=C.Ask)
def ask_one(ask_id: int):
    return _call(views.get_ask, ask_id)


@router.post("/notes", response_model=C.NoteCreated, status_code=201)
def add_note(body: C.NoteReq):
    res = _call(views.add_note, body.kind, body.text, body.repo, body.where, body.session)
    if res["loop"] is not None:
        store.publish("sec_loops", {"counts": views.loop_counts()})
    return res


@router.get("/notes", response_model=C.NotesResp)
def notes(cursor: str | None = None, limit: str | None = None):
    return views.notes(_int(cursor, 0, 0, 10**12, "cursor") or None, _int(limit, 50, 1, 200, "limit"))


# ---------------------------------------------------------------- brief, pause, hygiene, sweep


@router.get("/brief", response_model=C.BriefResp)
def brief(day: str | None = None):
    day = day or util.today()
    if not util.is_day(day):
        raise HTTPException(400, "day must look like 2026-10-04.")
    b = views.get_brief(day)
    if b is None:
        raise HTTPException(404, "No brief for that day.")
    return {"brief": views.brief_dict(b)}


@router.post("/brief/run", response_model=C.BriefResp, status_code=202)
async def brief_run(body: C.BriefRunReq | None = None):
    day = (body.day if body else None) or util.today()
    if day > util.today():
        raise HTTPException(400, "That day has not happened yet.")
    try:
        await SCHED.run_brief(day, "manual")
    except Busy as exc:
        raise HTTPException(409, str(exc)) from None
    except (NoLogin, Off) as exc:
        raise HTTPException(503, str(exc)) from None
    return {"brief": views.brief_dict(views.get_brief(day))}


@router.post("/pause", response_model=C.PauseResp)
def pause(body: C.PauseReq):
    views.set_paused(body.paused)
    store.publish("sec_state", None)
    SCHED._poke_threadsafe()
    return {"ok": True, "secretary": views.secretary_status(SCHED.running)}


@router.get("/hygiene", response_model=C.HygieneResp)
def hygiene():
    return views.hygiene()


@router.post("/sweep", response_model=C.SweepResp, status_code=202)
async def sweep():
    return {"ok": bool(config.SECRETARY), "started": SCHED.request_sweep()}
