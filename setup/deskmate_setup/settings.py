"""Deskmate's settings: .env in the repo (settings only, mode 0600), the secret files in the data folder,
the detected defaults, and the move from the old layout (tokens in .env, SESSIONS_PREFIX).

.env is read by Docker Compose, which interpolates it into compose.yaml, and by this tool. Secrets never go
in it: the hub token, the Claude login and the notify URL are 0600 files in <data>/secrets that compose
mounts with `secrets:`, so neither `docker inspect` nor `docker compose config` can print them.
"""

from __future__ import annotations

import os
import re
import secrets as _secrets
import time
from dataclasses import dataclass
from pathlib import Path

from . import detect, util

_REPO = Path(__file__).resolve().parents[2]


def repo_dir() -> Path:
    """The Deskmate clone this tool runs from (any path; nothing assumes where it is)."""
    return _REPO


def env_path() -> Path:
    return repo_dir() / ".env"


def home() -> str:
    return detect.home()


# ---------------------------------------------------------------- the settings


@dataclass
class Setting:
    key: str
    type: str  # text|int|bool|choice|multi|path|paths|secret|time|model|folders
    default: str
    label: str
    help: str
    choices: list | None = None
    secret: bool = False
    advanced: bool = False
    step: str = ""  # the wizard step that asks it; "" = detected or derived, never asked
    comment: str = ""  # its one-line comment in .env
    env: bool = True  # written to .env (secrets and wizard-only answers are not)


def _c(value, label, detail=""):
    out = {"value": value, "label": label}
    if detail:
        out["detail"] = detail
    return out


MODELS = [
    _c("claude-haiku-4-5-20251001", "Haiku 4.5", "Fast and light on your plan"),
    _c("claude-sonnet-5-5", "Sonnet 5.5", "Better writing, more of your plan"),
    _c("claude-opus-5-5", "Opus 5.5", "The strongest, the most of your plan"),
]
MONITOR_SIZES = ["1280x800", "1440x900", "1600x900", "1920x1080"]

SETTINGS: list = [
    # -- you
    Setting("DESKMATE_OWNER", "text", "the user", "Your name",
            "Agents see it, as in “Alex has the desk”, and so do notifications.",
            step="you", comment="Your name, as agents and notifications use it."),
    Setting("TZ", "choice", "UTC", "Time zone",
            "The desk's clock, the daily brief and the morning post use it.",
            step="you", comment="Time zone (IANA name) for the desk's clock, the daily brief and the morning post."),
    # -- this computer
    Setting("DESKMATE_DATA_DIR", "path", "", "Data folder",
            "The secretary's database, the token files and the files exchanged with the desk. Only you can open it. "
            "The desk's own home, with its logins and downloads, is a Docker volume.",
            advanced=True, step="desk",
            comment="Deskmate's data: the secretary's database, exchanged files, the secret files (mode 0700)."),
    Setting("HOST_HOME", "path", "", "Home folder", "Shown as ~ in Deskmate's page.",
            comment="Your home folder, shown as ~ in Deskmate's page."),
    Setting("HOST_UID", "int", "1000", "User id", "The hub runs as you, so the files it writes are yours.",
            comment="Your user id: the hub runs as you, so the files it writes are yours."),
    Setting("HOST_GID", "int", "1000", "Group id", "", comment="Your group id."),
    Setting("COMPOSE_PROJECT_NAME", "text", "deskmate", "Docker Compose project",
            "Names the containers and the desk's volume, which holds its logins. Keep 'deskmate' unless another "
            "user's Deskmate runs on this Docker.",
            advanced=True, step="desk", comment="Docker Compose project: names the containers and the desk's volumes."),
    Setting("COMPOSE_FILE", "text", "compose.yaml:deploy/net-host.yaml:compose.local.yaml", "Compose files", "",
            comment="Compose files: the shared one, the network mode (from DESK_NETWORK) and the generated mounts."),
    Setting("COMPOSE_PATH_SEPARATOR", "text", ":", "Separator", "", comment="Separator in COMPOSE_FILE."),
    # -- the desk
    Setting("DESK_NETWORK", "choice", "host", "What the desk's browser can open",
            "Changing it restarts the desk: open tabs close, logins stay.",
            step="desk", comment="What the desk's browser can open: host (Linux), host-access (Docker Desktop) or isolated."),
    Setting("HUB_PORT", "int", "7800", "Deskmate's port",
            "Deskmate's page, its MCP server and the hooks. On 127.0.0.1 only.",
            advanced=True, step="desk", comment="Port of Deskmate's page, MCP server and hooks (127.0.0.1 only)."),
    Setting("CDP_PORT", "int", "7802", "Browser control port",
            "Chromium's DevTools, used only by Deskmate, in host networking. On 127.0.0.1 only.",
            advanced=True, step="desk", comment="Chromium's DevTools port in host networking (127.0.0.1 only)."),
    Setting("DESK_DISPLAY", "int", "87", "Display number",
            "The desk's X display. In host networking it must not clash with a display on this computer.",
            advanced=True, step="desk", comment="The desk's X display number (must be free on this computer in host mode)."),
    Setting("DESK_MONITORS", "choice", "2", "Monitors",
            "Chromium sees each monitor as its own screen, so apps that open a second window can put it on monitor 2.",
            choices=[_c("1", "One"), _c("2", "Two", "For apps that open a second window"), _c("3", "Three"), _c("4", "Four")],
            step="desk", comment="How many monitors the desk has (1-4)."),
    Setting("DESK_MONITOR_SIZE", "choice", "1280x800", "Monitor size",
            "Smaller monitors keep screenshots cheap for agents.",
            choices=[_c(s, s.replace("x", " × ")) for s in MONITOR_SIZES],
            step="desk", comment="Size of each monitor, WxH."),
    # -- what the secretary may read
    Setting("CLAUDE_CONFIG_DIRS", "paths", "", "Claude Code's folder",
            "Where Claude Code keeps its sessions. Deskmate reads only its projects/ folder, read-only, never the "
            "rest (it holds your login).",
            step="sessions", comment="Claude Code config folders; only <folder>/projects is mounted, read-only."),
    Setting("SESSIONS_ROOTS", "folders", "", "Folders the secretary may read",
            "Found from each session's working folder. A folder includes everything below it. Your home folder is "
            "off at first: sessions started there are often one-off questions.",
            step="sessions", comment="The secretary reads sessions started in these folders or below (':'-separated), read-only."),
    Setting("DOCS_DIR", "path", "", "Docs folder",
            "A docs hub (changelogs, design docs) the secretary reads to build the board and open loops. Optional.",
            step="docs", comment="Optional docs hub the secretary reads (changelog/, design/), read-only."),
    Setting("READ_MEMORY", "bool", "on", "Claude Code's memory notes",
            "Short notes Claude Code keeps per project: what is pushed, what waits on you. Open loops use them.",
            step="docs", comment="Read Claude Code's memory notes for those folders: on or off."),
    Setting("READ_GIT", "bool", "on", "Git history of the repos in your folders",
            "Commit messages, times and changed file names, for the timeline. Code is read only to answer a question you ask.",
            step="docs", comment="Read the git history of repos in those folders: on or off."),
    # -- the secretary
    Setting("SECRETARY", "bool", "on", "Use the secretary",
            "Off skips the next two steps. The desk and its tools work either way.",
            step="secretary", comment="The secretary (session digests, daily brief, Ask): on or off."),
    Setting("SECRETARY_DIGEST_MODEL", "model", "claude-haiku-4-5-20251001", "Session digests",
            "One call per finished session. Haiku keeps your plan use low.", choices=MODELS,
            advanced=True, step="secretary", comment="Model for session digests."),
    Setting("SECRETARY_BRIEF_MODEL", "model", "claude-sonnet-5-5", "Daily brief", "One call a day.", choices=MODELS,
            advanced=True, step="secretary", comment="Model for the daily brief."),
    Setting("SECRETARY_ASK_MODEL", "model", "claude-sonnet-5-5", "Ask", "Only when you ask.", choices=MODELS,
            advanced=True, step="secretary", comment="Model for Ask."),
    Setting("SECRETARY_PAUSE_AT", "choice", "0.60", "Pause when your 5-hour window is this full",
            "Digests wait above this, so your own sessions come first. Ask still works, with a warning.",
            choices=[_c(f"{p / 100:.2f}", f"{p}%") for p in range(30, 95, 5)],
            advanced=True, step="secretary", comment="Pause digests when your 5-hour window is this full (0-1)."),
    Setting("SECRETARY_MAX_DIGESTS_PER_DAY", "int", "40", "Digests a day, at most",
            "Sessions past the cap wait for the next day.",
            advanced=True, step="secretary", comment="At most this many session digests a day."),
    Setting("BRIEF_AT", "time", "18:30", "Daily brief at", "Today's page is rebuilt then, and whenever you ask.",
            advanced=True, step="secretary", comment="Daily brief time, HH:MM local time."),
    Setting("MORNING_POST_AT", "time", "09:00", "Morning post at",
            "Yesterday's brief goes to your notifications channel. Used only when notifications are on.",
            advanced=True, step="secretary", comment="Morning post time, HH:MM local time (only with notifications on)."),
    # -- notifications
    Setting("NOTIFY_KIND", "choice", "none", "Send notifications to",
            "Knocks (an agent needs you), the morning brief and problems. Setup tells the kind from the URL.",
            choices=[_c("none", "Nowhere", "Knocks still show on Deskmate's own page."),
                     _c("discord", "A Discord channel", "Through an incoming webhook."),
                     _c("slack", "A Slack channel", "Through an incoming webhook."),
                     _c("ntfy", "ntfy", "A topic on ntfy.sh or your own ntfy server."),
                     _c("webhook", "Another webhook", "Any URL that takes a JSON POST.")],
            step="notify", comment="Where notifications go: none, discord, slack, ntfy or webhook."),
    Setting("NOTIFY_URL_FILE", "path", "", "Webhook URL file",
            "The file holding the webhook URL. Point it at a file you already have and Deskmate mounts it "
            "read-only, never copying it.",
            advanced=True, step="notify", comment="File holding the webhook URL, mounted read-only (never copied)."),
    # -- working habits
    Setting("HABITS", "bool", "off", "Set up working habits",
            "Rules in your folders' CLAUDE.md, skills for changelogs and design docs, and an end-of-turn check.",
            step="habits", comment="The working-habits pack (rules, skills, end-of-turn check): on or off."),
    # -- secrets: files in <data>/secrets, never in .env. Their keys are the files' names.
    Setting("claude-token", "secret", "", "Your Claude login",
            "In a terminal, run claude setup-token, sign in, and paste the token it prints. It is a long-lived login "
            "for your account only, kept in a file only you can read, and never shown again in full. "
            "Not the token in ~/.claude/.credentials.json: that one stops working within hours.",
            secret=True, step="secretary", env=False),
    Setting("notify-url", "secret", "", "Webhook URL",
            "Discord: the channel's settings, Integrations, Webhooks, New Webhook, Copy Webhook URL. Slack: an app "
            "with Incoming Webhooks, Add New Webhook. Kept in a file only you can read, never shown again in full.",
            secret=True, step="notify", env=False),
    # -- wizard-only answers: passed to the habits and connect parts, whose own files keep them
    Setting("HABITS_FOLDERS", "folders", "", "Folders",
            "Sessions started in these folders, or below them, get the rules. A folder inside another picked folder "
            "is refused: both CLAUDE.md files would load.", step="habits", env=False),
    Setting("HABITS_HUB", "choice", "auto", "Docs hub",
            "Where sessions write changelogs and design docs.",
            choices=[_c("auto", "A new hub from the skeleton",
                        "In <folder>/claude, with git init. A folder that already has a docs hub keeps using it."),
                     _c("existing", "An existing folder", "A docs hub you already keep, by its path."),
                     _c("none", "No hub", "The rules leave out the changelog and design-doc habits.")],
            step="habits", env=False),
    Setting("HABITS_HUB_PATH", "path", "", "Docs hub folder",
            "The folder with changelog/ and design/. Nothing in it is overwritten; missing skeleton files are not added.",
            step="habits", env=False),
    Setting("HABITS_CHECK", "choice", "remind", "End-of-turn check",
            "When a turn changed a repo but wrote no changelog entry: remind the session, or stop it until there is one.",
            choices=[_c("off", "Off"), _c("remind", "Remind", "Once per change; the session carries on"),
                     _c("require", "Require", "Asks again each turn until the entry exists")],
            step="habits", env=False),
    Setting("HABITS_AGENTS", "text", "", "Team agents from",
            "A folder or a git URL with agents/*.md, copied into <folder>/.claude/agents after a check for home paths "
            "and secrets. Empty: none, unless the docs hub lists its own.",
            advanced=True, step="habits", env=False),
    Setting("HABITS_ALLOW_NOTIFY", "bool", "on", "Milestones without a prompt",
            "Lets sessions post milestones to your notifications channel without asking each time.",
            advanced=True, step="habits", env=False),
    Setting("HABITS_MEMORY_ON", "bool", "off", "Turn Claude Code's auto memory on",
            "Only if it is off. The rules say what belongs in memory and what in the docs hub.",
            advanced=True, step="habits", env=False),
    Setting("CONNECT", "bool", "on", "Connect Claude Code",
            "Adds Deskmate's tools, hooks and skill for every Claude Code session (user scope).",
            step="connect", env=False),
    Setting("CONNECT_REMOVE", "multi", "", "Competing tools to remove",
            "Nothing is removed unless you tick it. A copy is kept, and ./deskmate uninstall can put it back.",
            step="connect", env=False),
]

KEYS = {s.key: s for s in SETTINGS}
ENV_KEYS = [s.key for s in SETTINGS if s.env and not s.secret]
SECRET_KEYS = {"claude-token": "claude-token", "notify-url": "notify-url"}
SECRET_NAMES = ("hub-token", "claude-token", "notify-url")
# Other names a secret may arrive under (environment variables, older wizard drafts).
ALIASES = {"CLAUDE_CODE_OAUTH_TOKEN": "claude-token", "CLAUDE_TOKEN": "claude-token",
           "NOTIFY_URL": "notify-url", "NOTIFY_WEBHOOK_URL": "notify-url"}
DERIVED = ("COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR")
# Keys of the old layout (before 2026-10-04): migrate_legacy() maps them and drops them from .env.
LEGACY_KEYS = ("DESKMATE_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "SESSIONS_PREFIX", "DISCORD_BRIEF_AT")


# ---------------------------------------------------------------- .env format


_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_@%+=,./:~\-]*$")


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] == "'":
        end = raw.find("'", 1)
        return raw[1:end] if end > 0 else raw[1:]
    if raw[:1] == '"':
        out, i = [], 1
        while i < len(raw):
            ch = raw[i]
            if ch == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                i += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            i += 1
        return "".join(out)
    m = re.search(r"\s#", raw)  # an inline comment after an unquoted value
    if m:
        raw = raw[: m.start()]
    return raw.strip()


def parse_env(text: str) -> dict:
    """KEY=value lines, as Docker Compose reads them: comments, `export`, single or double quotes."""
    out = {}
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def quote(value: str) -> str:
    """Plain when safe; in single quotes (taken literally by Compose) when it has spaces or $."""
    value = str(value)
    if _PLAIN_RE.match(value):
        return value
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def read_env(path: Path | None = None) -> dict:
    try:
        return parse_env((path or env_path()).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def installed() -> bool:
    return env_path().is_file()


# ---------------------------------------------------------------- folders


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    if not base or not os.path.isabs(base):
        base = os.path.join(home(), ".local", "share")
    return Path(base) / "deskmate"


def data_dir(values: dict | None = None) -> Path:
    v = (values or {}).get("DESKMATE_DATA_DIR") if values else None
    if not v or not isinstance(v, str):
        v = read_env().get("DESKMATE_DATA_DIR", "")
    return Path(v) if v else default_data_dir()


def secrets_dir(values: dict | None = None) -> Path:
    return data_dir(values) / "secrets"


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if not base or not os.path.isabs(base):
        base = os.path.join(home(), ".config")
    return Path(base) / "deskmate"


def compose_file_value(values: dict) -> str:
    net = values.get("DESK_NETWORK") or "host"
    return f"compose.yaml:deploy/net-{net}.yaml:compose.local.yaml"


# ---------------------------------------------------------------- secrets


def _read_file(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def secret_path(name: str, values: dict | None = None) -> Path:
    """Where a secret is read from. The notify URL may live in a file the user already had (NOTIFY_URL_FILE)."""
    if name not in SECRET_NAMES:
        raise ValueError(f"Unknown secret {name!r}: use one of {', '.join(SECRET_NAMES)}")
    if name == "notify-url":
        f = (values or {}).get("NOTIFY_URL_FILE") if values else None
        if not f:
            f = read_env().get("NOTIFY_URL_FILE", "")
        if f:
            return Path(f)
    return secrets_dir(values) / name


def read_secret(name: str, values: dict | None = None) -> str:
    """A secret's value ('' when unset). Before the move to secret files, the old .env still holds two."""
    value = _read_file(secret_path(name, values))
    if not value and name in ("hub-token", "claude-token"):
        old = read_env()
        value = old.get("DESKMATE_TOKEN" if name == "hub-token" else "CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    return value


def write_secret(name: str, value: str, values: dict | None = None) -> Path:
    """Write a secret into <data>/secrets/<name> (0600, folder 0700). Never into a file the user pointed at."""
    if name not in SECRET_NAMES:
        raise ValueError(f"Unknown secret {name!r}: use one of {', '.join(SECRET_NAMES)}")
    folder = util.private_dir(secrets_dir(values))
    util.private_dir(folder.parent)
    path = folder / name
    util.write_private(path, (value or "").strip() + ("\n" if (value or "").strip() else ""), 0o600)
    return path


def secret_state(name: str, values: dict | None = None) -> dict:
    """{"set": bool, "masked": "sk-ant-oat…3f9a"}: all a page or a log may ever see of a secret."""
    value = read_secret(name, values)
    return {"set": bool(value), "masked": util.mask(value, "url" if name == "notify-url" else "token")}


def all_secret_values(values: dict | None = None) -> list:
    """Every secret's value, so log lines can be scrubbed of them."""
    return [v for v in (read_secret(n, values) for n in SECRET_NAMES) if v]


def kind_from_url(url: str) -> str:
    """discord, slack or ntfy from the URL's host; any other http(s) URL is a plain webhook."""
    u = (url or "").strip().lower()
    if not (u.startswith("http://") or u.startswith("https://")):
        return "none"
    host = u.split("/")[2].split("@")[-1].split(":")[0]
    if re.match(r"^https://(ptb\.|canary\.)?discord(app)?\.com/api/webhooks/", u):
        return "discord"
    if host == "hooks.slack.com":
        return "slack"
    if "ntfy" in host:
        return "ntfy"
    return "webhook"


# ---------------------------------------------------------------- values: normalising and defaults


def normalize(values: dict | None) -> dict:
    """Canonical strings, as .env holds them: on/off for bools, ':'-joined paths, ','-joined multi choices.
    Secrets stay as given: a string is a new value, a {"set": …} dict or None means unchanged."""
    out = {}
    for k, v in (values or {}).items():
        if k in ALIASES:
            k = ALIASES[k]
            if k in (values or {}):  # the canonical key wins over an alias
                continue
        s = KEYS.get(k)
        if s is not None and s.secret:
            out[k] = v
            continue
        if v is None:
            continue
        if s is None:
            out[k] = v if isinstance(v, dict) else str(v)
        elif s.type == "bool":
            out[k] = util.on_off(v)
        elif s.type in ("paths", "folders"):
            out[k] = util.join_list(v, ":")
        elif s.type == "multi":
            out[k] = util.join_list(v, ",")
        elif isinstance(v, bool):
            out[k] = "on" if v else "off"
        elif isinstance(v, float) and k == "SECRETARY_PAUSE_AT":
            out[k] = f"{v:.2f}"
        else:
            out[k] = str(v).strip()
    return out


def _sessions_root_default(dep) -> str:
    rows = detect.session_folders(util.split_list(dep("CLAUDE_CONFIG_DIRS")))
    h = home()
    for g in detect.rank_roots(rows):
        if not g["home"] and g["path"] != "/":
            return g["path"]
    parent = str(repo_dir().parent)
    return "" if parent in (h, "/") else parent


def _docs_default(dep) -> str:
    hubs = detect.docs_hubs(util.split_list(dep("SESSIONS_ROOTS")))
    return hubs[0]["path"] if len(hubs) == 1 else ""


def _own_ports(dep) -> dict:
    """Ports and display the running Deskmate of this repo already uses: they count as free."""
    stack = detect.own_stack(repo_dir(), dep("COMPOSE_PROJECT_NAME"))
    if not stack["running"]:
        return {"ports": (), "display": ()}
    env = read_env()
    ports = tuple(int(env[k]) for k in ("HUB_PORT", "CDP_PORT") if env.get(k, "").isdigit()) or (7800, 7802)
    disp = (int(env["DESK_DISPLAY"]),) if env.get("DESK_DISPLAY", "").isdigit() else (87,)
    return {"ports": ports, "display": disp}


def _config_dirs_default(dep) -> str:
    dirs = [d["path"] for d in detect.claude_config_dirs() if d["has_projects"]]
    return ":".join(dirs) if dirs else detect.default_config_dir()


def _connect_default(dep) -> str:
    return "on" if detect.claude_cli()["installed"] else "off"


def _habits_current(key: str):
    """What the habits part saved last time (its habits.json), so edit mode starts from it."""
    try:
        import json

        data = json.loads((config_home() / "habits.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    roots = [r for r in data.get("roots", []) if isinstance(r, dict) and r.get("path")]
    if key == "HABITS_FOLDERS":
        return ":".join(r["path"] for r in roots) or None
    if key == "HABITS_CHECK":
        return data.get("mode") if data.get("mode") in ("off", "remind", "require") else None
    return None


_DETECTORS = {
    "DESKMATE_OWNER": lambda dep: detect.owner(),
    "TZ": lambda dep: detect.timezone(),
    "DESKMATE_DATA_DIR": lambda dep: str(default_data_dir()),
    "HOST_HOME": lambda dep: home(),
    "HOST_UID": lambda dep: str(os.getuid()),
    "HOST_GID": lambda dep: str(os.getgid()),
    "COMPOSE_PROJECT_NAME": lambda dep: detect.project_name(repo_dir()),
    "COMPOSE_FILE": lambda dep: compose_file_value({"DESK_NETWORK": dep("DESK_NETWORK")}),
    "COMPOSE_PATH_SEPARATOR": lambda dep: ":",
    "DESK_NETWORK": lambda dep: detect.network_default(),
    "HUB_PORT": lambda dep: str(detect.free_port(7800, ours=_own_ports(dep)["ports"])),
    "CDP_PORT": lambda dep: str(detect.free_port(7802, avoid=(int(dep("HUB_PORT") or 0),), ours=_own_ports(dep)["ports"])),
    "DESK_DISPLAY": lambda dep: str(detect.free_display(87, ours=_own_ports(dep)["display"]) if dep("DESK_NETWORK") == "host" else 87),
    "CLAUDE_CONFIG_DIRS": _config_dirs_default,
    "SESSIONS_ROOTS": _sessions_root_default,
    "DOCS_DIR": _docs_default,
    "NOTIFY_URL_FILE": lambda dep: str(Path(dep("DESKMATE_DATA_DIR")) / "secrets" / "notify-url"),
    "HABITS_FOLDERS": lambda dep: _habits_current("HABITS_FOLDERS") or dep("SESSIONS_ROOTS"),
    "HABITS_CHECK": lambda dep: _habits_current("HABITS_CHECK") or "remind",
    "HABITS_ALLOW_NOTIFY": lambda dep: "off" if dep("NOTIFY_KIND") in ("", "none") else "on",
    "CONNECT": _connect_default,
}


def detected_defaults(values: dict | None = None, keys=None) -> dict:
    """The detected default of each key (all non-secret keys unless `keys` is given). A default that depends
    on another key (CDP_PORT on HUB_PORT, DOCS_DIR on SESSIONS_ROOTS…) uses that key's value in `values`."""
    given = {k: v for k, v in normalize(values).items() if isinstance(v, str)}
    memo: dict = {}

    def own(k):
        if k not in memo:
            fn = _DETECTORS.get(k)
            try:
                memo[k] = fn(dep) if fn else KEYS[k].default
            except Exception:  # noqa: BLE001 - a failed detection falls back to the plain default
                memo[k] = KEYS[k].default
        return memo[k]

    def dep(k):
        return given[k] if k in given else own(k)

    wanted = list(keys) if keys is not None else [s.key for s in SETTINGS if not s.secret]
    return {k: own(k) for k in wanted}


# ---------------------------------------------------------------- the old layout


def is_legacy(env: dict | None = None) -> bool:
    env = read_env() if env is None else env
    return any(k in env for k in LEGACY_KEYS)


def decode_project_dir(encoded: str) -> list:
    """Folders whose Claude Code project-folder name is `encoded` ('-home-x-Projects' → /home/x/Projects).
    Claude Code turns every character that is not a letter or digit into '-', so the name is ambiguous:
    this walks the real folders and keeps every path that encodes to the same name."""
    target = encoded.strip()
    found = []

    def enc(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "-", name)

    def walk(folder: str, rest: str, depth: int):
        if depth > 12 or len(found) > 5:
            return
        if not rest:
            found.append(folder)
            return
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return
        for n in names:
            e = "-" + enc(n)
            if rest == e or (rest.startswith(e) and rest[len(e)] == "-"):
                p = os.path.join(folder, n)
                if os.path.isdir(p):
                    walk(p, rest[len(e):], depth + 1)

    if target.startswith("-"):
        walk("/", target, 0)
    return found


def legacy_values(env: dict) -> tuple:
    """(values, lines): what the old .env and the old fixed compose.yaml mean in today's keys."""
    vals, lines = {}, []
    h = home()
    fixed = {"HUB_PORT": "7800", "CDP_PORT": "7802", "DESK_DISPLAY": "87", "DESK_MONITORS": "2",
             "DESK_MONITOR_SIZE": "1280x800", "DESK_NETWORK": "host", "COMPOSE_PROJECT_NAME": "deskmate",
             "CLAUDE_CONFIG_DIRS": os.path.join(h, ".claude"), "SECRETARY": "on"}
    for k, v in fixed.items():  # what the old compose.yaml hardcoded
        if k not in env:
            vals[k] = v
    prefix = env.get("SESSIONS_PREFIX", "").strip()
    if prefix and "SESSIONS_ROOTS" not in env:
        cands = decode_project_dir(prefix)
        if len(cands) == 1:
            vals["SESSIONS_ROOTS"] = cands[0]
            lines.append(f"SESSIONS_PREFIX={prefix} became SESSIONS_ROOTS={cands[0]}")
        else:
            lines.append(f"SESSIONS_PREFIX={prefix} matches no single folder here: pick the folders in ./deskmate setup")
    old_at = env.get("DISCORD_BRIEF_AT", "").strip()
    if old_at and "MORNING_POST_AT" not in env:
        vals["MORNING_POST_AT"] = old_at
        lines.append(f"DISCORD_BRIEF_AT became MORNING_POST_AT={old_at}")
    hook = Path(h, ".config", "claude-notify", "discord-webhook.url")
    if "NOTIFY_KIND" not in env and hook.is_file():  # only checked, never opened
        vals["NOTIFY_KIND"] = "discord"
        vals["NOTIFY_URL_FILE"] = str(hook)
        lines.append(f"Notifications go to Discord through {util.tilde(str(hook), h)} "
                     "(mounted read-only, never copied)")
    return vals, lines


def migrate_legacy() -> list:
    """Move an install from before 2026-10-04 to today's layout and say what changed. Backs up the old
    .env first (.env.backup-<date>, 0600); moves DESKMATE_TOKEN and CLAUDE_CODE_OAUTH_TOKEN into the secret
    files (the same hub token, so signed-in browsers stay signed in); maps SESSIONS_PREFIX and
    DISCORD_BRIEF_AT; keeps COMPOSE_PROJECT_NAME=deskmate so the desk keeps its logins. Safe to re-run."""
    path = env_path()
    if not path.is_file():
        return []
    env = read_env()
    if not is_legacy(env):
        return []
    lines = []
    stamp = time.strftime("%Y-%m-%d")
    backup = path.with_name(f".env.backup-{stamp}")
    if backup.exists():
        backup = path.with_name(f".env.backup-{stamp}-{time.strftime('%H%M%S')}")
    util.write_private(backup, path.read_text(encoding="utf-8", errors="replace"), 0o600)
    lines.append(f"Backed up the old .env as {backup.name} (only you can read it)")
    values = load()
    util.private_dir(data_dir(values))
    for env_key, name, what in (("DESKMATE_TOKEN", "hub-token", "hub token"),
                                ("CLAUDE_CODE_OAUTH_TOKEN", "claude-token", "Claude login")):
        value = env.get(env_key, "").strip()
        if not value:
            continue
        target = secrets_dir(values) / name
        existing = _read_file(target)
        if existing and existing != value:
            lines.append(f"Kept the {what} already in {util.tilde(str(target), home())}; the old .env had another one")
        elif not existing:
            write_secret(name, value, values)
            same = " (the same token, so signed-in browsers stay signed in)" if name == "hub-token" else ""
            lines.append(f"Moved the {what} into {util.tilde(str(target), home())}{same}")
    lines.extend(legacy_values(env)[1])
    _write_env(values)
    lines.append("Wrote the new .env: settings only, no secrets")
    if values.get("COMPOSE_PROJECT_NAME") == "deskmate":
        lines.append("Kept COMPOSE_PROJECT_NAME=deskmate, so the desk keeps its logins")
    return lines


# ---------------------------------------------------------------- load and save


def load() -> dict:
    """Every setting: .env merged over the detected defaults (an old .env is read through the legacy
    mapping), the wizard-only answers at their defaults, and secrets as {"set": bool, "masked": "…"}."""
    env = read_env()
    known = {k: v for k, v in env.items() if k in KEYS and not KEYS[k].secret}
    if is_legacy(env):
        for k, v in legacy_values(env)[0].items():
            known.setdefault(k, v)
    values = {k: known[k] for k in [s.key for s in SETTINGS if not s.secret] if k in known}
    missing = [s.key for s in SETTINGS if not s.secret and s.key not in values]
    if missing:
        values.update(detected_defaults(values, missing))
    values["COMPOSE_FILE"] = compose_file_value(values)
    values["COMPOSE_PATH_SEPARATOR"] = ":"
    for key, name in SECRET_KEYS.items():
        values[key] = secret_state(name, values)
    return values


def extra_keys(env: dict | None = None) -> dict:
    """Keys in .env that Deskmate doesn't know: kept, at the end, under '# Kept from before'."""
    env = read_env() if env is None else env
    return {k: v for k, v in env.items() if k not in KEYS and k not in LEGACY_KEYS}


HEADER = """# Deskmate settings, written by ./deskmate setup. Only you can read this file (0600).
# Change them with ./deskmate setup, or ./deskmate config set KEY VALUE (then ./deskmate up).
# No secrets here: the hub token, the Claude login and the webhook URL are files in
# DESKMATE_DATA_DIR/secrets (./deskmate config set-secret NAME). Docker Compose reads this file.
"""


def render_env(values: dict, extra: dict | None = None) -> str:
    vals = normalize(values)
    lines = [HEADER.rstrip("\n")]
    for s in SETTINGS:
        if not s.env or s.secret:
            continue
        v = vals.get(s.key)
        if not isinstance(v, str):
            v = s.default
        if s.key == "COMPOSE_FILE":
            v = compose_file_value(vals)
        elif s.key == "COMPOSE_PATH_SEPARATOR":
            v = ":"
        lines += ["", f"# {s.comment}", f"{s.key}={quote(v)}"]
    if extra:
        lines += ["", "# Kept from before"]
        lines += [f"{k}={quote(v)}" for k, v in extra.items()]
    return "\n".join(lines) + "\n"


def _write_env(values: dict) -> Path:
    path = env_path()
    util.write_private(path, render_env(values, extra_keys()), 0o600)
    return path


def write_env(values: dict) -> Path:
    """Write .env (0600, LF line endings): every known key in a fixed order with a one-line comment,
    unknown keys kept at the end. An old-layout .env is migrated first, so its tokens are never lost."""
    migrate_legacy()
    return _write_env(values)


def merged(values: dict | None) -> dict:
    """The given answers over what is saved (or detected): a complete set of values."""
    out = load()
    for k, v in normalize(values).items():
        if k in KEYS and KEYS[k].secret:
            if isinstance(v, str):
                out[k] = v
            continue
        out[k] = v
    out["COMPOSE_FILE"] = compose_file_value(out)
    out["COMPOSE_PATH_SEPARATOR"] = ":"
    return out


def save(values: dict) -> None:
    """Write .env (umask 077) and any changed secret; generate the hub token when there is none; keep
    compose.local.yaml in step. Never logs a value."""
    migrate_legacy()
    full = merged(values)
    util.private_dir(data_dir(full))
    util.private_dir(secrets_dir(full))
    token = full.get("claude-token")
    if isinstance(token, str):
        write_secret("claude-token", token.strip(), full)
    url = full.get("notify-url")
    if isinstance(url, str):
        write_secret("notify-url", url.strip(), full)
        full["NOTIFY_URL_FILE"] = str(secrets_dir(full) / "notify-url")
        if url.strip() and full.get("NOTIFY_KIND") in ("", "none", None):
            full["NOTIFY_KIND"] = kind_from_url(url)
    if not _read_file(secrets_dir(full) / "hub-token"):
        write_secret("hub-token", _secrets.token_urlsafe(32), full)
    _write_env(full)
    from . import compose  # imported here: compose imports this module

    compose.write_local(full)
