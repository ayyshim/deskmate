"""Settings, all from the environment (compose.yaml and .env)."""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


PORT = int(_env("HUB_PORT", "7800"))
TOKEN = _env("DESKMATE_TOKEN")
OWNER = _env("DESKMATE_OWNER", "Ashim")
DATA = Path(_env("HUB_DATA", "/data"))

# The desk
CDP_URL = _env("CDP_URL", "http://127.0.0.1:7802")
RUN_DIR = Path(_env("RUN_DIR", "/run/desk"))
DESKD_SOCKET = str(RUN_DIR / "deskd.sock")

# Read-only views of this machine, and where they live on the host
HOST_HOME = _env("HOST_HOME", "/home/ashim")
PROJECTS_DIR = Path(_env("PROJECTS_DIR", "/ro/projects"))
SESSIONS_DIR = Path(_env("SESSIONS_DIR", "/ro/sessions"))
SESSIONS_PREFIX = _env("SESSIONS_PREFIX", "-home-ashim-Projects")
WEBHOOK_FILE = Path(_env("WEBHOOK_FILE", "/run/secrets/discord-webhook.url"))
EXCHANGE_DIR = Path(_env("EXCHANGE_DIR", "/exchange"))
EXCHANGE_HOST_DIR = _env("EXCHANGE_HOST_DIR", f"{HOST_HOME}/.local/share/deskmate/exchange")

# The secretary
OAUTH_TOKEN = _env("CLAUDE_CODE_OAUTH_TOKEN")
PAUSE_AT = float(_env("SECRETARY_PAUSE_AT", "0.60"))
MAX_DIGESTS = int(_env("SECRETARY_MAX_DIGESTS_PER_DAY", "40"))
DIGEST_MODEL = _env("SECRETARY_DIGEST_MODEL", "claude-haiku-4-5-20251001")
BRIEF_MODEL = _env("SECRETARY_BRIEF_MODEL", "claude-sonnet-5-5")
BRIEF_AT = _env("BRIEF_AT", "18:30")
DISCORD_BRIEF_AT = _env("DISCORD_BRIEF_AT", "09:00")

# The input lease (§3.2 of the design). A waiting session waits longer than a holder may sit idle,
# so it always gets the mouse once the holder stops using it.
INPUT_IDLE_RELEASE = 20.0
INPUT_WAIT = 45.0


def monitors() -> tuple[int, int, int]:
    """(count, width, height) as the desk wrote them at start-up."""
    try:
        count = int((RUN_DIR / "monitors").read_text().strip())
        w, h = (RUN_DIR / "monitor-size").read_text().strip().split("x")
        return count, int(w), int(h)
    except (OSError, ValueError):
        return 1, 1280, 800


def to_container(host_path: str) -> Path | None:
    """Map a path on the host (from a hook) to where the hub can read it, or None."""
    p = str(host_path)
    for host_root, mount in (
        (f"{HOST_HOME}/.claude/projects", SESSIONS_DIR),
        (f"{HOST_HOME}/Projects", PROJECTS_DIR),
    ):
        if p == host_root or p.startswith(host_root + "/"):
            return mount / p[len(host_root) :].lstrip("/")
    return None


def to_host(container_path: Path | str) -> str:
    """The reverse of to_container, for citations a person can open."""
    p = str(container_path)
    for host_root, mount in (
        (f"{HOST_HOME}/.claude/projects", str(SESSIONS_DIR)),
        (f"{HOST_HOME}/Projects", str(PROJECTS_DIR)),
    ):
        if p == mount or p.startswith(mount + "/"):
            return host_root + p[len(mount) :]
    return p
