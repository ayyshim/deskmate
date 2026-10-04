"""Connect Claude Code to Deskmate, and undo it (build contract §6).

connect() changes the Claude Code config that `claude` itself uses ($CLAUDE_CONFIG_DIR, else ~/.claude and
~/.claude.json):

- a user-scope MCP server "deskmate": HTTP, with a headersHelper that prints the Authorization header from
  the 0600 token file. Claude Code stores only the helper's path, never the token;
- the local plugin marketplace "deskmate" (<clone>/plugin) and its plugin deskmate@deskmate: the skill
  deskmate:desk and the hooks (PreToolUse for the deskmate tools; Stop, SessionEnd and PreCompact for the
  secretary);
- the old install, when there is one: the MCP entry with the token written in it, <config>/skills/deskmate,
  and the settings.json hooks that run .../deskmate/hook.sh. Each file is backed up before it changes.

Outside Claude Code it writes <data>/bin/mcp-headers and ~/.config/deskmate/client.env. Everything is
recorded in <data>/installed.json under "claude", and disconnect() undoes it.

Claude Code 2.1 loads a plugin from a local marketplace in place, so the hooks and the skill run from this
clone: edits and `git pull` apply at the next session start, and moving the clone breaks them until
`./deskmate connect` runs from the new place (status() reports it).

Every path is worked out when it is needed, from HOME, CLAUDE_CONFIG_DIR and XDG_*, so tests can point all
of them at a sandbox. Runs on Python 3.9 (macOS system Python), standard library only.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

PHASE = "Connect Claude Code"
MCP_NAME = "deskmate"
MARKETPLACE = "deskmate"
PLUGIN = "deskmate"
PLUGINS = ("deskmate", "habits")
MATCHER = "mcp__deskmate__.*"
# The oldest Claude Code these steps were run against. Older versions get a warning, not a refusal: the
# commands themselves report what they lack.
MIN_VERSION = (2, 1, 263)

# The old install (scripts/install.sh) ran <data>/hook.sh from settings.json and copied its skill to
# <config>/skills/deskmate. These are the sha256 sums of every SKILL.md it shipped.
LEGACY_HOOK_MARK = "/deskmate/hook.sh"
LEGACY_SKILL_SHA256 = frozenset({
    "6508b92decb194a376e6879d8f8a8115b6e4e5f5a0044f4b3482838d8d145619",
    "d309cc8e8392300f7c00a58d216ff23ed96d06942936a310f62e93b831d09a4e",
})

NO_CLAUDE = ("Claude Code wasn't found, so it was not connected. Install Claude Code, then run "
             "./deskmate connect (or ./deskmate setup again).")

# Tests point this at a copy of plugin/ (for example one with a stub habits plugin).
MARKETPLACE_DIR = None


class ConnectError(Exception):
    """A step failed; the message is meant for the user and holds no secret."""


# ---------------------------------------------------------------- paths


def repo_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def marketplace_dir() -> Path:
    return Path(MARKETPLACE_DIR) if MARKETPLACE_DIR else repo_dir() / "plugin"


def config_dir() -> Path:
    """The folder Claude Code reads settings.json, skills/ and plugins/ from."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".claude"


def global_config_path() -> Path:
    """Where user-scope MCP servers live: <CLAUDE_CONFIG_DIR>/.claude.json, else ~/.claude.json."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(env).expanduser() / ".claude.json"
    path = Path.home() / ".claude.json"
    old = config_dir() / ".config.json"  # very old Claude Code kept it here
    return old if not path.exists() and old.exists() else path


def settings_path() -> Path:
    return config_dir() / "settings.json"


def skills_dir() -> Path:
    return config_dir() / "skills"


def client_env_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "deskmate" / "client.env"


def _data_dir(values: dict | None = None) -> Path:
    """The Deskmate data folder: from values, else from client.env (what connect used), else settings."""
    v = (values or {}).get("DESKMATE_DATA_DIR")
    if v:
        return Path(str(v)).expanduser()
    if values is None:
        known = read_client_env().get("DATA_DIR")
        if known:
            return Path(known)
    try:
        from . import settings

        return Path(settings.data_dir(values))
    except Exception:
        base = os.environ.get("XDG_DATA_HOME", "").strip() or str(Path.home() / ".local" / "share")
        return Path(base) / "deskmate"


def _tilde(path) -> str:
    s, home = str(path), str(Path.home())
    if s == home:
        return "~"
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s


def _same_path(a, b) -> bool:
    return bool(a) and bool(b) and os.path.realpath(str(a)) == os.path.realpath(str(b))


# ---------------------------------------------------------------- small helpers


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _say(emit, level: str, text: str) -> None:
    if emit:
        emit({"phase": PHASE, "level": level, "text": text})


def _unquote(command: str) -> str:
    """The program part of a shell command (headersHelper and hooks are run through a shell)."""
    try:
        parts = shlex.split(command or "")
    except ValueError:
        return command or ""
    return parts[0] if parts else ""


def _secretary(values: dict) -> str:
    return "off" if str(values.get("SECRETARY", "on")).strip().lower() in ("off", "false", "0", "no") else "on"


def _port(values: dict) -> int:
    try:
        return int(values.get("HUB_PORT") or 7800)
    except (TypeError, ValueError):
        raise ConnectError(f"HUB_PORT is not a number: {values.get('HUB_PORT')!r}")


def _mask(text: str, data: Path | None = None) -> str:
    """CLI output can quote a static Authorization header; never pass one on."""
    text = re.sub(r"(?i)(bearer\s+)[^\s\"',}]+", r"\1…", text or "")
    if data is not None:
        try:
            token = (data / "secrets" / "hub-token").read_text().strip()
        except OSError:
            token = ""
        if len(token) >= 8:
            text = text.replace(token, "…")
    return text.strip()


def _read_json(path) -> tuple:
    """(data, None), or (None, message) when the file cannot be used. A missing or empty file is {}."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as e:
        return None, f"{_tilde(path)} cannot be read ({e.strerror})"
    if not text.strip():
        return {}, None
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, f"{_tilde(path)} is not valid JSON (line {getattr(e, 'lineno', '?')}: {getattr(e, 'msg', e)})"
    if not isinstance(data, dict):
        return None, f"{_tilde(path)} does not hold a JSON object"
    return data, None


def _indent_of(text: str):
    m = re.search(r"\n([ \t]+)\S", text or "")
    if not m:
        return 2
    return "\t" if m.group(1).startswith("\t") else len(m.group(1))


def _atomic_write(path, content, mode: int | None = None) -> None:
    """Write through a temp file and rename. A symlinked file (dotfiles) is written where it points."""
    path = Path(os.path.realpath(str(path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8") if isinstance(content, str) else content)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path, data: dict, like: str = "") -> None:
    """Write JSON with the indent the file already used, so a diff shows only what changed."""
    _atomic_write(path, json.dumps(data, indent=_indent_of(like), ensure_ascii=False) + "\n")


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), 0o700)


class _Backups:
    """One backup folder per run, <data>/backups/claude-<time>, made the first time something is saved.
    It lives in the data folder (0700) because a copy of .claude.json can hold tokens."""

    def __init__(self, data: Path):
        self.data = data
        self.dir = None

    def _ensure(self) -> Path:
        if self.dir is None:
            _mkdir_private(self.data)
            _mkdir_private(self.data / "backups")
            base = self.data / "backups" / ("claude-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
            d, n = base, 1
            while d.exists():
                n += 1
                d = Path(f"{base}-{n}")
            _mkdir_private(d)
            self.dir = d
        return self.dir

    def _target(self, name: str) -> Path:
        dst = self._ensure() / name
        n = 1
        while dst.exists():
            n += 1
            dst = self.dir / f"{name}.{n}"
        return dst

    def copy(self, src: Path, name: str | None = None) -> Path:
        dst = self._target(name or src.name)
        shutil.copy2(str(src), str(dst))
        os.chmod(str(dst), 0o600)
        return dst

    def move(self, src: Path, name: str) -> Path:
        dst = self._target(name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return dst


# ---------------------------------------------------------------- installed.json ("claude" key)


def _load_record(data: Path) -> dict:
    d, _ = _read_json(data / "installed.json")
    rec = (d or {}).get("claude")
    return rec if isinstance(rec, dict) else {}


def _save_record(data: Path, rec: dict) -> None:
    """Rewrite only the "claude" key; the other keys belong to other parts of setup (habits)."""
    path = data / "installed.json"
    d, err = _read_json(path)
    if d is None:  # unreadable: keep it aside rather than lose the other parts' records
        _Backups(data).copy(path, "installed.json.unreadable")
        d = {}
    if rec:
        d["claude"] = rec
    else:
        d.pop("claude", None)
    if not d:  # nothing installed any more
        if path.exists():
            path.unlink()
        return
    _mkdir_private(data)
    _atomic_write(path, json.dumps(d, indent=2, ensure_ascii=False) + "\n", 0o600)


# ---------------------------------------------------------------- client.env and the header helper


def read_client_env() -> dict:
    out = {}
    try:
        lines = client_env_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _client_env_text(data: Path, port: int, secretary: str) -> str:
    return (f"HUB_URL=http://127.0.0.1:{port}\n"
            f"TOKEN_FILE={data / 'secrets' / 'hub-token'}\n"
            f"SECRETARY={secretary}\n"
            f"DATA_DIR={data}\n")


def _helper_path(data: Path) -> Path:
    return data / "bin" / "mcp-headers"


def _helper_source() -> Path:
    return marketplace_dir() / PLUGIN / "bin" / "mcp-headers"


# ---------------------------------------------------------------- running claude


def claude_path() -> str | None:
    """`claude` on PATH, else the usual install places, else the newest VS Code extension binary."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for p in (home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude",
              Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if p.is_file() and os.access(str(p), os.X_OK):
            return str(p)
    bins = [p for p in home.glob(".vscode*/extensions/anthropic.claude-code-*/resources/native-binary/claude")
            if p.is_file() and os.access(str(p), os.X_OK)]
    if bins:
        return str(max(bins, key=lambda p: p.stat().st_mtime))
    return None


def claude_env() -> dict:
    """The environment for running `claude`: the user's own, so CLAUDE_CONFIG_DIR (if set) decides where it
    writes. A setup run from inside a Claude Code session must not look like a nested session."""
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    env.setdefault("NO_COLOR", "1")
    return env


def _claude(args: list, timeout: float = 120, cwd: str | None = None) -> tuple:
    """(code, stdout, stderr). Commands run from a neutral folder so no project .mcp.json is read."""
    exe = claude_path()
    if not exe:
        raise ConnectError(NO_CLAUDE)
    try:
        p = subprocess.run([exe] + list(args), env=claude_env(), cwd=cwd or tempfile.gettempdir(),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"`claude {' '.join(args[:3])}` did not finish within {int(timeout)} s"
    except OSError as e:
        return 126, "", f"`claude` could not be run ({e.strerror})"
    return p.returncode, p.stdout, p.stderr


def _claude_json(args: list):
    """Run a `claude … --json` command; (parsed, None) or (None, message). Lists come pretty-printed,
    plugin commands print one line, sometimes after a human line."""
    code, out, err = _claude(args)
    text = out.strip()
    try:
        return json.loads(text), None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line), None
            except ValueError:
                continue
    return None, _mask(err or out) or f"exit code {code}"


def claude_version() -> tuple:
    """(version string, tuple) or ("", None)."""
    try:
        code, out, _ = _claude(["--version"], timeout=30)
    except ConnectError:
        return "", None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return (m.group(0), tuple(int(x) for x in m.groups())) if m else ("", None)


# ---------------------------------------------------------------- the MCP server


def _want_entry(port: int, helper: Path) -> dict:
    # Claude Code runs headersHelper through a shell, so a path with a space must be quoted (tested).
    return {"type": "http", "url": f"http://127.0.0.1:{port}/mcp", "headersHelper": shlex.quote(str(helper))}


def _mcp_entry(gconf: dict):
    servers = gconf.get("mcpServers") if isinstance(gconf.get("mcpServers"), dict) else {}
    entry = servers.get(MCP_NAME)
    return entry if isinstance(entry, dict) else None


def _mcp_kind(entry, want: dict | None = None) -> str:
    """none | current (exactly what connect writes) | ours (an older connect) | legacy (token inline) | other."""
    if not entry:
        return "none"
    headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
    if any(str(k).lower() == "authorization" for k in headers):
        return "legacy"
    if want is not None and not headers and all(entry.get(k) == v for k, v in want.items()):
        return "current"
    if _unquote(str(entry.get("headersHelper") or "")).endswith("/bin/mcp-headers"):
        return "ours"
    return "other"


def _set_mcp(port: int, helper: Path, backups: _Backups, data: Path, emit) -> dict:
    gpath = global_config_path()
    gconf, err = _read_json(gpath)
    if err:
        raise ConnectError(err + ". Nothing else was changed.")
    want = _want_entry(port, helper)
    entry = _mcp_entry(gconf)
    kind = _mcp_kind(entry, want)
    if kind == "current":
        _say(emit, "info", f"The deskmate MCP server is already set up ({want['url']}).")
        return {"before": "current", "backup": None}
    backup = None
    if kind in ("legacy", "other") and gpath.exists():
        backup = str(backups.copy(gpath, ".claude.json"))
        _say(emit, "info", f"Backed up {_tilde(gpath)} to {_tilde(backup)} before replacing the old deskmate entry.")
    if entry is not None:
        _claude(["mcp", "remove", "--scope", "user", MCP_NAME])
    code, out, err = _claude(["mcp", "add-json", "--scope", "user", MCP_NAME, json.dumps(want)])
    if code != 0:
        raise ConnectError("Claude Code refused the deskmate MCP server: " + (_mask(err or out, data) or f"exit {code}"))
    gconf, err = _read_json(gpath)
    if err or _mcp_kind(_mcp_entry(gconf or {}), want) != "current":
        raise ConnectError(f"The deskmate MCP server was not stored in {_tilde(gpath)} as expected.")
    if kind == "legacy":
        _say(emit, "ok", "Replaced the old deskmate MCP server, which had the token written in Claude Code's config. "
                         "The new one reads it from Deskmate's own file.")
    else:
        _say(emit, "ok", f"Added the deskmate MCP server for every session ({want['url']}).")
    return {"before": kind, "backup": backup}


def mcp_live_status(timeout: float = 45) -> tuple:
    """(connected: bool | None, the status line). Asks `claude mcp get`, which connects to the hub."""
    try:
        code, out, err = _claude(["mcp", "get", MCP_NAME], timeout=timeout)
    except ConnectError:
        return None, NO_CLAUDE
    for line in (out or "").splitlines():
        if line.strip().lower().startswith("status:"):
            text = _mask(line.split(":", 1)[1])
            bad = re.search(r"✘|fail|disconnected|needs|error|pending", text, re.I)
            return bool(re.search(r"\bconnected\b", text, re.I)) and not bad, text
    return (None if code else False), _mask(err or out) or "no status"


# ---------------------------------------------------------------- the plugin marketplace and plugins


def _plugin_version(name: str) -> str:
    d, _ = _read_json(marketplace_dir() / name / ".claude-plugin" / "plugin.json")
    return str((d or {}).get("version") or "")


def _marketplace_info():
    data, err = _claude_json(["plugin", "marketplace", "list", "--json"])
    if data is None:
        raise ConnectError("Could not list Claude Code's plugin marketplaces: " + err)
    for m in data if isinstance(data, list) else []:
        if isinstance(m, dict) and m.get("name") == MARKETPLACE:
            return m
    return None


def _installed_plugins() -> dict:
    """{plugin id: info} for user-scope plugins."""
    data, err = _claude_json(["plugin", "list", "--json"])
    if data is None:
        raise ConnectError("Could not list Claude Code's plugins: " + err)
    return {p["id"]: p for p in data if isinstance(p, dict) and p.get("id") and p.get("scope", "user") == "user"}


def _outcome(data, err) -> str:
    """The message of a failed `claude plugin … --json` run, or "" when it went fine."""
    if data is None:
        return err or "no answer"
    if isinstance(data, dict) and data.get("outcome") not in (None, "ok"):
        return str(data.get("message") or data.get("failureCode") or "failed")
    return ""


def _note_settings_keys(rec: dict) -> None:
    """Remember, once, which settings keys the marketplace and plugins will add, and whether settings.json
    existed, so the last uninstall can leave settings.json as it was."""
    if "settings_keys_added" in rec:
        return
    path = settings_path()
    settings, err = _read_json(path)
    if err:
        raise ConnectError(err + ". Fix it, then run again; nothing was changed.")
    rec["settings_existed"] = path.exists()
    rec["settings_keys_added"] = [k for k in ("extraKnownMarketplaces", "enabledPlugins") if k not in settings]


def install_plugin(name: str, emit=None) -> None:
    """Install deskmate@deskmate or habits@deskmate for every session (user scope). Adds the local
    marketplace first if it is missing, or points it at this clone if it was added from somewhere else.
    Safe to run again: a newer version in the clone is updated, a disabled plugin is enabled."""
    if name not in PLUGINS:
        raise ValueError(f"unknown plugin {name!r}")
    mdir = marketplace_dir()
    if not (mdir / ".claude-plugin" / "marketplace.json").is_file():
        raise ConnectError(f"The plugin marketplace is missing: {_tilde(mdir)}/.claude-plugin/marketplace.json")
    if not (mdir / name / ".claude-plugin" / "plugin.json").is_file():
        raise ConnectError(f"The {name} plugin is missing: {_tilde(mdir / name)}")
    data = _data_dir(None)
    rec = _load_record(data)
    _note_settings_keys(rec)

    m = _marketplace_info()
    if m is None or not _same_path(m.get("path") or m.get("installLocation"), mdir):
        code, out, err = _claude(["plugin", "marketplace", "add", str(mdir), "--scope", "user"])
        if code != 0:
            raise ConnectError("Claude Code could not add the plugin marketplace: " + (_mask(err or out) or f"exit {code}"))
        if m is None:
            _say(emit, "ok", f"Added the local plugin marketplace “deskmate” ({_tilde(mdir)}).")
        else:
            _say(emit, "ok", f"Pointed the plugin marketplace “deskmate” at this clone ({_tilde(mdir)}); "
                             f"it was at {_tilde(m.get('path') or '?')}.")
        rec.setdefault("marketplace", {"name": MARKETPLACE, "added": m is None})["path"] = str(mdir)
    else:
        # Re-reads marketplace.json and plugin.json, so a version bump after `git pull` is seen.
        code, out, err = _claude(["plugin", "marketplace", "update", MARKETPLACE])
        if code != 0:
            _say(emit, "warn", "Could not refresh the plugin marketplace: " + (_mask(err or out) or f"exit {code}"))
        rec.setdefault("marketplace", {"name": MARKETPLACE, "added": False, "path": str(mdir)})

    pid = f"{name}@{MARKETPLACE}"
    want = _plugin_version(name)
    have = _installed_plugins().get(pid)
    if have is None:
        problem = _outcome(*_claude_json(["plugin", "install", pid, "--scope", "user", "--json"]))
        if problem:
            raise ConnectError(f"Claude Code could not install the plugin {pid}: {_mask(problem)}")
        _say(emit, "ok", f"Installed the plugin {pid}" + (f" {want}." if want else "."))
    else:
        if want and have.get("version") != want:
            problem = _outcome(*_claude_json(["plugin", "update", pid, "--scope", "user", "--json"]))
            if problem:
                _say(emit, "warn", f"Could not update the plugin {pid} to {want}: {_mask(problem)}")
            else:
                _say(emit, "ok", f"Updated the plugin {pid} from {have.get('version')} to {want}.")
        if have.get("enabled") is False:
            problem = _outcome(*_claude_json(["plugin", "enable", pid, "--scope", "user", "--json"]))
            if problem:
                raise ConnectError(f"Claude Code could not turn on the plugin {pid}: {_mask(problem)}")
            _say(emit, "ok", f"Turned the plugin {pid} back on.")
        else:
            _say(emit, "info", f"The plugin {pid} is installed" + (f" ({have.get('version')})." if have.get("version") else "."))
    entry = rec.setdefault("plugins", {}).setdefault(name, {"added": have is None})
    entry["version"] = want
    _save_record(data, rec)


def _tidy_settings(rec: dict) -> None:
    """After the last Deskmate plugin and the marketplace are gone, drop the keys they left empty ({}), and
    settings.json itself if connect created it and it is now empty."""
    keys = rec.get("settings_keys_added") or []
    path = settings_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    settings, err = _read_json(path)
    if err or not path.exists():
        return
    changed = False
    for k in keys:
        if k in settings and settings[k] == {}:
            del settings[k]
            changed = True
    if not settings and rec.get("settings_existed") is False:
        path.unlink()
    elif changed:
        _write_json(path, settings, text)


def uninstall_plugin(name: str, emit=None) -> None:
    """Uninstall <name>@deskmate. The marketplace goes too once no plugin from it is left (removing it
    earlier would uninstall the other one with it)."""
    if name not in PLUGINS:
        raise ValueError(f"unknown plugin {name!r}")
    data = _data_dir(None)
    rec = _load_record(data)
    pid = f"{name}@{MARKETPLACE}"
    if pid in _installed_plugins():
        data_out, err = _claude_json(["plugin", "uninstall", pid, "--scope", "user", "--json"])
        problem = _outcome(data_out, err)
        if problem and "not_installed" not in str(data_out):
            raise ConnectError(f"Claude Code could not uninstall the plugin {pid}: {_mask(problem)}")
        _say(emit, "ok", f"Uninstalled the plugin {pid}.")
    (rec.get("plugins") or {}).pop(name, None)
    left = [i for i in _installed_plugins() if i.endswith("@" + MARKETPLACE)]
    if not left:
        if _marketplace_info() is not None:
            code, out, err = _claude(["plugin", "marketplace", "remove", MARKETPLACE, "--scope", "user"])
            if code != 0:
                raise ConnectError("Claude Code could not remove the plugin marketplace: " + (_mask(err or out) or f"exit {code}"))
            _say(emit, "ok", "Removed the plugin marketplace “deskmate”.")
        _tidy_settings(rec)
        for k in ("marketplace", "plugins", "settings_keys_added", "settings_existed"):
            rec.pop(k, None)
    _save_record(data, rec)


# ---------------------------------------------------------------- the old install


def _legacy_hook(hook) -> bool:
    return isinstance(hook, dict) and LEGACY_HOOK_MARK in str(hook.get("command", ""))


def _strip_hooks(settings: dict, pred) -> tuple:
    """(new settings, [(event, matcher, hook)]) without the hooks pred() picks. Groups and events that end
    up empty go; nothing else changes."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings, []
    removed, new_hooks = [], {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        kept_groups = []
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                kept_groups.append(g)
                continue
            keep = [h for h in inner if not pred(event, h)]
            removed += [(event, g.get("matcher"), h) for h in inner if pred(event, h)]
            if keep:
                kept_groups.append(dict(g, hooks=keep) if len(keep) != len(inner) else g)
        if kept_groups:
            new_hooks[event] = kept_groups
    if not removed:
        return settings, []
    out = dict(settings)
    if new_hooks:
        out["hooks"] = new_hooks
    else:
        del out["hooks"]
    return out, removed


def _hook_positions(settings: dict, pred) -> list:
    """[[event, matcher, hook, group index, hook index]] for the hooks pred() picks, so they can go back in place."""
    out = []
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    for event, groups in hooks.items():
        for gi, g in enumerate(groups if isinstance(groups, list) else []):
            inner = g.get("hooks") if isinstance(g, dict) else None
            for hi, h in enumerate(inner if isinstance(inner, list) else []):
                if pred(event, h):
                    out.append([event, g.get("matcher"), h, gi, hi])
    return out


def _legacy_skill():
    """{"path", "exact"} when <config>/skills/deskmate holds the old Deskmate skill (as shipped, or edited)."""
    d = skills_dir() / "deskmate"
    try:
        raw = (d / "SKILL.md").read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() in LEGACY_SKILL_SHA256:
        return {"path": d, "exact": True}
    text = raw.decode("utf-8", "replace")
    if re.search(r"(?m)^name:\s*deskmate\s*$", text) and "desk_ask_human" in text and "browser_open" in text:
        return {"path": d, "exact": False}
    return None


def _legacy_files(settings: dict, data: Path) -> list:
    """The old hook script and its copy of the token: in the data folder, and wherever the old hooks point."""
    dirs = [data]
    for _, _, h in _strip_hooks(settings, lambda e, h: _legacy_hook(h))[1]:
        prog = _unquote(str(h.get("command", "")))
        if prog.endswith(LEGACY_HOOK_MARK):
            d = Path(prog).parent
            if all(not _same_path(d, x) for x in dirs):
                dirs.append(d)
    out = []
    for d in dirs:
        script = d / "hook.sh"
        try:
            if script.is_file() and "/hooks/" in script.read_text(errors="replace"):
                out.append(script)
        except OSError:
            pass
        if (d / "token").is_file() and (script.is_file() or _same_path(d, data)):
            out.append(d / "token")
    return out


def legacy() -> dict:
    """What is left of the old install in the active Claude Code config (read-only)."""
    settings, _ = _read_json(settings_path())
    gconf, _ = _read_json(global_config_path())
    return {
        "mcp": _mcp_kind(_mcp_entry(gconf or {})) == "legacy",
        "hooks": [(e, h.get("command")) for e, _, h in _strip_hooks(settings or {}, lambda e, h: _legacy_hook(h))[1]],
        "skill": _legacy_skill(),
    }


def _remove_legacy(data: Path, backups: _Backups, emit) -> list:
    """Remove the old hooks, skill and files (after the plugin that replaces them is in). Returns records."""
    done = []
    path = settings_path()
    settings, err = _read_json(path)
    if err:
        raise ConnectError(err + ". The old Deskmate hooks were left in place.")
    files = _legacy_files(settings, data)
    new, removed = _strip_hooks(settings, lambda e, h: _legacy_hook(h))
    if removed:
        text = path.read_text(encoding="utf-8")
        backup = backups.copy(path, "settings.json")
        _write_json(path, new, text)
        names = ", ".join(sorted({e for e, _, _ in removed}))
        _say(emit, "ok", f"Removed {len(removed)} old Deskmate hook(s) ({names}) from {_tilde(path)}; "
                         f"nothing else in it changed. Backup: {_tilde(backup)}.")
        done.append({"what": "old hooks", "where": str(path), "hooks": [[e, m, h] for e, m, h in removed],
                     "backup": str(backup)})
    skill = _legacy_skill()
    if skill:
        dst = backups.move(skill["path"], "skills-deskmate")
        _say(emit, "ok", f"Removed the old deskmate skill ({_tilde(skill['path'])}); the plugin's deskmate:desk "
                         f"replaces it. {'It had been edited; ' if not skill['exact'] else ''}Backup: {_tilde(dst)}.")
        done.append({"what": "old skill", "where": str(skill["path"]), "edited": not skill["exact"], "backup": str(dst)})
    token_ok = (data / "secrets" / "hub-token").is_file() and (data / "secrets" / "hub-token").stat().st_size > 0
    for f in files:
        if f.name == "hook.sh":
            dst = backups.move(f, "hook.sh")
            done.append({"what": "old hook script", "where": str(f), "backup": str(dst)})
            _say(emit, "ok", f"Removed the old hook script {_tilde(f)}.")
        elif token_ok:  # the hub token lives on in secrets/; this was a second copy for hook.sh
            f.unlink()
            done.append({"what": "old token copy", "where": str(f), "backup": None})
            _say(emit, "ok", f"Removed the old copy of the hub token, {_tilde(f)}.")
        else:
            _say(emit, "warn", f"Kept {_tilde(f)}: there is no hub token in {_tilde(data / 'secrets')} yet.")
    return done


# ---------------------------------------------------------------- preview, connect, disconnect, status


def preview(values: dict) -> dict:
    """{changes: [{what, where, detail}], conflicts: [...]}: what connect(values) would change. Read-only."""
    values = values or {}
    changes = []
    data = _data_dir(values)
    try:
        port = _port(values)
    except ConnectError as e:
        return {"changes": [], "conflicts": [], "error": str(e)}
    helper = _helper_path(data)
    mdir = marketplace_dir()
    settings, serr = _read_json(settings_path())
    gconf, gerr = _read_json(global_config_path())
    if serr or gerr:
        return {"changes": [], "conflicts": conflicts(values), "error": (serr or gerr) + ". Fix it first; nothing will change until then."}
    if not claude_path():
        return {"changes": [], "conflicts": conflicts(values), "error": NO_CLAUDE}

    want = _want_entry(port, helper)
    kind = _mcp_kind(_mcp_entry(gconf), want)
    where = _tilde(global_config_path())
    how = (f"{want['url']}. The Authorization header comes from {_tilde(helper)}, which reads the token file; "
           "the token itself is not written into Claude Code's files.")
    if kind == "none":
        changes.append({"what": "Add the deskmate MCP server for every session (user scope)", "where": where, "detail": how})
    elif kind == "legacy":
        changes.append({"what": "Replace the old deskmate MCP server, which has the token written in it", "where": where,
                        "detail": how + " A backup of the file is kept in the data folder."})
    elif kind in ("ours", "other"):
        changes.append({"what": "Update the deskmate MCP server", "where": where, "detail": how})

    try:
        helper_same = helper.read_bytes() == _helper_source().read_bytes()
    except OSError:
        helper_same = False
    if not helper_same:
        changes.append({"what": "Write the header helper", "where": _tilde(helper),
                        "detail": f"It prints the Authorization header from {_tilde(data / 'secrets' / 'hub-token')}."})
    env_text = _client_env_text(data, port, _secretary(values))
    try:
        env_same = client_env_path().read_text(encoding="utf-8") == env_text
    except OSError:
        env_same = False
    if not env_same:
        changes.append({"what": "Write the settings the helper and the hooks read", "where": _tilde(client_env_path()),
                        "detail": "HUB_URL, TOKEN_FILE, SECRETARY and DATA_DIR. No token."})

    known = settings.get("extraKnownMarketplaces") if isinstance(settings.get("extraKnownMarketplaces"), dict) else {}
    src = (known.get(MARKETPLACE) or {}).get("source") if isinstance(known.get(MARKETPLACE), dict) else None
    mpath = src.get("path") if isinstance(src, dict) else None
    if not mpath:
        changes.append({"what": "Add the local plugin marketplace “deskmate”", "where": _tilde(settings_path()),
                        "detail": f"{_tilde(mdir)}. Claude Code loads Deskmate's plugins from this folder, so keep the clone where it is."})
    elif not _same_path(mpath, mdir):
        changes.append({"what": "Point the plugin marketplace “deskmate” at this clone", "where": _tilde(settings_path()),
                        "detail": f"From {_tilde(mpath)} to {_tilde(mdir)}."})
    enabled = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
    pid = f"{PLUGIN}@{MARKETPLACE}"
    if enabled.get(pid) is not True:
        changes.append({"what": f"Install the plugin {pid}", "where": f"{_tilde(settings_path())} and {_tilde(config_dir() / 'plugins')}",
                        "detail": "The skill deskmate:desk. Hooks: PreToolUse for the deskmate tools (links each session to the "
                                  "desk's activity feed); Stop, SessionEnd and PreCompact for the secretary (they do nothing while "
                                  "the secretary is off). Each gives up after 1 second and never holds up a turn."})

    old_hooks = _strip_hooks(settings, lambda e, h: _legacy_hook(h))[1]
    if old_hooks:
        changes.append({"what": "Remove the old Deskmate hooks", "where": _tilde(settings_path()),
                        "detail": "; ".join(f"{e}: {h.get('command')}" for e, _, h in old_hooks) +
                                  ". Nothing else in the file changes; a backup is kept in the data folder."})
    skill = _legacy_skill()
    if skill:
        changes.append({"what": "Remove the old deskmate skill (the plugin's deskmate:desk replaces it)", "where": _tilde(skill["path"]),
                        "detail": ("It was edited since it was installed. " if not skill["exact"] else "") +
                                  "It moves to a backup in the data folder."})
    files = _legacy_files(settings, data)
    if files:
        changes.append({"what": "Remove the old hook script and token copy", "where": ", ".join(_tilde(f) for f in files),
                        "detail": "The hub token stays in the secrets folder."})
    return {"changes": changes, "conflicts": conflicts(values)}


def connect(values: dict, emit=None) -> dict:
    """Connect Claude Code (see the module docstring). Safe to run again; a re-run changes only what differs.

    values: the settings (HUB_PORT, DESKMATE_DATA_DIR, SECRETARY), plus optional CONNECT_REMOVE, the ids
    of conflicts() the user chose to remove (a list, or a comma-separated string).
    Returns {ok, error, changes, warnings, backup, connected, status}."""
    values = values or {}
    result = {"ok": False, "error": None, "changes": [], "warnings": [], "backup": None, "connected": None, "status": None}

    def note(event):
        if event.get("level") == "ok":
            result["changes"].append(event["text"])
        elif event.get("level") == "warn":
            result["warnings"].append(event["text"])
        if emit:
            emit(event)

    try:
        data = _data_dir(values)
        port = _port(values)
        backups = _Backups(data)
        if not claude_path():
            raise ConnectError(NO_CLAUDE)
        version, vt = claude_version()
        if vt is not None and vt < MIN_VERSION:
            _say(note, "warn", f"Claude Code {version} is older than {'.'.join(map(str, MIN_VERSION))}, the oldest "
                               "version Deskmate was tested with. If a step fails, update it with `claude update`.")
        token = data / "secrets" / "hub-token"
        if not token.is_file() or token.stat().st_size == 0:
            raise ConnectError(f"There is no hub token at {_tilde(token)} yet. Run ./deskmate setup first.")
        for path in (settings_path(), global_config_path()):
            _, err = _read_json(path)
            if err:
                raise ConnectError(err + ". Fix it, then run again; nothing was changed.")
        if not (_helper_source().is_file() and (marketplace_dir() / ".claude-plugin" / "marketplace.json").is_file()):
            raise ConnectError(f"This clone has no Claude Code plugin folder ({_tilde(marketplace_dir())}).")

        rec = _load_record(data)
        _note_settings_keys(rec)
        rec.update({"version": 1, "config_dir": str(config_dir()), "global_config": str(global_config_path())})
        _save_record(data, rec)

        # 1. The helper and client.env, which the MCP server and the hooks need.
        _mkdir_private(data)
        _mkdir_private(data / "bin")
        helper = _helper_path(data)
        src = _helper_source().read_bytes()
        if not helper.is_file() or helper.read_bytes() != src:
            _atomic_write(helper, src, 0o700)
            _say(note, "ok", f"Wrote the header helper {_tilde(helper)}.")
        os.chmod(str(helper), 0o700)
        env_path = client_env_path()
        text = _client_env_text(data, port, _secretary(values))
        old = env_path.read_text(encoding="utf-8") if env_path.is_file() else None
        if old != text:
            _mkdir_private(env_path.parent)
            _atomic_write(env_path, text, 0o600)
            _say(note, "ok", f"Wrote {_tilde(env_path)} (hub address, token file, secretary {_secretary(values)}).")
        os.chmod(str(env_path), 0o600)

        # 2. The MCP server (replacing an old entry with the token inline).
        mcp = _set_mcp(port, helper, backups, data, note)
        rec = _load_record(data)
        rec["mcp"] = {"name": MCP_NAME, "url": f"http://127.0.0.1:{port}/mcp", "headersHelper": str(helper),
                      "before": rec.get("mcp", {}).get("before", mcp["before"])}
        rec["helper"] = str(helper)
        rec["client_env"] = str(env_path)
        _save_record(data, rec)

        # 3. The plugin: skill and hooks.
        install_plugin(PLUGIN, note)

        # 4. Only now that the replacements are in: the old hooks, skill and files.
        done = _remove_legacy(data, backups, note)
        rec = _load_record(data)
        if done or mcp["backup"]:
            rec.setdefault("legacy_removed", [])
            if mcp["backup"]:
                rec["legacy_removed"].append({"what": "old MCP entry" if mcp["before"] == "legacy" else "MCP entry",
                                              "where": str(global_config_path()), "backup": mcp["backup"]})
            rec["legacy_removed"] += done
        if backups.dir:
            rec.setdefault("backups", []).append(str(backups.dir))
            result["backup"] = str(backups.dir)
        rec["connected_at"] = _now()
        _save_record(data, rec)

        # 5. Competing tools the user ticked.
        chosen = values.get("CONNECT_REMOVE") or []
        if isinstance(chosen, str):
            chosen = [c.strip() for c in chosen.split(",") if c.strip()]
        if chosen:
            removal = remove_conflicts(chosen, note, values)
            result["warnings"] += removal["errors"]

        # 6. Is it live? The hub runs before this phase; if it is not answering yet, the health check says so.
        live, line = mcp_live_status()
        result["connected"] = live
        if live:
            _say(note, "ok", f"Claude Code reaches the hub: {line}.")
        else:
            _say(note, "warn", f"Claude Code does not reach the hub yet ({line}). New sessions retry on their own.")
        result["status"] = status(values)
        result["ok"] = True
        _say(note, "info", "New Claude Code sessions get the desk; restart sessions that are already open.")
    except ConnectError as e:
        result["error"] = str(e)
        _say(emit, "error", str(e))
    return result


def disconnect(emit=None, remove_plugins: bool = True) -> dict:
    """Undo connect(): the MCP server, the header helper, client.env and (remove_plugins) the deskmate
    plugin, plus the marketplace once no Deskmate plugin is left. With remove_plugins=False the plugin
    stays; its hooks then do nothing, because client.env is gone. Removed competing tools stay removed;
    restore_conflicts() puts them back. Returns {ok, removed, kept, errors}."""
    data = _data_dir(None)
    rec = _load_record(data)
    out = {"ok": True, "removed": [], "kept": [], "errors": []}

    def fail(text):
        out["ok"] = False
        out["errors"].append(text)
        _say(emit, "error", text)

    have_claude = bool(claude_path())
    gpath = global_config_path()
    gconf, err = _read_json(gpath)
    if err:
        fail(err + ". The deskmate MCP server was left in place.")
        gconf = None
    kind = _mcp_kind(_mcp_entry(gconf or {}))
    if kind in ("ours", "current", "legacy"):
        if not have_claude:
            fail(NO_CLAUDE)
        else:
            if kind == "legacy" and gpath.exists():
                _Backups(data).copy(gpath, ".claude.json")
            code, o, e = _claude(["mcp", "remove", "--scope", "user", MCP_NAME])
            if code == 0:
                out["removed"].append(f"the deskmate MCP server ({_tilde(gpath)})")
                _say(emit, "ok", "Removed the deskmate MCP server.")
            else:
                fail("Claude Code could not remove the deskmate MCP server: " + (_mask(e or o, data) or f"exit {code}"))
    elif kind == "other":
        out["kept"].append(f"an MCP server named deskmate that Deskmate did not add ({_tilde(gpath)})")

    if remove_plugins:
        if not have_claude:
            fail(NO_CLAUDE)
        else:
            try:
                had = f"{PLUGIN}@{MARKETPLACE}" in _installed_plugins()
                uninstall_plugin(PLUGIN, emit)
                if had:
                    out["removed"].append(f"the plugin {PLUGIN}@{MARKETPLACE}")
                if _load_record(data).get("marketplace"):
                    out["kept"].append("the plugin marketplace “deskmate” (another Deskmate plugin still uses it)")
            except ConnectError as e:
                fail(str(e))

    # Hooks and the skill of the old install, when connect never ran to remove them.
    try:
        done = _remove_legacy(data, _Backups(data), emit)
        out["removed"] += [f"{d['what']} ({_tilde(d['where'])})" for d in done]
    except ConnectError as e:
        fail(str(e))

    for path in (_helper_path(data), client_env_path()):
        if path.is_file():
            path.unlink()
            out["removed"].append(_tilde(path))
            _say(emit, "ok", f"Removed {_tilde(path)}.")
        try:
            path.parent.rmdir()  # only when empty: ~/.config/deskmate also holds habits.json
        except OSError:
            pass

    # Keep only what still matters: removed tools restore_conflicts() can put back, and the plugin records
    # while the marketplace stays (habits still installed, or remove_plugins=False).
    rec = _load_record(data)
    keep = {"removed": rec["removed"]} if rec.get("removed") else {}
    if rec.get("marketplace"):
        keep.update({k: rec[k] for k in ("marketplace", "plugins", "settings_keys_added", "settings_existed") if k in rec})
    _save_record(data, keep)
    return out


def status(values: dict | None = None, live: bool = False) -> dict:
    """{mcp: ok|missing|legacy|broken, mcp_detail, connected, plugins: {name: {...}}, marketplace: {...},
    hooks: ok|missing|legacy|off, skill: ok|missing|legacy|off, client_env: ok|missing, claude: {...}}.
    Read-only. live=True also asks `claude mcp get`, which connects to the hub."""
    data = _data_dir(values)
    env = read_client_env()
    out = {"claude": {"installed": bool(claude_path()), "path": claude_path() or ""},
           "mcp": "missing", "mcp_detail": "", "connected": None, "plugins": {}, "marketplace": {},
           "hooks": "missing", "skill": "missing", "client_env": "ok" if env.get("HUB_URL") else "missing"}

    gconf, err = _read_json(global_config_path())
    entry = _mcp_entry(gconf or {})
    kind = _mcp_kind(entry)
    if err:
        out.update(mcp="broken", mcp_detail=err)
    elif kind == "legacy":
        out.update(mcp="legacy", mcp_detail="The old entry with the token written in Claude Code's config; connect replaces it.")
    elif kind in ("ours", "current"):
        helper = Path(_unquote(str(entry.get("headersHelper") or "")))
        token = Path(env.get("TOKEN_FILE") or data / "secrets" / "hub-token")
        port = (values or {}).get("HUB_PORT") or (env.get("HUB_URL", "").rsplit(":", 1)[-1] if env.get("HUB_URL") else "")
        problems = []
        if not (helper.is_file() and os.access(str(helper), os.X_OK)):
            problems.append(f"the header helper {_tilde(helper)} is missing")
        if not token.is_file() or token.stat().st_size == 0:
            problems.append(f"the hub token {_tilde(token)} is missing")
        if port and entry.get("url") != f"http://127.0.0.1:{port}/mcp":
            problems.append(f"it points at {entry.get('url')}, the hub is on port {port}")
        out.update(mcp="broken" if problems else "ok", mcp_detail="; ".join(problems) or str(entry.get("url")))
    elif kind == "other":
        out.update(mcp="broken", mcp_detail="An MCP server named deskmate that Deskmate did not add.")

    settings, _ = _read_json(settings_path())
    settings = settings or {}
    known = settings.get("extraKnownMarketplaces") if isinstance(settings.get("extraKnownMarketplaces"), dict) else {}
    src = (known.get(MARKETPLACE) or {}).get("source") if isinstance(known.get(MARKETPLACE), dict) else None
    mpath = src.get("path") if isinstance(src, dict) else ""
    if mpath:
        ok = (Path(mpath) / ".claude-plugin" / "marketplace.json").is_file()
        out["marketplace"] = {"present": True, "path": mpath, "path_ok": ok, "this_clone": _same_path(mpath, marketplace_dir())}
    else:
        out["marketplace"] = {"present": False, "path": "", "path_ok": False, "this_clone": False}
    enabled = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
    installed = {}
    if out["claude"]["installed"]:
        try:
            installed = _installed_plugins()
        except ConnectError:
            installed = {}
    for name in PLUGINS:
        pid = f"{name}@{MARKETPLACE}"
        info = installed.get(pid) or {}
        out["plugins"][name] = {"installed": bool(info) or pid in enabled, "enabled": enabled.get(pid) is True,
                                "version": info.get("version", ""), "available": _plugin_version(name)}

    plug = out["plugins"][PLUGIN]
    root = Path(mpath) / PLUGIN if mpath else None
    old = legacy()
    if plug["installed"] and plug["enabled"] and root is not None:
        hooks_ok = MATCHER in ((root / "hooks" / "hooks.json").read_text() if (root / "hooks" / "hooks.json").is_file() else "")
        out["hooks"] = "ok" if hooks_ok and out["marketplace"]["path_ok"] else "missing"
        out["skill"] = "ok" if (root / "skills" / "desk" / "SKILL.md").is_file() else "missing"
    elif plug["installed"]:
        out["hooks"] = out["skill"] = "off"
    if old["hooks"]:
        out["hooks"] = "legacy" if out["hooks"] != "ok" else "ok"
        out["legacy_hooks"] = len(old["hooks"])
    if old["skill"] and out["skill"] != "ok":
        out["skill"] = "legacy"
    if live and out["mcp"] in ("ok", "broken"):
        out["connected"], out["mcp_detail"] = mcp_live_status()
    return out


# ---------------------------------------------------------------- competing tools


def conflicts(values: dict | None = None) -> list:
    """Tools that compete with the desk in the active Claude Code config, as found by detect.conflicts():
    [{id, kind: mcp|skill|hook|plugin|legacy, name, where, removable, detail}]. Read-only."""
    try:
        from . import detect

        return detect.conflicts([str(config_dir())])
    except ImportError:
        return []


def _hook_is_viway(event, hook) -> bool:
    return isinstance(hook, dict) and "viway" in str(hook.get("command", "")).lower()


def remove_conflicts(ids, emit=None, values: dict | None = None) -> dict:
    """Remove the chosen conflicts() items, each with a backup, and record how to put them back.
    MCP servers: `claude mcp remove`; skills: moved to <config>/skills.deskmate-removed/; viway hooks:
    taken out of settings.json. Plugins are only reported (turn them off in /plugin). The old Deskmate
    install is handled by connect(). Returns {removed: [...], errors: [...]}."""
    data = _data_dir(values)
    backups = _Backups(data)
    found = {c["id"]: c for c in conflicts(values)}
    rec = _load_record(data)
    out = {"removed": [], "errors": []}
    for cid in ids:
        c = found.get(cid)
        if c is None:
            continue  # already gone
        try:
            if c["kind"] == "mcp":
                gpath = global_config_path()
                gconf, err = _read_json(gpath)
                if err:
                    raise ConnectError(err)
                local = "@" in cid
                project = cid.split("@", 1)[1] if local else None
                servers = ((gconf.get("projects") or {}).get(project) or {}).get("mcpServers") if local else gconf.get("mcpServers")
                entry = (servers or {}).get(c["name"])
                if entry is None:
                    continue
                if local and not Path(project).is_dir():
                    raise ConnectError(f"{c['name']}: its folder {_tilde(project)} no longer exists; remove it with /mcp")
                saved = backups._target(f"mcp-{c['name']}.json")
                _atomic_write(saved, json.dumps(entry, indent=2) + "\n", 0o600)
                backups.copy(gpath, ".claude.json")
                code, o, e = _claude(["mcp", "remove", "--scope", "local" if local else "user", c["name"]],
                                     cwd=project if local else None)
                if code != 0:
                    raise ConnectError(f"{c['name']}: " + (_mask(e or o) or f"exit {code}"))
                rec.setdefault("removed", []).append({"id": cid, "kind": "mcp", "name": c["name"], "scope": "local" if local else "user",
                                                      "project": project, "saved": str(saved), "at": _now()})
            elif c["kind"] == "skill":
                src = Path(c["where"])
                dst = config_dir() / "skills.deskmate-removed" / src.name
                if dst.exists():
                    dst = Path(f"{dst}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                rec.setdefault("removed", []).append({"id": cid, "kind": "skill", "name": c["name"], "from": str(src),
                                                      "to": str(dst), "at": _now()})
            elif c["kind"] == "hook":
                path = settings_path()
                settings, err = _read_json(path)
                if err:
                    raise ConnectError(err)
                # detect.conflicts() makes one item per event: id "hook:viway <event> hook@<event>".
                event = cid.split("@", 1)[1] if "@" in cid else None
                text = path.read_text(encoding="utf-8")
                pick = lambda e, h: (event is None or e == event) and _hook_is_viway(e, h)  # noqa: E731
                where = _hook_positions(settings, pick)
                new, removed = _strip_hooks(settings, pick)
                if not removed:
                    continue
                backup = backups.copy(path, "settings.json")
                _write_json(path, new, text)
                rec.setdefault("removed", []).append({"id": cid, "kind": "hook", "name": c["name"], "settings": str(path),
                                                      "hooks": where, "backup": str(backup), "at": _now()})
            else:
                continue  # plugins: reported only; legacy: connect() replaces it
            out["removed"].append(c["name"])
            _say(emit, "ok", f"Removed {c['name']} ({_tilde(c['where'])}); ./deskmate uninstall can put it back.")
        except (ConnectError, OSError) as e:
            out["errors"].append(f"Could not remove {c['name']}: {e}")
            _say(emit, "warn", f"Could not remove {c['name']}: {e}")
    if backups.dir:
        rec.setdefault("backups", []).append(str(backups.dir))
    _save_record(data, rec)
    return out


def restore_conflicts(emit=None, ids=None) -> dict:
    """Put back what remove_conflicts() removed (all, or the given ids). Returns {restored, errors}."""
    data = _data_dir(None)
    rec = _load_record(data)
    out = {"restored": [], "errors": []}
    left = []
    for item in rec.get("removed") or []:
        if ids is not None and item.get("id") not in ids:
            left.append(item)
            continue
        try:
            if item["kind"] == "mcp":
                entry = json.loads(Path(item["saved"]).read_text(encoding="utf-8"))
                # add-json takes the server's config on its command line, as adding it did originally.
                code, o, e = _claude(["mcp", "add-json", "--scope", item["scope"], item["name"], json.dumps(entry)],
                                     cwd=item.get("project") if item["scope"] == "local" else None)
                if code != 0:
                    raise ConnectError(_mask(e or o) or f"exit {code}")
            elif item["kind"] == "skill":
                if Path(item["from"]).exists():
                    raise ConnectError(f"{_tilde(item['from'])} exists again")
                shutil.move(item["to"], item["from"])
                try:
                    Path(item["to"]).parent.rmdir()  # skills.deskmate-removed, once empty
                except OSError:
                    pass
            elif item["kind"] == "hook":
                path = Path(item["settings"])
                settings, err = _read_json(path)
                if err:
                    raise ConnectError(err)
                text = path.read_text(encoding="utf-8") if path.exists() else ""
                hooks = settings.setdefault("hooks", {})
                # Each entry: [event, matcher, hook, group index, hook index]; put back at the same place.
                for event, matcher, hook, *pos in item["hooks"]:
                    gi, hi = (pos + [None, None])[:2]
                    groups = hooks.setdefault(event, [])
                    group = next((g for g in groups if isinstance(g, dict) and g.get("matcher") == matcher), None)
                    if group is None:
                        group = {"matcher": matcher, "hooks": []} if matcher is not None else {"hooks": []}
                        groups.insert(len(groups) if gi is None else min(gi, len(groups)), group)
                    inner = group.setdefault("hooks", [])
                    if hook not in inner:
                        inner.insert(len(inner) if hi is None else min(hi, len(inner)), hook)
                _write_json(path, settings, text)
            out["restored"].append(item["name"])
            _say(emit, "ok", f"Put back {item['name']}.")
        except (ConnectError, OSError, ValueError, KeyError) as e:
            left.append(item)
            out["errors"].append(f"Could not put back {item.get('name')}: {e}")
            _say(emit, "warn", f"Could not put back {item.get('name')}: {e}")
    if left:
        rec["removed"] = left
    else:
        rec.pop("removed", None)
    _save_record(data, rec)
    return out
