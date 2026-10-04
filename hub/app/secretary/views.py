"""What each Secretary page shows, assembled from SQLite (research contract §1 data map, §2.3 rules).

api.py validates parameters and returns these dicts under the pydantic models in contract.py; condense.py
reuses them for the daily brief, so the brief and the Today page always agree. Everything here only reads,
except the small user-state writers at the end (loop state, notes, pause), which the API owns.
"""

from __future__ import annotations

import json
import os
import re
import time

from .. import config
from . import gate, gitlog, store, util

VISIBLE = "skipped_reason is null and coalesce(prompts, 0) > 0"
VISIBLE_S = "s.skipped_reason is null and coalesce(s.prompts, 0) > 0"  # the same, for queries that alias cc_sessions as s
RUNNING_S = 120
ENDED_S = 24 * 3600
TEXT_CUT = 20_000

DIGEST_LABELS = {"ready": "digest ready", "queued": "digest queued", "running": "writing the digest…",
                 "capped": "daily cap reached", "no_token": "no Claude login", "failed": "digest failed",
                 "none": "no digest yet"}


class BadRequest(ValueError):
    pass


class NotFound(LookupError):
    pass


def _j(raw, default):
    if not raw:
        return default
    try:
        v = json.loads(raw)
        return v if isinstance(v, type(default)) else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------- sessions


def session_state(row, now: float | None = None) -> str:
    now = now or time.time()
    mtime = row["mtime"] or 0
    if row["ended"] or now - mtime > ENDED_S:
        return "ended"
    return "running" if now - mtime < RUNNING_S else "open"


def digest_dict(d, read_now: int | None) -> dict:
    """read_now: how far the session's transcript has been read. Compared with what the digest covers, not
    with the file size, which counts a half-written last line too (the digest would look stale for good)."""
    loops = [x for x in _j(d["open_loops"], []) if isinstance(x, dict) and x.get("text")]
    outcome = d["outcome"] if d["outcome"] in ("shipped", "pushed", "in_progress", "explored", "blocked") else None
    to = int(d["to_offset"] or 0)
    return {"session": d["session"], "ts": float(d["ts"]), "day": d["day"], "model": d["model"],
            "title": d["title"] or "", "repos": [x for x in _j(d["repos"], []) if isinstance(x, str)],
            "outcome": outcome, "summary": d["summary"] or "",
            "shipped": [str(x) for x in _j(d["shipped"], [])], "decisions": [str(x) for x in _j(d["decisions"], [])],
            "open_loops": [{"text": str(x["text"]), "owner": x.get("owner") if x.get("owner") in ("you", "claude") else "you"}
                           for x in loops],
            "blockers": [str(x) for x in _j(d["blockers"], [])], "to_offset": to,
            "stale": bool(read_now is not None and read_now > to)}


def latest_digests(ids: list[str] | None = None) -> dict[str, dict]:
    sql = "select * from digests where id in (select max(id) from digests group by session)"
    rows = store.q(sql)
    out = {}
    for r in rows:
        if ids is None or r["session"] in ids:
            out[r["session"]] = r
    return out


def digest_label(row) -> str:
    st = row["digest_state"] or "none"
    if st == "paused":
        if store.kv_get("sec.paused") == "1":
            return "paused by you"
        return gate.usage_label() or "paused"
    if st == "skipped" and (row["digested_offset"] or 0) > 0:
        return DIGEST_LABELS["ready"]  # (stores from before the fix) only the part after the digest was too small
    if st == "skipped":
        return f"skipped: {row['digest_error'] or 'too small'}"
    if st == "failed" and row["digest_error"]:
        return f"digest failed: {gate.failure_phrase(gate.failure_code(row['digest_error']))}"
    return DIGEST_LABELS.get(st, "no digest yet")


def title_of(row, d) -> tuple[str, str]:
    if row["custom_title"]:
        return row["custom_title"], "custom"
    if d is not None and d["title"]:
        return d["title"], "digest"
    if row["ai_title"]:
        return row["ai_title"], "ai"
    if row["first_prompt"]:
        fp = row["first_prompt"]
        return (fp if len(fp) <= 80 else fp[:79] + "…"), "prompt"
    return "Untitled session", "none"


def session_row(row, d=None, now: float | None = None, day: str | None = None) -> dict:
    now = now or time.time()
    state = session_state(row, now)
    first, last = row["first_ts"] or row["mtime"] or now, row["last_ts"] or row["mtime"] or now
    title, tsrc = title_of(row, d)
    if d is not None and d["summary"]:
        summary, ssrc = d["summary"], "digest"
    elif row["first_prompt"]:
        summary, ssrc = row["first_prompt"], "prompt"
    else:
        summary, ssrc = None, None
    outcome = d["outcome"] if d is not None and d["outcome"] in ("shipped", "pushed", "in_progress", "explored",
                                                                   "blocked") else None
    tl = util.time_range_label(first, last, state == "running")
    if day:
        dd = store.one("select first_ts, last_ts from cc_session_days where session = ? and day = ?", (row["id"], day))
        if dd:
            tl = util.time_range_label(dd["first_ts"], dd["last_ts"], state == "running" and day == util.today())
    return {"id": row["id"], "title": title, "title_source": tsrc, "day": util.day_of(first),
            "day_label": util.day_label(util.day_of(first)), "first_ts": float(first), "last_ts": float(last),
            "time_label": tl, "state": state, "outcome": outcome, "summary": summary, "summary_source": ssrc,
            "repos": [x for x in _j(row["repos"], []) if isinstance(x, str)], "cwd": row["cwd"],
            "cwd_label": util.tilde(row["cwd"]) if row["cwd"] else None, "model": row["model"],
            "model_label": util.model_label(row["model"]), "prompts": int(row["prompts"] or 0),
            "turns": int(row["turns"] or 0), "steps": int(row["steps"] or 0), "errors": int(row["errors"] or 0),
            "digest_state": row["digest_state"] or "none", "digest_label": digest_label(row),
            "digest_stale": bool(d is not None and (row["read_offset"] or 0) > int(d["to_offset"] or 0))}


def visible_sessions(repo: str | None = None) -> list:
    if repo:
        return store.q(f"""select * from cc_sessions where {VISIBLE} and exists
                           (select 1 from json_each(cc_sessions.repos) where value = ?) order by last_ts desc""", (repo,))
    return store.q(f"select * from cc_sessions where {VISIBLE} order by last_ts desc")


def get_session(sid: str):
    row = store.one(f"select * from cc_sessions where id = ? and {VISIBLE}", (sid,))
    if row is None:
        raise NotFound("No such session.")
    return row


def sessions_list(q: str, repo: str | None, offset: int, limit: int) -> dict:
    rows = visible_sessions(repo)
    ds = latest_digests()
    now = time.time()
    total = len(rows)
    out = []
    needle = (q or "").strip().lower()
    for r in rows:
        sr = session_row(r, ds.get(r["id"]), now)
        if needle and needle not in sr["title"].lower() and needle not in (sr["summary"] or "").lower():
            continue
        out.append(sr)
    page = out[offset: offset + limit]
    nxt = str(offset + limit) if offset + limit < len(out) else None
    return {"total": total, "matched": len(out), "sessions": page, "next_cursor": nxt}


def search(q: str, repo: str | None, offset: int, limit: int) -> dict:
    if len(re.sub(r"\W", "", q or "")) < 2:
        raise BadRequest("Type at least two characters.")
    res = util.search(store.conn(), q, repo=repo, limit_sessions=limit, offset=offset, visible_sql=VISIBLE)
    ids = [r["session"] for r in res["results"]]
    rows = {r["id"]: r for r in store.q(f"select * from cc_sessions where id in ({','.join('?' * len(ids))})", ids)} if ids else {}
    ds = latest_digests(ids)
    now = time.time()
    results = [{"session": session_row(rows[r["session"]], ds.get(r["session"]), now), "hits": r["hits"],
                "more": r["more"]} for r in res["results"] if r["session"] in rows]
    nxt = str(res["next_offset"]) if res["next_offset"] is not None else None
    return {"q": q, "total_hits": res["total_hits"], "total_sessions": res["total_sessions"], "results": results,
            "next_cursor": nxt}


def _step_status(st: dict, running: bool) -> tuple[str, str]:
    if st["status"] == "running" and not running:
        return "unknown", "—"
    return st["status"], st["status_label"] or ("…" if st["status"] == "running" else "—")


def session_sources(row) -> list[dict]:
    """Changelog entries this session wrote (rule 11) and memory notes it started."""
    out: list[dict] = []
    if config.DOCS_DIR:
        cl = os.path.join(config.DOCS_DIR, "changelog") + "/"
        files = store.q("select path from cc_files where session = ? and action != 'read' and path like ?",
                        (row["id"], cl + "%"))
        lo, hi = (row["first_ts"] or 0) - 900, (row["last_ts"] or 0) + 900
        for f in files:
            for e in store.q("select * from hub_entries where kind = 'changelog' and path = ? order by line", (f["path"],)):
                x = _j(e["extra"], {})
                ts = util.local_ts(x.get("file_day") or e["day"], e["time"])
                if ts is None or lo <= ts <= hi:
                    out.append(changelog_source(e))
    for m in store.q("select * from hub_entries where kind = 'memory'"):
        if _j(m["extra"], {}).get("origin_session") == row["id"]:
            out.append(util.source("memory", f"memory/{os.path.basename(m['path'] or '')[:-3]}", path=m["path"], line=1))
    return out


def changelog_source(e) -> dict:
    x = _j(e["extra"], {})
    label = f"{e['repo']}/{x.get('file_day') or e['day']}" + (f" · {e['time']}" if e["time"] else "")
    return util.source("changelog", label, path=e["path"], line=e["line"])


def session_detail(sid: str) -> dict:
    row = get_session(sid)
    d = latest_digests([sid]).get(sid)
    now = time.time()
    meta = session_row(row, d, now)
    running = meta["state"] == "running"
    meta.update({"transcript": row["transcript"], "transcript_exists": bool(row["transcript"] and os.path.exists(row["transcript"])),
                 # "HEAD" is what Claude Code records outside a repo or on a detached checkout: no branch to show
                 "branch": row["branch"] if row["branch"] != "HEAD" else None,
                 "resume_command": util.resume_command(sid, row["cwd"]),
                 "compactions": int(row["compactions"] or 0), "subagents": int(row["subagents"] or 0),
                 "items": int(row["items"] or 0)})
    files = store.q("select * from cc_files where session = ? order by count desc, last_ts desc", (sid,))
    cmds = store.q("select * from cc_commands where session = ? order by ts desc", (sid,))
    failed = [c for c in cmds if c["status"] == "error"]
    shown = (failed + [c for c in cmds if c["status"] != "error"])[:20]
    commands = []
    for c in shown:
        status, label = _step_status({"status": c["status"] or "running",
                                      "status_label": (f"exit {c['exit_code']}" if c["status"] == "error" and c["exit_code"]
                                                       else c["status"] if c["status"] in ("denied", "interrupted") else
                                                       "started" if c["status"] == "background" else
                                                       "error" if c["status"] == "error" else "ok")}, running)
        commands.append({"item": int(c["item"] or 0), "n": int(c["n"] or 0), "turn": int(c["turn"] or 0),
                         "command": (c["command"] or "").splitlines()[0][:300] if c["command"] else "",
                         "description": c["description"], "status": status, "status_label": label,
                         "exit_code": c["exit_code"], "ts": c["ts"]})
    published = []
    for ln in store.q("select * from cc_links where session = ? order by ts", (sid,)):
        x = _j(ln["extra"], {})
        if ln["kind"] == "commit":
            detail = " on ".join(v for v in (x.get("sha"), x.get("branch")) if v) or None
        elif ln["kind"] == "push":
            detail = x.get("branch")
        elif ln["kind"] in ("discord", "notify"):
            detail = str(x["status"]) if x.get("status") else None
        elif ln["kind"] == "pr":
            detail = " ".join(str(v) for v in (f"#{x['number']}" if x.get("number") else None, x.get("repo")) if v) or None
        else:
            detail = None
        published.append({"kind": ln["kind"], "title": ln["title"] or ln["kind"], "url": ln["url"], "detail": detail,
                          "ts": ln["ts"], "item": ln["item"], "turn": ln["turn"]})
    return {"session": meta, "digest": digest_dict(d, row["read_offset"]) if d else None,
            "side": {"files": [{"path": f["path"], "label": util.rel(f["path"]), "repo": f["repo"], "action": f["action"],
                                "count": int(f["count"] or 0), "last_item": f["last_item"]} for f in files[:12]],
                     "files_total": len(files), "commands": commands, "commands_total": len(cmds),
                     "commands_failed": len(failed), "published": published},
            "sources": session_sources(row)}


def _item_dict(r, steps: list, running: bool, digested: int, last_assistant: set, live_idx: int | None,
               full: bool = False) -> dict:
    flags = _j(r["flags"], {})
    base = {"i": int(r["idx"]), "turn": int(r["turn"]), "ts": r["ts"], "time": util.hhmm(r["ts"])}
    kind = r["kind"]
    text = r["text"] or ""
    cut = (not full) and len(text) > TEXT_CUT
    chars = max(int(r["chars"] or 0), len(text))
    in_digest = (r["offset"] or 0) < digested
    if kind == "prompt":
        return dict(base, kind="prompt", text=text[:TEXT_CUT] if cut else text, chars=chars, truncated=cut,
                    mid_turn=bool(flags.get("mid_turn")), slash=flags.get("slash"), images=int(flags.get("images") or 0),
                    in_digest=in_digest)
    if kind == "assistant":
        return dict(base, kind="assistant", text=text[:TEXT_CUT] if cut else text, chars=chars, truncated=cut,
                    in_digest=in_digest and int(r["idx"]) in last_assistant, live=r["idx"] == live_idx,
                    model=flags.get("model"), error=bool(flags.get("error")))
    if kind == "steps":
        out = []
        for st in steps:
            status, label = _step_status(st, running)
            out.append({"n": int(st["n"]), "tool": st["tool"], "label": st["label"], "arg": st["arg"] or "",
                        "status": status, "status_label": label, "exit_code": st["exit_code"], "ms": st["ms"],
                        "ts": st["ts"], "path": st["path"]})
        return dict(base, kind="steps", summary=text or util.steps_summary([(s["label"], s["status"]) for s in out]),
                    n=len(out), ts_end=r["ts_end"], live=r["idx"] == live_idx, steps=out)
    if kind == "compact":
        trig = flags.get("trigger") if flags.get("trigger") in ("auto", "manual") else None
        return dict(base, kind="compact", text=text, trigger=trig)
    return dict(base, kind="interrupt", text=text or "You interrupted")


def _last_assistant_per_turn(sid: str) -> set:
    return {r[0] for r in store.q("select max(idx) from cc_items where session = ? and kind = 'assistant' group by turn",
                                  (sid,))}


def items(sid: str, cursor: int, limit: int) -> dict:
    row = get_session(sid)
    running = session_state(row) == "running"
    total = int(store.val("select count(*) from cc_items where session = ?", (sid,), 0))
    rows = store.q("select * from cc_items where session = ? and idx >= ? order by idx limit ?", (sid, cursor, limit))
    idxs = [r["idx"] for r in rows if r["kind"] == "steps"]
    steps: dict[int, list] = {}
    if idxs:
        for st in store.q(f"select * from cc_steps where session = ? and item in ({','.join('?' * len(idxs))}) order by item, n",
                          (sid, *idxs)):
            steps.setdefault(st["item"], []).append(dict(st))
    live_idx = total - 1 if running and total else None
    last_a = _last_assistant_per_turn(sid)
    digested = int(row["digested_offset"] or 0)
    out = [_item_dict(r, steps.get(r["idx"], []), running, digested, last_a, live_idx) for r in rows]
    nxt = cursor + len(rows) if cursor + len(rows) < total else None
    return {"session": sid, "total": total, "turns": int(row["turns"] or 0), "cursor": cursor, "next_cursor": nxt,
            "live": running, "items": out}


def item(sid: str, i: int) -> dict:
    row = get_session(sid)
    r = store.one("select * from cc_items where session = ? and idx = ?", (sid, i))
    if r is None:
        raise NotFound("No such item.")
    running = session_state(row) == "running"
    steps = [dict(s) for s in store.q("select * from cc_steps where session = ? and item = ? order by n", (sid, i))]
    total = int(store.val("select count(*) from cc_items where session = ?", (sid,), 0))
    return _item_dict(r, steps, running, int(row["digested_offset"] or 0), _last_assistant_per_turn(sid),
                      total - 1 if running else None, full=True)


def find(sid: str, q: str) -> dict:
    get_session(sid)
    if len(re.sub(r"\W", "", q or "")) < 2:
        raise BadRequest("Type at least two characters.")
    return util.find_in_session(store.conn(), sid, q)


# ---------------------------------------------------------------- loops


def _loop_states() -> dict:
    return {r["id"]: r for r in store.q("select * from loop_state")}


def loop_dict(r, st, today: str) -> dict:
    state, until, sts = "open", None, None
    if st is not None:
        sts = st["ts"]
        if st["state"] == "done":
            state = "done"
        elif st["state"] == "snoozed" and st["until"] and st["until"] > today:
            state, until = "snoozed", st["until"]
    src = _j(r["src"], {})
    if not src.get("kind"):
        src = util.source("note", "source")
    return {"id": r["id"], "group": r["grp"] if r["grp"] in ("you", "followup") else "you", "kind": r["kind"],
            "text": r["text"], "where": r["area"], "repo": r["repo"],
            "origin_day": r["origin_day"] if util.is_day(r["origin_day"]) else None,
            "age_days": util.age_days(r["origin_day"], today) if util.is_day(r["origin_day"]) else None,
            "age_label": util.age_label(r["origin_day"] if util.is_day(r["origin_day"]) else None, today),
            "state": state, "until": until, "state_ts": sts, "gone": bool(r["gone"]), "src": src}


def all_loops(include_gone: bool = False) -> list[dict]:
    today = util.today()
    sts = _loop_states()
    rows = store.q("select * from loops" + ("" if include_gone else " where gone = 0"))
    return [loop_dict(r, sts.get(r["id"]), today) for r in rows]


def loop_counts(loops: list[dict] | None = None) -> dict:
    loops = [lp for lp in (loops if loops is not None else all_loops()) if not lp["gone"]]
    return {"open": sum(lp["state"] == "open" for lp in loops),
            "you": sum(lp["state"] == "open" and lp["group"] == "you" for lp in loops),
            "followup": sum(lp["state"] == "open" and lp["group"] == "followup" for lp in loops),
            "snoozed": sum(lp["state"] == "snoozed" for lp in loops),
            "done": sum(lp["state"] == "done" for lp in loops)}


def _loop_order(group: str):
    if group == "you":
        return lambda lp: (lp["origin_day"] or "0000", lp["text"])
    return lambda lp: (lp["origin_day"] or "0000", lp["text"])


def loops(state: str, group: str, repo: str | None) -> dict:
    every = all_loops(include_gone=state == "all")
    counts = loop_counts(every)
    sel = [lp for lp in every if state == "all" or lp["state"] == state]
    if repo:
        sel = [lp for lp in sel if lp["repo"] == repo or lp["where"] == repo]
    you = sorted([lp for lp in sel if lp["group"] == "you"], key=_loop_order("you"), reverse=True)
    fu = sorted([lp for lp in sel if lp["group"] == "followup"], key=_loop_order("followup"), reverse=True)
    return {"state": state, "counts": counts, "you": you if group in ("all", "you") else [],
            "followup": fu if group in ("all", "followup") else []}


def loop_action(loop_id: str, action: str, days: int | None = None, until: str | None = None) -> dict:
    r = store.one("select * from loops where id = ?", (loop_id,))
    if r is None:
        raise NotFound("That loop is gone.")
    today = util.today()
    now = time.time()
    if action == "done":
        st = store.one("select * from loop_state where id = ?", (loop_id,))
        if st is None or st["state"] != "done":
            store.run("insert or replace into loop_state(id, state, until, ts) values(?, 'done', null, ?)", (loop_id, now))
    elif action == "reopen":
        store.run("insert or replace into loop_state(id, state, until, ts) values(?, 'open', null, ?)", (loop_id, now))
    else:
        if until is not None:
            if not util.is_day(until) or until <= today:
                raise BadRequest("Snooze until a day after today.")
        else:
            until = util.add_days(today, days or 3)
        store.run("insert or replace into loop_state(id, state, until, ts) values(?, 'snoozed', ?, ?)", (loop_id, until, now))
    st = store.one("select * from loop_state where id = ?", (loop_id,))
    return {"ok": True, "loop": loop_dict(r, st, today), "counts": loop_counts()}


# ---------------------------------------------------------------- decisions


def decision_rows() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for e in store.q("select * from hub_entries where kind = 'decision'"):
        x = _j(e["extra"], {})
        if x.get("withdrawn"):
            continue
        folder = x.get("folder") or ""
        label = f"design/{folder}" + (f" · {x['ref']}" if x.get("ref") else "")
        key = util.norm_text(e["title"])
        seen.add(key)
        out.append({"id": util.opaque_id("dc", e["id"]), "day": e["day"] if util.is_day(e["day"]) else None,
                    "text": e["title"] or "", "ref": x.get("ref"), "outcome": x.get("outcome"), "where": folder or None,
                    "repos": [p for p in x.get("projects") or [] if isinstance(p, str)], "kind": "design",
                    "is_question": bool(x.get("is_question")),
                    "src": util.source("decision", label, path=e["path"], line=e["line"])})
    ds = latest_digests()
    for sid, d in ds.items():
        s = store.one(f"select * from cc_sessions where id = ? and {VISIBLE}", (sid,))
        if s is None:
            continue
        reps = [x for x in _j(d["repos"], []) if isinstance(x, str)]
        title = title_of(s, d)[0][:40]
        for t in _j(d["decisions"], []):
            t = str(t)
            key = util.norm_text(t)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"id": util.opaque_id("dc", f"digest|{sid}|{key}"), "day": d["day"], "text": t, "ref": None,
                        "outcome": None, "where": reps[0] if reps else None, "repos": reps, "kind": "digest",
                        "is_question": False, "src": util.source("session", f"session · {title}", session=sid)})
    for n in store.q("select * from notes where kind = 'decision'"):
        key = util.norm_text(n["text"])
        if key in seen:
            continue
        seen.add(key)
        out.append(note_decision(n))
    out.sort(key=lambda r: (r["day"] is None, "" if r["day"] is None else "".join(chr(255 - ord(ch)) for ch in r["day"])))
    return out


def note_decision(n) -> dict:
    return {"id": util.opaque_id("dc", f"note|{n['id']}"), "day": util.day_of(n["ts"]), "text": n["text"], "ref": None,
            "outcome": None, "where": n["area"] or n["repo"], "repos": [n["repo"]] if n["repo"] else [], "kind": "note",
            "is_question": False, "src": util.source("note", "note", session=n["session"]) if n["session"]
            else util.source("note", "note")}


def decisions(q: str, repo: str | None, offset: int, limit: int) -> dict:
    rows = decision_rows()
    if repo:
        rows = [r for r in rows if repo in r["repos"] or r["where"] == repo]
    needle = (q or "").strip().lower()
    if needle:
        rows = [r for r in rows if any(needle in (r[k] or "").lower() for k in ("text", "outcome", "where", "ref"))]
    page = rows[offset: offset + limit]
    return {"total": len(rows), "rows": page, "next_cursor": str(offset + limit) if offset + limit < len(rows) else None}


# ---------------------------------------------------------------- board


LANES = [("building", "Designing or building", "Not ready to try yet"),
         ("built", "Built, not shipped", "Works somewhere, waits on a push, a deploy or a check"),
         ("shipped", "Shipped", "Deployed or implemented")]


def board() -> dict:
    today = util.today()
    open_by_area: dict[str, int] = {}
    for lp in all_loops():
        if lp["state"] == "open" and lp["where"]:
            open_by_area[lp["where"]] = open_by_area.get(lp["where"], 0) + 1
    cards: dict[str, list] = {k: [] for k, _, _ in LANES}
    unindexed = []
    for e in store.q("select * from hub_entries where kind = 'design'"):
        x = _j(e["extra"], {})
        folder = e["id"][3:]
        if not x.get("in_index"):
            unindexed.append(folder)
        lane = x.get("lane") if x.get("lane") in cards else "building"
        la = x.get("last_activity_day")
        cards[lane].append({"folder": folder, "title": e["title"] or folder, "status": x.get("status") or "",
                            "status_short": x.get("status_short") or "—",
                            "status_day": x.get("status_day") if util.is_day(x.get("status_day")) else None,
                            "lane": lane, "projects": [p for p in x.get("projects") or [] if isinstance(p, str)],
                            "last_activity_day": la if util.is_day(la) else None,
                            "last_activity_label": util.age_label(la if util.is_day(la) else None, today),
                            "open_loops": open_by_area.get(folder, 0), "prototype_url": x.get("prototype_url"),
                            "doc": util.source("design", f"design/{folder}", path=e["path"])})
    lanes = []
    for k, title, hint in LANES:
        cs = sorted(cards[k], key=lambda c: (c["last_activity_day"] or "", c["folder"]), reverse=True)
        lanes.append({"key": k, "title": title, "hint": hint, "cards": cs})
    return {"lanes": lanes, "unindexed": sorted(unindexed)}


# ---------------------------------------------------------------- hygiene


def hygiene() -> dict:
    h = store.kv_json("sec.hygiene", None)
    if not h:
        return {"ts": None, "counts": {"warn": 0, "ok": 0, "info": 0}, "checks": []}
    checks = h.get("checks") or []
    return {"ts": h.get("ts"), "counts": {lv: sum(c["level"] == lv for c in checks) for lv in ("warn", "ok", "info")},
            "checks": checks}


# ---------------------------------------------------------------- today and timeline


def brief_dict(b) -> dict:
    status = b["status"] if b["status"] in ("running", "ready", "failed") else "ready"
    trig = b["trigger"] if b["trigger"] in ("schedule", "manual") else None
    return {"day": b["day"], "ts": b["ts"], "status": status, "model": b["model"], "trigger": trig,
            "headline": b["headline"], "lede": b["body"], "error": b["error"], "posted_ts": b["posted_ts"]}


def get_brief(day: str):
    return store.one("select * from briefs where day = ?", (day,))


def _sessions_of_day(day: str, repo: str | None = None) -> list:
    sql = f"""select s.* from cc_session_days d join cc_sessions s on s.id = d.session
              where d.day = ? and {VISIBLE_S}"""
    args: list = [day]
    if repo:
        sql += " and exists (select 1 from json_each(s.repos) where value = ?)"
        args.append(repo)
    return store.q(sql + " order by d.first_ts", args)


def _linked_entries() -> dict[str, list]:
    """session id -> changelog hub_entries it wrote (rule 11)."""
    out: dict[str, list] = {}
    if not config.DOCS_DIR:
        return out
    cl = os.path.join(config.DOCS_DIR, "changelog") + "/"
    rows = store.q("""select f.session, s.first_ts, s.last_ts, e.* from cc_files f join cc_sessions s on s.id = f.session
                      join hub_entries e on e.kind = 'changelog' and e.path = f.path
                      where f.action != 'read' and f.path like ?""", (cl + "%",))
    for r in rows:
        x = _j(r["extra"], {})
        ts = util.local_ts(x.get("file_day") or r["day"], r["time"])
        if ts is None or (r["first_ts"] or 0) - 900 <= ts <= (r["last_ts"] or 0) + 900:
            out.setdefault(r["session"], []).append(r)
    return out


def needs_you(limit: int = 8) -> tuple[list[dict], int]:
    you = [lp for lp in all_loops() if lp["state"] == "open" and lp["group"] == "you"]
    you.sort(key=lambda lp: (lp["origin_day"] or "0000"), reverse=True)
    return you[:limit], len(you)


def day_neighbours(day: str) -> tuple[str | None, str | None]:
    prev = store.val(f"""select max(day) from (select d.day from cc_session_days d join cc_sessions s on s.id = d.session
                         where {VISIBLE_S} and d.day < ?
                         union select day from hub_entries where kind = 'changelog' and day < ?)""", (day, day))
    nxt = None
    if day < util.today():
        nxt = store.val(f"""select min(day) from (select d.day from cc_session_days d join cc_sessions s on s.id = d.session
                            where {VISIBLE_S} and d.day > ?
                            union select day from hub_entries where kind = 'changelog' and day > ?)""", (day, day))
        if nxt is None or nxt > util.today():
            nxt = util.today()
    return prev, nxt


def today_page(day: str | None) -> dict:
    today = util.today()
    day = day or today
    if not util.is_day(day):
        raise BadRequest("day must look like 2026-10-04.")
    if day > today:
        raise BadRequest("That day has not happened yet.")
    sess = _sessions_of_day(day)
    ids = [s["id"] for s in sess]
    ds = latest_digests(ids)
    linked = _linked_entries()
    shipped = []
    for e in store.q("select * from hub_entries where kind = 'changelog' and day = ? order by time is null, time, line", (day,)):
        x = _j(e["extra"], {})
        st = x.get("ship_state") if x.get("ship_state") in ("deployed", "pushed", "merged", "committed", "built", "shipped") else "shipped"
        shipped.append({"id": util.opaque_id("sh", e["id"]), "time": e["time"],
                        "ts": util.local_ts(x.get("file_day") or day, e["time"]), "text": e["title"] or "", "repo": e["repo"],
                        "state": st, "src": changelog_source(e)})
    for s in sess:
        d = ds.get(s["id"])
        if d is not None and d["outcome"] in ("shipped", "pushed") and s["id"] not in linked and d["day"] == day:
            shipped.append({"id": util.opaque_id("sh", f"session|{s['id']}"), "time": util.hhmm(d["ts"]),
                            "ts": float(d["ts"]), "text": d["title"] or title_of(s, d)[0],
                            "repo": (_j(d["repos"], []) or [None])[0], "state": d["outcome"],
                            "src": util.source("session", f"session · {title_of(s, d)[0][:40]}", session=s["id"])})
    shipped.sort(key=lambda r: (r["time"] is None, r["time"] or ""))
    in_progress = []
    for s in sess:
        d = ds.get(s["id"])
        if d is not None and d["outcome"] == "in_progress":
            t = title_of(s, d)[0]
            reps = _j(s["repos"], [])
            in_progress.append({"id": util.opaque_id("pg", f"session|{s['id']}"), "text": t,
                                "where": reps[0] if reps else None,
                                "src": util.source("session", f"session · {t[:40]}", session=s["id"])})
    for e in store.q("select * from hub_entries where kind = 'design'"):
        x = _j(e["extra"], {})
        if x.get("lane") == "building" and x.get("last_activity_day") == day:
            folder = e["id"][3:]
            in_progress.append({"id": util.opaque_id("pg", e["id"]), "text": e["title"] or folder, "where": folder,
                                "src": util.source("design", f"design/{folder}", path=e["path"])})
    decided = [r for r in decision_rows() if r["day"] == day]
    ny, ny_total = needs_you()
    counts = loop_counts()
    b = get_brief(day)
    brief = brief_dict(b) if b else None
    if brief and brief["status"] == "ready" and brief["lede"]:
        lede, lsrc = brief["lede"], "brief"
    else:
        firsts = []
        for s in sess:
            d = ds.get(s["id"])
            if d is not None and d["summary"]:
                m = re.match(r"(.+?[.!?])(\s|$)", d["summary"].strip())
                firsts.append(m.group(1) if m else d["summary"].strip())
            if len(firsts) == 3:
                break
        if firsts:
            lede, lsrc = " ".join(firsts), "digests"
        elif sess:
            reps = sorted({r for s in sess for r in _j(s["repos"], [])})
            lede = f"{len(sess)} session{'s' if len(sess) != 1 else ''}" + (f" in {', '.join(reps[:4])}" if reps else "") + \
                   (". The digests are written when the sessions settle." if day == today else ".")
            lsrc = "empty"
        else:
            lede = (f"No sessions yet today. The brief is written at {config.BRIEF_AT}." if day == today
                    else "No sessions on this day.")
            lsrc = "empty"
    folders = []
    dk = gitlog.docs_key()
    for r in store.q("select distinct repo from hub_entries where kind = 'changelog' and repo is not null"):
        folders.append(r[0])
    if config.DOCS_DIR and os.path.isdir(os.path.join(config.DOCS_DIR, "changelog")):
        try:
            for name in os.listdir(os.path.join(config.DOCS_DIR, "changelog")):
                if os.path.isdir(os.path.join(config.DOCS_DIR, "changelog", name)) and not name.startswith((".", "_")) \
                        and name not in folders:
                    folders.append(name)
        except OSError:
            pass
    by_repo = []
    for f in folders:
        n = int(store.val("select count(*) from hub_entries where kind = 'changelog' and repo = ? and day = ?", (f, day), 0))
        by_repo.append({"repo": f, "label": gitlog.label(f) or f, "count": n})
    if dk and dk not in folders:
        roots = [os.path.join(config.DOCS_DIR, "design") + "/", os.path.join(config.DOCS_DIR, "knowledge-base") + "/"]
        n = int(store.val("select count(distinct path) from hub_entries where kind = 'docs_change' and day = ? and "
                          "(path like ? or path like ?)", (day, roots[0] + "%", roots[1] + "%"), 0))
        by_repo.append({"repo": dk, "label": gitlog.docs_label(), "count": n})
    by_repo.sort(key=lambda r: (-r["count"], r["repo"]))
    prev, nxt = day_neighbours(day)
    sweep_ts = store.kv_get("sec.last_sweep_ts")
    upd = [float(sweep_ts)] if sweep_ts else []
    upd += [float(d["ts"]) for d in ds.values()]
    if b and b["ts"]:
        upd.append(float(b["ts"]))
    return {"day": day, "day_label": util.day_label(day), "is_today": day == today, "prev_day": prev,
            "next_day": None if day == today else nxt, "updated_ts": max(upd) if upd else None, "brief": brief,
            "lede": lede, "lede_source": lsrc,
            "stats": {"sessions": len(sess), "shipped": len(shipped), "open_loops": counts["open"], "decisions": len(decided)},
            "needs_you": ny, "needs_you_total": ny_total, "shipped": shipped, "in_progress": in_progress,
            "decided": decided, "by_repo": by_repo}


def timeline(repo: str | None, before: str | None, days: int) -> dict:
    today = util.today()
    if before is not None and not util.is_day(before):
        raise BadRequest("before must look like 2026-10-04.")
    if not 1 <= days <= 31:
        raise BadRequest("days must be between 1 and 31.")
    before = before or util.add_days(today, 1)
    sql = f"""select distinct d.day from cc_session_days d join cc_sessions s on s.id = d.session
              where {VISIBLE_S} and d.day < ?"""
    args: list = [before]
    if repo:
        sql += " and exists (select 1 from json_each(s.repos) where value = ?)"
        args.append(repo)
    day_list = [r[0] for r in store.q(sql + " order by d.day desc limit ?", (*args, days + 1))]
    more = len(day_list) > days
    day_list = day_list[:days]
    ds = latest_digests()
    now = time.time()
    linked = _linked_entries()
    out = []
    for day in day_list:
        sess = _sessions_of_day(day, repo)
        rows = []
        for s in sess:
            d = ds.get(s["id"])
            sr = session_row(s, d, now, day=day)
            srcs = [changelog_source(e) for e in linked.get(s["id"], [])]
            sr.update({"digest": digest_dict(d, s["read_offset"]) if d else None, "sources": srcs})
            rows.append(sr)
        b = get_brief(day)
        if b and b["status"] == "ready" and b["headline"]:
            head, hsrc = b["headline"], "brief"
        else:
            reps = []
            for s in sess:
                for rp in _j(s["repos"], []):
                    if rp not in reps:
                        reps.append(rp)
            head = f"{len(sess)} session{'s' if len(sess) != 1 else ''}" + (f" · {', '.join(reps[:4])}" if reps else "")
            hsrc = "computed"
        out.append({"day": day, "day_label": util.day_label(day), "headline": head, "headline_source": hsrc,
                    "sessions": rows})
    return {"repo": repo or "all", "repos": repo_refs(), "days": out,
            "next_before": day_list[-1] if more and day_list else None}


# ---------------------------------------------------------------- state


def repo_refs() -> list[dict]:
    seen: dict[str, str] = {}
    for r in gitlog.repos():
        seen.setdefault(r["key"], r["label"])
    dk = gitlog.docs_key()
    if dk:
        seen[dk] = gitlog.docs_label()
    for r in store.q(f"select repos from cc_sessions where {VISIBLE}"):
        for k in _j(r[0], []):
            if isinstance(k, str):
                seen.setdefault(k, gitlog.label(k) or k)
    return [{"key": k, "label": v} for k, v in sorted(seen.items())]


def sources() -> dict:
    d = config.DOCS_DIR
    found = bool(d) and os.path.isdir(d) and os.access(d, os.R_OK)
    cl = found and os.path.isdir(os.path.join(d, "changelog"))
    de = found and os.path.isdir(os.path.join(d, "design"))
    return {"roots": [util.tilde(r) for r in config.SESSIONS_ROOTS],
            "config_dirs": [util.tilde(c) for c in config.CLAUDE_CONFIG_DIRS],
            "docs": {"set": bool(d), "found": found, "label": util.tilde(d) if d else None,
                     "kind": "hub" if (cl or de) else ("docs" if found else "none"), "changelog": cl, "design": de},
            "memory": bool(config.READ_MEMORY), "git": bool(config.READ_GIT), "repos": len(gitlog.repos())}


def counts() -> dict:
    now = time.time()
    lc = loop_counts()
    hy = hygiene()
    return {"sessions": int(store.val(f"select count(*) from cc_sessions where {VISIBLE}", (), 0)),
            "running_sessions": int(store.val(f"select count(*) from cc_sessions where {VISIBLE} and coalesce(ended, 0) = 0 "
                                              "and mtime > ?", (now - RUNNING_S,), 0)),
            "loops_open": lc["open"], "loops_you": lc["you"], "hygiene_warn": hy["counts"]["warn"]}


def digests_today() -> int:
    return int(store.val("select count(*) from model_runs where kind = 'digest' and day = ?", (util.today(),), 0))


def briefs_today() -> int:
    return int(store.val("select count(*) from briefs where day = ? and trigger = 'schedule'", (util.today(),), 0))


def secretary_status(running: str | None = None) -> dict:
    paused = store.kv_get("sec.paused") == "1"
    token = gate.available()
    queued = int(store.val(f"select count(*) from cc_sessions where {VISIBLE} and digest_state in "
                           "('queued', 'paused', 'capped', 'no_token')", (), 0))
    u = gate.usage_snapshot()
    if not config.SECRETARY:
        status, label = "off", "Off"
    elif paused:
        status, label = "paused_user", "Paused"
    elif not token:
        status, label = "no_token", "No Claude login"
    elif digests_today() >= config.MAX_DIGESTS:
        status, label = "capped", "Daily cap reached"
    elif u.get("over_pause"):
        status, label = "paused_usage", (gate.usage_label() or "paused for plan usage").capitalize()
    else:
        status, label = "on", "On"
    last_digest = store.val("select max(ts) from digests", (), None)
    sweep = store.kv_get("sec.last_sweep_ts")
    return {"status": status, "label": label, "paused_by_user": paused, "token": token, "queued": queued,
            "running": running if running in ("digest", "brief", "ask", "sweep") else None,
            "last_sweep_ts": float(sweep) if sweep else None, "last_digest_ts": last_digest,
            "last_error": store.kv_get("sec.last_error")}


def state(running: str | None = None) -> dict:
    return {"today": util.today(), "tz": config.TZ or "UTC", "first_sweep_done": store.kv_get("sec.first_sweep_done") == "1",
            "secretary": secretary_status(running),
            "caps": {"digests_today": digests_today(), "digests_max": config.MAX_DIGESTS, "briefs_today": briefs_today(),
                     "briefs_max": 1, "pause_at": config.PAUSE_AT},
            "models": {"digest": config.DIGEST_MODEL, "digest_label": util.model_label(config.DIGEST_MODEL) or config.DIGEST_MODEL,
                       "brief": config.BRIEF_MODEL, "brief_label": util.model_label(config.BRIEF_MODEL) or config.BRIEF_MODEL,
                       "ask": config.ASK_MODEL, "ask_label": util.model_label(config.ASK_MODEL) or config.ASK_MODEL},
            "schedule": {"brief_at": config.BRIEF_AT, "discord_at": config.MORNING_POST_AT},
            "usage": gate.usage_snapshot(), "counts": counts(), "repos": repo_refs(), "sources": sources()}


# ---------------------------------------------------------------- asks and notes


def ask_dict(a) -> dict:
    origin = a["origin"] or "ui"
    kind = "mcp" if origin.startswith("mcp") else "ui"
    label = "you"
    if kind == "mcp":
        label = "a session"
        try:
            from .. import sessions as mcp_sessions

            sid = origin.split(":", 1)[1] if ":" in origin else None
            label = mcp_sessions.label(sid) if sid else label
        except Exception:
            pass
    status = a["status"] if a["status"] in ("running", "done", "failed") else "done"
    return {"id": int(a["id"]), "ts": float(a["ts"] or 0), "origin": kind, "origin_label": label,
            "question": a["question"] or "", "status": status, "answer_md": a["answer"],
            "citations": [c for c in _j(a["citations"], []) if isinstance(c, dict) and c.get("kind")],
            "ms": a["ms"], "model": a["model"], "error": a["error"], "warning": a["warning"]}


def suggestions() -> list[str]:
    out = []
    built = [c for c in board()["lanes"][1]["cards"]]
    if built:
        best = max(built, key=lambda c: c["open_loops"])
        out.append(f"What is left before {best['title']} can ship?")
    out.append("Which features are built but not shipped?" if built or config.DOCS_DIR else
               "What did I work on yesterday?")
    dec = next((r for r in decision_rows() if r["kind"] == "design"), None)
    if dec:
        words = dec["text"].split()[:6]
        out.append(f"Why did we decide “{' '.join(words).rstrip('.,;:')}…”?")  # quoted: the words are a title
    else:
        out.append("What is waiting on me?")
    return out[:3]


def ask_warning() -> str | None:
    """Shown with Ask while plan usage pauses the digests: the same reason the status card and meter give."""
    u = gate.usage_snapshot()
    if not u.get("over_pause"):
        return None
    windows = [w for w in (u.get("five_hour"), u.get("seven_day")) if w]
    if any(w["status"] == "rejected" or w["utilization"] >= 1.0 for w in windows):
        return f"{u['reason'] or 'The plan is at its usage limit.'} Ask fails too until the window resets."
    return f"{u['reason'] or 'Plan usage is high.'} Ask still works; digests and the brief are paused."


def asks(cursor: int | None, limit: int) -> dict:
    if cursor:
        rows = store.q("select * from asks where id < ? order by id desc limit ?", (cursor, limit + 1))
    else:
        rows = store.q("select * from asks order by id desc limit ?", (limit + 1,))
    more = len(rows) > limit
    rows = rows[:limit]
    enabled = bool(config.SECRETARY) and gate.available()
    reason = None if enabled else ("off" if not config.SECRETARY else "no_token")
    return {"asks": [ask_dict(a) for a in rows], "next_cursor": str(rows[-1]["id"]) if more and rows else None,
            "suggestions": suggestions(), "enabled": enabled, "disabled_reason": reason, "warning": ask_warning()}


def get_ask(ask_id: int) -> dict:
    a = store.one("select * from asks where id = ?", (ask_id,))
    if a is None:
        raise NotFound("No such question.")
    return ask_dict(a)


def note_dict(n) -> dict:
    origin = n["origin"] or "ui"
    return {"id": int(n["id"]), "ts": float(n["ts"] or 0), "origin": "mcp" if origin.startswith("mcp") else "ui",
            "kind": n["kind"] if n["kind"] in ("followup", "decision", "note") else "note", "text": n["text"] or "",
            "repo": n["repo"], "where": n["area"], "session": n["session"], "consumed": bool(n["consumed"])}


def add_note(kind: str, text: str, repo: str | None, where: str | None, session: str | None, origin: str = "ui") -> dict:
    from .redact import scrub

    text = scrub(text).strip()
    if not text:
        raise BadRequest("Write something first.")
    now = time.time()
    nid = store.run("insert into notes(ts, origin, kind, text, consumed, repo, area, session) values(?, ?, ?, ?, 0, ?, ?, ?)",
                    (now, origin, kind, text, repo, where, session))
    n = store.one("select * from notes where id = ?", (nid,))
    loop = None
    dec = None
    if kind == "followup":
        nk = f"note|{nid}"
        lid = util.opaque_id("lp", nk)
        src = util.source("note", "note", session=session) if session else util.source("note", "note")
        store.run("insert or ignore into loops(id, natural_key, grp, kind, text, repo, area, origin_day, first_seen, "
                  "last_seen, gone, src) values(?, ?, 'you', 'note', ?, ?, ?, ?, ?, ?, 0, ?)",
                  (lid, nk, text, repo, where or repo, util.day_of(now), now, now, json.dumps(src)))
        r = store.one("select * from loops where id = ?", (lid,))
        loop = loop_dict(r, None, util.today())
    elif kind == "decision":
        dec = note_decision(n)
    return {"note": note_dict(n), "loop": loop, "decision": dec}


def notes(cursor: int | None, limit: int) -> dict:
    if cursor:
        rows = store.q("select * from notes where id < ? order by id desc limit ?", (cursor, limit + 1))
    else:
        rows = store.q("select * from notes order by id desc limit ?", (limit + 1,))
    more = len(rows) > limit
    rows = rows[:limit]
    return {"notes": [note_dict(n) for n in rows], "next_cursor": str(rows[-1]["id"]) if more and rows else None}


def set_paused(paused: bool) -> None:
    store.kv_put("sec.paused", "1" if paused else "0")
    store.kv_put("sec.paused_ts", time.time())
