"""The hub sweep (design §3.3 step 5): docs, memory and git into hub_entries, loops and hygiene. No model.

Runs every 5 minutes in a worker thread, and on POST /api/sec/sweep. Each source is optional: without
DOCS_DIR there are no changelog, design or decision rows; READ_MEMORY=off skips memory notes; READ_GIT=off
skips commits and branches. Rows of a source that is switched off are removed, so nothing stale lingers.

loops (research contract §2.3.14) is rebuilt from the sources on every sweep: a loop seen again keeps its
first_seen and the user's done/snooze state (loop_state, keyed by the same lp_ id); a loop no longer
produced is marked gone. Hygiene results go to kv 'sec.hygiene'.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from .. import config
from . import gitlog, hubdocs, store, util

log = logging.getLogger(__name__)

FOLLOWUP_DAYS = 14
DIGEST_LOOP_DAYS = 14
MEMORY_STALE_DAYS = 7
DESIGN_STATUS_PHRASES = [
    ("merged, not pushed", r"merged,?\s+not\s+pushed"),
    ("not deployed", r"\bnot\s+(?:yet\s+)?deployed\b"),
    ("not pushed", r"\bnot\s+(?:yet\s+)?pushed\b"),
    ("check on a device", r"\bcheck\s+on\s+a\s+(?:real\s+)?(?:device|phone)\b"),
    ("verify on a device", r"\bverify\s+on\s+(?:a\s+)?(?:real\s+)?(?:phone|device)\b"),
    ("cutover pending", r"\bcutover\s+pending\b"),
    ("needs manual QA", r"\bneeds\s+manual\s+qa\b"),
]
CHECK_ORDER = {"warn": 0, "info": 1, "ok": 2}


def ship_state(text: str) -> str:
    """Research contract §2.3.12: the state of a changelog entry from its title and Branch/Commit/Deploy lines."""
    t = (text or "").lower().replace("not deployed", " ").replace("not pushed", " ")
    if "deployed" in t:
        return "deployed"
    if "pushed" in t:
        return "pushed"
    if "merged" in t:
        return "merged"
    if "uncommitted" in t or "not committed" in t:
        return "built"
    if "committed" in t or re.search(r"\b[0-9a-f]{7,40}\b", t):
        return "committed"
    return "shipped"


def entry_id(e: dict, seen: dict) -> str:
    base = f"cl:{e['repo']}/{e['date']}#" + (e["time"] or re.sub(r"[^a-z0-9]+", "-", e["title"].lower())[:40].strip("-"))
    n = seen.get(base, 0) + 1
    seen[base] = n
    return base if n == 1 else f"{base}~{n}"


def _docs_hub() -> Path | None:
    d = config.DOCS_DIR
    return Path(d) if d and os.path.isdir(d) else None


def in_scope_projects() -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in store.q("select config_dir, project from cc_sessions "
                                          "where skipped_reason is null and project is not null")}


def run_sweep() -> dict:
    """One sweep. Runs in a worker thread; returns counts only."""
    t0 = time.monotonic()
    now = time.time()
    today = util.today()
    hub = _docs_hub()
    gitlog.forget_cache()
    repos = gitlog.repos()
    docs_key = gitlog.docs_key()
    folders_cl = hubdocs.changelog_folders(hub) if hub else []
    known = sorted({r["key"] for r in repos} | set(folders_cl) | ({docs_key} if docs_key else set()),
                   key=lambda k: -len(k))
    E = hubdocs.entries(hub) if hub else []
    D = hubdocs.designs(hub, known) if hub and (hub / "design").is_dir() else []
    mem_dirs = hubdocs.memory_dirs(in_scope_projects())
    M = hubdocs.memory_notes(mem_dirs)
    commits: dict[str, list[dict]] = {}
    branches: dict[str, list[dict]] = {}
    remotes: dict[str, bool] = {}
    docs_changes: list[dict] = []
    if config.READ_GIT:
        for r in repos:
            commits[r["key"]] = gitlog.commits_since(r, 14)
            remotes[r["key"]] = gitlog.has_remote(r)
            branches[r["key"]] = gitlog.unpushed_branches(r) if remotes[r["key"]] else []
        docs_changes = gitlog.docs_changes(30)

    rows: list[tuple] = []
    seen_ids: dict[str, int] = {}
    entry_rows: list[dict] = []
    for e in E:
        eid = entry_id(e, seen_ids)
        day = e["effective_date"] or e["date"]
        st = ship_state(" ".join([e["title"], e["ship_text"] or ""]))
        extra = {"types": e["types"], "scope": e["scope"], "design_docs": e["design_docs"], "ship_state": st,
                 "commits": e["commits"], "branch": e["branch"], "file_day": e["date"], "has_type": e["has_type"],
                 "has_followups": e["has_followups"]}
        rows.append((eid, "changelog", e["repo"], day, e["time"], e["title"], e["scope"], e["path"], e["line"],
                     json.dumps(extra)))
        entry_rows.append(dict(e, id=eid, day=day, ship_state=st))
        for f in e["follow_ups"]:
            fid = f"fu:{eid}:{util.sha10(f['text'])}"
            rows.append((fid, "followup", e["repo"], day, e["time"], f["text"], None, e["path"], f["line"],
                         json.dumps({"entry": eid, "section": f["section"], "withdrawn": f["withdrawn"],
                                     "none": f["none"], "open_loops": f["open_loops"],
                                     "design_docs": e["design_docs"]})))
    last_activity: dict[str, str] = {}
    for e in entry_rows:
        for f in e["design_docs"]:
            last_activity[f] = max(last_activity.get(f, ""), e["day"])
    if hub:
        design_root = str(hub / "design") + "/"
        for ch in docs_changes:
            for p in ch["paths"]:
                if p.startswith(design_root):
                    f = p[len(design_root):].split("/", 1)[0]
                    last_activity[f] = max(last_activity.get(f, ""), ch["day"])
        for r in store.q("select path, max(last_ts) t from cc_files where action != 'read' and path like ? group by path",
                         (design_root + "%",)):
            f = r[0][len(design_root):].split("/", 1)[0]
            d = util.day_of(r[1])
            if d:
                last_activity[f] = max(last_activity.get(f, ""), d)
    design_rows: list[dict] = []
    for d in D:
        idx = d["index"]
        doc_status = d["status"]["text"] if d["status"] else None
        status = (idx["status"] if idx and idx["status"] else None) or doc_status or ""
        lane = hubdocs.lane_of(status)
        doc_lane = hubdocs.lane_of(doc_status) if doc_status else None
        extra = {"status": status, "status_short": lane["short"], "status_day": lane["day"], "lane": lane["lane"],
                 "projects": idx["projects"] if idx else [], "prototype_url": d["prototype_url"],
                 "doc_status": doc_status, "doc_status_day": doc_lane["day"] if doc_lane else None,
                 "doc_lane": doc_lane["lane"] if doc_lane else None, "doc_status_short": doc_lane["short"] if doc_lane else None,
                 "index_status": idx["status"] if idx else None, "index_lane": hubdocs.lane_of(idx["status"])["lane"] if idx else None,
                 "index_line": idx["line"] if idx else None, "in_index": idx is not None,
                 "last_activity_day": last_activity.get(d["folder"]) or None}
        rows.append((f"ds:{d['folder']}", "design", (idx["projects"][0] if idx and idx["projects"] else None),
                     lane["day"], None, d["title"], status, d["main_doc"],
                     d["status"]["line"] if d["status"] else 1, json.dumps(extra)))
        design_rows.append(dict(d, extra=extra))
        for dec in d["decisions"]:
            key = dec["ref"] or util.sha10(dec["text"])
            did = f"dc:{d['folder']}/{os.path.basename(dec['path'])}#{key}"
            rows.append((did, "decision", (idx["projects"][0] if idx and idx["projects"] else None), dec["day"], None,
                         dec["text"], dec["outcome"], dec["path"], dec["line"],
                         json.dumps({"ref": dec["ref"], "outcome": dec["outcome"], "folder": d["folder"],
                                     "file": os.path.basename(dec["path"]), "is_question": dec["is_question"],
                                     "withdrawn": dec["withdrawn"], "reversed": dec["reversed"],
                                     "projects": idx["projects"] if idx else []})))
    index_rows = hubdocs.design_index(hub, known) if hub and (hub / "design" / "README.md").is_file() else []
    design_set = {d["folder"] for d in D}
    for m in M:
        mid = f"mm:{util.sha10(m['dir'])}:{m['file']}"
        rows.append((mid, "memory", None, m["day"], None, m["name"], m["description"], m["path"], 1,
                     json.dumps({k: m[k] for k in ("name", "description", "modified", "mtime", "origin_session", "type",
                                                    "signals", "loop", "not_pushed", "in_index", "dir", "date_source",
                                                    "file")})))
    for key, cs in commits.items():
        for c in cs:
            rows.append((f"gc:{key}:{c['sha']}", "commit", key, c["day"], c["time"], util.scrub_line(c["subject"]),
                         None, None, None, json.dumps({"sha": c["short"], "ts": c["ts"], "mine": c["mine"],
                                                       "merge": c["merge"]})))
    for key, bs in branches.items():
        for b in bs:
            rows.append((f"br:{key}:{b['branch']}", "branch", key, b["last_day"], None, b["branch"], None, None, None,
                         json.dumps({"name": b["branch"], "unpushed": b["count"]})))
    for k, ch in enumerate(docs_changes):
        for j, p in enumerate(ch["paths"][:200]):
            rows.append((f"dg:{int(ch['ts'])}:{k}:{j}", "docs_change", docs_key, ch["day"], util.hhmm(ch["ts"]), None,
                         None, p, None, None))
    with store.tx() as c:
        c.execute("delete from hub_entries")
        c.executemany("insert or replace into hub_entries(id, kind, repo, day, time, title, body, path, line, extra) "
                      "values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    loops = build_loops(entry_rows, design_rows, M, branches, repos, today)
    counts = write_loops(loops, now)
    checks = hygiene(hub, entry_rows, design_rows, index_rows, design_set, M, commits, branches, repos, today, now)
    store.kv_put("sec.hygiene", json.dumps({"ts": now, "checks": checks}))
    store.kv_put("sec.last_sweep_ts", now)
    store.kv_put("sec.first_sweep_done", "1")
    return {"entries": len(E), "followups": sum(len(e["follow_ups"]) for e in E), "designs": len(D),
            "decisions": sum(len(d["decisions"]) for d in D), "memory": len(M), "repos": len(repos),
            "commits": sum(len(v) for v in commits.values()), "branches": sum(len(v) for v in branches.values()),
            "loops": counts, "hygiene": {lv: sum(1 for ch in checks if ch["level"] == lv) for lv in ("warn", "info", "ok")},
            "ms": util.monotonic_ms(t0)}


# ---------------------------------------------------------------- loops


def _where_of_entry(e: dict) -> str:
    return e["design_docs"][0] if e["design_docs"] else e["repo"]


def build_loops(entries: list[dict], designs: list[dict], notes: list[dict], branches: dict, repos: list[dict],
                today: str) -> list[dict]:
    out: list[dict] = []
    since = util.add_days(today, -FOLLOWUP_DAYS)
    for e in entries:
        if e["day"] < since or e["day"] > today:
            continue
        for f in e["follow_ups"]:
            if f["withdrawn"] or f["none"] or not f["text"].strip():
                continue
            label = f"{e['repo']}/{e['date']}" + (f" · {e['time']}" if e["time"] else "")
            out.append({"nk": f"followup|{e['repo']}|{e['day']}|{e['time'] or ''}|{util.norm_text(f['text'])}",
                        "grp": "followup", "kind": "followup", "text": f["text"], "repo": e["repo"],
                        "area": _where_of_entry(e), "origin_day": e["day"],
                        "src": util.source("followup", label, path=e["path"], line=f["line"])})
    for d in designs:
        x = d["extra"]
        st = x["status"] or ""
        for phrase, rx in DESIGN_STATUS_PHRASES:
            if re.search(rx, st, re.I):
                line = x["index_line"] if x["index_status"] and re.search(rx, x["index_status"], re.I) else (
                    d["status"]["line"] if d["status"] else 1)
                path = str(Path(config.DOCS_DIR) / "design" / "README.md") if x["index_status"] and re.search(
                    rx, x["index_status"], re.I) else d["main_doc"]
                out.append({"nk": f"design_status|{d['folder']}|{phrase}", "grp": "you", "kind": "design_status",
                            "text": f"{d['title']}: {x['status_short']}", "repo": (x["projects"] or [None])[0],
                            "area": d["folder"], "origin_day": x["status_day"],
                            "src": util.source("design", f"design/{d['folder']} · Status", path=path, line=line)})
                break
    design_folders = {d["folder"] for d in designs}
    for m in notes:
        if not m["loop"]:
            continue
        text = hubdocs.first_sentence(m["description"]) or m["name"]
        where = next((f for f in design_folders if f in (m["description"] or "") or f in m["name"]), None)
        out.append({"nk": f"memory|{m['dir']}|{m['name']}", "grp": "you", "kind": "memory", "text": text,
                    "repo": None, "area": where, "origin_day": m["day"],
                    "src": util.source("memory", f"memory/{m['file'][:-3] if m['file'].endswith('.md') else m['file']}",
                                       path=m["path"], line=1)})
    by_key = {r["key"]: r for r in repos}
    for key, bs in branches.items():
        for b in bs:
            n = b["count"]
            out.append({"nk": f"branch|{key}|{b['branch']}", "grp": "you", "kind": "branch",
                        "text": f"Push {b['branch']} in {key} ({n} commit{'s' if n != 1 else ''} not on any remote)",
                        "repo": key, "area": key, "origin_day": b["last_day"],
                        "src": util.source("branch", f"{key} · {b['branch']}", path=by_key.get(key, {}).get("path"))})
    since_d = util.add_days(today, -DIGEST_LOOP_DAYS)
    for r in store.q("""select d.session, d.day, d.open_loops, d.repos, coalesce(s.custom_title, d.title, s.ai_title) t
                        from digests d join cc_sessions s on s.id = d.session
                        where d.id in (select max(id) from digests group by session) and d.day >= ?""", (since_d,)):
        try:
            items = json.loads(r["open_loops"] or "[]")
            reps = json.loads(r["repos"] or "[]")
        except ValueError:
            continue
        for it in items:
            if not isinstance(it, dict) or not str(it.get("text") or "").strip():
                continue
            owner = it.get("owner") if it.get("owner") in ("you", "claude") else "you"
            title = (r["t"] or "session")[:40]
            out.append({"nk": f"digest|{r['session']}|{util.norm_text(it['text'])}",
                        "grp": "you" if owner == "you" else "followup", "kind": "digest", "text": str(it["text"])[:500],
                        "repo": reps[0] if reps else None, "area": reps[0] if reps else None, "origin_day": r["day"],
                        "src": util.source("session", f"session · {title}", session=r["session"])})
    for n in store.q("select id, ts, text, repo, area, session from notes where kind = 'followup'"):
        out.append({"nk": f"note|{n['id']}", "grp": "you", "kind": "note", "text": n["text"], "repo": n["repo"],
                    "area": n["area"] or n["repo"], "origin_day": util.day_of(n["ts"]),
                    "src": util.source("note", "note", session=n["session"]) if n["session"]
                    else util.source("note", "note")})
    return out


def write_loops(loops: list[dict], now: float) -> dict:
    by_nk: dict[str, dict] = {}
    for lp in loops:
        by_nk.setdefault(lp["nk"], lp)
    with store.tx() as c:
        existing = {r["natural_key"]: r for r in c.execute("select id, natural_key, kind from loops")}
        for nk, lp in by_nk.items():
            lid = util.opaque_id("lp", nk)
            args = (lp["grp"], lp["kind"], lp["text"], lp["repo"], lp["area"], lp["origin_day"], json.dumps(lp["src"]))
            if nk in existing:
                c.execute("update loops set grp = ?, kind = ?, text = ?, repo = ?, area = ?, origin_day = ?, src = ?, "
                          "last_seen = ?, gone = 0 where natural_key = ?", (*args, now, nk))
            else:
                c.execute("insert into loops(id, natural_key, grp, kind, text, repo, area, origin_day, src, first_seen, "
                          "last_seen, gone) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)", (lid, nk, *args, now, now))
        gone = [nk for nk, r in existing.items() if nk not in by_nk and r["kind"] != "note"]
        c.executemany("update loops set gone = 1 where natural_key = ?", [(nk,) for nk in gone])
    return {"live": len(by_nk), "gone": len(gone)}


# ---------------------------------------------------------------- hygiene (no model)


def _check(rule: str, level: str, key: str, text: str, src: dict, repo: str | None = None, where: str | None = None,
           hand_off: str | None = None) -> dict:
    return {"id": util.opaque_id("hy", f"{rule}|{key}"), "rule": rule, "level": level, "text": text, "repo": repo,
            "where": where, "src": src, "hand_off": hand_off if level == "warn" else None}


def hygiene(hub: Path | None, entries: list[dict], designs: list[dict], index_rows: list[dict], design_set: set[str],
            notes: list[dict], commits: dict, branches: dict, repos: list[dict], today: str, now: float) -> list[dict]:
    out: list[dict] = []
    docs_key = gitlog.docs_key()
    yesterday = util.add_days(today, -1)
    if hub and config.READ_GIT and (hub / "changelog").is_dir():
        cmap = hubdocs.changelog_repo_map(hub)
        eff = {(e["repo"], e["day"]) for e in entries}
        first_day = min((e["date"] for e in entries), default=None)
        fine: list[str] = []
        for r in repos:
            key = r["key"]
            if key == docs_key:
                continue
            folder = cmap.get(key) or cmap.get(os.path.basename(r["path"]))
            if not folder or not (hub / "changelog" / folder).is_dir():
                continue
            for day in (today, yesterday):
                if first_day and day < first_day:
                    continue
                own = [c for c in commits.get(key, []) if c["day"] == day and c["mine"] and not c["merge"]]
                if not own:
                    continue
                f = hub / "changelog" / folder / f"{day}.md"
                if f.exists() or (folder, day) in eff:
                    if key not in fine:
                        fine.append(key)
                    continue
                n = len(own)
                when = "today" if day == today else "yesterday"
                subjects = "; ".join(c["subject"] for c in own[:3])
                out.append(_check("commits_without_changelog", "warn", f"{key}|{day}",
                                  f"{key} has {n} commit{'s' if n != 1 else ''} {when} but no changelog entry for {day}.",
                                  util.source("changelog", f"{folder}/{day}", path=str(hub / "changelog" / folder)),
                                  repo=key, where=key,
                                  hand_off=util.scrub_line(f"Write the changelog entry for {key} on {day} in "
                                                           f"changelog/{folder}/{day}.md: {n} commit"
                                                           f"{'s' if n != 1 else ''} ({subjects}).", 600)))
        if fine:
            out.append(_check("commits_without_changelog", "ok", "fine",
                              "Every repo with commits today or yesterday has its changelog: " + ", ".join(fine) + ".",
                              util.source("changelog", "changelog", path=str(hub / "changelog"))))
        elif not any(ch["rule"] == "commits_without_changelog" for ch in out):
            out.append(_check("commits_without_changelog", "ok", "none", "No commits today or yesterday.",
                              util.source("changelog", "changelog", path=str(hub / "changelog"))))
    if hub and designs:
        newest: dict[str, dict] = {}
        for e in entries:
            for f in e["design_docs"]:
                if f not in newest or e["day"] > newest[f]["day"]:
                    newest[f] = e
        for d in designs:
            x = d["extra"]
            ds = x["doc_status_day"]
            e = newest.get(d["folder"])
            if ds and e and util.age_days(ds, e["day"]) and util.age_days(ds, e["day"]) >= 1:
                out.append(_check("design_status_stale", "warn", d["folder"],
                                  f"{d['title']}: the status line is dated {ds}, but a changelog entry on {e['day']} "
                                  f"names this design.",
                                  util.source("design", f"design/{d['folder']} · Status", path=d["main_doc"],
                                              line=d["status"]["line"] if d["status"] else None),
                                  repo=e["repo"], where=d["folder"],
                                  hand_off=f"Update the **Status:** line of design/{d['folder']}/"
                                           f"{os.path.basename(d['main_doc'] or '00-overview.md')} to match the "
                                           f"{e['repo']} changelog entry of {e['day']} ({e['title'][:120]})."))
        for d in designs:
            x = d["extra"]
            if not x["in_index"]:
                out.append(_check("index_missing", "warn", d["folder"], f"design/{d['folder']} has no row in the design index.",
                                  util.source("design", "design index", path=str(hub / "design" / "README.md")),
                                  where=d["folder"],
                                  hand_off=f"Add a row for design/{d['folder']} to design/README.md "
                                           f"(feature, status, projects touched)."))
            elif x["doc_lane"] and x["index_lane"] and x["doc_lane"] != x["index_lane"]:
                out.append(_check("index_status_mismatch", "warn", d["folder"],
                                  f"{d['title']}: the design index says “{hubdocs.lane_of(x['index_status'])['short']}”, "
                                  f"the doc says “{x['doc_status_short']}”.",
                                  util.source("design", f"design/{d['folder']} · Status", path=d["main_doc"],
                                              line=d["status"]["line"] if d["status"] else None),
                                  where=d["folder"],
                                  hand_off=f"Make the design index row for {d['folder']} and the **Status:** line of "
                                           f"its doc agree. Index: {x['index_status'][:160]}. Doc: {(x['doc_status'] or '')[:160]}."))
        for r in index_rows:
            if r["folder"] not in design_set:
                out.append(_check("index_missing", "info", f"row|{r['folder']}",
                                  f"The design index lists {r['folder']}, but design/{r['folder']} does not exist.",
                                  util.source("design", "design index", path=str(hub / "design" / "README.md"),
                                              line=r["line"])))
    if notes:
        any_unpushed = any(branches.values())
        for m in notes:
            if m["not_pushed"]:
                age = util.age_days(m["day"], today)
                if age is not None and age > MEMORY_STALE_DAYS:
                    extra = " git shows nothing unpushed." if config.READ_GIT and not any_unpushed else ""
                    stem = m["file"][:-3]
                    out.append(_check("memory_not_pushed", "warn", m["path"],
                                      f"The {stem} memory note is {age} days old and still says something is not "
                                      f"pushed.{extra}",
                                      util.source("memory", f"memory/{stem}", path=m["path"], line=1),
                                      hand_off=f"Update memory/{m['file']}: it says not pushed and is {age} days old. "
                                               f"Check git, then rewrite or delete the note."))
            if not m["in_index"]:
                out.append(_check("memory_index_missing", "warn", m["path"], f"memory/{m['file']} is not listed in MEMORY.md.",
                                  util.source("memory", f"memory/{m['file'][:-3]}", path=m["path"], line=1),
                                  hand_off=f"Add memory/{m['file']} to MEMORY.md with a one-line description."))
    if config.READ_GIT and hub and (hub / "changelog").is_dir():
        cmap = hubdocs.changelog_repo_map(hub)
        week = util.add_days(today, -7)
        for r in repos:
            key = r["key"]
            if key == docs_key:
                continue
            folder = cmap.get(key) or cmap.get(os.path.basename(r["path"]))
            recent = [c for c in commits.get(key, []) if c["day"] >= week and not c["merge"]]
            if recent and not (folder and (hub / "changelog" / folder).is_dir()):
                out.append(_check("changelog_folder_missing", "info", key,
                                  f"{key} has {len(recent)} commit{'s' if len(recent) != 1 else ''} in the last 7 days "
                                  f"but no changelog folder.",
                                  util.source("commit", key, path=r["path"]), repo=key, where=key))
    out.sort(key=lambda ch: (CHECK_ORDER[ch["level"]], ch["rule"], ch["text"]))
    return out
