"""The setup wizard's steps, as data both wizard UIs render (the web page and the terminal), plus what
they call: validate() per step, action() for the buttons, and apply() for Install.

model() reads this computer (cached) and changes nothing. Nothing is written before apply(). Secrets are
never returned: a secret field's value is {"set": bool, "masked": "sk-ant-oat…3f9a"}, and a client sends
the text only when the user changes it.
"""

from __future__ import annotations

import json
import os
import re
import time
import traceback
import urllib.error
import urllib.request

from . import compose, detect, doctor, settings, util

STEP_IDS = ["check", "you", "desk", "secretary", "sessions", "docs", "notify", "habits", "connect", "review", "done"]
PHASES = ["Save settings", "Prepare folders", "Build images", "Start Deskmate", "Connect Claude Code",
          "Working habits", "Health check"]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MODEL_RE = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]*$")
_TOKEN_RE = re.compile(r"^sk-ant-oat\d\d-[A-Za-z0-9_\-]{20,}$")
_GIT_URL_RE = re.compile(r"^(https?://|ssh://|git://|git@[^:\s]+:|file://)\S+$")
_URL_PATTERNS = {
    "discord": r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$",
    "slack": r"^https://hooks\.slack\.com/services/T\w+/B\w+/\w+$",
    "ntfy": r"^https?://[^/\s]+/[\w-]+/?$",
    "webhook": r"^https?://\S+$",
}
_KIND_LABELS = {"none": "Nowhere", "discord": "Discord", "slack": "Slack", "ntfy": "ntfy", "webhook": "Webhook"}


# ---------------------------------------------------------------- values


def _values(values: dict | None) -> dict:
    """The given answers over what is saved or detected. A secret is a string (new) or {"set", "masked"}."""
    return settings.merged(values)


def _secret_state(value, kind: str = "token") -> dict:
    if isinstance(value, str):
        return {"set": bool(value.strip()), "masked": util.mask(value, kind), "changed": True}
    if isinstance(value, dict):
        return {"set": bool(value.get("set")), "masked": str(value.get("masked") or "")}
    return {"set": False, "masked": ""}


def _secret_set(value) -> bool:
    return _secret_state(value)["set"]


def _secret_text(v: dict, key: str) -> str:
    """The secret's text: the new one if given, else the saved one (for checks, never for the model)."""
    val = v.get(key)
    if isinstance(val, str):
        return val.strip()
    return settings.read_secret(settings.SECRET_KEYS[key], v)


def _public(v: dict) -> dict:
    out = {}
    for k, val in v.items():
        s = settings.KEYS.get(k)
        if s is not None and s.secret:
            out[k] = _secret_state(val, "url" if k == "notify-url" else "token")
            out[k].pop("changed", None)
        elif isinstance(val, (str, int, float, bool)) or val is None:
            out[k] = val
    return out


def _typed(s, raw):
    """A value as the page wants it: bools as true/false, ints as numbers, lists as lists."""
    if s.secret:
        st = _secret_state(raw, "url" if s.key == "notify-url" else "token")
        st.pop("changed", None)
        return st
    if s.type == "bool":
        return util.is_on(raw)
    if s.type == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return raw
    if s.type in ("paths", "folders"):
        return util.split_list(raw or "")
    if s.type == "multi":
        return util.split_list(raw or "", ",")
    return "" if raw is None else str(raw)


class _Ctx:
    """What one model() call has detected, so each step reads it once."""

    def __init__(self, v: dict):
        self.v = v
        self.mode = "edit" if settings.installed() else "install"
        self.platform = detect.platform()
        self.docker = detect.docker()
        self.claude = detect.claude_cli()
        self.home = detect.home()
        self._defaults = None
        self._checks = None
        self._rows = None

    def default(self, key: str):
        if self._defaults is None:
            self._defaults = settings.detected_defaults(self.v)
        return self._defaults.get(key, settings.KEYS[key].default if key in settings.KEYS else "")

    def checks(self) -> list:
        if self._checks is None:
            self._checks = doctor.preinstall(self.v)
        return self._checks

    def session_rows(self) -> list:
        if self._rows is None:
            self._rows = detect.session_folders(util.split_list(self.v.get("CLAUDE_CONFIG_DIRS", "")))
        return self._rows

    def tilde(self, path: str) -> str:
        return util.tilde(path, self.home)

    @property
    def where(self) -> str:
        return "Mac" if self.platform["os"] == "macos" else "computer"


def _field(key: str, ctx: _Ctx, **extra) -> dict:
    s = settings.KEYS[key]
    f = {"key": key, "label": s.label, "type": s.type, "value": _typed(s, ctx.v.get(key)),
         "default": None if s.secret else _typed(s, ctx.default(key)),
         "choices": [dict(c) for c in s.choices] if s.choices else None, "help": s.help, "advanced": s.advanced,
         "error": None, "warning": None, "readonly": False}
    f.update(extra)
    return f


def _check(cid, label, status="ok", detail="", fix=""):
    return {"id": cid, "label": label, "status": status, "detail": detail, "fix": fix}


def _step(sid: str, title: str, intro: str = "") -> dict:
    return {"id": sid, "title": title, "summary": "", "status": "todo", "intro": intro, "fields": [], "checks": [],
            "info": {}}


def _status_from(errors: dict, warnings: dict, base: str = "ok") -> str:
    if errors:
        return "fail"
    if warnings:
        return "warn"
    return base


# ---------------------------------------------------------------- the steps


def _s_check(ctx: _Ctx) -> dict:
    s = _step("check", "Check", f"Setup looks before it asks anything. Nothing on this {ctx.where} changes until you "
                                 "press Install.")
    checks = list(ctx.checks())
    if ctx.mode == "edit" and ctx.docker["running"]:
        state = {c["service"]: c for c in compose.ps(ctx.v)}
        up = [n for n in ("desk", "hub") if state.get(n, {}).get("state") == "running"]
        if len(up) == 2:
            checks.append(_check("running", "The desk and the hub are running",
                                 detail=f"On 127.0.0.1:{ctx.v.get('HUB_PORT')}."))
        else:
            checks.append(_check("running", "Deskmate isn't running", "warn", "Install starts it again.", "./deskmate up"))
    bad = doctor.blocking(checks)
    warns = [c for c in checks if c["status"] == "warn"]
    s["checks"] = checks
    s["status"] = "fail" if bad else ("warn" if warns else "ok")
    s["summary"] = bad[0]["label"] if bad else (warns[0]["label"] if len(warns) == 1 else
                                                f"{len(warns)} need a look" if warns else "All good")
    s["info"] = {"blocking": [c["id"] for c in bad], "line": doctor.summary(checks)}
    if bad:
        s["info"]["footer"] = "You can carry on. Install waits until this is fixed."
    return s


def _s_you(ctx: _Ctx) -> dict:
    v = ctx.v
    s = _step("you", "You", "Agents and notifications use your name. The daily brief follows your time zone.")
    name = str(v.get("DESKMATE_OWNER") or "").strip()
    zones = detect.all_timezones()
    tz = str(v.get("TZ") or "")
    if zones:
        if tz and tz not in zones:
            zones = [tz] + zones
        tz_field = _field("TZ", ctx, choices=[{"value": z, "label": z.replace("_", " ")} for z in zones],
                          help=f"Detected from this {ctx.where}. " + settings.KEYS["TZ"].help)
    else:
        tz_field = _field("TZ", ctx, type="text", choices=None, help="An IANA name, like Europe/London. " +
                          settings.KEYS["TZ"].help)
    s["fields"] = [
        _field("DESKMATE_OWNER", ctx, help=f"Agents see it, as in “{name or 'Alex'} has the desk”, and so do "
                                           "notifications. Found in your git settings, else your account."),
        tz_field,
    ]
    s["summary"] = f"{name or 'No name'} · {tz}"
    return s


def _monitor_help(v: dict) -> str:
    try:
        n = int(v.get("DESK_MONITORS") or 2)
        w, h = (int(x) for x in str(v.get("DESK_MONITOR_SIZE") or "1280x800").split("x"))
        return f"The whole desk is {n * w} × {h}. Smaller monitors keep screenshots cheap for agents."
    except ValueError:
        return settings.KEYS["DESK_MONITOR_SIZE"].help


def _s_desk(ctx: _Ctx) -> dict:
    v = ctx.v
    s = _step("desk", "Desk", "The computer your Claude Code sessions share: Chromium and a terminal in a container. "
                              "You watch it live and can take over at any moment.")
    nets = detect.network_choices(ctx.docker, ctx.platform)
    size = str(v.get("DESK_MONITOR_SIZE") or "1280x800")
    size_choices = [dict(c) for c in settings.KEYS["DESK_MONITOR_SIZE"].choices]
    if size not in [c["value"] for c in size_choices]:
        size_choices.append({"value": size, "label": size.replace("x", " × ")})
    host = {"DESK_NETWORK": ["host"]}
    s["fields"] = [
        _field("DESK_MONITORS", ctx),
        _field("DESK_MONITOR_SIZE", ctx, choices=size_choices, help=_monitor_help(v)),
        _field("DESK_NETWORK", ctx, choices=nets),
        _field("HUB_PORT", ctx),
        _field("CDP_PORT", ctx, show_if=host),
        _field("DESK_DISPLAY", ctx, show_if=host),
        _field("DESKMATE_DATA_DIR", ctx),
        _field("COMPOSE_PROJECT_NAME", ctx),
    ]
    s["info"] = {"advanced_title": "Ports, data folder and project"}
    if v.get("DESK_NETWORK") == "host-access":
        s["checks"].append(_check("ipv6", "Start dev servers on 127.0.0.1", detail=(
            "The desk reaches your localhost through Docker Desktop, which may not reach a server that listens "
            "only on ::1. If a page doesn't load, start it with --host 127.0.0.1.")))
    if ctx.mode == "edit":
        s["checks"].append(_check("restart", "Changing the monitors, their size or what the desk can open restarts the desk.",
                                  detail="Open tabs close. Logins stay."))
    try:
        mons = int(v.get("DESK_MONITORS") or 2)
    except ValueError:
        mons = 2
    local = v.get("DESK_NETWORK") != "isolated"
    s["summary"] = (f"{mons} monitor{'s' if mons != 1 else ''} · {size.replace('x', '×')} · "
                    + ("opens localhost" if local else "internet only"))
    return s


def _s_secretary(ctx: _Ctx) -> dict:
    v = ctx.v
    s = _step("secretary", "Secretary", "It reads the sessions and notes you choose next, then writes a daily brief, a "
                                        "timeline and answers to your questions. It uses your own Claude login and backs "
                                        "off when your plan is busy.")
    on = util.is_on(v.get("SECRETARY"))
    tok = _secret_state(v.get("claude-token"))
    off_help = ("The secretary is off. Nothing is read and no model is called. Turn it on any time by running "
                "./deskmate setup again.")
    s["fields"] = [
        _field("SECRETARY", ctx, help=settings.KEYS["SECRETARY"].help if on else off_help),
        _field("claude-token", ctx, action="check_token", command="claude setup-token",
               show_if={"SECRETARY": [True, "on"]}),
        _field("SECRETARY_DIGEST_MODEL", ctx),
        _field("SECRETARY_BRIEF_MODEL", ctx),
        _field("SECRETARY_ASK_MODEL", ctx),
        _field("SECRETARY_PAUSE_AT", ctx),
        _field("SECRETARY_MAX_DIGESTS_PER_DAY", ctx),
        _field("BRIEF_AT", ctx),
        _field("MORNING_POST_AT", ctx),
    ]
    s["info"] = {"advanced_title": "Models, limits and times"}
    if not on:
        s["summary"] = "Off"
    elif tok["set"]:
        s["summary"] = f"On · login {tok['masked']}"
    else:
        s["summary"] = "On · no login yet"
    return s


def _folder_rows(ctx: _Ctx, picked: list) -> tuple:
    """Rows for the sessions step: each group (the folder directly under home, home itself, or a folder
    outside home), then its sub-folders. Returns (rows, total sessions, sessions inside the picked folders)."""
    rows, total, read = [], 0, 0
    data = ctx.session_rows()
    groups = detect.rank_roots(data)
    for r in data:
        total += r["sessions"]
        if any(r["path"] == p or r["path"].startswith(p.rstrip("/") + "/") for p in picked):
            read += r["sessions"]
    for g in groups:
        label = ctx.tilde(g["path"])
        row = {"path": g["path"], "label": label, "sessions": g["sessions"], "last": util.human_time(g["last_ts"]),
               "selected": g["path"] in picked, "depth": 0}
        if g["home"]:
            row["note"] = "your home folder: ticking it includes everything in it"
        rows.append(row)
        kids = {}
        for r in data:
            if r["parent"] != g["path"] or r["path"] == g["path"]:
                continue
            first = g["path"].rstrip("/") + "/" + r["path"][len(g["path"].rstrip("/")) + 1:].split("/")[0]
            k = kids.setdefault(first, [0, 0.0])
            k[0] += r["sessions"]
            k[1] = max(k[1], r["last_ts"])
        if g["home"]:
            continue  # home's own sub-folders are groups of their own
        for path, (n, last) in sorted(kids.items(), key=lambda kv: (-kv[1][0], kv[0])):
            rows.append({"path": path, "label": ctx.tilde(path), "sessions": n, "last": util.human_time(last),
                         "selected": path in picked, "depth": 1, "parent": g["path"],
                         "included": g["path"] in picked})
    known = {r["path"] for r in rows}
    for p in picked:
        if p not in known:
            rows.append({"path": p, "label": ctx.tilde(p), "sessions": 0, "last": "", "selected": True, "depth": 0,
                         "note": "no sessions yet" if os.path.isdir(p) else "this folder doesn't exist"})
    return rows, total, read


def _s_sessions(ctx: _Ctx) -> dict:
    v = ctx.v
    s = _step("sessions", "Sessions", f"Claude Code keeps every session on this {ctx.where}. Tick the folders the "
                                      "secretary may read. It only reads, and only what you tick.")
    dirs = detect.claude_config_dirs()
    picked = util.split_list(v.get("SESSIONS_ROOTS", ""))
    rows, total, read = _folder_rows(ctx, picked)
    cfg_now = util.split_list(v.get("CLAUDE_CONFIG_DIRS", ""))
    cfg_choices = [{"value": d["path"], "label": ctx.tilde(d["path"]),
                    "detail": (f"{d['sessions']} sessions" if d["has_projects"] else "no sessions yet")
                    + (f" · from {d['source']}" if d["source"] not in ("default",) else "")} for d in dirs]
    for p in cfg_now:
        if p not in [c["value"] for c in cfg_choices]:
            cfg_choices.append({"value": p, "label": ctx.tilde(p), "detail": ""})
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    where = (f"From CLAUDE_CONFIG_DIR ({ctx.tilde(env_dir)})." if env_dir else "From ~/.claude: CLAUDE_CONFIG_DIR isn't set.")
    s["fields"] = [
        _field("CLAUDE_CONFIG_DIRS", ctx, choices=cfg_choices, readonly=len(cfg_choices) <= 1,
               help=f"{where} Deskmate mounts only each folder's projects/, read-only."),
        _field("SESSIONS_ROOTS", ctx),
    ]
    stats = detect.session_stats(cfg_now)
    s["info"] = {"folders": rows, "total": total, "read": read, "skipped": stats["skipped"]}
    if not total:
        s["checks"].append(_check("none", f"Claude Code has no sessions in {', '.join(ctx.tilde(d) for d in cfg_now) or '~/.claude'} yet.",
                                  "warn", "That's fine. Pick the folder you work in: the secretary reads sessions there "
                                          "as they appear."))
    if stats["skipped"]:
        s["checks"].append(_check("skipped", f"{stats['skipped']} transcripts couldn't be read", "warn",
                                  "They were left out of the counts."))
    if not util.is_on(v.get("SECRETARY")):
        s.update(skip=True, summary="Skipped: the secretary is off")
        return s
    if not picked:
        s["summary"] = "No folder"
    else:
        names = ", ".join(ctx.tilde(p) for p in picked[:2]) + (f" and {len(picked) - 2} more" if len(picked) > 2 else "")
        s["summary"] = f"{names} · {read} of {total} sessions" if total else names
        s["info"]["line"] = (f"The secretary reads {read} of {total} sessions: everything started in "
                             f"{names} or below, including folders you make later.")
    return s


def _docs_layout(hub: dict) -> list:
    out = []
    if hub.get("changelog_entries"):
        out.append({"label": "Changelogs", "path": "changelog/<repo>/<date>.md",
                    "detail": f"{hub['changelog_entries']} entries in {hub['changelog_repos']} repos"})
    if hub.get("design_docs"):
        out.append({"label": "Design docs", "path": "design/<feature>/", "detail": f"{hub['design_docs']} folders"})
    if hub.get("project_indexes"):
        out.append({"label": "Project indexes", "path": "projects/<repo>/INDEX.md", "detail": f"{hub['project_indexes']} repos"})
    if hub.get("kb_notes"):
        out.append({"label": "Knowledge base", "path": "knowledge-base/", "detail": f"{hub['kb_notes']} notes"})
    return out


def _memory_notes(ctx: _Ctx, picked: list) -> int:
    n = 0
    for d in util.split_list(ctx.v.get("CLAUDE_CONFIG_DIRS", "")):
        try:
            folders = [f for f in os.scandir(os.path.join(d, "projects")) if f.is_dir()]
        except OSError:
            continue
        for f in folders:
            mem = os.path.join(f.path, "memory")
            if not os.path.isdir(mem):
                continue
            files = [x for x in os.listdir(mem) if x.endswith(".md")] if os.access(mem, os.R_OK) else []
            if not files:
                continue
            cwd = ""
            for t in os.listdir(f.path):
                if t.endswith(".jsonl"):
                    cwd = detect.first_cwd(os.path.join(f.path, t)) or ""
                    if cwd:
                        break
            if cwd and any(cwd == p or cwd.startswith(p.rstrip("/") + "/") for p in picked):
                n += len(files)
    return n


def _s_docs(ctx: _Ctx) -> dict:
    v = ctx.v
    s = _step("docs", "Notes & docs", "Optional. The more it can read, the more the board, open loops and Ask can tell "
                                      "you. Everything is read-only.")
    picked = util.split_list(v.get("SESSIONS_ROOTS", ""))
    hubs = detect.docs_hubs(picked)
    choices = [{"value": h["path"], "label": f"{h['label']} (docs hub)",
                "detail": "Found under your folders. Setup recognised its layout:", "layout": _docs_layout(h),
                "note": "The board, open loops and decisions are built from these, without a model."} for h in hubs]
    cur = str(v.get("DOCS_DIR") or "")
    if cur and cur not in [c["value"] for c in choices]:
        choices.append({"value": cur, "label": ctx.tilde(cur), "detail": "Your choice."})
    choices.append({"value": "", "label": "None", "detail": "The secretary reads sessions, memory notes and git history only."})
    found = detect.repos(picked) if picked else []
    names = sorted({r["name"] for r in found if not r["worktree_of"]})
    notes = _memory_notes(ctx, picked) if picked else 0
    s["fields"] = [
        _field("DOCS_DIR", ctx, choices=choices),
        _field("READ_MEMORY", ctx, help=settings.KEYS["READ_MEMORY"].help + (f" {notes} notes in your folders." if notes else "")),
        _field("READ_GIT", ctx, chips=names[:40], readonly=not names,
               help=settings.KEYS["READ_GIT"].help if names else "No repos in the ticked folders."),
    ]
    sources, missing = compose.mounts(v)
    s["info"] = {"mounts": [ctx.tilde(p) for p in sources], "repos": len(names), "memory_notes": notes,
                 "line": "Deskmate sees only these, read-only: " + (", ".join(ctx.tilde(p) for p in sources) or "nothing")}
    if not util.is_on(v.get("SECRETARY")):
        s.update(skip=True, summary="Skipped: the secretary is off")
        return s
    bits = (["docs hub"] if cur else []) + (["memory"] if util.is_on(v.get("READ_MEMORY")) else []) + \
           (["git"] if util.is_on(v.get("READ_GIT")) and names else [])
    s["summary"] = " · ".join(bits) or "Nothing extra"
    return s


def _s_notify(ctx: _Ctx) -> dict:
    v = ctx.v
    s = _step("notify", "Notifications", "Where Deskmate tells you that an agent needs you, and where the morning brief goes.")
    kind = v.get("NOTIFY_KIND") or "none"
    url = _secret_state(v.get("notify-url"), "url")
    s["fields"] = [
        _field("NOTIFY_KIND", ctx),
        _field("notify-url", ctx, action="test_notify", show_if={"NOTIFY_KIND": ["discord", "slack", "ntfy", "webhook"]}),
        _field("NOTIFY_URL_FILE", ctx),
    ]
    old = os.path.join(ctx.home, ".config", "claude-notify", "discord-webhook.url")
    if os.path.isfile(old) and os.path.normpath(str(v.get("NOTIFY_URL_FILE") or "")) != os.path.normpath(old):
        s["checks"].append(_check("found", f"Found a Discord webhook file: {ctx.tilde(old)}", detail=(
            "To use it, choose Discord and set the webhook file (Advanced) to it. Deskmate mounts it read-only "
            "and never copies it.")))
        s["info"]["found_file"] = old
    s["summary"] = _KIND_LABELS.get(kind, kind) + (f" · {url['masked']}" if kind != "none" and url["set"] else "")
    return s


def habits_values(v: dict) -> dict:
    """The answers as the habits part reads them: the global hub and agents choices become per-folder
    options (HABITS_FOLDER_OPTIONS); options the client sent per folder win."""
    out = {k: val for k, val in v.items() if not (k in settings.KEYS and settings.KEYS[k].secret)}
    folders = util.split_list(v.get("HABITS_FOLDERS", ""))
    hub = str(v.get("HABITS_HUB") or "auto")
    agents = str(v.get("HABITS_AGENTS") or "").strip()
    given = v.get("HABITS_FOLDER_OPTIONS")
    if isinstance(given, str):
        try:
            given = json.loads(given) if given.strip() else {}
        except ValueError:
            given = {}
    given = given if isinstance(given, dict) else {}
    opts = {}
    for f in folders:
        o = {}
        if hub == "existing":
            o.update(hub="existing", hub_path=os.path.expanduser(str(v.get("HABITS_HUB_PATH") or "").strip()))
        elif hub in ("new", "none"):
            o["hub"] = hub
        if agents.lower() == "none":
            o["agents"] = "none"
        elif agents:
            o.update(agents="git" if _GIT_URL_RE.match(agents) else "folder", agents_source=agents)
        o.update(given.get(f) or {})
        if o:
            opts[f] = o
    for f, o in given.items():
        opts.setdefault(f, o)
    out["HABITS_FOLDER_OPTIONS"] = opts
    return out


def _habits_detect(v: dict) -> dict:
    try:
        from . import habits
    except ImportError:
        return {"ok": False, "error": "This copy of Deskmate has no habits module.", "folders": []}
    try:
        return habits.detect(habits_values(v)) or {}
    except Exception as exc:  # noqa: BLE001 - detection must never break the wizard
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "folders": []}


def _s_habits(ctx: _Ctx) -> dict:
    v = ctx.v
    s = _step("habits", "Working habits", "Optional. Rules in CLAUDE.md, three skills and an end-of-turn check that keep "
                                          "changelogs and design docs up to date, for the folders you pick.")
    on = util.is_on(v.get("HABITS"))
    picked = util.split_list(v.get("HABITS_FOLDERS", ""))
    found = _habits_detect(v) if on and picked else {"folders": []}
    by_path = {f.get("path"): f for f in found.get("folders") or [] if isinstance(f, dict)}
    cands = [g["path"] for g in detect.rank_roots(ctx.session_rows()) if not g["home"]][:8]
    sessions = {g["path"]: g for g in detect.rank_roots(ctx.session_rows())}
    rows = []
    for p in picked + [c for c in cands if c not in picked]:
        f = by_path.get(p) or {}
        g = sessions.get(p) or {}
        hub = (f.get("hub") or {}) if f else {}
        if f:
            repos = len(f.get("repos") or [])
            hub_label = hub.get("label") if hub.get("path") else None
        else:
            repos = len([r for r in detect.repos([p], depth=2) if not r["worktree_of"]]) if os.path.isdir(p) else 0
            hubs = detect.docs_hubs([p]) if os.path.isdir(p) else []
            hub_label = ctx.tilde(hubs[0]["path"]) if hubs else None
        row = {"path": p, "label": ctx.tilde(p), "sessions": g.get("sessions", 0), "last": util.human_time(g.get("last_ts", 0)),
               "repos": repos, "hub": hub_label, "selected": p in picked}
        if f.get("block_reason"):
            row["note"] = f["block_reason"]
        if f.get("errors"):
            row["note"] = "; ".join(f["errors"])
        if hub.get("kind"):
            row["hub_kind"] = hub["kind"]
        rows.append(row)
    s["fields"] = [
        _field("HABITS", ctx, help="Off changes nothing in your folders. You can turn it on later with "
                                   "./deskmate habits apply."),
        _field("HABITS_FOLDERS", ctx, show_if={"HABITS": [True, "on"]}),
        _field("HABITS_HUB", ctx, show_if={"HABITS": [True, "on"]}),
        _field("HABITS_HUB_PATH", ctx, show_if={"HABITS_HUB": ["existing"]}),
        _field("HABITS_CHECK", ctx, show_if={"HABITS": [True, "on"]}),
        _field("HABITS_AGENTS", ctx),
        _field("HABITS_ALLOW_NOTIFY", ctx, show_if={"NOTIFY_KIND": ["discord", "slack", "ntfy", "webhook"]}),
        _field("HABITS_MEMORY_ON", ctx),
    ]
    s["info"] = {"folders": rows, "actions": [{"name": "preview_habits", "label": "Show the changes", "auto": True}],
                 "advanced_title": "Team agents and Claude Code settings"}
    if found.get("error"):
        s["checks"].append(_check("habits", "Couldn't look at the folders", "warn", found["error"]))
    if on:
        s["info"]["after"] = ("Start Claude Code in " + (ctx.tilde(picked[0]) if picked else "your folder") +
                              " for work across repos. Sessions started inside a repo get the same rules but keep their own memory.")
        s["summary"] = f"On · {len(picked)} folder{'s' if len(picked) != 1 else ''} · check {v.get('HABITS_CHECK') or 'remind'}"
    else:
        s["summary"] = "Off"
    return s


def _connect_conflicts(v: dict) -> list:
    try:
        from . import claude_connect

        return claude_connect.conflicts(v) or []
    except ImportError:
        return detect.conflicts([detect.default_config_dir()])
    except Exception:  # noqa: BLE001
        return []


def _s_connect(ctx: _Ctx) -> dict:
    v = ctx.v
    s = _step("connect", "Connect Claude Code", "What setup adds to Claude Code so that every session can use the desk. "
                                                "You see each change before it is made.")
    if not ctx.claude["installed"]:
        s["checks"].append(_check("claude", "Claude Code wasn't found, so this step is skipped.", "warn",
                                  "Install Claude Code, then run ./deskmate setup again (or ./deskmate connect). It opens "
                                  "with your answers and connects Claude Code."))
        s.update(skip=True, summary="Skipped: no Claude Code")
        return s
    found = _connect_conflicts(v)
    removable = [c for c in found if c.get("removable") and c.get("kind") != "legacy"]
    s["fields"] = [_field("CONNECT", ctx)]
    if removable:
        verbs = {"mcp": "Remove the {} MCP server", "skill": "Move out the {} skill", "hook": "Remove the {}"}
        s["fields"].append(_field("CONNECT_REMOVE", ctx, label=f"Found {len(removable)} thing{'s' if len(removable) != 1 else ''} "
                                                                "that compete with the desk",
                                  choices=[{"value": c["id"], "label": verbs.get(c["kind"], "Remove {}").format(c["name"]),
                                            "detail": f"{c['detail']} ({ctx.tilde(c['where'])})"} for c in removable],
                                  show_if={"CONNECT": [True, "on"]}))
    legacy = [c for c in found if c.get("kind") == "legacy"]
    if legacy:
        s["checks"].append(_check("legacy", "Replaces the old Deskmate connection", detail=(
            "The old MCP entry (with its token in Claude Code's config), the copied skill and the old hooks are replaced; "
            "a backup of each file is kept.")))
    for c in found:
        if c.get("kind") == "plugin":
            s["checks"].append(_check("plugin", f"The plugin {c['name']} also has browser tools", "warn", c["detail"]))
    s["info"] = {"actions": [{"name": "preview_connect", "label": "Show the changes", "auto": True}],
                 "conflicts": found, "config_dir": ctx.tilde(detect.default_config_dir()),
                 "footer": f"Applies to every Claude Code session you start, from {ctx.tilde(detect.default_config_dir())}. "
                           "Restart sessions that are already open to give them the desk."}
    if not util.is_on(v.get("CONNECT")):
        s["summary"] = "Off: Claude Code isn't connected"
    else:
        rm = util.split_list(v.get("CONNECT_REMOVE", ""), ",")
        s["summary"] = "Adds the tools, hooks and skill" + (f" · removes {len(rm)}" if rm else "")
    return s


def changes(v: dict) -> list:
    """Edit mode: every setting that differs from .env, as [{key, label, old, new, tag}]; secrets masked."""
    saved = settings.read_env()
    if not saved:
        return []
    out = []
    tags = {"DESK_MONITORS": "restarts the desk", "DESK_MONITOR_SIZE": "restarts the desk",
            "DESK_NETWORK": "restarts the desk and the hub", "HUB_PORT": "restarts the hub; reconnects Claude Code",
            "TZ": "restarts the desk and the hub", "DESKMATE_OWNER": "restarts the hub; new sessions only"}
    norm = settings.normalize(v)
    for s in settings.SETTINGS:
        if not s.env or s.key in settings.DERIVED:
            continue
        old = saved.get(s.key)
        new = norm.get(s.key)
        if old is None or new is None or old == new:
            continue
        out.append({"key": s.key, "label": s.label, "old": old, "new": new, "tag": tags.get(s.key, "restarts the hub")})
    for key, name in settings.SECRET_KEYS.items():
        val = v.get(key)
        if isinstance(val, str):
            kind = "url" if key == "notify-url" else "token"
            before = settings.secret_state(name, v)
            out.append({"key": key, "label": settings.KEYS[key].label, "old": before["masked"] or "none",
                        "new": util.mask(val, kind) or "none", "tag": "applies at once"})
    return out


def _s_review(ctx: _Ctx, steps: list) -> dict:
    v = ctx.v
    edit = ctx.mode == "edit"
    s = _step("review", "Review & apply" if edit else "Review & install",
              "Your current settings. Change any section; your changes are listed here before anything is applied."
              if edit else "Check your answers, then install. The first build takes 5 to 10 minutes; later ones take seconds.")
    rows = [{"step": st["id"], "title": st["title"], "summary": st["summary"], "status": st["status"],
             "skip": bool(st.get("skip"))} for st in steps if st["id"] not in ("check", "review", "done")]
    phases = [p for p in PHASES if p != "Working habits" or _habits_phase_needed(v)]
    repo = str(settings.repo_dir())
    s["info"] = {"repo": repo, "rows": rows, "phases": phases,
                 "files": [f".env in {ctx.tilde(repo)}: your settings, only you can read it",
                           f"compose.local.yaml in {ctx.tilde(repo)}: the folders the hub may read",
                           f"{ctx.tilde(str(settings.data_dir(v)))}: the data folder, with the secret files"]}
    if edit:
        s["info"]["changes"] = changes(v)
    check = steps[0]
    for c in check["checks"]:
        if c["id"] in check["info"].get("blocking", []):
            s["checks"].append(dict(c))
    if util.is_on(v.get("SECRETARY")) and not _secret_set(v.get("claude-token")):
        s["checks"].append(_check("login", "The secretary has no login yet.", "warn",
                                  "You can install anyway: it reads, but makes no model calls until you add one."))
    s["status"] = "fail" if check["info"].get("blocking") else "todo"
    s["summary"] = "Install waits for the problems in Check" if check["info"].get("blocking") else ""
    return s


def _s_done(ctx: _Ctx) -> dict:
    s = _step("done", "Done", f"Restart Claude Code sessions that were already open on this {ctx.where}, so they get the desk.")
    repo = ctx.tilde(str(settings.repo_dir()))
    s["info"] = {
        "heading": "Deskmate is running",
        "prompt": "Use the desk to open localhost:5173 and tell me what the page shows.",
        "where": [f"Settings: {repo}/.env (only you can read it)",
                  f"Generated: {repo}/compose.local.yaml (don't edit it)",
                  f"Data: {ctx.tilde(str(settings.data_dir(ctx.v)))} (the desk's logins are in a Docker volume)",
                  "Claude Code: the deskmate MCP server and the deskmate@deskmate plugin"],
        "commands": ["./deskmate setup", "./deskmate doctor", "./deskmate open", "./deskmate uninstall"],
    }
    return s


_BUILDERS = {"check": _s_check, "you": _s_you, "desk": _s_desk, "secretary": _s_secretary, "sessions": _s_sessions,
             "docs": _s_docs, "notify": _s_notify, "habits": _s_habits, "connect": _s_connect, "done": _s_done}


def _build(sid: str, ctx: _Ctx, steps: list) -> dict:
    try:
        s = _s_review(ctx, steps) if sid == "review" else _BUILDERS[sid](ctx)
    except Exception as exc:  # noqa: BLE001 - one broken step must not take the wizard down
        s = _step(sid, sid.capitalize())
        s["checks"].append(_check("error", "This step couldn't be prepared", "warn",
                                  f"{exc.__class__.__name__}: {util.scrub(str(exc))}"[:300]))
        s["info"]["trace"] = util.scrub(traceback.format_exc())[-2000:]
    if sid not in ("check", "review", "done"):
        errors, warnings = _errors(sid, ctx)
        for f in s["fields"]:
            f["error"] = errors.get(f["key"])
            f["warning"] = warnings.get(f["key"])
        if s.get("skip"):
            s["status"] = "ok"
        elif s["status"] == "todo":
            s["status"] = _status_from(errors, warnings, "ok")
        if s["status"] == "ok" and any(c["status"] == "warn" for c in s["checks"]) and not s.get("skip"):
            s["status"] = "warn"
    return s


def model(values: dict | None = None) -> dict:
    """The whole wizard: {version, mode, platform, steps, values}. Reads, never writes."""
    v = _values(values)
    ctx = _Ctx(v)
    steps = []
    for sid in STEP_IDS:
        steps.append(_build(sid, ctx, steps))
    return {"version": 1, "mode": ctx.mode, "platform": ctx.platform, "steps": steps, "values": _public(v),
            "repo": str(settings.repo_dir())}


# ---------------------------------------------------------------- validation


def token_problem(tok: str) -> str:
    tok = (tok or "").strip()
    if not tok:
        return "Paste the token first. It starts with sk-ant-oat01-."
    if tok.startswith("sk-ant-api"):
        return ("That is an API key, not your login. The secretary runs on your Claude plan, not on API billing. "
                "Run claude setup-token and paste what it prints.")
    if not _TOKEN_RE.match(tok):
        return ("This doesn't look like a whole token. A token from claude setup-token starts with sk-ant-oat01- and is "
                "one long line. Copy all of it, without spaces.")
    return ""


def url_problem(kind: str, url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "Paste a webhook URL, or choose Nowhere."
    if not re.match(_URL_PATTERNS.get(kind, _URL_PATTERNS["webhook"]), url):
        return {"discord": "That isn't a Discord webhook. Discord's start with https://discord.com/api/webhooks/.",
                "slack": "That isn't a Slack webhook. Slack's start with https://hooks.slack.com/services/.",
                "ntfy": "Paste the topic's URL, like https://ntfy.sh/your-topic."}.get(kind, "Use a full https:// URL.")
    return ""


def _port(v: dict, key: str):
    try:
        return int(str(v.get(key) or "").strip())
    except ValueError:
        return None


def _errors(sid: str, ctx: _Ctx) -> tuple:
    """({KEY: message}, {KEY: warning}) for one step."""
    v = ctx.v
    e, w = {}, {}
    on = util.is_on(v.get("SECRETARY"))
    if sid == "you":
        raw = str(v.get("DESKMATE_OWNER") or "")
        name = raw.strip()
        if not name:
            e["DESKMATE_OWNER"] = "Type the name agents should use, for example Alex."
        elif len(name) > 40:
            e["DESKMATE_OWNER"] = "Use 40 characters or fewer."
        elif re.search(r"[<>\x00-\x1f\x7f]", name) or "'" in name:
            e["DESKMATE_OWNER"] = "Leave out < > ' and line breaks."
        if not detect.valid_tz(str(v.get("TZ") or "")):
            e["TZ"] = "Pick a time zone from the list."
    elif sid == "desk":
        try:
            mons = int(str(v.get("DESK_MONITORS")))
            if not 1 <= mons <= 4:
                raise ValueError
        except ValueError:
            e["DESK_MONITORS"] = "Pick 1 to 4 monitors."
            mons = 1
        m = re.match(r"^(\d{3,4})x(\d{3,4})$", str(v.get("DESK_MONITOR_SIZE") or ""))
        if not m or not (800 <= int(m.group(1)) <= 3840 and 600 <= int(m.group(2)) <= 2160) \
                or int(m.group(1)) % 2 or int(m.group(2)) % 2:
            e["DESK_MONITOR_SIZE"] = "Use a size like 1280x800: width 800 to 3840, height 600 to 2160, even numbers."
        elif mons * int(m.group(1)) > 7680:
            e["DESK_MONITOR_SIZE"] = "That is wider than 7680 pixels in all. Choose fewer monitors or a smaller size."
        nets = {c["value"]: c for c in detect.network_choices(ctx.docker, ctx.platform)}
        net = v.get("DESK_NETWORK")
        if net not in nets:
            e["DESK_NETWORK"] = "Pick what the desk's browser can open."
        elif nets[net].get("disabled"):
            e["DESK_NETWORK"] = "This computer can't use that: " + nets[net]["detail"]
        host = net == "host"
        keys = ["HUB_PORT"] + (["CDP_PORT"] if host else [])
        for k in keys:
            p = _port(v, k)
            if p is None or not 1024 <= p <= 65535:
                e[k] = "Use ports from 1024 to 65535."
        if host and not e.get("HUB_PORT") and not e.get("CDP_PORT") and _port(v, "HUB_PORT") == _port(v, "CDP_PORT"):
            e["CDP_PORT"] = "The two ports must differ."
        if not any(k in e for k in keys):
            ours = doctor._ours(v)["ports"]
            taken = detect.ports([_port(v, k) for k in keys])
            for k in keys:
                info = taken.get(_port(v, k))
                if info and not info["free"] and _port(v, k) not in ours:
                    who = info["owner"] or "another program"
                    e[k] = f"Port {_port(v, k)} is taken by {who}. Pick another, like {detect.free_port(_port(v, k) + 10)}."
        if host:
            d = _port(v, "DESK_DISPLAY")
            if d is None or not 1 <= d <= 999:
                e["DESK_DISPLAY"] = "Use a display number from 1 to 999, like 87."
            elif ctx.platform["os"] in ("linux", "wsl") and d not in doctor._ours(v)["display"] and not detect.display_free(d):
                e["DESK_DISPLAY"] = f"Display :{d} is in use here. Pick another, like {detect.free_display(d + 1)}."
        problem = doctor.data_dir_problem(str(v.get("DESKMATE_DATA_DIR") or ""), ctx.platform)
        if problem:
            e["DESKMATE_DATA_DIR"] = problem
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,62}$", str(v.get("COMPOSE_PROJECT_NAME") or "")):
            e["COMPOSE_PROJECT_NAME"] = "Use lowercase letters, digits, - and _, starting with a letter or digit."
        else:
            other = detect.own_stack(settings.repo_dir(), v["COMPOSE_PROJECT_NAME"]).get("other_dir")
            if other:
                w["COMPOSE_PROJECT_NAME"] = (f"Deskmate '{v['COMPOSE_PROJECT_NAME']}' already runs from {ctx.tilde(other)}. "
                                             "Installing here replaces those containers; the desk's logins are kept.")
    elif sid == "secretary":
        tok = v.get("claude-token")
        if isinstance(tok, str) and tok.strip():
            msg = token_problem(tok)
            if msg:
                e["claude-token"] = msg
        elif on and not _secret_set(tok):
            w["claude-token"] = "No login yet: the secretary reads, but makes no model calls until it has one."
        for k in ("SECRETARY_DIGEST_MODEL", "SECRETARY_BRIEF_MODEL", "SECRETARY_ASK_MODEL"):
            if not _MODEL_RE.match(str(v.get(k) or "")):
                e[k] = "Pick a model from the list."
        try:
            pause = float(str(v.get("SECRETARY_PAUSE_AT")))
            if not 0.05 <= pause <= 1.0:
                raise ValueError
        except ValueError:
            e["SECRETARY_PAUSE_AT"] = "Use a share from 0.05 to 1, like 0.60."
        cap = _port(v, "SECRETARY_MAX_DIGESTS_PER_DAY")
        if cap is None or not 5 <= cap <= 200:
            e["SECRETARY_MAX_DIGESTS_PER_DAY"] = "Use a number from 5 to 200."
        for k in ("BRIEF_AT", "MORNING_POST_AT"):
            if not _TIME_RE.match(str(v.get(k) or "")):
                e[k] = "Use a time like 18:30."
    elif sid == "sessions" and on:
        dirs = util.split_list(v.get("CLAUDE_CONFIG_DIRS", ""))
        for d in dirs:
            if not os.path.isabs(d):
                e["CLAUDE_CONFIG_DIRS"] = "Use full paths, starting with /."
            elif not os.path.isdir(d):
                w["CLAUDE_CONFIG_DIRS"] = f"Claude Code hasn't run with {ctx.tilde(d)} yet; setup makes its projects/ folder."
        if not dirs:
            e["CLAUDE_CONFIG_DIRS"] = "Pick Claude Code's folder."
        roots = util.split_list(v.get("SESSIONS_ROOTS", ""))
        bad = [f"{ctx.tilde(r)}: {compose.collides(r)}" for r in roots if compose.collides(r)]
        if bad:
            e["SESSIONS_ROOTS"] = " ".join(bad)
        elif not roots:
            w["SESSIONS_ROOTS"] = "No folder ticked: the secretary reads only the docs folder."
        else:
            gone = [ctx.tilde(r) for r in roots if not os.path.isdir(r)]
            wide = [ctx.tilde(r) for r in roots for d in dirs if d == r or d.startswith(r.rstrip("/") + "/")]
            if gone:
                w["SESSIONS_ROOTS"] = f"{', '.join(gone)} doesn't exist; it is left out until it does."
            elif wide:
                w["SESSIONS_ROOTS"] = (f"{wide[0]} holds Claude Code's own folder, with your login. Deskmate refuses to "
                                       "read it, but narrower folders are safer.")
    elif sid == "docs" and on:
        d = str(v.get("DOCS_DIR") or "").strip()
        if d:
            if not os.path.isabs(d):
                e["DOCS_DIR"] = "Use a full path, starting with /."
            elif os.path.normpath(d) in (ctx.home, "/"):
                e["DOCS_DIR"] = "Pick the docs folder itself, not your whole home folder."
            elif compose.collides(d):
                e["DOCS_DIR"] = compose.collides(d)
            elif not os.path.isdir(d) or not os.access(d, os.R_OK | os.X_OK):
                e["DOCS_DIR"] = "The docs folder is missing or can't be read."
    elif sid == "notify":
        kind = v.get("NOTIFY_KIND") or "none"
        if kind not in _KIND_LABELS:
            e["NOTIFY_KIND"] = "Pick where notifications go."
        elif kind != "none":
            url = v.get("notify-url")
            if isinstance(url, str) and url.strip():
                msg = url_problem(kind, url)
                if msg:
                    e["notify-url"] = msg
            elif not _secret_set(url):
                f = str(v.get("NOTIFY_URL_FILE") or "")
                if not (f and os.path.isfile(f) and os.path.getsize(f) > 0):
                    e["notify-url"] = "Paste a webhook URL, or choose Nowhere."
        f = str(v.get("NOTIFY_URL_FILE") or "")
        default = str(settings.data_dir(v) / "secrets" / "notify-url")
        if f and os.path.normpath(f) != os.path.normpath(default):
            if not os.path.isabs(f):
                e["NOTIFY_URL_FILE"] = "Use a full path, starting with /."
            elif not os.path.isfile(f):
                e["NOTIFY_URL_FILE"] = "That file doesn't exist."
    elif sid == "habits" and util.is_on(v.get("HABITS")):
        folders = util.split_list(v.get("HABITS_FOLDERS", ""))
        if not folders:
            e["HABITS_FOLDERS"] = "Tick at least one folder, or turn working habits off."
        else:
            missing = [ctx.tilde(f) for f in folders if not os.path.isdir(f)]
            nested = [ctx.tilde(f) for f in folders for g in folders if f != g and f.startswith(g.rstrip("/") + "/")]
            if missing:
                e["HABITS_FOLDERS"] = f"{', '.join(missing)} is not a folder."
            elif nested:
                e["HABITS_FOLDERS"] = (f"{nested[0]} is inside another picked folder. Pick one of them: both CLAUDE.md "
                                       "files would load.")
        hub = v.get("HABITS_HUB") or "auto"
        if hub not in ("auto", "existing", "new", "none"):
            e["HABITS_HUB"] = "Pick a docs hub option."
        elif hub == "existing":
            hp = os.path.expanduser(str(v.get("HABITS_HUB_PATH") or "").strip())
            if not hp or not os.path.isabs(hp):
                e["HABITS_HUB_PATH"] = "Type the full path of your docs hub, or choose another option."
            elif not os.path.isdir(hp):
                e["HABITS_HUB_PATH"] = "That folder doesn't exist."
        agents = str(v.get("HABITS_AGENTS") or "").strip()
        if agents and agents.lower() != "none" and not _GIT_URL_RE.match(agents) and not os.path.isdir(agents):
            e["HABITS_AGENTS"] = "Use a folder that exists, a git URL, or leave it empty."
        if v.get("HABITS_CHECK") not in ("off", "remind", "require"):
            e["HABITS_CHECK"] = "Pick off, remind or require."
        if not e and folders:
            found = _habits_detect(v)
            errs = [f"{ctx.tilde(f.get('path', ''))}: {x}" for f in found.get("folders") or [] if isinstance(f, dict)
                    for x in f.get("errors") or []]
            if errs:
                e["HABITS_FOLDERS"] = "; ".join(errs)
    elif sid == "connect":
        chosen = util.split_list(v.get("CONNECT_REMOVE", ""), ",")
        if chosen and util.is_on(v.get("CONNECT")):
            known = {c["id"] for c in _connect_conflicts(v)}
            gone = [c for c in chosen if c not in known]
            if gone:
                w["CONNECT_REMOVE"] = "Some of these are already gone: " + ", ".join(gone)
    return e, w


def validate(step_id: str, values: dict) -> dict:
    """{"errors": {KEY: message}, "warnings": {KEY: message}, "step": the step, recomputed}. Review checks every step."""
    v = _values(values)
    ctx = _Ctx(v)
    if step_id == "review":
        errors, warnings = {}, {}
        for sid in STEP_IDS[1:-2]:
            e, w = _errors(sid, ctx)
            for k, msg in e.items():
                errors.setdefault(k, msg)
            for k, msg in w.items():
                warnings.setdefault(k, msg)
        steps = [_build(sid, ctx, []) for sid in ("check",)]
        step = _build("review", ctx, steps)
        return {"errors": errors, "warnings": warnings, "step": step}
    if step_id not in STEP_IDS:
        return {"errors": {"step": f"Unknown step {step_id!r}"}, "warnings": {}, "step": None}
    errors, warnings = _errors(step_id, ctx)
    return {"errors": errors, "warnings": warnings, "step": _build(step_id, ctx, [])}


# ---------------------------------------------------------------- actions


def _post_json(url: str, payload, headers: dict | None = None, timeout: float = 10.0, raw: bytes | None = None):
    """(status, headers, body) of a POST; status 0 and the reason when it never got an answer."""
    data = raw if raw is not None else json.dumps(payload).encode()
    h = {"Content-Type": "application/json", "User-Agent": "deskmate-setup"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(20_000)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(20_000)
        except Exception:  # noqa: BLE001
            body = b""
        return exc.code, dict(exc.headers or {}), body
    except Exception as exc:  # noqa: BLE001 - offline, DNS, TLS: no answer at all
        return 0, {}, str(getattr(exc, "reason", exc)).encode()


def check_token(tok: str) -> dict:
    """The same probe Fleet uses: one Haiku token, read for the plan's usage headers."""
    msg = token_problem(tok)
    if msg:
        return {"ok": False, "message": msg, "data": {}}
    status, headers, body = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"model": "claude-haiku-4-5-20251001", "max_tokens": 1, "messages": [{"role": "user", "content": "quota"}]},
        {"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20", "anthropic-version": "2023-06-01"},
        timeout=15)
    low = {k.lower(): v for k, v in headers.items()}

    def pct(key):
        try:
            return float(low.get(f"anthropic-ratelimit-unified-{key}-utilization"))
        except (TypeError, ValueError):
            return None

    five, seven = pct("5h"), pct("7d")
    usage = ""
    if five is not None:
        usage = f" Your 5-hour window is {round(five * 100)}% used" + (
            f", the 7-day window {round(seven * 100)}%." if seven is not None else ".")
    if status == 200 or (status == 429 and five is not None):
        head = "**Works.**" if status == 200 else "**Works, but your plan is busy right now.**"
        return {"ok": True, "message": head + usage, "data": {"five_hour": five, "seven_day": seven}}
    if status == 401:
        return {"ok": False, "message": "Claude refused this token (401). It was revoked or copied wrongly. Run claude "
                                        "setup-token again and paste the new token.", "data": {}}
    if status == 403:
        return {"ok": False, "message": "This login has no Claude plan that allows this (403).", "data": {}}
    if status == 429:
        return {"ok": False, "message": "Claude is busy. Try again in a minute.", "data": {"keep": True}}
    if status == 0:
        return {"ok": False, "message": "Couldn't reach Claude to check it (" + body.decode("utf-8", "replace")[:120] +
                                        "). It is kept, marked as not checked.", "data": {"keep": True, "checked": False}}
    return {"ok": False, "message": f"Claude answered {status}. Try again in a minute.", "data": {"keep": True}}


def test_notify(kind: str, url: str, owner: str) -> dict:
    """Post one harmless line, with no paths and no transcript text."""
    msg = url_problem(kind, url)
    if msg:
        return {"ok": False, "message": msg, "data": {}}
    text = f"Deskmate test from {owner}'s {detect.machine_name()}"
    if kind == "discord":
        status, _, body = _post_json(url, {"content": text, "allowed_mentions": {"parse": []}})
    elif kind == "slack":
        status, _, body = _post_json(url, {"text": text})
    elif kind == "ntfy":
        status, _, body = _post_json(url, None, {"Content-Type": "text/plain; charset=utf-8", "Title": "Deskmate"},
                                     raw=text.encode())
    else:
        status, _, body = _post_json(url, {"text": text, "source": "deskmate"})
    where = {"discord": "Discord channel", "slack": "Slack channel", "ntfy": "ntfy app"}.get(kind, "webhook's target")
    if 200 <= status < 300:
        return {"ok": True, "message": f"**Sent.** Look in your {where} for “{text}”.", "data": {}}
    text_body = body.decode("utf-8", "replace")[:200]
    if kind == "discord" and status == 404:
        m = "Discord says this webhook doesn't exist (404). It was probably deleted. Make a new one in the channel's settings."
    elif kind == "discord" and status == 401:
        m = "Discord refused it (401): the token part of the URL is wrong. Copy the whole webhook URL again."
    elif kind == "slack" and status in (403, 404, 410):
        m = {403: "Slack says the token is invalid (403).", 404: "Slack has no such webhook (404).",
             410: "Slack says the channel is archived (410)."}[status]
    elif status == 429:
        m = "Too many messages. Try again in a few seconds."
    elif status == 0:
        return {"ok": False, "message": f"Couldn't reach it ({text_body[:120]}). It is kept, not tested.", "data": {"keep": True}}
    else:
        m = f"It answered {status}."
    return {"ok": False, "message": m, "data": {}}


def action(name: str, values: dict) -> dict:
    """recheck | check_token | test_notify | preview_connect | preview_habits → {ok, message, data}."""
    v = _values(values)
    if name == "recheck":
        detect.clear_cache()
        m = model(values)
        return {"ok": True, "message": "Checked just now.", "data": {"model": m, "checks": m["steps"][0]["checks"],
                                                                   "step": m["steps"][0]}}
    if name == "check_token":
        tok = _secret_text(v, "claude-token")
        if not tok:
            return {"ok": False, "message": "Paste the token first. It starts with sk-ant-oat01-.", "data": {}}
        return check_token(tok)
    if name == "test_notify":
        kind = v.get("NOTIFY_KIND") or "none"
        url = _secret_text(v, "notify-url")
        if kind == "none":
            kind = settings.kind_from_url(url)
        if not url:
            return {"ok": False, "message": "Paste a webhook URL first.", "data": {}}
        return test_notify(kind, url, str(v.get("DESKMATE_OWNER") or "the user"))
    if name == "preview_connect":
        try:
            from . import claude_connect
        except ImportError:
            return {"ok": False, "message": "This copy of Deskmate can't connect Claude Code (no claude_connect module).", "data": {}}
        try:
            data = claude_connect.preview(v) or {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": util.scrub(f"{exc.__class__.__name__}: {exc}"), "data": {}}
        return {"ok": not data.get("error"), "message": data.get("error") or "", "data": data}
    if name == "preview_habits":
        if not util.is_on(v.get("HABITS")):
            return {"ok": True, "message": "Working habits are off: nothing changes.", "data": {"changes": []}}
        try:
            from . import habits
        except ImportError:
            return {"ok": False, "message": "This copy of Deskmate has no habits module.", "data": {}}
        try:
            data = habits.preview(habits_values(v)) or {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": util.scrub(f"{exc.__class__.__name__}: {exc}"), "data": {}}
        data = dict(data)
        data["diffs"] = [{"path": c.get("where"), "what": c.get("what"), "diff": c["diff"]}
                         for c in data.get("changes") or [] if c.get("diff")]
        data["settings"] = [c for c in data.get("changes") or [] if not c.get("diff")]
        errs = data.get("errors") or []
        return {"ok": not errs, "message": "; ".join(errs), "data": data}
    return {"ok": False, "message": f"Unknown action {name}.", "data": {}}


# ---------------------------------------------------------------- install


def _habits_phase_needed(v: dict) -> bool:
    """On, or switched off after being on (the habits part then takes its changes back)."""
    if util.is_on(v.get("HABITS")):
        return True
    return util.is_on(settings.read_env().get("HABITS", "off"))


def _hint(lines: list, phase: str) -> str:
    text = "\n".join(lines[-40:]).lower()
    if "no space left" in text:
        return "The disk is full. Free some space (docker system prune), then Retry."
    if re.search(r"timeout|timed out|temporary failure in name resolution|could not resolve|connection reset|tls handshake", text):
        return ("The build stopped: a download failed or timed out. Check your internet connection, then Retry. "
                "Finished steps are kept, so the retry is quick.")
    if "address already in use" in text or "port is already allocated" in text:
        return "A port is taken now. Pick another on the Desk step (Advanced), then Retry."
    if "permission denied" in text and "docker" in text:
        return "Docker refused: your user needs to be in the docker group (sudo usermod -aG docker $USER, then log in again)."
    if "bind source path does not exist" in text:
        return "A folder Deskmate mounts is missing. Run ./deskmate doctor to see which, then Retry."
    if phase == "Build images":
        return "The build stopped. The log above shows why; Retry when it is fixed. Finished steps are kept, so the retry is quick."
    return "The log above shows why. Retry when it is fixed."


def apply(values: dict, emit, start: str | None = None, until: str | None = None) -> dict:
    """Install, or apply changes: phases in order, each safe to run again. `start` resumes at a phase;
    `until` stops after one (the CLI's --dry-run stops after "Prepare folders").
    Returns {ok, signin_url, failed_phase, hint}."""
    compose.reset_cancel()
    v = _values(values)
    known = [val for val in (v.get("claude-token"), v.get("notify-url")) if isinstance(val, str) and val.strip()]
    count = [0]

    def send(phase: str, level: str, text: str) -> None:
        count[0] += 1
        secrets = known + settings.all_secret_values(v)
        emit({"phase": phase, "level": level if level in ("info", "ok", "warn", "error") else "info",
              "text": util.scrub(str(text), secrets), "n": count[0], "ts": time.time()})

    phases = [p for p in PHASES if p != "Working habits" or _habits_phase_needed(v)]
    begin = phases.index(start) if start in phases else 0
    for phase in phases[begin:]:
        say = (lambda p: lambda level, text: send(p, level, text))(phase)
        try:
            ok, hint = _RUNNERS[phase](v, say)
        except Exception as exc:  # noqa: BLE001 - a crash in one phase is that phase failing, with its reason
            say("error", f"{exc.__class__.__name__}: {exc}")
            ok, hint = False, "Setup stopped on an unexpected error. Retry runs this phase again."
        if compose._CURRENT.get("cancelled"):
            return {"ok": False, "signin_url": "", "failed_phase": phase, "hint": "Stopped. Retry carries on from here."}
        if not ok:
            return {"ok": False, "signin_url": "", "failed_phase": phase, "hint": hint}
        if phase == until:
            return {"ok": True, "signin_url": "", "failed_phase": None, "hint": "", "stopped_after": phase}
    return {"ok": True, "signin_url": compose.signin_url(v), "failed_phase": None, "hint": ""}


def cancel() -> None:
    """Stop a running apply() at the next line of docker output (the web wizard's Stop)."""
    compose.cancel()


def _phase_save(v: dict, say) -> tuple:
    detect.clear_cache()
    checks = doctor.preinstall(v)
    bad = doctor.blocking(checks)
    if bad:
        for c in bad:
            say("error", c["label"] + (f": {c['fix']}" if c.get("fix") else ""))
        return False, f"{bad[0]['label']}. Fix it, then Retry. Nothing was changed."
    ctx = _Ctx(v)
    problems = []
    for sid in STEP_IDS[1:-2]:
        e, _ = _errors(sid, ctx)
        problems += [f"{settings.KEYS[k].label if k in settings.KEYS else k}: {m}" for k, m in e.items()]
    if problems:
        for p in problems:
            say("error", p)
        return False, "Some answers need a fix first. Nothing was changed."
    for line in settings.migrate_legacy():
        say("info", line)
    settings.save(v)
    say("ok", "Saved your settings in .env (only you can read it) and compose.local.yaml")
    return True, ""


def _phase_prepare(v: dict, say) -> tuple:
    ok = compose.prepare(settings.merged(_plain(v)), lambda ev: say(ev["level"], ev["text"]))
    return ok, "" if ok else "A file Deskmate needs is missing. Fix it as the log says, then Retry."


def _plain(v: dict) -> dict:
    """The answers without any secret (new or saved): what the other parts and doctor get."""
    return {k: val for k, val in v.items()
            if not isinstance(val, dict) and not (k in settings.KEYS and settings.KEYS[k].secret)}


def _compose_step(v: dict, say, args: list, phase: str) -> tuple:
    lines = []

    def out(ev):
        lines.append(ev["text"])
        say("error" if ev["level"] == "error" else "info", ev["text"])

    code = compose.run(args, out, phase=phase, values=_plain(v))
    return code, lines


def _phase_build(v: dict, say) -> tuple:
    say("info", "Building the desk and the hub. The first time takes 5 to 10 minutes and about 2.6 GB.")
    code, lines = _compose_step(v, say, ["build"], "Build images")
    if code != 0:
        return False, _hint(lines, "Build images")
    say("ok", "The desk and the hub are built")
    return True, ""


def _phase_start(v: dict, say) -> tuple:
    code, lines = _compose_step(v, say, ["up", "-d", "--remove-orphans"], "Start Deskmate")
    if code != 0:
        return False, _hint(lines, "Start Deskmate")
    port = v.get("HUB_PORT") or "7800"
    if not compose.wait_for_hub(port, 90, lambda ev: say(ev["level"], ev["text"])):
        return False, f"The hub didn't answer on 127.0.0.1:{port} within 90 seconds. ./deskmate logs hub shows why; then Retry."
    say("ok", f"Deskmate is up on http://127.0.0.1:{port}")
    return True, ""


def _phase_connect(v: dict, say) -> tuple:
    if not util.is_on(v.get("CONNECT", "on")):
        say("info", "Skipped: you chose not to connect Claude Code. ./deskmate connect does it later.")
        return True, ""
    if not detect.claude_cli()["installed"]:
        say("warn", "Claude Code wasn't found, so it isn't connected. Install it, then run ./deskmate connect.")
        return True, ""
    try:
        from . import claude_connect
    except ImportError:
        say("warn", "This copy of Deskmate can't connect Claude Code (no claude_connect module).")
        return True, ""
    res = claude_connect.connect(_plain(v), lambda ev: say(ev.get("level", "info"), ev.get("text", "")))
    if isinstance(res, dict) and res.get("ok") is False:
        return False, (res.get("error") or "Connecting Claude Code failed.") + " Deskmate keeps running; Retry, or ./deskmate connect later."
    return True, ""


def _phase_habits(v: dict, say) -> tuple:
    try:
        from . import habits
    except ImportError:
        say("warn", "This copy of Deskmate has no habits module; skipped.")
        return True, ""
    res = habits.apply(habits_values(_plain(v)), lambda ev: say(ev.get("level", "info"), ev.get("text", "")))
    if isinstance(res, dict) and res.get("ok") is False:
        errs = res.get("errors") or []
        return False, ("; ".join(str(e) for e in errs[:3]) or "Setting up the working habits failed.") + \
            " Deskmate keeps running; Retry, or ./deskmate habits apply later."
    return True, ""


def _phase_health(v: dict, say) -> tuple:
    detect.clear_cache()
    checks = doctor.run(_plain(v))
    for c in checks:
        level = {"ok": "ok", "warn": "warn"}.get(c["status"], "error")
        text = c["label"] + (f". {c['detail']}" if c["status"] != "ok" and c.get("detail") else "")
        text += f" → {c['fix']}" if c["status"] != "ok" and c.get("fix") else ""
        say(level, text)
    say("info", doctor.summary(checks))
    fails = [c for c in checks if c["status"] == "fail"]
    if fails:
        return False, "Deskmate runs, but a check failed: " + fails[0]["label"] + (f". {fails[0]['fix']}" if fails[0].get("fix") else "")
    return True, ""


_RUNNERS = {"Save settings": _phase_save, "Prepare folders": _phase_prepare, "Build images": _phase_build,
            "Start Deskmate": _phase_start, "Connect Claude Code": _phase_connect, "Working habits": _phase_habits,
            "Health check": _phase_health}
