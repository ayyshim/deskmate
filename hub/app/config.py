"""Settings, all from the environment, which `./deskmate setup` writes into .env (no secrets there).

Secrets are files: compose mounts them under /run/secrets (hub_token, claude_token, notify_url), so
neither `docker inspect` nor `docker compose config` prints them. Host folders are mounted read-only
at the same path they have on the host ("identity mounts"), so a path in a transcript or a hook payload
is also the path the hub reads; nothing translates between the two.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    """An empty value counts as unset: compose passes every key as `${KEY:-}`, so a key missing from
    .env arrives as "" and must still get its default here (the one place defaults live)."""
    value = os.environ.get(name, "")
    return value if value.strip() else default


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(_env(name, str(default)))))
    except ValueError:
        return default


def _size(text: str, default: tuple[int, int] = (1280, 800)) -> tuple[int, int]:
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    """A ':'-separated list of absolute paths, like PATH. Empty items and duplicates dropped."""
    out: list[str] = []
    for item in _env(name).split(":"):
        item = item.strip().rstrip("/")
        if item and item not in out:
            out.append(item)
    return out


def _on(name: str, default: str = "on") -> bool:
    return _env(name, default).strip().lower() in ("on", "1", "true", "yes")


def secret(name: str, legacy_env: str = "") -> str:
    """A secret from /run/secrets/<name>; falls back to an env var for stacks set up before 2026-10-04."""
    path = Path(_env("SECRETS_DIR", "/run/secrets")) / name
    try:
        value = path.read_text().strip()
        if value:
            return value
    except OSError:
        pass
    return _env(legacy_env).strip() if legacy_env else ""


# ---------------------------------------------------------------- the person and the hub

OWNER = _env("DESKMATE_OWNER", "the user").strip() or "the user"
TZ = _env("TZ", "UTC")
PORT = _int("HUB_PORT", 7800, 1, 65535)
# 127.0.0.1 in host mode. In the bridge modes 0.0.0.0 inside the hub's own network namespace, because a
# published port cannot reach a process bound to the container's loopback; Docker publishes only
# 127.0.0.1:PORT of this machine, and auth.TrustedHosts refuses any other Host header.
BIND = _env("HUB_BIND", "127.0.0.1")
# Where people and Claude Code reach the hub. The published port equals PORT in every mode, because the
# Host and Origin allow-lists (auth.py) are built from it.
HUB_URL = f"http://127.0.0.1:{PORT}"
TOKEN = secret("hub_token", "DESKMATE_TOKEN")
DATA = Path(_env("HUB_DATA", "/data"))
HOST_HOME = _env("HOST_HOME", "")

# ---------------------------------------------------------------- the desk

NETWORK = _env("DESK_NETWORK", "host").strip().lower()  # host | host-access | isolated
CDP_URL = _env("CDP_URL", "http://127.0.0.1:7802")
# The layout setup chose; the desk reports the real one in RUN_DIR (monitors()). Tool texts are built
# at import, possibly before the desk has written it, so they use these.
DESK_MONITORS = _int("DESK_MONITORS", 2, 1, 4)
DESK_MONITOR_SIZE = _size(_env("DESK_MONITOR_SIZE", "1280x800"))
RUN_DIR = Path(_env("RUN_DIR", "/run/desk"))
DESKD_SOCKET = str(RUN_DIR / "deskd.sock")
EXCHANGE_DIR = Path(_env("EXCHANGE_DIR", "/exchange"))
DATA_DIR_HOST = _env("DESKMATE_DATA_DIR", "")
EXCHANGE_HOST_DIR = f"{DATA_DIR_HOST}/exchange" if DATA_DIR_HOST else "the exchange folder"

# ---------------------------------------------------------------- what the secretary may read (identity mounts)

CLAUDE_CONFIG_DIRS = _list("CLAUDE_CONFIG_DIRS")  # each has projects/<slug>/<session>.jsonl and memory/
SESSIONS_ROOTS = _list("SESSIONS_ROOTS")  # a session counts if its cwd is inside one of these
DOCS_DIR = _env("DOCS_DIR", "").rstrip("/")  # optional docs hub, e.g. a team's claude/ folder
READ_MEMORY = _on("READ_MEMORY")
READ_GIT = _on("READ_GIT")

# ---------------------------------------------------------------- the secretary

SECRETARY = _on("SECRETARY")
OAUTH_TOKEN = secret("claude_token", "CLAUDE_CODE_OAUTH_TOKEN")
PAUSE_AT = float(_env("SECRETARY_PAUSE_AT", "0.60"))
MAX_DIGESTS = int(_env("SECRETARY_MAX_DIGESTS_PER_DAY", "40"))
DIGEST_MODEL = _env("SECRETARY_DIGEST_MODEL", "claude-haiku-4-5-20251001")
BRIEF_MODEL = _env("SECRETARY_BRIEF_MODEL", "claude-sonnet-5-5")
ASK_MODEL = _env("SECRETARY_ASK_MODEL", "") or BRIEF_MODEL
BRIEF_AT = _env("BRIEF_AT", "18:30")
MORNING_POST_AT = _env("MORNING_POST_AT", "") or _env("DISCORD_BRIEF_AT", "09:00")

# ---------------------------------------------------------------- notifications

NOTIFY_KIND = _env("NOTIFY_KIND", "none").strip().lower()  # none | discord | slack | ntfy | webhook


def notify_url() -> str:
    """Read on every use, so a new webhook takes effect without a restart."""
    return secret("notify_url")


# ---------------------------------------------------------------- the input lease (§3.2 of the design)
# A waiting session waits longer than a holder may sit idle, so it always gets the mouse once the
# holder stops using it.
INPUT_IDLE_RELEASE = 20.0
INPUT_WAIT = 45.0


def monitors() -> tuple[int, int, int]:
    """(count, width, height) as the desk wrote them at start-up; the configured layout until it has."""
    try:
        count = int((RUN_DIR / "monitors").read_text().strip())
        w, h = (RUN_DIR / "monitor-size").read_text().strip().split("x")
        return count, int(w), int(h)
    except (OSError, ValueError):
        return DESK_MONITORS, *DESK_MONITOR_SIZE


def readable_roots() -> list[str]:
    """Every host folder mounted into the hub (read-only, at its own path)."""
    roots = [*CLAUDE_CONFIG_DIRS, *SESSIONS_ROOTS]
    if DOCS_DIR:
        roots.append(DOCS_DIR)
    return roots


def is_readable(path: str | Path) -> bool:
    """Whether a host path lies inside a folder the hub may read. Resolves symlinks first."""
    try:
        real = os.path.realpath(str(path))
    except OSError:
        return False
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in readable_roots())


def in_sessions_roots(cwd: str | None) -> bool:
    """Whether a session's working folder is one the secretary may read (D9, now configurable)."""
    if not cwd:
        return False
    real = os.path.normpath(cwd)
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in SESSIONS_ROOTS)


def tilde(path: str | None) -> str:
    """A host path with the home folder shown as ~, for display."""
    if not path:
        return ""
    if HOST_HOME and (path == HOST_HOME or path.startswith(HOST_HOME + "/")):
        return "~" + path[len(HOST_HOME):]
    return path
