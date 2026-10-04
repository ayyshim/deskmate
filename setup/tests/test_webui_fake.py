"""A stand-in for steps.py that the web wizard's tests and demos run against.

FakeSteps returns the step model (build contract §8) for a fictional colleague, Alex, on a Mac, or on
Linux with platform="linux", and can play the problems the wizard must show: Docker not running, a port
taken, no Claude Code, no sessions yet, a revoked token, an API key pasted instead of a login, a deleted
webhook and a build that fails once. apply() emits events over a few seconds like the real one.

The data is made up. Nothing here reads this computer.
"""

from __future__ import annotations

import copy
import re
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STEP_IDS = ["check", "you", "desk", "secretary", "sessions", "docs", "notify", "habits", "connect", "review", "done"]
FIELD_TYPES = {"text", "int", "bool", "choice", "multi", "path", "paths", "secret", "time", "model", "folders"}
FAKE_HUB_TOKEN = "fake-hub-token-0123456789abcdefghij"
GOOD_TOKEN = "sk-ant-oat01-" + "Fake" * 10 + "Qx7A"
REVOKED_TOKEN = "sk-ant-oat01-" + "Fake" * 9 + "revokedZp2B"
API_KEY = "sk-ant-api03-" + "Fake" * 10
DISCORD_URL = "https://discord.com/api/webhooks/130112345678901234/" + "FakeWebhookSecret" * 3
DELETED_DISCORD_URL = "https://discord.com/api/webhooks/130199999999999999/" + "deletedFakeSecret" * 3
PHASES = ["Save settings", "Prepare folders", "Build images", "Start Deskmate", "Connect Claude Code",
          "Working habits", "Health check"]
ZONES = ["UTC", "Europe/London", "Europe/Berlin", "Europe/Madrid", "Europe/Warsaw", "Africa/Lagos", "Africa/Nairobi",
         "Asia/Dubai", "Asia/Kolkata", "Asia/Kathmandu", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney",
         "Pacific/Auckland", "America/Sao_Paulo", "America/New_York", "America/Chicago", "America/Denver",
         "America/Los_Angeles"]
MODELS = [("claude-haiku-4-5-20251001", "Haiku 4.5"), ("claude-sonnet-5-5", "Sonnet 5.5"), ("claude-opus-5-5", "Opus 5.5")]


def _on(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v if v is not None else "").strip().lower() in ("on", "1", "true", "yes")


def _secret_set(v) -> bool:
    return bool(v.get("set")) if isinstance(v, dict) else bool(str(v or "").strip())


def _mask(v: str) -> str:
    v = v.strip()
    if v.startswith("http"):
        m = re.match(r"(https?://[^/]+/(?:api/webhooks/|services/)?)([^/]{0,4})", v)
        return (m.group(1) + m.group(2) if m else "https://…") + "…/••••••••"
    return (v[:10] if v.startswith("sk-ant-") else "") + "…" + v[-4:]


def _secret_view(v) -> dict:
    if isinstance(v, dict):
        return {"set": bool(v.get("set")), "masked": str(v.get("masked") or "")}
    v = str(v or "").strip()
    return {"set": bool(v), "masked": _mask(v) if v else ""}


def _list(v) -> list:
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [x for x in str(v or "").split(":") if x.strip()]


def F(key, label, type_, value, **kw) -> dict:
    field = {"key": key, "label": label, "type": type_, "value": value, "default": kw.pop("default", value),
             "choices": kw.pop("choices", None), "help": kw.pop("help", ""), "advanced": kw.pop("advanced", False),
             "error": kw.pop("error", None), "readonly": kw.pop("readonly", False)}
    field.update(kw)
    return field


def C(value, label, detail="", **kw) -> dict:
    choice = {"value": str(value), "label": label, "detail": detail}
    choice.update(kw)
    return choice


def check(cid, label, status="ok", detail="", fix="") -> dict:
    return {"id": cid, "label": label, "status": status, "detail": detail, "fix": fix}


class FakeSteps:
    """steps.model / validate / action / apply for Alex. `problems` is a set of: docker, port, claude, nosessions."""

    def __init__(self, platform: str = "macos", mode: str = "install", problems=(), fail_once: bool = False,
                 speed: float = 0.03, leak: bool = False):
        self.platform = platform
        self.mode = mode
        self.problems = set(problems)
        self.fail_once = fail_once
        self.speed = speed
        self.leak = leak  # apply() logs the token by mistake, to prove the server masks it
        self.applied = []  # every values dict apply() received
        self.actions = []  # (name, values) for every action call
        self.lock = threading.Lock()
        self.mac = platform == "macos"
        self.home = "/Users/alex" if self.mac else "/home/alex"
        self.repo = self.home + "/work/deskmate"

    # ------------------------------------------------------------ values

    def defaults(self) -> dict:
        h = self.home
        v = {
            "DESKMATE_OWNER": "Alex", "TZ": "Europe/London", "DESKMATE_DATA_DIR": h + "/.local/share/deskmate",
            "HOST_HOME": h, "HOST_UID": 501 if self.mac else 1000, "HOST_GID": 20 if self.mac else 1000,
            "COMPOSE_PROJECT_NAME": "deskmate",
            "COMPOSE_FILE": "compose.yaml:deploy/net-%s.yaml:compose.local.yaml" % ("host-access" if self.mac else "host"),
            "COMPOSE_PATH_SEPARATOR": ":", "DESK_NETWORK": "host-access" if self.mac else "host",
            "HUB_PORT": 7810 if "port" in self.problems else 7800, "CDP_PORT": 7802, "DESK_DISPLAY": 87,
            "DESK_MONITORS": 2, "DESK_MONITOR_SIZE": "1280x800", "CLAUDE_CONFIG_DIRS": h + "/.claude",
            "SESSIONS_ROOTS": h + "/work", "DOCS_DIR": h + "/work/acme/handbook", "READ_MEMORY": "on", "READ_GIT": "on",
            "SECRETARY": "on", "SECRETARY_DIGEST_MODEL": MODELS[0][0], "SECRETARY_BRIEF_MODEL": MODELS[1][0],
            "SECRETARY_ASK_MODEL": MODELS[1][0], "SECRETARY_PAUSE_AT": "0.60", "SECRETARY_MAX_DIGESTS_PER_DAY": 40,
            "BRIEF_AT": "18:30", "MORNING_POST_AT": "09:00", "NOTIFY_KIND": "none",
            "NOTIFY_URL_FILE": h + "/.local/share/deskmate/secrets/notify-url", "HABITS": "off",
            "HABITS_FOLDERS": h + "/work/acme", "HABITS_HUB": "auto", "HABITS_HUB_PATH": "", "HABITS_AGENTS": "",
            "HABITS_CHECK": "remind", "HABITS_ALLOW_NOTIFY": False, "HABITS_MEMORY_ON": False, "CONNECT": True,
            "CONNECT_REMOVE": [],
            "claude-token": {"set": False, "masked": ""}, "notify-url": {"set": False, "masked": ""},
        }
        if self.mode == "edit":
            v.update({"claude-token": {"set": True, "masked": "sk-ant-oat…Qx7A"}, "NOTIFY_KIND": "discord",
                      "notify-url": {"set": True, "masked": "https://discord.com/api/webhooks/1301…/••••••••"}})
        if "nosessions" in self.problems:
            v["SESSIONS_ROOTS"] = h + "/work"
        return v

    def _values(self, values) -> dict:
        v = self.defaults()
        for k, x in (values or {}).items():
            v[k] = x
        return v

    # ------------------------------------------------------------ the model

    def model(self, values=None) -> dict:
        v = self._values(values)
        steps = [self._step(sid, v) for sid in STEP_IDS]
        out_values = dict(v)
        for k in ("claude-token", "notify-url"):
            out_values[k] = _secret_view(v[k])
        return {"version": 1, "mode": self.mode, "platform": self._platform(), "steps": steps, "values": out_values,
                "repo": self.repo}

    def _platform(self) -> dict:
        if self.mac:
            return {"os": "macos", "arch": "arm64", "label": "macOS 26.1 on Apple Silicon", "python": "3.9.6"}
        return {"os": "linux", "arch": "x86_64", "label": "Ubuntu 24.04 on x86-64", "python": "3.12.3"}

    def _step(self, sid: str, v: dict) -> dict:
        build = getattr(self, "_s_" + sid)
        step = {"id": sid, "title": "", "summary": "", "status": "todo", "intro": "", "fields": [], "checks": [], "info": {}}
        build(step, v)
        for f in step["fields"]:
            if f["type"] == "secret":
                f["value"] = _secret_view(v.get(f["key"]))
                f["default"] = {"set": False, "masked": ""}
            elif f["key"] in v:
                f["value"] = v[f["key"]]
        return step

    def _s_check(self, s, v):
        s.update(title="Check", intro="Setup looks before it asks anything. Nothing on this %s changes until you press Install."
                 % ("Mac" if self.mac else "computer"))
        p, mac = self.problems, self.mac
        if self.mode == "edit":
            s["checks"] = [
                check("installed", "Deskmate is installed", detail="Since Friday 2 October, from `%s`." % self.repo),
                check("running", "The desk and the hub are running", detail="Up 2 days, on 127.0.0.1:%s. They start with Docker." % v["HUB_PORT"]),
                check("connected", "Claude Code is connected", detail="The `deskmate` MCP server and plugin are on, with the hooks and the skill."),
                check("docker", "Docker Desktop 4.61 is running" if mac else "Docker Engine 29.7.2 is running",
                      detail="8 GB of memory · 209 GB free on this disk."),
                check("doctor", "Last health check: all good", detail="`./deskmate doctor` at 14:20 today. 9 checks passed."),
            ]
            s.update(status="ok", summary="Installed and running")
            return
        c = [check("platform", self._platform()["label"],
                   detail="32 GB of memory. Python 3.9.6 runs setup. The images are built for arm64." if mac
                   else "16 GB of memory. Python 3.12.3 runs setup. The images are built for x86-64.")]
        if "docker" in p:
            c.append(check("docker", "Docker Desktop isn't running" if mac else "Docker isn't running", "fail",
                           "It is installed, but its engine is stopped." if mac else "The Docker service is stopped.",
                           "Start Docker Desktop, or run `open -a Docker`, then press Check again." if mac
                           else "Start it with `sudo systemctl start docker`, then press Check again. Setup never runs sudo itself."))
            c.append(check("compose", "Docker Compose", "warn", "Checked once Docker runs."))
            c.append(check("memory", "Memory for Docker", "warn", "Checked once Docker runs."))
        else:
            c.append(check("docker", "Docker Desktop 4.61 is running" if mac else "Docker Engine 29.7.2 is running",
                           detail="Engine 29.7.2. Docker works without sudo." if mac else "Rootful, with the compose plugin. Docker works without sudo."))
            c.append(check("compose", "Docker Compose 5.5.1", detail="Setup needs 2.24 or newer."))
            c.append(check("memory", "8 GB of memory for Docker", detail="4 GB or more keeps Chromium comfortable." +
                           (" Docker Desktop sets this in Settings → Resources." if mac else "")))
        c.append(check("disk", "212 GB free on this disk", detail="The desk and the hub take about 2.6 GB."))
        if "claude" in p:
            c.append(check("claude", "Claude Code isn't installed", "warn",
                           "Setup didn't find `claude` on your PATH. Deskmate needs it to connect your sessions.",
                           "Install Claude Code, open a new terminal and press Check again. You can carry on: Connect is skipped until it is found."))
        else:
            c.append(check("claude", "Claude Code 2.1.278", detail="At `%s`. Its sessions are in `%s/.claude`."
                           % ("/opt/homebrew/bin/claude" if mac else self.home + "/.local/bin/claude", self.home)))
        if "port" in p:
            c.append(check("ports", "Port 7800 is taken", "warn",
                           "**node** (process 41212) listens on 127.0.0.1:7800. It was started in `%s/work/acme/storefront`. Port 7802 is free." % self.home,
                           "Deskmate will use 7810 instead. You can change it on the Desk step."))
        else:
            c.append(check("ports", "Ports %s and %s are free" % (v["HUB_PORT"], v["CDP_PORT"]),
                           detail="On 127.0.0.1 only: %s for Deskmate's page, tools and hooks, %s for the desk's browser." % (v["HUB_PORT"], v["CDP_PORT"])))
        c.append(check("existing", "No earlier install", detail="Running setup again later opens this wizard with your answers, so you can change them."))
        s["checks"] = c
        if "docker" in p:
            s.update(status="fail", summary="Docker isn't running")
        elif "port" in p:
            s.update(status="warn", summary="Uses port 7810")
        elif "claude" in p:
            s.update(status="warn", summary="Claude Code not found")
        else:
            s.update(status="ok", summary="All good")

    def _s_you(self, s, v):
        s.update(title="You", intro="Agents and notifications use your name. The daily brief follows your time zone.")
        s["fields"] = [
            F("DESKMATE_OWNER", "Your name", "text", v["DESKMATE_OWNER"], default="Alex",
              help="Agents see it, as in “%s has the desk”, and so do notifications. Found in your git settings." % (str(v["DESKMATE_OWNER"]).strip() or "Alex")),
            F("TZ", "Time zone", "choice", v["TZ"], choices=[C(z, z) for z in ZONES],
              help="Detected from this %s. The daily brief, the morning post and the desk's clock use it." % ("Mac" if self.mac else "computer")),
        ]
        name = str(v["DESKMATE_OWNER"]).strip()
        s.update(summary="%s · %s" % (name or "No name", v["TZ"]), status="ok" if name else "fail")

    def _s_desk(self, s, v):
        s.update(title="Desk", intro="The computer your Claude Code sessions share: Chromium and a terminal in a container. "
                                     "You watch it live and can take over at any moment.")
        mons = int(v["DESK_MONITORS"]) if str(v["DESK_MONITORS"]).isdigit() else 2
        w, h = (str(v["DESK_MONITOR_SIZE"]) + "x").split("x")[:2]
        net = [C("host-access", "Your Mac's localhost, through Docker Desktop",
                 "Agents can check your dev servers, like localhost:3000, and any website. Uses Docker Desktop's host networking, which is on.")
               if self.mac else
               C("host", "This computer's localhost", "Agents can check your dev servers, like localhost:3000, and any website. Uses the host network directly."),
               C("isolated", "Internet only", "The desk can't reach anything on this %s. Choose this if agents will browse sites you don't fully trust."
                 % ("Mac" if self.mac else "computer"))]
        s["fields"] = [
            F("DESK_MONITORS", "Monitors", "int", v["DESK_MONITORS"], choices=[C(n, str(n)) for n in (1, 2, 3, 4)],
              help="Chromium sees each monitor as its own screen, so two-window apps open their second window on monitor 2. Each monitor gets its own live view."),
            F("DESK_MONITOR_SIZE", "Monitor size", "choice", v["DESK_MONITOR_SIZE"],
              choices=[C(x, x.replace("x", " × ")) for x in ("1280x800", "1440x900", "1600x900", "1920x1080")],
              help="The whole desk is %s × %s. Smaller screens keep screenshots cheap for agents." % (int(w) * mons if w.isdigit() else "?", h)),
            F("DESK_NETWORK", "What the desk's browser can open", "choice", v["DESK_NETWORK"], choices=net),
            F("HUB_PORT", "Deskmate's port", "int", v["HUB_PORT"], advanced=True, help="Its page, the MCP server and the hooks. On 127.0.0.1 only."),
            F("CDP_PORT", "Browser control port", "int", v["CDP_PORT"], advanced=True, help="Chromium's DevTools. Only Deskmate uses it. On 127.0.0.1 only."),
            F("DESKMATE_DATA_DIR", "Data folder", "path", v["DESKMATE_DATA_DIR"], advanced=True,
              help="The secretary's database, the secret files and the file exchange with the desk. The desk's own home, with its logins and downloads, is a Docker volume."),
        ]
        s["info"] = {"advanced_title": "Ports and data folder"}
        if self.mode == "edit":
            s["checks"] = [check("restart", "Changing the monitors, their size or what the desk can open restarts the desk.", "ok",
                                 "Open tabs close. Logins stay.")]
        local = v["DESK_NETWORK"] != "isolated"
        s.update(summary="%d monitor%s · %s" % (mons, "" if mons == 1 else "s", "opens localhost" if local else "internet only"), status="ok")

    def _s_secretary(self, s, v):
        s.update(title="Secretary", intro="It reads the sessions and notes you choose next, then writes a daily brief, a timeline and "
                                          "answers to your questions. It uses your own Claude login and backs off when your plan is busy.")
        on = _on(v["SECRETARY"])
        tok = _secret_set(v["claude-token"])
        model = lambda key, label, ids, help_: F(key, label, "model", v[key], advanced=True, help=help_,
                                                 choices=[C(i, n) for i, n in MODELS if i in ids])
        s["fields"] = [
            F("SECRETARY", "Use the secretary", "bool", v["SECRETARY"],
              help="Off skips the next two steps. The desk and its tools work either way." if on else
              "The secretary is off. Nothing is read and no model is called. Turn it on any time by running setup again."),
            F("claude-token", "Your Claude login", "secret", None, action="check_token" if self.mac else None,
              command="claude setup-token",
              help="In a terminal, run `claude setup-token`, sign in and paste the token it prints. It is a long-lived login for your account "
                   "only, kept in a file only you can read, and never shown again in full."),
            model("SECRETARY_DIGEST_MODEL", "Session digests", [MODELS[0][0], MODELS[1][0]], "One call per finished session. Haiku keeps plan use low."),
            model("SECRETARY_BRIEF_MODEL", "Daily brief", [m[0] for m in MODELS], "One call a day."),
            model("SECRETARY_ASK_MODEL", "Ask", [m[0] for m in MODELS], "Only when you ask."),
            F("SECRETARY_PAUSE_AT", "Pause when your 5-hour window is this full", "choice", v["SECRETARY_PAUSE_AT"], advanced=True,
              choices=[C("%.2f" % (p / 100.0), "%d%% used" % p) for p in range(30, 95, 5)],
              help="Digests wait above this, so your own sessions come first. Ask still works, with a warning."),
            F("SECRETARY_MAX_DIGESTS_PER_DAY", "Digests a day, at most", "int", v["SECRETARY_MAX_DIGESTS_PER_DAY"], advanced=True,
              help="Sessions past the cap wait for the next day."),
            F("BRIEF_AT", "Daily brief at", "time", v["BRIEF_AT"], advanced=True, help="Today's page is rebuilt then, and whenever you ask."),
            F("MORNING_POST_AT", "Morning post at", "time", v["MORNING_POST_AT"], advanced=True,
              help="Yesterday's brief goes to your notifications channel."),
        ]
        s["info"] = {"advanced_title": "Models, limits and times"}
        if not on:
            s.update(summary="Off", status="ok")
        else:
            s.update(summary="On · login works" if tok else "On · no login yet", status="ok" if tok else "warn")

    def _folders(self, v):
        h = self.home
        rows = [("work", 150, "today 14:02", 7, None), ("work/acme", 118, "today 14:02", 5, "handbook/"),
                ("work/acme/storefront", 41, "today 14:02", 1, None), ("work/acme/billing-api", 34, "today 13:52", 1, None),
                ("work/acme/mobile-app", 27, "today 11:20", 1, None), ("work/acme/handbook", 8, "2 Oct", 1, None),
                ("work/acme/infra", 8, "29 Sep", 1, None), ("work/side", 32, "30 Sep", 2, None),
                ("work/side/ledger", 24, "30 Sep", 1, None), ("work/side/dotfiles", 8, "12 Sep", 1, None)]
        picked = _list(v["SESSIONS_ROOTS"])
        out = [{"path": h + "/" + p, "label": "~/" + p, "sessions": n, "last": last, "repos": r, "hub": hub,
                "selected": (h + "/" + p) in picked} for p, n, last, r, hub in rows]
        out.append({"path": h, "label": "~", "note": "your home folder itself, not below it", "sessions": 17, "last": "3 Oct",
                    "exact": True, "selected": h in picked})
        out.append({"path": h + "/Downloads", "label": "~/Downloads", "sessions": 2, "last": "21 Aug", "selected": (h + "/Downloads") in picked})
        tmp = "/private/tmp" if self.mac else "/tmp"
        out.append({"path": tmp, "label": tmp, "sessions": 1, "last": "14 Sep", "selected": tmp in picked})
        return out

    def _s_sessions(self, s, v):
        s.update(title="Sessions", intro="Claude Code keeps every session on this %s. Tick the folders the secretary may read. "
                                         "It only reads, and only what you tick." % ("Mac" if self.mac else "computer"))
        if not _on(v["SECRETARY"]):
            s.update(skip=True, summary="Skipped: the secretary is off", status="ok")
        none = "nosessions" in self.problems
        s["fields"] = [
            F("CLAUDE_CONFIG_DIRS", "Claude Code's folder", "paths", v["CLAUDE_CONFIG_DIRS"], readonly=True,
              help="From `~/.claude`: CLAUDE_CONFIG_DIR isn't set. Deskmate mounts only its `projects` folder, read-only."),
            F("SESSIONS_ROOTS", "Folders the secretary may read", "folders", v["SESSIONS_ROOTS"],
              help="Found from each session's working folder. Your home folder is off at first: sessions started there are often one-off questions."),
        ]
        s["info"] = {"folders": [] if none else self._folders(v), "total": 0 if none else 170}
        if none:
            s["checks"] = [check("none", "Claude Code has no sessions in `%s/.claude` yet." % self.home, "warn",
                                 "That's fine. Pick the folder you work in: the secretary reads sessions there as they appear.")]
        picked = _list(v["SESSIONS_ROOTS"])
        if not s.get("skip"):
            if not picked:
                s.update(summary="No folder", status="warn")
            else:
                s.update(summary=", ".join(p.replace(self.home, "~") for p in picked) + (" and below · 150" if picked == [self.home + "/work"] else ""),
                         status="ok")

    def _s_docs(self, s, v):
        s.update(title="Notes & docs", intro="Optional. The more it can read, the more the Board, Open loops and Ask can tell you. Everything is read-only.")
        if not _on(v["SECRETARY"]):
            s.update(skip=True, summary="Skipped: the secretary is off", status="ok")
        h = self.home
        layout = [{"label": "Changelogs", "path": "changelog/<repo>/<date>.md", "detail": "214 entries in 6 repos"},
                  {"label": "Design docs", "path": "design/<feature>/", "detail": "23 folders, listed in design/README.md"},
                  {"label": "Project indexes", "path": "projects/<repo>/INDEX.md", "detail": "5 repos"},
                  {"label": "Knowledge base", "path": "knowledge-base/", "detail": "12 notes"}]
        s["fields"] = [
            F("DOCS_DIR", "Docs folder", "path", v["DOCS_DIR"], choices=[
                C(h + "/work/acme/handbook", "docs hub", "Found under your folders. Setup recognised its layout:", layout=layout,
                  note="The Board, Open loops, Decisions and Hygiene are built from these, without a model."),
                C(h + "/work/side/ledger/docs", "Markdown", "9 Markdown files. Ask can use them. The Board and Open loops need changelogs and design docs."),
                C("", "None", "The secretary reads sessions, memory notes and git history only."),
            ]),
            F("READ_MEMORY", "Claude Code's memory notes", "bool", v["READ_MEMORY"],
              help="Short notes Claude Code keeps for each project: what is pushed, what waits on you. 23 notes for 3 of your folders. Open loops use them."),
            F("READ_GIT", "Git history of the repos in your folders", "bool", v["READ_GIT"],
              help="Commit messages, times and the names of changed files, for the Timeline and Hygiene. Code is read only to answer a question you ask.",
              chips=["storefront", "billing-api", "mobile-app", "handbook", "infra", "ledger", "dotfiles"]),
        ]
        s["info"] = {"mounts": ["~/.claude/projects"] + [p.replace(h, "~") for p in _list(v["SESSIONS_ROOTS"])]}
        if not s.get("skip"):
            bits = (["docs hub"] if v["DOCS_DIR"] else []) + (["memory"] if _on(v["READ_MEMORY"]) else []) + (["git"] if _on(v["READ_GIT"]) else [])
            s.update(summary=" · ".join(bits) or "Nothing extra", status="ok")

    def _s_notify(self, s, v):
        s.update(title="Notifications", intro="Where Deskmate tells you that an agent needs you, and where the morning brief goes.")
        kind = v["NOTIFY_KIND"]
        s["fields"] = [
            F("NOTIFY_KIND", "Send notifications to", "choice", kind, choices=[
                C("none", "Nowhere", "Knocks still show on Deskmate's own page."),
                C("discord", "A Discord channel", "Through an incoming webhook: the channel's settings → Integrations → Webhooks → New Webhook → Copy Webhook URL."),
                C("slack", "A Slack channel", "An app with Incoming Webhooks → Add New Webhook."),
                C("ntfy", "An ntfy topic", "Pushes to your phone through the ntfy app. Paste the topic's URL."),
                C("webhook", "Another webhook", "Deskmate posts JSON with a text field."),
            ]),
            F("notify-url", "Webhook URL", "secret", None, action="test_notify",
              show_if={"NOTIFY_KIND": ["discord", "slack", "ntfy", "webhook"]},
              help="Kept in a file only you can read, and never shown again in full."),
            F("NOTIFY_URL_FILE", "Webhook file", "path", v["NOTIFY_URL_FILE"], advanced=True,
              help="Where the URL is kept. Point this at a file you already have and Deskmate mounts it read-only instead of copying it."),
        ]
        labels = {"none": "Nowhere", "discord": "Discord", "slack": "Slack", "ntfy": "ntfy", "webhook": "Webhook"}
        s.update(summary=labels.get(kind, kind), status="ok")

    def _s_habits(self, s, v):
        s.update(title="Working habits", intro="Optional. Rules in CLAUDE.md, three skills and an end-of-turn check that keep changelogs "
                                               "and design docs up to date, for the folders you pick.")
        h = self.home
        on = _on(v["HABITS"])
        s["fields"] = [
            F("HABITS", "Set up working habits", "bool", v["HABITS"],
              help="Off changes nothing in your folders. You can turn it on later with `./deskmate habits apply`."),
            F("HABITS_FOLDERS", "Folders", "folders", v["HABITS_FOLDERS"], show_if={"HABITS": [True, "on"]},
              help="Sessions started in these folders, or below them, get the rules. A folder inside another picked folder is refused: both CLAUDE.md files would load."),
            F("HABITS_HUB", "Docs hub", "choice", v["HABITS_HUB"], show_if={"HABITS": [True, "on"]}, choices=[
                C("auto", "A new hub from the skeleton", "In <folder>/claude, with git init. A folder that already has a docs hub keeps using it."),
                C("existing", "An existing folder", "A docs hub you already keep, by its path."),
                C("none", "No hub", "The rules leave out the changelog and design-doc habits."),
            ], help="Where sessions write changelogs and design docs."),
            F("HABITS_HUB_PATH", "Docs hub folder", "path", v["HABITS_HUB_PATH"], show_if={"HABITS_HUB": ["existing"]},
              help="The full path of the docs hub you keep."),
            F("HABITS_CHECK", "End-of-turn check", "choice", v["HABITS_CHECK"], show_if={"HABITS": [True, "on"]},
              choices=[C("off", "Off"), C("remind", "Remind", "Once per change; the session carries on"),
                       C("require", "Require", "Asks again each turn until the entry exists")],
              help="When a turn changed a repo but wrote no changelog entry: remind the session, or stop it until there is one."),
            F("HABITS_AGENTS", "Team agents from", "text", v["HABITS_AGENTS"], advanced=True,
              help="A folder or a git URL with agents/*.md. Checked for home paths and secrets before anything is copied. Empty means none."),
            F("HABITS_ALLOW_NOTIFY", "Let sessions post milestones", "bool", v["HABITS_ALLOW_NOTIFY"], advanced=True,
              show_if={"NOTIFY_KIND": ["discord", "slack", "ntfy", "webhook"]},
              help="Adds mcp__deskmate__notify to the allowed tools, so milestone posts don't ask each time."),
            F("HABITS_MEMORY_ON", "Turn on auto memory", "bool", v["HABITS_MEMORY_ON"], advanced=True,
              help="Claude Code's own memory notes, per project. Setup never reads them."),
        ]
        s["info"] = {"folders": [{"path": h + "/work/acme", "label": "~/work/acme", "repos": 5, "hub": "~/work/acme/handbook", "hub_kind": "existing",
                                  "sessions": 118, "last": "today 14:02", "selected": (h + "/work/acme") in _list(v["HABITS_FOLDERS"])},
                                 {"path": h + "/work/side", "label": "~/work/side", "repos": 2, "hub": None, "sessions": 32, "last": "30 Sep",
                                  "selected": (h + "/work/side") in _list(v["HABITS_FOLDERS"]),
                                  "note": "This folder already has its own rules: its CLAUDE.md names a changelog folder and design docs."}],
                     "actions": [{"name": "preview_habits", "label": "Show the changes", "auto": True}],
                     "advanced_title": "Team agents and Claude Code settings"}
        if on:
            first = _list(v["HABITS_FOLDERS"])[:1]
            s["info"]["after"] = ("Start Claude Code in %s for work across repos. Sessions started inside a repo get the same "
                                  "rules but keep their own memory." % (first[0].replace(h, "~") if first else "your folder"))
        n = len(_list(v["HABITS_FOLDERS"]))
        s.update(summary="On · %d folder%s · check %s" % (n, "" if n == 1 else "s", v["HABITS_CHECK"]) if on else "Off", status="ok")

    def _s_connect(self, s, v):
        s.update(title="Connect Claude Code", intro="What setup adds to Claude Code so that every session can use the desk. "
                                                    "You see each change before it is made.")
        if "claude" in self.problems:
            s.update(skip=True, summary="Skipped: no Claude Code", status="warn")
            s["checks"] = [check("claude", "Claude Code wasn't found, so this step is skipped.", "warn",
                                 "Install Claude Code, then run `./deskmate setup` again. It opens with your answers and connects Claude Code.")]
            return
        s["fields"] = [
            F("CONNECT", "Connect Claude Code", "bool", v["CONNECT"],
              help="Off leaves Claude Code alone. ./deskmate connect does it later."),
            F("CONNECT_REMOVE", "Found 2 things that compete with the desk", "multi", v["CONNECT_REMOVE"], show_if={"CONNECT": [True, "on"]}, choices=[
                C("mcp:playwright", "Remove the playwright MCP server",
                  "Another browser tool, added for every session as `npx @playwright/mcp`. A copy is kept, and `./deskmate uninstall` can put it back."),
                C("skills:viway", "Move out the 11 viway skills",
                  "viway-browser and viway-computer in `~/.claude/skills` compete with the desk. They go to `~/.claude/skills.deskmate-removed`. Nothing is deleted."),
            ], help="Nothing is removed unless you tick it."),
        ]
        s["info"] = {"actions": [{"name": "preview_connect", "label": "Show the changes", "auto": True}],
                     "config_dir": "~/.claude",
                     "footer": "Applies to every Claude Code session you start, from ~/.claude. Restart sessions that are already open to give them the desk."}
        rm = _list(v["CONNECT_REMOVE"])
        if not _on(v["CONNECT"]):
            s.update(summary="Off: Claude Code isn't connected", status="ok")
            return
        s.update(summary="Adds the tools, hooks and skill" + (" · removes %d" % len(rm) if rm else ""), status="ok")

    def _s_review(self, s, v):
        edit = self.mode == "edit"
        s.update(title="Review & apply" if edit else "Review & install",
                 intro="Your current settings. Change any section; your changes are listed here before anything is applied." if edit
                 else "Check your answers, then install. The first build takes 5 to 10 minutes. Later ones take seconds.")
        if _on(v["SECRETARY"]) and not _secret_set(v["claude-token"]):
            s["checks"] = [check("login", "The secretary has no login yet.", "warn",
                                 "You can install anyway: it reads, but makes no model calls until you add one.")]
        s["info"] = {"repo": self.repo, "phases": [p for p in PHASES if p != "Working habits" or _on(v["HABITS"])]}
        s["status"] = "todo"

    def _s_done(self, s, v):
        s.update(title="Done", intro="On Alex's %s. Restart Claude Code sessions that were already open, so they get the desk."
                 % ("MacBook Pro" if self.mac else "workstation"))
        s["info"] = {"heading": "Deskmate is running", "prompt": "Use the desk to open localhost:3000 and tell me what the page shows."}

    # ------------------------------------------------------------ validation

    def _errors(self, sid: str, v: dict) -> dict:
        e = {}
        if sid == "you":
            name = str(v["DESKMATE_OWNER"] or "")
            if not name.strip():
                e["DESKMATE_OWNER"] = "Type the name agents should use, for example Alex."
            elif len(name.strip()) > 40:
                e["DESKMATE_OWNER"] = "Use 40 characters or fewer."
            elif re.search(r"[<>\x00-\x1f]", name):
                e["DESKMATE_OWNER"] = "Leave out < and > and line breaks."
            if v["TZ"] not in ZONES:
                e["TZ"] = "Pick a time zone from the list."
        elif sid == "desk":
            ports = {}
            for k in ("HUB_PORT", "CDP_PORT"):
                try:
                    ports[k] = int(v[k])
                except (TypeError, ValueError):
                    ports[k] = 0
                if not 1024 <= ports[k] <= 65535:
                    e[k] = "Use ports from 1024 to 65535."
            if not e and ports["HUB_PORT"] == ports["CDP_PORT"]:
                e["CDP_PORT"] = "The two ports must differ."
            w = int(str(v["DESK_MONITOR_SIZE"]).split("x")[0])
            if int(v["DESK_MONITORS"]) * w > 7680:
                e["DESK_MONITOR_SIZE"] = "That is wider than 7680 pixels in all. Choose fewer monitors or a smaller size."
            d = str(v["DESKMATE_DATA_DIR"] or "")
            if not d.startswith("/") or " " in d:
                e["DESKMATE_DATA_DIR"] = "Use a full path without spaces, like %s/.local/share/deskmate." % self.home
        elif sid == "secretary" and _on(v["SECRETARY"]):
            try:
                cap = int(v["SECRETARY_MAX_DIGESTS_PER_DAY"])
            except (TypeError, ValueError):
                cap = 0
            if not 5 <= cap <= 200:
                e["SECRETARY_MAX_DIGESTS_PER_DAY"] = "Use a number from 5 to 200."
            for k in ("BRIEF_AT", "MORNING_POST_AT"):
                if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(v[k] or "")):
                    e[k] = "Use a time like 18:30."
            tok = v["claude-token"]
            if isinstance(tok, str) and tok.strip():
                msg = self._token_problem(tok.strip())
                if msg:
                    e["claude-token"] = msg
        elif sid == "docs" and _on(v["SECRETARY"]):
            d = str(v["DOCS_DIR"] or "")
            if d and not d.startswith("/"):
                e["DOCS_DIR"] = "Use a full path, like %s/work/notes." % self.home
        elif sid == "notify":
            kind, url = v["NOTIFY_KIND"], v["notify-url"]
            if kind != "none" and not _secret_set(url):
                e["notify-url"] = "Paste a webhook URL, or choose Nowhere."
            elif kind != "none" and isinstance(url, str):
                msg = self._url_problem(kind, url.strip())
                if msg:
                    e["notify-url"] = msg
        elif sid == "habits" and _on(v["HABITS"]):
            if v["HABITS_HUB"] not in ("auto", "existing", "none"):
                e["HABITS_HUB"] = "Pick a docs hub option."
            elif v["HABITS_HUB"] == "existing" and not str(v["HABITS_HUB_PATH"] or "").strip():
                e["HABITS_HUB_PATH"] = "Type the full path of your docs hub, or choose another option."
            agents = str(v["HABITS_AGENTS"] or "").strip()
            if agents and not (agents.startswith("/") or agents.startswith("~") or re.match(r"^(https?://|git@|ssh://)", agents)):
                e["HABITS_AGENTS"] = "Use a folder that exists, a git URL, or leave it empty."
            if not _list(v["HABITS_FOLDERS"]):
                e["HABITS_FOLDERS"] = "Tick at least one folder, or turn working habits off."
        return e

    def validate(self, step_id: str, values: dict) -> dict:
        v = self._values(values)
        if step_id == "review":
            errors = {}
            for sid in STEP_IDS:
                for k, msg in self._errors(sid, v).items():
                    errors.setdefault(k, msg)
        else:
            errors = self._errors(step_id, v)
        step = self._step(step_id, v) if step_id in STEP_IDS else None
        if step:
            for f in step["fields"]:
                f["error"] = errors.get(f["key"])
        return {"errors": errors, "step": step}

    @staticmethod
    def _token_problem(tok: str) -> str:
        if tok.startswith("sk-ant-api"):
            return ("That is an API key, not your login. The secretary runs on your Claude plan, not on API billing. "
                    "Run `claude setup-token` and paste what it prints.")
        if not re.match(r"^sk-ant-oat\d\d-[A-Za-z0-9_-]{20,}$", tok):
            return ("This doesn't look like a whole token. A token from `claude setup-token` starts with sk-ant-oat01- and is one "
                    "long line. Copy all of it, without spaces.")
        return ""

    @staticmethod
    def _url_problem(kind: str, url: str) -> str:
        pats = {"discord": r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$",
                "slack": r"^https://hooks\.slack\.com/services/T\w+/B\w+/\w+$", "ntfy": r"^https://[^/\s]+/[\w-]+$",
                "webhook": r"^https://\S+$"}
        if not re.match(pats.get(kind, r"^https://\S+$"), url):
            return {"discord": "That isn't a Discord webhook. Discord's start with https://discord.com/api/webhooks/.",
                    "slack": "That isn't a Slack webhook. Slack's start with https://hooks.slack.com/services/."}.get(
                kind, "Use a full https:// URL.")
        return ""

    # ------------------------------------------------------------ actions

    def action(self, name: str, values: dict) -> dict:
        v = self._values(values)
        with self.lock:
            self.actions.append((name, copy.deepcopy(values)))
        if name == "recheck":
            time.sleep(self.speed * 5)
            self.problems.discard("docker")  # Docker Desktop has started meanwhile
            self.problems.discard("claude")
            return {"ok": True, "message": "Checked just now.", "data": {"model": self.model(values)}}
        if name == "check_token":
            tok = v["claude-token"]
            if isinstance(tok, dict):
                return ({"ok": True, "message": "**Still works.** Your 5-hour window is 34% used, the 7-day window 12%."}
                        if tok.get("set") else {"ok": False, "message": "Paste the token first. It starts with sk-ant-oat01-."})
            tok = str(tok or "").strip()
            if not tok:
                return {"ok": False, "message": "Paste the token first. It starts with sk-ant-oat01-."}
            msg = self._token_problem(tok)
            if msg:
                return {"ok": False, "message": msg}
            time.sleep(self.speed * 10)
            if "revoked" in tok:
                return {"ok": False, "message": "Claude refused this token (401). It was revoked or copied wrongly. "
                                                "Run `claude setup-token` again and paste the new token."}
            return {"ok": True, "message": "**Works.** Signed in to the Team plan. Your 5-hour window is 34% used, the 7-day window 12%.",
                    "data": {"five_hour": 0.34, "seven_day": 0.12, "plan": "Team"}}
        if name == "test_notify":
            url = v["notify-url"]
            kind = v["NOTIFY_KIND"]
            if isinstance(url, str):
                msg = self._url_problem(kind, url.strip())
                if msg:
                    return {"ok": False, "message": msg}
            elif not _secret_set(url):
                return {"ok": False, "message": "Paste a webhook URL first."}
            time.sleep(self.speed * 10)
            if isinstance(url, str) and "deleted" in url:
                return {"ok": False, "message": "Discord says this webhook doesn't exist (404). It was probably deleted. "
                                                "Make a new one in the channel's settings and paste it here."}
            where = {"discord": "Discord channel", "slack": "Slack channel", "ntfy": "ntfy app"}.get(kind, "webhook")
            return {"ok": True, "message": "**Sent.** Look in your %s for “Deskmate test from Alex's %s”." %
                                           (where, "MacBook Pro" if self.mac else "workstation")}
        if name == "preview_connect":
            time.sleep(self.speed * 5)
            port = v["HUB_PORT"]
            rm = _list(v["CONNECT_REMOVE"])
            changes = [
                {"what": "The deskmate MCP server, for every session", "where": "~/.claude.json",
                 "detail": "claude mcp add-json --scope user deskmate '{\"type\":\"http\",\"url\":\"http://127.0.0.1:%s/mcp\","
                           "\"headersHelper\":\"%s/.local/share/deskmate/bin/mcp-headers\"}'\n# The token itself is never written into Claude Code's files." % (port, self.home)},
                {"what": "The deskmate plugin: the skill and the hooks", "where": "~/.claude/settings.json",
                 "detail": "+ \"extraKnownMarketplaces\": { \"deskmate\": … \"%s/plugin\" }\n+ \"enabledPlugins\": { \"deskmate@deskmate\": true }" % self.repo},
            ]
            for r in rm:
                changes.append({"what": {"mcp:playwright": "Remove the playwright MCP server", "skills:viway": "Move out the 11 viway skills"}.get(r, r),
                                "where": "~/.claude.json" if r.startswith("mcp") else "~/.claude/skills", "detail": "A backup is kept."})
            return {"ok": True, "message": "", "data": {"changes": changes, "conflicts": [
                {"kind": "mcp", "name": "playwright", "where": "~/.claude.json", "removable": True, "detail": "npx @playwright/mcp"},
                {"kind": "skill", "name": "viway-browser and 10 more", "where": "~/.claude/skills", "removable": True, "detail": ""}]}}
        if name == "preview_habits":
            time.sleep(self.speed * 5)
            if not _on(v["HABITS"]):
                return {"ok": True, "message": "Working habits are off: nothing changes.", "data": {}}
                return {"ok": True, "message": "Working habits are off: nothing changes.", "data": {"changes": []}}
            folders = _list(v["HABITS_FOLDERS"])
            h = self.home
            changes = [{"what": "Add the working-habits block", "where": f.replace(h, "~") + "/CLAUDE.md", "detail": "a backup is kept",
                        "diff": "+<!-- deskmate:habits:begin v1 -->\n+## Working habits\n+- Read `handbook/` before coding.\n"
                                "+- Add a changelog entry in `handbook/changelog/<repo>/<date>.md` after every change.\n"
                                "+<!-- deskmate:habits:end -->"} for f in folders]
            changes.append({"what": "The habits plugin", "where": "~/.claude/settings.json", "detail": "enabledPlugins: habits@deskmate"})
            # The same shape steps.action() gives: the raw changes, plus diffs and settings split out for the page.
            return {"ok": True, "message": "", "data": {
                "changes": changes, "warnings": [], "errors": [],
                "diffs": [{"path": c["where"], "what": c["what"], "diff": c["diff"]} for c in changes if c.get("diff")],
                "settings": [c for c in changes if not c.get("diff")]}}
        return {"ok": False, "message": "Unknown action %s." % name}

    # ------------------------------------------------------------ install

    def apply(self, values: dict, emit) -> dict:
        v = self._values(values)
        with self.lock:
            self.applied.append(copy.deepcopy(values))
        edit = self.mode == "edit"
        port = v["HUB_PORT"]
        lines = {
            "Save settings": [("info", "wrote .env (0600) and compose.local.yaml"), ("ok", "Settings saved")],
            "Prepare folders": [("info", "data folder %s (0700)" % v["DESKMATE_DATA_DIR"]), ("ok", "Secret files ready")],
            "Build images": ([("info", "[desk 1/9] FROM debian:trixie-slim"), ("info", "[desk 4/9] apt-get install chromium xvfb x11vnc"),
                              ("info", "[hub  3/7] pip install -r requirements.txt"), ("info", "[hub  5/7] fetch noVNC 1.6.0")]
                             if not edit else [("info", "images are up to date")]),
            "Start Deskmate": [("info", "docker compose up -d"), ("ok", "deskmate-desk and deskmate-hub are up on 127.0.0.1:%s" % port)],
            "Connect Claude Code": [("info", "$ claude mcp add-json --scope user deskmate {…headersHelper…}"),
                                    ("ok", "The deskmate MCP server is connected"), ("ok", "Plugin deskmate@deskmate installed (scope: user)")],
            "Working habits": [("info", "rules block written to %s/work/acme/CLAUDE.md (backup kept)" % self.home), ("ok", "Plugin habits@deskmate installed")],
            "Health check": [("ok", "Hub answers on 127.0.0.1:%s and refuses requests without the token" % port),
                             ("ok", "Desk: %s monitors, a screenshot in 38 ms" % v["DESK_MONITORS"]),
                             ("warn", "Secretary: no login yet, so no model calls") if not _secret_set(v["claude-token"]) else ("ok", "Secretary: the login works"),
                             ("ok", "Nothing listens outside 127.0.0.1"),
                             ("info", "Sign in: http://127.0.0.1:%s/login?t=%s" % (port, FAKE_HUB_TOKEN))],
        }
        if not edit:
            lines["Build images"] += [("info", "[desk 9/9] COPY deskd.mjs entrypoint.sh"), ("info", "[hub  7/7] COPY app"),
                                      ("ok", "Built deskmate-desk (1.57 GB) and deskmate-hub (1.06 GB)")]
        if self.leak and isinstance(v["claude-token"], str):
            lines["Save settings"].insert(0, ("info", "token to save: " + v["claude-token"]))
        for phase in PHASES:
            if phase == "Working habits" and not _on(v["HABITS"]):
                continue
            for i, (level, text) in enumerate(lines[phase]):
                if phase == "Build images" and self.fail_once and i == 3:
                    self.fail_once = False
                    emit({"phase": phase, "level": "error", "text": "[hub  5/7] fetch noVNC 1.6.0: the download timed out after 120 s"})
                    return {"ok": False, "signin_url": "", "failed_phase": phase,
                            "hint": "The build stopped: a download timed out. Check your internet connection, then Retry. "
                                    "Finished steps are kept, so the retry is quick."}
                time.sleep(self.speed)
                emit({"phase": phase, "level": level, "text": text})
        return {"ok": True, "signin_url": "http://127.0.0.1:%s/login?t=%s" % (port, FAKE_HUB_TOKEN), "failed_phase": None, "hint": ""}


def shape_problems(model) -> list:
    """What, if anything, in a model differs from the shapes in the build contract §8. Used on the
    fake here, and by hand on the real steps.model() when it exists."""
    out = []
    if not isinstance(model, dict):
        return ["the model is not an object"]
    for k in ("version", "mode", "platform", "steps", "values"):
        if k not in model:
            out.append("missing %s" % k)
    if model.get("mode") not in ("install", "edit"):
        out.append("mode is %r" % model.get("mode"))
    for s in model.get("steps") or []:
        sid = s.get("id")
        for k in ("id", "title", "summary", "status", "intro", "fields", "checks", "info"):
            if k not in s:
                out.append("%s: missing %s" % (sid, k))
        if s.get("status") not in ("todo", "ok", "warn", "fail"):
            out.append("%s: status %r" % (sid, s.get("status")))
        for f in s.get("fields") or []:
            for k in ("key", "label", "type", "value", "help", "advanced", "error", "readonly"):
                if k not in f:
                    out.append("%s.%s: missing %s" % (sid, f.get("key"), k))
            if f.get("type") not in FIELD_TYPES:
                out.append("%s.%s: type %r" % (sid, f.get("key"), f.get("type")))
            if f.get("type") == "secret" and not (isinstance(f.get("value"), dict) and "set" in f["value"] and "masked" in f["value"]):
                out.append("%s.%s: a secret's value must be {set, masked}" % (sid, f.get("key")))
            if f.get("type") in ("choice", "multi") and not f.get("choices"):
                out.append("%s.%s: no choices" % (sid, f.get("key")))
        for c in s.get("checks") or []:
            if c.get("status") not in ("ok", "warn", "fail"):
                out.append("%s: check %s has status %r" % (sid, c.get("id"), c.get("status")))
        if any(f.get("type") == "folders" for f in s.get("fields") or []) and not isinstance((s.get("info") or {}).get("folders"), list):
            out.append("%s: a folders field needs info.folders" % sid)
    ids = [s.get("id") for s in model.get("steps") or []]
    if ids[:1] != ["check"] or ids[-2:] != ["review", "done"]:
        out.append("steps run %s" % ids)
    return out


class FakeShapeTest(unittest.TestCase):
    """The fake has to keep the contract's shapes, or the server tests prove nothing."""

    def test_models_keep_the_contract(self):
        for platform in ("macos", "linux"):
            for mode in ("install", "edit"):
                for problems in ((), ("docker",), ("port",), ("claude",), ("nosessions",)):
                    m = FakeSteps(platform, mode, problems).model()
                    self.assertEqual(shape_problems(m), [], (platform, mode, problems))
                    self.assertEqual([s["id"] for s in m["steps"]], STEP_IDS)

    def test_secretary_off_skips_two_steps(self):
        m = FakeSteps().model({"SECRETARY": "off"})
        skipped = [s["id"] for s in m["steps"] if s.get("skip")]
        self.assertEqual(skipped, ["sessions", "docs"])

    def test_no_secret_text_in_a_model(self):
        m = FakeSteps().model({"claude-token": GOOD_TOKEN, "notify-url": DISCORD_URL})
        import json
        text = json.dumps(m)
        self.assertNotIn(GOOD_TOKEN, text)
        self.assertNotIn(DISCORD_URL, text)


if __name__ == "__main__":
    unittest.main()
