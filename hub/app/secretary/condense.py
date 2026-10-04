"""What the model layer gets: a condensed session delta for a digest, and the day's material for the brief.

Both are built from SQLite only (already redacted when stored) and redacted once more on the way out
(design §3.5: before the model call and again before writing). The shapes are documented in README.md;
llm.digest_session() and llm.daily_brief() accept exactly these dicts.

The pre-filter (design §3.3 step 3) skips a delta with no file edits, no commits or pushes, no artifact,
PR or notification posts and under 1,500 characters of answers: not worth a model call.
"""

from __future__ import annotations

import json

from .. import config
from . import gitlog, store, util, views
from .redact import scrub_obj

MIN_ASSISTANT_CHARS = 1500
MAX_TURNS = 30
PROMPT_CHARS = 1500
ANSWER_CHARS = 2500
MAX_FILES = 40
MAX_COMMANDS = 40
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
LINK_KINDS = ("commit", "push", "artifact", "pr", "discord", "notify")


def _first_item(sid: str, from_offset: int) -> int:
    v = store.val("select min(idx) from cc_items where session = ? and offset >= ?", (sid, from_offset), None)
    return int(v) if v is not None else int(store.val("select count(*) from cc_items where session = ?", (sid,), 0))


def delta_stats(sid: str) -> dict:
    """Numbers for the pre-filter, for what was read after the last digest."""
    row = store.one("select digested_offset, read_offset from cc_sessions where id = ?", (sid,))
    if row is None:
        return {"items": 0, "assistant_chars": 0, "edits": 0, "links": 0, "prompts": 0}
    first = _first_item(sid, int(row["digested_offset"] or 0))
    return {
        "from_item": first,
        "items": int(store.val("select count(*) from cc_items where session = ? and idx >= ?", (sid, first), 0)),
        "assistant_chars": int(store.val("select coalesce(sum(chars), 0) from cc_items where session = ? and idx >= ? "
                                         "and kind = 'assistant'", (sid, first), 0)),
        "prompts": int(store.val("select count(*) from cc_items where session = ? and idx >= ? and kind = 'prompt'",
                                 (sid, first), 0)),
        "edits": int(store.val(f"""select count(*) from cc_steps where session = ? and item >= ? and status = 'ok'
                                   and tool in ({','.join('?' * len(EDIT_TOOLS))})""", (sid, first, *EDIT_TOOLS), 0))
        + int(store.val("select count(*) from cc_files where session = ? and last_item >= ? and action != 'read'",
                        (sid, first), 0)),
        "links": int(store.val(f"select count(*) from cc_links where session = ? and item >= ? and kind in "
                               f"({','.join('?' * len(LINK_KINDS))})", (sid, first, *LINK_KINDS), 0)),
    }


def trivial(stats: dict) -> bool:
    return not stats["edits"] and not stats["links"] and stats["assistant_chars"] < MIN_ASSISTANT_CHARS


def _cut(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def session_delta(sid: str) -> dict:
    """The condensed material for llm.digest_session (see README.md, 'Digest material')."""
    s = store.one("select * from cc_sessions where id = ?", (sid,))
    if s is None:
        raise LookupError(sid)
    from_offset = int(s["digested_offset"] or 0)
    to_offset = int(s["read_offset"] or 0)
    first = _first_item(sid, from_offset)
    items = store.q("select idx, turn, kind, ts, text, flags from cc_items where session = ? and idx >= ? order by idx",
                    (sid, first))
    turns: dict[int, dict] = {}
    last_answer: dict[int, str] = {}
    for it in items:
        t = turns.setdefault(it["turn"], {"turn": it["turn"], "time": util.hhmm(it["ts"]), "prompt": None,
                                          "mid_turn": [], "answer": None, "interrupted": False, "compacted": False})
        flags = json.loads(it["flags"] or "{}")
        if it["kind"] == "prompt":
            if flags.get("mid_turn"):
                t["mid_turn"].append(_cut(it["text"], PROMPT_CHARS))
            elif t["prompt"] is None:
                t["prompt"] = _cut(it["text"], PROMPT_CHARS) or ("(an image)" if flags.get("images") else "")
                t["time"] = util.hhmm(it["ts"])
        elif it["kind"] == "assistant" and not flags.get("error"):
            last_answer[it["turn"]] = it["text"]
        elif it["kind"] == "interrupt":
            t["interrupted"] = True
        elif it["kind"] == "compact":
            t["compacted"] = True
    for k, text in last_answer.items():
        turns[k]["answer"] = _cut(text, ANSWER_CHARS)
    tl = [turns[k] for k in sorted(turns)]
    dropped = max(0, len(tl) - MAX_TURNS)
    tl = tl[-MAX_TURNS:]
    files = [{"path": util.rel(f["path"]), "action": f["action"], "count": int(f["count"] or 0)}
             for f in store.q("select path, action, count from cc_files where session = ? and last_item >= ? "
                              "and action != 'read' order by count desc limit ?", (sid, first, MAX_FILES))]
    reads = int(store.val("select count(*) from cc_files where session = ? and last_item >= ? and action = 'read'",
                          (sid, first), 0))
    cmds = store.q("select command, description, status, exit_code from cc_commands where session = ? and item >= ? "
                   "order by ts", (sid, first))
    failed = [c for c in cmds if c["status"] == "error"]
    notable = [c for c in cmds if c["status"] != "error" and c["command"] and any(
        w in c["command"] for w in ("git ", "deploy", "docker", "pytest", "npm ", "pnpm ", "make ", "migrat"))]
    chosen = (failed + notable)[-MAX_COMMANDS:]
    commands = [{"command": _cut((c["command"] or "").splitlines()[0] if c["command"] else "", 200),
                 "description": c["description"], "status": c["status"],
                 "exit_code": c["exit_code"]} for c in chosen]
    links = store.q("select kind, url, title, extra from cc_links where session = ? and item >= ? order by ts",
                    (sid, first))
    commits, pushes, artifacts, posts, prs = [], [], [], [], []
    for ln in links:
        x = json.loads(ln["extra"] or "{}")
        if ln["kind"] == "commit":
            commits.append({"repo": x.get("repo"), "sha": x.get("sha"), "subject": ln["title"], "branch": x.get("branch")})
        elif ln["kind"] == "push":
            pushes.append({"repo": x.get("repo"), "branch": x.get("branch")})
        elif ln["kind"] == "artifact":
            artifacts.append({"title": ln["title"], "url": ln["url"]})
        elif ln["kind"] in ("discord", "notify"):
            posts.append({"text": ln["title"], "status": x.get("status")})
        elif ln["kind"] == "pr":
            prs.append({"title": ln["title"], "url": ln["url"]})
    prev = store.one("select * from digests where session = ? order by id desc limit 1", (sid,))
    previous = None
    if prev is not None:
        d = views.digest_dict(prev, None)
        previous = {k: d[k] for k in ("title", "repos", "outcome", "summary", "shipped", "decisions", "open_loops",
                                      "blockers")}
    first_ts = items[0]["ts"] if items else s["first_ts"]
    last_ts = max((it["ts"] or 0 for it in items), default=0) or s["last_ts"]
    reps = json.loads(s["repos"] or "[]")
    material = {
        "kind": "session_digest", "version": 1,
        "session": sid, "owner": config.OWNER, "tz": config.TZ,
        "title_hint": s["custom_title"] or s["ai_title"] or (s["first_prompt"] or "")[:120] or None,
        "cwd": util.tilde(s["cwd"]) if s["cwd"] else None,
        "repos": reps, "repo_labels": {r: gitlog.label(r) for r in reps},
        "day": util.day_of(last_ts) if last_ts else util.today(),
        "window": {"from": util.hhmm(first_ts), "to": util.hhmm(last_ts)},
        "model": s["model"],
        "previous": previous,
        "delta": {
            "from_offset": from_offset, "to_offset": to_offset, "turns_dropped": dropped,
            "turns": tl, "files": files, "files_read": reads, "commands": commands,
            "commands_total": len(cmds), "commands_failed": len(failed),
            "commits": commits, "pushes": pushes, "artifacts": artifacts, "notifications": posts, "prs": prs,
        },
        "stats": delta_stats(sid),
    }
    return scrub_obj(material)


def brief_material(day: str) -> dict:
    """The day's material for llm.daily_brief (see README.md, 'Brief material')."""
    t = views.today_page(day)
    sess = views._sessions_of_day(day)
    ds = views.latest_digests([s["id"] for s in sess])
    sessions = []
    for s in sess[:30]:
        d = ds.get(s["id"])
        sr = views.session_row(s, d, day=day)
        entry = {"title": sr["title"], "time": sr["time_label"], "repos": sr["repos"], "outcome": sr["outcome"],
                 "prompts": sr["prompts"], "summary": sr["summary"] if sr["summary_source"] == "digest" else None}
        if d is not None:
            dd = views.digest_dict(d, None)
            entry.update({k: dd[k] for k in ("shipped", "decisions", "open_loops", "blockers")})
        sessions.append(entry)
    loops = views.all_loops()
    fu = sorted([lp for lp in loops if lp["state"] == "open" and lp["group"] == "followup"],
                key=lambda lp: lp["origin_day"] or "", reverse=True)
    hy = [c["text"] for c in views.hygiene()["checks"] if c["level"] == "warn"][:8]
    commits: dict[str, int] = {}
    for r in store.q("select repo, count(*) n from hub_entries where kind = 'commit' and day = ? group by repo", (day,)):
        commits[r["repo"]] = int(r["n"])
    notes = [{"kind": n["kind"], "text": n["text"]} for n in store.q(
        "select kind, text from notes where consumed = 0 order by id desc limit 20")]
    material = {
        "kind": "daily_brief", "version": 1,
        "day": day, "day_label": t["day_label"], "owner": config.OWNER, "tz": config.TZ, "is_today": t["is_today"],
        "sessions": sessions,
        "shipped": [{"time": x["time"], "text": x["text"], "repo": x["repo"], "state": x["state"]} for x in t["shipped"]],
        "in_progress": [{"text": x["text"], "where": x["where"]} for x in t["in_progress"]],
        "decisions": [{"text": x["text"], "where": x["where"], "ref": x["ref"]} for x in t["decided"]],
        "needs_you": [{"text": x["text"], "where": x["where"], "age": x["age_label"], "kind": x["kind"]}
                      for x in t["needs_you"]],
        "followups": [{"text": lp["text"], "where": lp["where"]} for lp in fu[:12]],
        "commits": commits, "notes": notes, "hygiene": hy,
        "counts": {"sessions": t["stats"]["sessions"], "shipped": t["stats"]["shipped"],
                   "open_loops": t["stats"]["open_loops"], "needs_you": t["needs_you_total"],
                   "decisions": t["stats"]["decisions"], "commits": sum(commits.values())},
    }
    return scrub_obj(material)


def morning_text(day: str) -> str:
    """The morning post when there is no brief text: titles and counts only (design §3.5)."""
    t = views.today_page(day)
    st = t["stats"]
    lines = [f"{t['day_label']}: {st['sessions']} session{'s' if st['sessions'] != 1 else ''}, "
             f"{st['shipped']} shipped, {t['needs_you_total']} waiting on you."]
    for x in t["shipped"][:5]:
        lines.append(f"• {x['text'][:120]}")
    if t["needs_you"]:
        lines.append("Waiting on you:")
        for x in t["needs_you"][:3]:
            lines.append(f"• {x['text'][:120]}")
    return "\n".join(lines)
