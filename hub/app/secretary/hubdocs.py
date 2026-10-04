"""The docs folder and memory notes, parsed without a model (adapted from the hub research parser).

DOCS_DIR is optional. When it is a docs hub it has:
  changelog/<repo>/<YYYY-MM-DD>.md   entries '## HH:MM — title' with **Type:**, **Design doc:**, Follow-ups
  design/README.md                   an index table '| Feature / module | Status | Projects touched |'
  design/<folder>/*.md               a **Status:** line, prototype links, decision tables, ❓ questions
Memory notes live in <config dir>/projects/<project>/memory/*.md (frontmatter name, description,
metadata.{type, originSessionId, modified}); MEMORY.md is their index. Only the memory of projects whose
sessions the secretary may read is parsed (see memory_dirs).

Every free text that leaves this module is redacted. Files are parsed again only when they change.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import threading
from pathlib import Path

from .. import config
from . import util
from .redact import scrub

FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d+[.)])[ \t]+(?P<text>.*)$")
ISO_DATE_RE = re.compile(r"\b(20\d\d-[01]\d-[0-3]\d)\b")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
ARTIFACT_RE = re.compile(r"https://claude\.ai/(?:public/)?artifacts?/[A-Za-z0-9_-]+")
HTTPS_RE = re.compile(r"https://[^\s)\]>\"'`]+")
QMARK = "❓"

_cache: dict[tuple, object] = {}
_cache_lock = threading.Lock()


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def cached(kind: str, path: Path, fn):
    """fn(path) once per file version."""
    stamp = _stamp(path)
    if stamp is None:
        return None
    key = (kind,) + stamp
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    val = fn(path)
    with _cache_lock:
        for k in [k for k in _cache if k[0] == kind and k[1] == stamp[0]]:
            del _cache[k]
        _cache[key] = val
    return val


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return []


def iter_unfenced(lines: list[str]):
    """(lineno, line, in_fence); fence lines themselves count as fenced."""
    fence = None
    for i, line in enumerate(lines, 1):
        m = FENCE_RE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
                yield i, line, True
                continue
            if m.group(1) == fence:
                fence = None
                yield i, line, True
                continue
        yield i, line, fence is not None


_CODE_SPAN = re.compile(r"(`+)(.+?)\1")


def strip_md(s: str) -> str:
    """Plain text: links become their label, emphasis markers and backticks go. Code spans keep their text as it
    is, so `mcp__deskmate__.*` or `__init__.py` do not lose their underscores."""
    s = MD_LINK_RE.sub(lambda m: m.group(1), s or "")
    out, at = [], 0
    for m in _CODE_SPAN.finditer(s):
        out.append(s[at:m.start()].replace("**", "").replace("__", "").replace("`", ""))
        out.append(m.group(2))
        at = m.end()
    out.append(s[at:].replace("**", "").replace("__", "").replace("`", ""))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def no_code(s: str) -> str:
    return INLINE_CODE_RE.sub(" ", s)


def _cut(t: str, limit: int) -> str:
    return t if len(t) <= limit else t[: limit - 1] + "…"


def clean(s: str | None, limit: int = 2000) -> str:
    """Display text: markdown stripped, redacted, cut. Give it markdown, not text strip_md already made plain."""
    return _cut(scrub(strip_md(s or "")), limit)


def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, buf, in_code, i = [], [], False, 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    if in_code:
        return [c.strip() for c in s.split("|")]
    return cells


def parse_tables(lines: list[str]) -> list[dict]:
    tables, heading, hline = [], None, 0
    unf = list(iter_unfenced(lines))
    k = 0
    while k < len(unf):
        i, line, fenced = unf[k]
        if not fenced:
            hm = HEADING_RE.match(line)
            if hm:
                heading, hline = strip_md(hm.group(2)), i
            elif line.lstrip().startswith("|") and k + 1 < len(unf) and TABLE_SEP_RE.match(unf[k + 1][1]):
                header = [strip_md(c) for c in split_row(line)]
                rows, k2 = [], k + 2
                while k2 < len(unf) and not unf[k2][2] and unf[k2][1].lstrip().startswith("|"):
                    rows.append((unf[k2][0], split_row(unf[k2][1])))
                    k2 += 1
                tables.append({"line": i, "heading": heading, "heading_line": hline, "header": header, "rows": rows})
                k = k2
                continue
        k += 1
    return tables


def collect_items(block: list[tuple[int, str]]) -> list[dict]:
    """Top-level list items (or paragraphs when there are no bullets): [{line, text, children}]."""
    bl = [(n, ln) for n, ln in block if BULLET_RE.match(ln)]
    items: list[dict] = []
    if bl:
        top = min(len(BULLET_RE.match(ln).group("indent").expandtabs(4)) for _, ln in bl)
        cur = None
        for n, ln in block:
            m = BULLET_RE.match(ln)
            if m and len(m.group("indent").expandtabs(4)) <= top:
                cur = {"line": n, "text": m.group("text").strip(), "children": []}
                items.append(cur)
            elif cur is not None and ln.strip():
                if m:
                    cur["children"].append(m.group("text").strip())
                elif cur["children"] and len(ln) - len(ln.lstrip()) > top + 2:
                    cur["children"][-1] += " " + ln.strip()
                else:
                    cur["text"] += " " + ln.strip()
    else:
        cur = None
        for n, ln in block:
            if not ln.strip():
                cur = None
                continue
            if cur is None:
                cur = {"line": n, "text": ln.strip(), "children": []}
                items.append(cur)
            else:
                cur["text"] += " " + ln.strip()
    return items


# ---------------------------------------------------------------- open-loop lexicon

OPEN_LOOP = [
    ("not_pushed", r"\b(?:not|never)\s+(?:yet\s+)?(?:been\s+)?pushed\b|\bnothing\s+(?:is\s+|was\s+|has\s+been\s+)?pushed\b|\bunpushed\b"
                   r"|\bnot\s+(?:yet\s+)?(?:merged|committed|deployed)\s+(?:or|and|nor)\s+pushed\b"),
    ("not_deployed", r"\b(?:not|never)\s+(?:yet\s+)?(?:been\s+)?deployed\b|\bnothing\s+(?:is\s+|was\s+)?deployed\b|\bnone\s+deployed\b"
                     r"|\bundeployed\b|\bnot\s+(?:yet\s+)?pushed\s+(?:or|and|nor)\s+deployed\b"),
    ("not_merged", r"\bnot\s+(?:yet\s+)?merged\b|\bunmerged\b"),
    ("not_committed", r"\bnot\s+(?:yet\s+)?committed\b|\buncommitted\b"),
    ("to_verify", r"\b(?:still\s+)?to\s+verify\b|\bnot\s+yet\s+(?:verified|tried|tested|run)\b|\bnever\s+tested\b|\bunverified\b"),
    ("pending", r"\bpending\s+(?:deploy|release|merge|push|review|revocation|approval|user|decision|confirmation|verification|"
                r"provisioning|cutover|migration|fix|install)\w*|\b(?:is|are|still|remains?|left)\s+pending\b|\b\w+\s+pending\s*(?:[.;,)]|$)"),
    ("waiting_on", r"\bwaiting\s+(?:on|for)\b"),
    ("still_to", r"\bstill\s+(?:to\s+\w+|has\s+to|have\s+to|needs?\b)"),
    ("needs_before", r"\bneeds?\b[^.;\n]{0,80}?\bbefore\s+(?:prod|production|deploy|release|claiming|it\b)"),
    ("before_claiming", r"\bbefore\s+claiming\b"),
    ("open_items", r"\bopen\s+items?\b|\bwhat\s+is\s+left\b|\bwhat'?s\s+left\b"),
    ("next_step", r"(?:^|\s)\**next:?\**:"),
]
OPEN_LOOP_RE = [(k, re.compile(p, re.I)) for k, p in OPEN_LOOP]
# The memory loop rule (research contract §2.3.14) uses a subset of these.
MEMORY_LOOP_KINDS = {"not_pushed", "not_deployed", "to_verify", "pending", "waiting_on"}
MEMORY_LOOP_EXTRA = re.compile(r"\buser\s+to\b|\bwaits?\s+on\b", re.I)


def open_loop_hits(text: str) -> list[tuple[str, str, int]]:
    out = []
    for kind, rx in OPEN_LOOP_RE:
        for m in rx.finditer(text):
            out.append((kind, m.group(0).strip(), m.start()))
    return out


def sentence_at(text: str, pos: int, limit: int = 180) -> str:
    starts = [m.end() for m in re.finditer(r"(?<=[.;!?])\s+", text[:pos])]
    a = starts[-1] if starts else 0
    m = re.search(r"[.;!?](?:\s|$)", text[pos:])
    b = pos + m.end() if m else len(text)
    s = text[a:b].strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def first_sentence(text: str, limit: int = 200) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    m = re.search(r"[.!?](?:\s|$)", t)
    s = t[: m.end()].strip() if m else t
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ---------------------------------------------------------------- changelogs

ENTRY_RE = re.compile(
    r"^##[ \t]+(?:(?P<time>[0-2]?\d:[0-5]\d)(?:[ \t]*\((?P<when>[^)]{1,40})\))?"
    r"(?:[ \t]*(?P<sep>—|–|--?|:)[ \t]*|[ \t]+))?(?P<title>\S.*?)[ \t]*$")
FIELD_RE = re.compile(r"^\*\*(?P<label>[^*\n]{1,60}?):\*\*[ \t]*(?P<value>.*)$|^\*\*(?P<label2>[^*\n]{1,60}?)\*\*:[ \t]*(?P<value2>.*)$")
FOLLOWUP_NAME_RE = re.compile(r"follow[\s-]*ups?\b", re.I)
OPEN_SECTION_RE = re.compile(r"^(?:still open|known gaps?|not done|not verified|open(?: items?| questions?)?|risks?)\b", re.I)
KNOWN_SECTION_LABEL_RE = re.compile(r"follow[\s-]*ups?|verif|^why\b|^what (?:changed|it adds)|^risks?$|^deploy", re.I)
DESIGN_FIELD_RE = re.compile(r"design|related|spec", re.I)
TYPE_CANON = {"feat": "feature", "feature": "feature", "fix": "fix", "bugfix": "fix", "hotfix": "fix",
              "chore": "chore", "docs": "docs", "doc": "docs", "refactor": "refactor", "test": "test",
              "tests": "test", "ops": "ops", "security": "security", "perf": "perf", "merge": "merge",
              "style": "style", "revert": "revert"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
NONE_ITEM_RE = re.compile(r"^(?:\*\*)?(?:none|n/?a|nil|nothing|no follow[\s-]*ups?)\b", re.I)


def section_kind(name: str) -> str:
    n = name.lower()
    if FOLLOWUP_NAME_RE.search(n):
        return "follow_ups"
    if OPEN_SECTION_RE.match(n):
        return "open_items"
    if re.search(r"verif|^checks?$|how to test", n):
        return "how_to_verify"
    if n.startswith("why"):
        return "why"
    if re.match(r"what (changed|it adds)|changed$|added\b|what happened", n):
        return "what_changed"
    if n.startswith("cross-project"):
        return "cross_project"
    return "other"


def design_refs(text: str, folders: set[str]) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"(?:[\w.-]+/|(?:\.\./)+|(?<![\w/.-]))design/(?P<folder>[A-Za-z0-9._-]+)/?(?:[A-Za-z0-9._-]+\.md)?", text or ""):
        f = m.group("folder")
        if folders and f not in folders:
            continue
        if f not in out:
            out.append(f)
    return out


def norm_types(raw: str | None) -> list[str]:
    if not raw:
        return []
    s = re.sub(r"\([^)]*\)", " ", raw).lower()
    out: list[str] = []
    for t in re.findall(r"[a-z]+", s):
        c = TYPE_CANON.get(t)
        if c and c not in out:
            out.append(c)
    return out


def effective_date(file_date: dt.date, when: str | None) -> dt.date | None:
    """Work after midnight is logged in the previous day's file as '00:30 (Oct 4) — …'."""
    if not when:
        return file_date
    m = ISO_DATE_RE.search(when)
    if m:
        return dt.date.fromisoformat(m.group(1))
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})", when) or re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})", when)
    if m:
        a, b = m.groups()
        mon, day = (a, b) if a.isalpha() else (b, a)
        mi = MONTHS.get(mon[:3].lower())
        if mi:
            year = file_date.year + (1 if (file_date.month == 12 and mi == 1) else 0)
            try:
                return dt.date(year, mi, int(day))
            except ValueError:
                return None
    if re.search(r"next day|\+1", when, re.I):
        return file_date + dt.timedelta(days=1)
    return None


LABEL_PREFIX_RE = re.compile(r"^[A-Z][\w /&-]{1,30}:\s*")


def followup_item(it: dict) -> dict:
    text = it["text"]
    core = re.sub(r"^\*\*(?=~~)", "", text)
    withdrawn = core.startswith("~~")
    plain = strip_md(text)
    loops = sorted({k for k, _, _ in open_loop_hits(strip_md(no_code(text)))}) if not withdrawn else []
    # "Cross-project: none. …" or a bare "Cross-project:" heading over sub-items says there is nothing to do
    body = LABEL_PREFIX_RE.sub("", plain, count=1)
    none = (not body.strip() or bool(NONE_ITEM_RE.match(plain)) or bool(NONE_ITEM_RE.match(body)))
    return {"line": it["line"], "text": scrub(plain), "withdrawn": withdrawn,
            "none": none and not withdrawn and not loops, "open_loops": loops}


def _parse_changelog(path: Path, repo: str, folders: frozenset) -> list[dict]:
    try:
        file_date = dt.date.fromisoformat(path.stem)
    except ValueError:
        return []
    lines = read_lines(path)
    entries, cur = [], None
    for i, line, fenced in iter_unfenced(lines):
        if not fenced and line.startswith("## "):
            m = ENTRY_RE.match(line)
            if not m:
                continue
            cur = {"repo": repo, "path": str(path), "line": i, "date": file_date.isoformat(), "time": m.group("time"),
                   "when": m.group("when"), "title": clean(m.group("title"), 300), "_body": []}
            entries.append(cur)
            continue
        if cur is None:
            continue
        if not fenced and (line.startswith("# ") or re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", line)):
            continue
        cur["_body"].append((i, line, fenced))
    return [_finish_entry(e, file_date, folders) for e in entries]


def _finish_entry(e: dict, file_date: dt.date, folders: frozenset) -> dict:
    body = e.pop("_body")
    fields: dict[str, str] = {}
    sections: list[dict] = []
    sec = None
    preamble = True
    last_field = None
    for i, line, fenced in body:
        if not fenced:
            hm = HEADING_RE.match(line)
            if hm and len(hm.group(1)) >= 3:
                name = strip_md(hm.group(2))
                if len(hm.group(1)) == 3 or FOLLOWUP_NAME_RE.search(name) or sec is None:
                    sec = {"name": name, "kind": section_kind(name), "line": i, "lines": []}
                    sections.append(sec)
                    preamble, last_field = False, None
                    continue
            fm = FIELD_RE.match(line)
            if fm:
                label = (fm.group("label") or fm.group("label2")).strip()
                value = (fm.group("value") if fm.group("label") else fm.group("value2")).strip()
                if not value and (preamble or KNOWN_SECTION_LABEL_RE.search(label)):
                    sec = {"name": label, "kind": section_kind(label), "line": i, "lines": []}
                    sections.append(sec)
                    preamble, last_field = False, None
                    continue
                if value and (preamble or label.lower() in ("type", "scope", "design doc")):
                    fields.setdefault(label, value)
                    last_field = label if preamble else None
                    continue
            elif preamble and last_field and line.strip() and not line.lstrip().startswith(("-", "*", "#", "|", ">")):
                fields[last_field] += " " + line.strip()
                continue
            elif not line.strip():
                last_field = None
        if sec is not None:
            sec["lines"].append((i, "" if fenced else line))
    type_raw = next((v for k, v in fields.items() if k.lower() == "type"), None)
    dd_raw = [v for k, v in fields.items() if DESIGN_FIELD_RE.search(k)]
    follow_ups = []
    for s in sections:
        if s["kind"] in ("follow_ups", "open_items"):
            follow_ups += [dict(followup_item(it), section=s["name"]) for it in collect_items(s["lines"])]
    eff = effective_date(file_date, e["when"])
    ship_fields = " ".join(v for k, v in fields.items() if re.match(r"(?i)branch|commits?\b|deploy", k))
    shas = re.findall(r"\b[0-9a-f]{7,40}\b", ship_fields)
    branch = next((v for k, v in fields.items() if k.lower().startswith("branch")), None)
    e.update({
        "effective_date": eff.isoformat() if eff else None,
        "types": norm_types(type_raw), "has_type": type_raw is not None,
        "scope": clean(next((v for k, v in fields.items() if k.lower() == "scope"), ""), 400) or None,
        "design_docs": design_refs(" ".join(dd_raw), set(folders)),
        "ship_text": clean(ship_fields, 600),
        "commits": shas[:10],
        "branch": clean(branch, 200) if branch else None,
        "follow_ups": follow_ups,
        "has_followups": any(s["kind"] == "follow_ups" for s in sections),
    })
    return e


def changelog_folders(hub: Path) -> list[str]:
    d = hub / "changelog"
    try:
        return sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
    except OSError:
        return []


def design_folders(hub: Path) -> list[str]:
    d = hub / "design"
    try:
        return sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
    except OSError:
        return []


def entries(hub: Path) -> list[dict]:
    """Every changelog entry, in folder, file and line order."""
    folders = frozenset(design_folders(hub))
    out: list[dict] = []
    for repo in changelog_folders(hub):
        try:
            files = sorted((hub / "changelog" / repo).glob("*.md"))
        except OSError:
            continue
        for f in files:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem):
                out += cached(f"cl:{repo}:{','.join(sorted(folders))}", f,
                              lambda p, r=repo: _parse_changelog(p, r, folders)) or []
    return out


def changelog_repo_map(hub: Path) -> dict[str, str]:
    """repo folder name -> changelog folder, from a 'Folder | Repo' table in changelog/README.md, else the same name."""
    m: dict[str, str] = {}
    for t in parse_tables(read_lines(hub / "changelog" / "README.md")):
        hdr = [h.lower() for h in t["header"]]
        fi = next((i for i, h in enumerate(hdr) if "folder" in h), None)
        ri = next((i for i, h in enumerate(hdr) if "repo" in h), None)
        if fi is None or ri is None:
            continue
        for _, cells in t["rows"]:
            if max(fi, ri) >= len(cells):
                continue
            folder = strip_md(cells[fi]).strip("/ ")
            raw = strip_md(cells[ri]).strip()
            pm = re.search(r"([^\s/`]+)/?\s*$", raw.split("(")[0].strip())
            if folder and pm:
                m[pm.group(1)] = folder
    for f in changelog_folders(hub):
        m.setdefault(f, f)
    return m


# ---------------------------------------------------------------- design index, status, lanes

def parse_projects(cell: str, known: list[str]) -> list[str]:
    """Repos a design touches: per comma/semicolon part, the first known repo outside parentheses
    ('reads x' parts do not count as touched)."""
    parts, depth, buf = [], 0, ""
    for ch in strip_md(cell):
        depth += ch == "("
        depth -= ch == ")"
        if ch in ",;" and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    out: list[str] = []
    for p in (x.strip() for x in parts if x.strip()):
        if re.match(r"reads\b", p, re.I):
            continue
        outside = re.sub(r"\([^)]*\)", " ", p)
        rm = next((k for k in known if re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", outside)), None)
        if rm and rm not in out:
            out.append(rm)
    return out


def design_index(hub: Path, known: list[str]) -> list[dict]:
    """Rows of the first table in design/README.md with a 'feature' and a 'status' column."""
    lines = read_lines(hub / "design" / "README.md")
    rows = []
    for t in parse_tables(lines):
        hdr = [h.lower() for h in t["header"]]
        if not any("status" in h for h in hdr) or not any("feature" in h for h in hdr):
            continue
        fi = next(i for i, h in enumerate(hdr) if "feature" in h)
        si = next(i for i, h in enumerate(hdr) if "status" in h)
        pi = next((i for i, h in enumerate(hdr) if "project" in h), None)
        for ln, cells in t["rows"]:
            cells = cells + [""] * (len(hdr) - len(cells))
            fm = MD_LINK_RE.search(cells[fi])
            link = fm.group(2) if fm else None
            name = strip_md(fm.group(1) if fm else cells[fi])
            folder = link.split("/")[0] if link and "/" in link else (link or name)
            status = cells[si]
            rows.append({"line": ln, "feature": _cut(scrub(name), 200), "folder": folder, "link": link,
                         "link_exists": bool(link) and (hub / "design" / link.split("#")[0]).exists(),
                         "status": clean(status, 1000),
                         "prototypes": ARTIFACT_RE.findall(status),
                         "projects": parse_projects(cells[pi], known) if pi is not None and pi < len(cells) else []})
        break
    return rows


QUALIFIERS = ["not deployed", "not pushed", "not merged", "uncommitted", "cutover pending", "pending",
              "check on a device", "not built"]
KEYWORDS = ["deployed", "implemented", "feature-complete", "built", "pushed", "in review", "in-review",
            "in progress", "approved", "draft"]


_SENTENCE_END = re.compile(r"(?<=[^\s.])\.\s+(?=\S)")


def _stage(t: str) -> tuple[str | None, str | None]:
    """(keyword, qualifier) of a lower-case status text: the most advanced stage word, its qualifier."""
    qual = next((q for q in QUALIFIERS if q in t), None)
    t2 = t
    for q in QUALIFIERS:
        t2 = t2.replace(q, " ")
    kw = next((k for k in KEYWORDS if re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", t2)), None)
    return kw, qual


def lane_of(status: str) -> dict:
    """Research contract §2.3.13: the board lane and the short status from a status text.

    The first sentence decides: "in progress (2026-10-04). M0–M3 built, …" is in progress, and "implemented +
    deployed …. One part is not merged." is deployed. Later sentences describe parts of the work; only a
    status whose first sentence has no stage word is read whole."""
    t = strip_md(status or "").lower()
    m = _SENTENCE_END.search(t)
    kw, qual = _stage(t[: m.start()] if m else t)
    if kw is None:
        kw, qual = _stage(t)
    if kw in ("deployed", "implemented") and not qual:
        lane = "shipped"
    elif kw in ("built", "feature-complete", "pushed") or (kw in ("deployed", "implemented") and qual):
        lane = "built"
    else:
        lane = "building"
    dm = ISO_DATE_RE.search(status or "")
    day = dm.group(1) if dm else None
    if kw:
        short = kw.replace("in-review", "in review").capitalize()
        if day:
            try:
                short += f" {util.short_date(day)}"
            except ValueError:
                pass
        if qual:
            short += f", {qual}"
    else:
        s = strip_md(status or "")
        short = s if len(s) <= 50 else s[:49] + "…"
    return {"lane": lane, "short": short or "—", "day": day, "keyword": kw, "qualifier": qual}


STATUS_LINE_RE = re.compile(r"^(?P<bullet>\s*[-*+]\s+)?(?:>\s*)?\*\*Status(?:\s*\([^)]*\))?:?\*\*:?\s*(?P<value>\S.*)$")
PROTO_LINE_RE = re.compile(r"^\s*(?:[-*+]\s+)?\*\*Prototype[^*]*?:?\*\*:?(?P<value>.*)$")


def main_doc(hub: Path, folder: str, index_link: str | None) -> Path | None:
    if index_link:
        p = hub / "design" / index_link.split("#")[0]
        if p.is_file():
            return p
    d = hub / "design" / folder
    for cand in (d / "00-overview.md", d / f"{folder}.md"):
        if cand.is_file():
            return cand
    try:
        mds = sorted(d.glob("*.md"))
    except OSError:
        mds = []
    return mds[0] if mds else None


def header_cols(header: list[str]) -> dict:
    col: dict[str, int] = {}
    for i, x in enumerate(h.lower().strip() for h in header):
        if x in ("#", "id", "no", "no."):
            col.setdefault("id", i)
        elif x in ("date", "when", "decided"):
            col.setdefault("date", i)
        elif x == "question":
            col.setdefault("question", i)
        elif "decision" in x:
            col.setdefault("decision", i)
        elif x.startswith("finding"):
            col.setdefault("finding", i)
        elif x in ("outcome", "consequence", "result", "answer") or x.startswith("assumption"):
            col.setdefault("outcome", i)
        elif "why" in x or "rationale" in x or "cost" in x:
            col.setdefault("why", i)
    return col


def decision_rows(t: dict, path: Path, is_decisions_file: bool) -> list[dict]:
    col = header_cols(t["header"])
    if not ({"decision", "question", "finding"} & set(col)):
        return []
    heading = t["heading"] or ""
    if not (is_decisions_file or re.search(r"decision", heading, re.I)):
        return []
    hdate = ISO_DATE_RE.search(heading)
    out = []
    for ln, cells in t["rows"]:
        def g(k):
            return cells[col[k]].strip() if k in col and col[k] < len(cells) else ""
        q, d = g("question"), g("decision")
        if q and d:
            decision, outcome = q, d
        else:
            decision = d or g("finding") or q
            outcome = g("outcome") or g("why") or None
        if not strip_md(decision):
            continue
        rid = strip_md(g("id")) or None
        if not rid:
            im = re.match(r"^\W*(D\d+[a-z]?|Q\d+|L\d+)\b", strip_md(decision))
            rid = im.group(1) if im else None
        dcell = g("date")
        dm = ISO_DATE_RE.search(dcell)
        date = dm.group(1) if dm else (hdate.group(1) if hdate else None)
        rowtext = no_code(" | ".join(cells))
        is_q = bool(re.match(r"^open\b", heading, re.I) or (rid or "").startswith("Q")
                    or ("question" in col and "decision" not in col) or QMARK in rowtext)
        withdrawn = decision.lstrip("* ").startswith("~~")
        text = strip_md(decision.replace(QMARK, "").replace("> ", ""))
        text = re.sub(r"^(?:D\d+[a-z]?|Q\d+|L\d+)\.?\s*(?:\(was Q\d+\)\.?\s*)?", "", text).strip() or text
        out.append({"path": str(path), "line": ln, "ref": rid, "day": date, "is_question": is_q,
                    "withdrawn": withdrawn, "reversed": bool(re.search(r"\bREVERSED\b|\bsuperseded by\b", decision)),
                    "text": _cut(scrub(text), 600),  # already plain: a second strip_md would eat mcp__x__ names
                    "outcome": clean(outcome.replace(QMARK, "").replace("> ", ""), 600) if outcome else None})
    return out


def _parse_design_doc(path: Path) -> dict:
    lines = read_lines(path)
    first_h2 = next((i for i, ln, f in iter_unfenced(lines) if not f and ln.startswith("## ")), len(lines) + 1)
    h1 = None
    status, prototypes, decisions, questions = None, [], [], 0
    is_dec = path.name.startswith("99-decisions")
    for i, line, fenced in iter_unfenced(lines):
        if fenced:
            continue
        if h1 is None and line.startswith("# "):
            h1 = strip_md(line[2:])
        sm = STATUS_LINE_RE.match(line)
        if sm and status is None and i < first_h2 and not sm.group("bullet"):
            status = {"line": i, "text": clean(sm.group("value"), 1000)}
        pm = PROTO_LINE_RE.match(line)
        if pm:
            url = next(iter(ARTIFACT_RE.findall(line) or HTTPS_RE.findall(line)), None)
            if url:
                prototypes.append({"line": i, "url": url})
        if QMARK in no_code(line):
            questions += 1
    for t in parse_tables(lines):
        decisions += decision_rows(t, path, is_dec)
    return {"h1": _cut(scrub(h1), 300) if h1 else None, "status": status, "prototypes": prototypes,
            "decisions": decisions, "questions": questions}


def designs(hub: Path, known: list[str]) -> list[dict]:
    """One record per design folder: index row, main doc, title, status, prototype, decisions."""
    idx = {r["folder"]: r for r in design_index(hub, known)}
    out = []
    for folder in design_folders(hub):
        row = idx.get(folder)
        md = main_doc(hub, folder, row["link"] if row else None)
        docs = []
        try:
            docs = sorted((hub / "design" / folder).glob("*.md"))
        except OSError:
            pass
        parsed = {p: cached("design", p, _parse_design_doc) or {} for p in docs}
        mainp = parsed.get(md, {}) if md else {}
        title = mainp.get("h1") or folder
        title = re.split(r":\s|\s—\s|\s–\s", title, maxsplit=1)[0].strip() or folder
        proto = next((p["url"] for p in mainp.get("prototypes", []) if p["url"].startswith("https://")), None)
        if not proto and row and row["prototypes"]:
            proto = row["prototypes"][0]
        decs = [d for p in docs for d in parsed[p].get("decisions", [])]
        out.append({"folder": folder, "title": _cut(title, 120), "main_doc": str(md) if md else None,
                    "index": row, "status": mainp.get("status"), "prototype_url": proto,
                    "decisions": decs})
    return out


# ---------------------------------------------------------------- memory notes

def parse_frontmatter(text: str) -> tuple[dict, str, int]:
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return {}, text, 0
    data: dict = {}
    parent = None
    for raw in m.group(1).split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        km = re.match(r"^(\s*)([A-Za-z_][\w-]*):\s*(.*)$", raw)
        if not km:
            continue
        indent, key, val = len(km.group(1)), km.group(2), km.group(3).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].replace('\\"', '"')
        if indent == 0:
            if val == "":
                data[key] = {}
                parent = key
            else:
                data[key] = val
                parent = None
        elif parent is not None and isinstance(data.get(parent), dict):
            data[parent][key] = val
    return data, text[m.end():], m.group(0).count("\n")


def _parse_memory(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    fm, body, offset = parse_frontmatter(text)
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    modified = None
    raw_mod = meta.get("modified") or fm.get("modified")
    if isinstance(raw_mod, str) and raw_mod:
        modified = util.parse_ts(raw_mod)
    desc = fm.get("description") if isinstance(fm.get("description"), str) else ""
    signals = []
    for kind, phrase, pos in open_loop_hits(desc):
        signals.append({"kind": kind, "line": 1, "context": scrub(sentence_at(desc, pos))})
    loop_text = desc
    for n, line in enumerate(body.split("\n"), offset + 1):
        for kind, phrase, pos in open_loop_hits(line):
            signals.append({"kind": kind, "line": n, "context": scrub(sentence_at(line, pos))})
        if MEMORY_LOOP_EXTRA.search(line):
            loop_text += " " + line
    is_loop = any(s["kind"] in MEMORY_LOOP_KINDS for s in signals) or bool(MEMORY_LOOP_EXTRA.search(loop_text))
    when = modified or mtime
    return {"path": str(path), "file": path.name, "name": scrub(str(fm.get("name") or path.stem))[:120],
            "description": scrub(desc)[:600], "type": meta.get("type") or fm.get("type"),
            "origin_session": meta.get("originSessionId") or fm.get("originSessionId"),
            "modified": modified, "mtime": mtime, "day": util.day_of(when),
            "date_source": "modified" if modified else "mtime",
            "signals": signals[:20], "loop": is_loop,
            "not_pushed": any(s["kind"] == "not_pushed" for s in signals)}


def memory_notes(dirs: list[str]) -> list[dict]:
    out = []
    for d in dirs:
        try:
            files = sorted(Path(d).glob("*.md"))
        except OSError:
            continue
        index = memory_index(Path(d))
        for f in files:
            if f.name == "MEMORY.md":
                continue
            note = cached("memory", f, _parse_memory)
            if note:
                out.append(dict(note, dir=d, in_index=f.name in index))
    return out


def memory_index(d: Path) -> set[str]:
    """File names that MEMORY.md links to."""
    names: set[str] = set()
    for line in read_lines(d / "MEMORY.md"):
        for m in MD_LINK_RE.finditer(line):
            names.add(os.path.basename(m.group(2).split("#")[0]))
        for m in re.finditer(r"([\w.-]+\.md)\b", line):
            names.add(m.group(1))
    return names


def _enc(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path).lower()


def memory_dirs(in_scope_projects: set[tuple[str, str]]) -> list[str]:
    """Memory folders the secretary may read: those of projects with a session it reads, or whose
    folder name is exactly a configured root (a project started in the root itself)."""
    if not config.READ_MEMORY:
        return []
    roots = {_enc(r) for r in config.SESSIONS_ROOTS}
    out = []
    for d in config.CLAUDE_CONFIG_DIRS:
        base = os.path.join(d, "projects")
        try:
            slugs = sorted(os.listdir(base))
        except OSError:
            continue
        for slug in slugs:
            mem = os.path.join(base, slug, "memory")
            if not os.path.isdir(mem):
                continue
            if (d, slug) in in_scope_projects or re.sub(r"[^A-Za-z0-9]", "-", slug).lower() in roots:
                out.append(mem)
    return out
