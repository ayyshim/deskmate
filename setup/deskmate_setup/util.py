"""Small helpers shared by the host tool: running commands, private files, masking secrets, the terminal.

Nothing here prints or logs a secret: every text that may reach a screen or a log goes through scrub().
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ON_WORDS = ("on", "1", "true", "yes", "y")


def is_on(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value if value is not None else "").strip().lower() in ON_WORDS


def on_off(value) -> str:
    return "on" if is_on(value) else "off"


def split_list(value, sep: str = ":") -> list:
    """A ':'-separated list (like PATH), or an actual list. Empty items and duplicates dropped."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(sep)
    out = []
    for item in items:
        item = str(item).strip()
        if len(item) > 1:
            item = item.rstrip("/")
        if item and item not in out:
            out.append(item)
    return out


def join_list(items, sep: str = ":") -> str:
    return sep.join(split_list(items, sep))


# ---------------------------------------------------------------- commands


def run(cmd, timeout: float = 30.0, env=None, cwd=None, input_text=None):
    """Run a command and return (code, stdout, stderr). Never raises: a missing program gives 127,
    a timeout 124, anything else the OS refuses 126."""
    try:
        p = subprocess.run(
            [str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, cwd=None if cwd is None else str(cwd), input=input_text,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: no answer after {timeout:g} s"
    except OSError as exc:
        return 126, "", str(exc)


def which(name: str, extra=()) -> str:
    """The program on PATH, else the first of `extra` that is an executable file."""
    found = shutil.which(name)
    if found:
        return found
    for path in extra:
        p = Path(path).expanduser()
        if p.is_file() and os.access(str(p), os.X_OK):
            return str(p)
    return ""


def version_tuple(text: str) -> tuple:
    """(major, minor, patch) from the first x.y[.z] in a version string; () if there is none."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return ()
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


# ---------------------------------------------------------------- files


def write_private(path, text: str, mode: int = 0o600) -> None:
    """Write through a temp file in the same folder and a rename, so a crash never leaves half a file
    and the file is never readable by others, not even for a moment (mkstemp creates it 0600)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def private_dir(path) -> Path:
    """Create a folder only its owner can enter (0700), tightening one that already exists."""
    path = Path(path)
    old = os.umask(0o077)
    try:
        path.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old)
    try:
        if path.stat().st_uid == os.getuid() and (path.stat().st_mode & 0o777) != 0o700:
            os.chmod(str(path), 0o700)
    except OSError:
        pass
    return path


def mode_of(path) -> int:
    try:
        return Path(path).stat().st_mode & 0o777
    except OSError:
        return -1


def nearest_existing(path) -> Path:
    """The path itself or its closest parent that exists (for free-space and write checks)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def tilde(path, home: str = "") -> str:
    """A path with the home folder shown as ~, for display."""
    path = str(path or "")
    home = (home or os.path.expanduser("~")).rstrip("/")
    if home and home != "/" and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


# ---------------------------------------------------------------- secrets: masks and scrubbing

_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9]{2,8}-[A-Za-z0-9_\-]{8,}")
_DISCORD_RE = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/(\d+)/[\w\-]+")
_SLACK_RE = re.compile(r"https://hooks\.slack\.com/services/([\w]+)/[\w/]+")
_QUERY_TOKEN_RE = re.compile(r"([?&](?:t|token|code)=)[^&\s\"']+")
_BEARER_RE = re.compile(r"(Bearer\s+)[\w\-.~+/]+=*")
DOTS = "••••••••"


def mask_token(value: str) -> str:
    """'sk-ant-oat…3f9a' for a Claude token, '…3f9a' for anything else; '' when empty."""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) < 12:
        return "…"
    head = v[:10] if v.startswith("sk-ant-") else ""
    return f"{head}…{v[-4:]}"


def mask_url(value: str) -> str:
    """Scheme, host and the first characters of the path, never the secret part of a webhook URL."""
    v = (value or "").strip()
    if not v:
        return ""
    m = re.match(r"(https?://[^/]+/(?:api/webhooks/|services/)?)([^/]{0,4})", v)
    if not m:
        return DOTS
    return f"{m.group(1)}{m.group(2)}…/{DOTS}"


def mask(value: str, kind: str = "token") -> str:
    return mask_url(value) if kind == "url" else mask_token(value)


def scrub(text: str, secrets=()) -> str:
    """Remove secrets from text that may be printed or logged: the known values, plus anything shaped
    like a Claude token, a webhook URL, a bearer header or a token in a query string."""
    out = str(text)
    for s in secrets:
        s = (s or "").strip()
        if len(s) >= 8:
            out = out.replace(s, mask_url(s) if s.startswith("http") else mask_token(s))
    out = _TOKEN_RE.sub(lambda m: mask_token(m.group(0)), out)
    out = _DISCORD_RE.sub(lambda m: mask_url(m.group(0)), out)
    out = _SLACK_RE.sub(lambda m: mask_url(m.group(0)), out)
    out = _QUERY_TOKEN_RE.sub(r"\1" + DOTS, out)
    out = _BEARER_RE.sub(r"\1" + DOTS, out)
    return out


# ---------------------------------------------------------------- the terminal


class Style:
    """Colour only when the stream is a terminal and NO_COLOR is unset; plain symbols when it is not UTF-8."""

    def __init__(self, stream=None):
        stream = stream or sys.stdout
        tty = hasattr(stream, "isatty") and stream.isatty()
        self.color = bool(tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb")
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        self.utf8 = enc in ("utf8", "") or enc.startswith("utf")
        self.ok_mark = "✓" if self.utf8 else "OK"
        self.warn_mark = "!"
        self.fail_mark = "✗" if self.utf8 else "X"
        self.arrow = "→" if self.utf8 else "->"
        self.dot = "·" if self.utf8 else "-"

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, text: str) -> str:
        return self._c("1", text)

    def dim(self, text: str) -> str:
        return self._c("2", text)

    def green(self, text: str) -> str:
        return self._c("32", text)

    def yellow(self, text: str) -> str:
        return self._c("33", text)

    def red(self, text: str) -> str:
        return self._c("31", text)

    def mark(self, status: str) -> str:
        if status in ("ok", "info"):
            return self.green(self.ok_mark)
        if status in ("warn", "skip"):
            return self.yellow(self.warn_mark)
        return self.red(self.fail_mark)


def human_time(ts: float, now: float = 0.0) -> str:
    """'today 14:02', 'yesterday', '3 Oct' or '3 Oct 2025', in local time."""
    if not ts:
        return ""
    now = now or time.time()
    t, n = time.localtime(ts), time.localtime(now)
    if t.tm_year == n.tm_year and t.tm_yday == n.tm_yday:
        return time.strftime("today %H:%M", t)
    if now - ts < 2 * 86400 and (n.tm_yday - t.tm_yday) % 366 == 1:
        return "yesterday"
    if t.tm_year == n.tm_year:
        return f"{t.tm_mday} {time.strftime('%b', t)}"
    return f"{t.tm_mday} {time.strftime('%b %Y', t)}"


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    for f in ("/proc/sys/kernel/osrelease", "/proc/version"):
        try:
            if "microsoft" in Path(f).read_text().lower():
                return True
        except OSError:
            pass
    return False


def open_url(url: str) -> bool:
    """Open a link in the user's browser. macOS: open; WSL: wslview, then Windows' own Start-Process
    (the link passes through the environment, not the command line), then explorer.exe; elsewhere
    xdg-open; Python's webbrowser last. Returns whether something accepted it."""
    env = dict(os.environ)
    cmds = []
    if sys.platform == "darwin":
        cmds.append(["open", url])
    elif is_wsl():
        if which("wslview"):
            cmds.append(["wslview", url])
        if which("powershell.exe"):
            env["DESKMATE_OPEN_URL"] = url
            env["WSLENV"] = (env.get("WSLENV", "") + ":DESKMATE_OPEN_URL").lstrip(":")
            cmds.append(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                         "Start-Process $env:DESKMATE_OPEN_URL"])
        cmds.append(["explorer.exe", url])
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        cmds.append(["xdg-open", url])
    for cmd in cmds:
        if not which(cmd[0]):
            continue
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            return True
        except OSError:
            continue
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - any failure just means "print the link instead"
        return False


def can_open_browser() -> bool:
    """Whether a browser can be opened from here (decides the web or the terminal wizard)."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform == "darwin":
        return True
    if is_wsl():
        return bool(which("wslview") or which("powershell.exe"))
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
