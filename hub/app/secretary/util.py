"""Small shared helpers, so every endpoint agrees on ids, labels, links, times and search.

Paths are host paths (identity mounts): a path in a transcript is the path the hub reads. What the UI
shows is relative to the configured root that holds it ("claude/design/x.md" for a root ~/Projects),
memory notes as "memory/<file>.md", anything else with the home folder as ~.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import sqlite3
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import config

MARK_OPEN, MARK_CLOSE = "\x02", "\x03"  # snippet markers; the client turns them into <mark></mark>


# ---------------------------------------------------------------- ids


def opaque_id(prefix: str, natural_key: str) -> str:
    """URL-safe stable id. Natural keys hold '/', '#', ':' and Starlette decodes %2F before routing."""
    return f"{prefix}_{hashlib.sha1(natural_key.encode()).hexdigest()[:16]}"


def norm_text(s: str | None) -> str:
    """For natural keys: case, whitespace and trailing full stops do not matter."""
    return re.sub(r"\s+", " ", s or "").strip().strip(".").lower()


def sha10(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:10]


# ---------------------------------------------------------------- time (hub zone, config.TZ)

_zone_cache: dict[str, ZoneInfo] = {}


def zone() -> ZoneInfo:
    name = config.TZ or "UTC"
    z = _zone_cache.get(name)
    if z is None:
        try:
            z = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            z = ZoneInfo("UTC")
        _zone_cache[name] = z
    return z


def local(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, zone())


def now_local() -> dt.datetime:
    return dt.datetime.now(zone())


def today() -> str:
    return now_local().strftime("%Y-%m-%d")


def day_of(ts: float | None) -> str | None:
    return local(ts).strftime("%Y-%m-%d") if ts else None


def hhmm(ts: float | None) -> str | None:
    return local(ts).strftime("%H:%M") if ts else None


def day_label(day: str) -> str:
    """'2026-10-04' -> 'Sunday 4 October'."""
    d = dt.date.fromisoformat(day)
    return f"{d:%A} {d.day} {d:%B}"


def short_date(day: str) -> str:
    """'2026-10-01' -> '1 Oct'."""
    d = dt.date.fromisoformat(day)
    return f"{d.day} {d:%b}"


def add_days(day: str, n: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


def age_days(day: str | None, ref: str) -> int | None:
    if not day:
        return None
    try:
        return max(0, (dt.date.fromisoformat(ref) - dt.date.fromisoformat(day)).days)
    except ValueError:
        return None


def age_label(day: str | None, ref: str) -> str:
    """'today', '1 d', '17 d', or '—'."""
    n = age_days(day, ref)
    return "—" if n is None else "today" if n == 0 else f"{n} d"


def time_range_label(first_ts: float, last_ts: float, running: bool = False) -> str:
    """'09:55–10:20', '22:10–01:40' across midnight, '12:20–now' while running, and '20:47–Sun 12:09' when the
    session ends on a later day and the plain times would read like one day (or more than a night lies between)."""
    fa, fb = local(first_ts), local(last_ts)
    a = fa.strftime("%H:%M")
    b = "now" if running else fb.strftime("%H:%M")
    if not running and fb.date() != fa.date() and (b >= a or (fb.date() - fa.date()).days > 1):
        b = f"{fb.strftime('%a')} {b}"
    return a if a == b else f"{a}–{b}"


def parse_ts(iso: str | None) -> float | None:
    """Transcript timestamps are ISO-8601 UTC ('2026-10-04T06:52:52.587Z')."""
    if not iso or not isinstance(iso, str):
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_day(s: str | None) -> bool:
    if not s or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return False
    try:
        dt.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def local_ts(day: str, hm: str | None) -> float | None:
    """Epoch seconds of a local day and 'HH:MM' (None when there is no time)."""
    if not hm:
        return None
    try:
        h, m = (int(x) for x in hm.split(":"))
        return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(h % 24, m), zone()).timestamp()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- paths and labels


def roots() -> list[str]:
    return list(config.SESSIONS_ROOTS)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def root_of(path: str | None) -> str | None:
    """The configured root (longest first) that holds a path: a sessions root or the docs folder."""
    if not path:
        return None
    cands = [r for r in roots() if _inside(path, r)]
    if config.DOCS_DIR and _inside(path, config.DOCS_DIR) and not cands:
        return os.path.dirname(config.DOCS_DIR) or "/"
    return max(cands, key=len) if cands else None


def tilde(path: str | None) -> str:
    return config.tilde(path) if path else ""


def rel(path: str | None) -> str:
    """What the UI shows for a host path: relative to its root, 'memory/<file>' for memory notes, else ~/…"""
    if not path:
        return ""
    m = re.search(r"/projects/[^/]+/memory/([^/]+)$", path)
    if m and any(_inside(path, d) for d in config.CLAUDE_CONFIG_DIRS):
        return f"memory/{m.group(1)}"
    r = root_of(path)
    if r:
        return path[len(r.rstrip("/")) + 1:] or os.path.basename(path)
    return tilde(path)


def source(kind: str, label: str, *, path: str | None = None, line: int | None = None, url: str | None = None,
           session: str | None = None, turn: int | None = None) -> dict:
    """The one Source shape. href: '#s-<id>[-t<turn>]', 'vscode://file<abs path>[:line]', an http(s) URL, or None."""
    href = None
    if session:
        href = f"#s-{session}" + (f"-t{turn}" if turn else "")
    elif url and re.match(r"https?://", url):
        href = url
    elif path and path.startswith("/"):
        href = f"vscode://file{path}" + (f":{line}" if line else "")
    return {"kind": kind, "label": label, "href": href, "path": path, "line": line, "session": session, "turn": turn}


def resume_command(sid: str, cwd: str | None) -> str:
    """'claude --resume' only works from the session's folder."""
    where = tilde(cwd) if cwd else ""
    if where and " " in where:
        where = "'" + where.replace("'", "'\\''") + "'"
    return f"cd {where} && claude --resume {sid}" if where else f"claude --resume {sid}"


# ---------------------------------------------------------------- models


def model_label(model: str | None) -> str | None:
    """'claude-haiku-4-5-20251001' -> 'Haiku 4.5', 'claude-opus-5-5' -> 'Opus 5.5', '<synthetic>' -> None."""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+(?:-\d{1,2})*?)(?:-\d{8})?", model or "")
    if not m:
        return None
    return f"{m.group(1).capitalize()} {m.group(2).replace('-', '.')}"


# ---------------------------------------------------------------- search


def fts_query(q: str) -> str | None:
    """User text -> a safe FTS5 MATCH expression, or None when nothing searchable is left.

    Raw input breaks FTS5 ('x11vnc -unixsockonly' reads as a column filter, 'foo-' is a syntax error).
    Each whitespace-separated term becomes a quoted phrase (implicit AND); the last term also matches as
    a prefix while it has 2+ word characters, so typing 'dock' finds 'docker'.
    """
    terms = [t for t in re.split(r"\s+", (q or "").strip()) if re.search(r"\w", t)]
    if not terms:
        return None
    parts = ['"' + t.replace('"', '""') + '"' for t in terms[:12]]
    if len(re.sub(r"\W", "", terms[-1])) >= 2:
        parts[-1] += "*"
    return " ".join(parts)


def search(c: sqlite3.Connection, q: str, *, repo: str | None = None, limit_sessions: int = 20,
           per_session: int = 4, offset: int = 0, visible_sql: str = "") -> dict:
    """Search inside every transcript: sessions ordered by their best hit (bm25), hits in conversation order."""
    match = fts_query(q)
    if match is None:
        return {"total_hits": 0, "total_sessions": 0, "results": [], "next_offset": None}
    where, args = "", [match]
    if repo:
        where += (" and session in (select id from cc_sessions where exists "
                  "(select 1 from json_each(cc_sessions.repos) where value = ?))")
        args.append(repo)
    if visible_sql:
        where += f" and session in (select id from cc_sessions where {visible_sql})"
    rows = c.execute(
        f"""select session, item, turn, step, kind, ts, bm25(cc_fts) as score,
                   snippet(cc_fts, 0, '{MARK_OPEN}', '{MARK_CLOSE}', '…', 14) as snip
            from cc_fts where cc_fts match ? {where}
            order by rank limit 2000""",
        args,
    ).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)
    order = sorted(by, key=lambda s: min(x[6] for x in by[s]))
    page = order[offset: offset + limit_sessions]
    results = []
    for sid in page:
        hits = sorted(by[sid], key=lambda x: (int(x[1]), int(x[3]) if x[3] is not None else -1))
        results.append({
            "session": sid,
            "hits": [{"item": int(h[1]), "turn": int(h[2]), "step": None if h[3] is None else int(h[3]), "kind": h[4],
                      "ts": None if h[5] is None else float(h[5]), "snippet": h[7]} for h in hits[:per_session]],
            "more": max(0, len(hits) - per_session),
        })
    nxt = offset + limit_sessions if offset + limit_sessions < len(order) else None
    return {"total_hits": len(rows), "total_sessions": len(order), "results": results, "next_offset": nxt}


def find_in_session(c: sqlite3.Connection, session: str, q: str, limit: int = 500) -> dict:
    """Every hit in one session, in conversation order."""
    match = fts_query(q)
    if match is None:
        return {"q": q, "total": 0, "hits": []}
    rows = c.execute(
        f"""select item, turn, step, kind, snippet(cc_fts, 0, '{MARK_OPEN}', '{MARK_CLOSE}', '…', 14)
            from cc_fts where cc_fts match ? and session = ? limit ?""",
        (match, session, limit),
    ).fetchall()
    hits = [{"item": int(r[0]), "turn": int(r[1]), "step": None if r[2] is None else int(r[2]), "kind": r[3],
             "snippet": r[4]} for r in rows]
    hits.sort(key=lambda h: (h["item"], h["step"] if h["step"] is not None else -1))
    return {"q": q, "total": len(hits), "hits": hits}


def scrub_line(text: str | None, limit: int = 300) -> str:
    """Redacted, on one line, cut to `limit` characters."""
    from .redact import scrub

    t = re.sub(r"\s+", " ", scrub(text or "")).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def clean_for_index(text: str | None) -> str:
    """Strip the snippet marker characters so stored text can never forge a <mark>."""
    return (text or "").replace(MARK_OPEN, " ").replace(MARK_CLOSE, " ")


# ---------------------------------------------------------------- steps


def steps_summary(labels_and_status: list[tuple[str, str]]) -> str:
    """'14 steps · read 7 files · ran 4 commands · edited 2 files · wrote 3 files · 3 Discord posts'."""
    n = len(labels_and_status)
    c = {k: 0 for k in ("read", "ran", "edited", "wrote", "discord", "notes", "agents", "asked", "web", "failed")}
    for label, status in labels_and_status:
        if label in ("Read", "Grep", "Glob", "NotebookRead", "LS"):
            c["read"] += 1
        elif label in ("Bash", "Monitor"):
            c["ran"] += 1
        elif label in ("Edit", "MultiEdit", "NotebookEdit"):
            c["edited"] += 1
        elif label == "Write":
            c["wrote"] += 1
        elif label == "Discord":
            c["discord"] += 1
        elif label == "Notification":
            c["notes"] += 1
        elif label in ("Agent", "Task", "Workflow"):
            c["agents"] += 1
        elif label == "AskUserQuestion":
            c["asked"] += 1
        elif label in ("WebFetch", "WebSearch"):
            c["web"] += 1
        if status == "error":
            c["failed"] += 1

    def p(k: str, one: str, many: str) -> str:
        return f"{c[k]} {one if c[k] == 1 else many}"

    parts = [f"{n} step" + ("" if n == 1 else "s")]
    if c["read"]:
        parts.append("read " + p("read", "file", "files"))
    if c["ran"]:
        parts.append("ran " + p("ran", "command", "commands"))
    if c["edited"]:
        parts.append("edited " + p("edited", "file", "files"))
    if c["wrote"]:
        parts.append("wrote " + p("wrote", "file", "files"))
    if c["discord"]:
        parts.append(p("discord", "Discord post", "Discord posts"))
    if c["notes"]:
        parts.append(p("notes", "notification", "notifications"))
    if c["agents"]:
        parts.append(p("agents", "agent", "agents"))
    if c["asked"]:
        parts.append(p("asked", "question card", "question cards"))
    if c["web"]:
        parts.append(p("web", "web lookup", "web lookups"))
    if c["failed"]:
        parts.append(p("failed", "failed", "failed"))
    return " · ".join(parts)


def monotonic_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
