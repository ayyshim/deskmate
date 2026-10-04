"""What this machine has: the platform, Docker, Claude Code, free ports, the time zone, the user's name,
Claude Code's sessions and the folders they ran in, repos, docs hubs, and tools that compete with the desk.

Every function returns plain dicts and lists and never raises. Slow ones (Docker, the claude CLI, the
transcript scan) are cached for the life of the process; clear_cache() forgets them ("Check again").
Nothing here writes anything.
"""

from __future__ import annotations

import errno
import json
import os
import platform as _platform
import re
import shutil
import socket
import sys
import time
from pathlib import Path

from . import util

try:  # Unix only; the launcher refuses native Windows before importing this
    import pwd
except ImportError:  # pragma: no cover
    pwd = None

_CACHE: dict = {}


def _cached(key, fn):
    if key not in _CACHE:
        _CACHE[key] = fn()
    return _CACHE[key]


def clear_cache() -> None:
    _CACHE.clear()


def home() -> str:
    h = os.environ.get("HOME")
    if not h and pwd is not None:
        try:
            h = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            h = ""
    return (h or os.path.expanduser("~")).rstrip("/") or "/"


# ---------------------------------------------------------------- the computer


def _os_release() -> dict:
    out = {}
    for f in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in Path(f).read_text().splitlines():
                k, _, v = line.partition("=")
                if k:
                    out[k.strip()] = v.strip().strip('"')
            break
        except OSError:
            continue
    return out


def _host_memory_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    if sys.platform == "darwin":
        code, out, _ = util.run(["sysctl", "-n", "hw.memsize"], timeout=5)
        if code == 0 and out.strip().isdigit():
            return round(int(out.strip()) / 2**30, 1)
    return 0.0


def platform() -> dict:
    """{os: linux|macos|wsl|windows, arch, label, python, memory_gb}."""

    def _do():
        arch = _platform.machine() or "unknown"
        arch = {"amd64": "x86_64", "AMD64": "x86_64", "aarch64": "arm64"}.get(arch, arch)
        py = ".".join(str(x) for x in sys.version_info[:3])
        if sys.platform == "darwin":
            chip = "Apple Silicon" if arch == "arm64" else "Intel"
            os_id, label = "macos", f"macOS {_platform.mac_ver()[0] or ''} on {chip}".replace("  ", " ")
        elif sys.platform.startswith("linux"):
            name = _os_release().get("PRETTY_NAME") or "Linux"
            if util.is_wsl():
                os_id, label = "wsl", f"{name} in WSL 2 on {arch}"
            else:
                os_id, label = "linux", f"{name} on {arch}"
        elif sys.platform in ("win32", "cygwin", "msys"):
            os_id, label = "windows", "Windows"
        else:
            os_id, label = sys.platform, sys.platform
        return {"os": os_id, "arch": arch, "label": label, "python": py, "memory_gb": _host_memory_gb()}

    return _cached("platform", _do)


# ---------------------------------------------------------------- Docker

_DOCKER_EXTRA = ("/usr/local/bin/docker", "/opt/homebrew/bin/docker", "/Applications/Docker.app/Contents/Resources/bin/docker")


def docker(timeout: float = 10.0) -> dict:
    """{installed, running, version, desktop, rootless, compose_version, sudo_needed, memory_gb, label, fix, ...}."""

    def _do():
        p = platform()
        out = {"installed": False, "running": False, "version": "", "desktop": False, "rootless": False,
               "compose_version": "", "sudo_needed": False, "memory_gb": 0.0, "label": "", "fix": "",
               "path": "", "os_name": "", "context": "", "root_dir": "", "desktop_version": "", "error": ""}
        path = util.which("docker", extra=_DOCKER_EXTRA)
        if not path:
            out["label"] = "Docker isn't installed"
            out["fix"] = ("Install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/"
                          if p["os"] == "macos" else
                          "Install Docker Engine with the compose plugin: https://docs.docker.com/engine/install/"
                          if p["os"] == "linux" else
                          "Install Docker Desktop and turn on WSL integration for this distro, or Docker Engine inside WSL: "
                          "https://docs.docker.com/desktop/features/wsl/")
            return out
        out["installed"], out["path"] = True, path
        code, so, se = util.run([path, "info", "--format", "{{json .}}"], timeout=timeout)
        info = {}
        if so.strip().startswith("{"):
            try:
                info = json.loads(so.strip().splitlines()[0])
            except ValueError:
                info = {}
        if code == 0 and info.get("ServerVersion"):
            out["running"] = True
            out["version"] = info.get("ServerVersion", "")
            out["os_name"] = info.get("OperatingSystem", "")
            out["desktop"] = "docker desktop" in out["os_name"].lower()
            out["rootless"] = any("rootless" in str(s) for s in (info.get("SecurityOptions") or []))
            out["memory_gb"] = round((info.get("MemTotal") or 0) / 2**30, 1)
            out["root_dir"] = info.get("DockerRootDir", "")
        else:
            err = (se + "\n" + so).strip()
            low = err.lower()
            out["error"] = next((ln.strip() for ln in se.splitlines() if ln.strip()), "")[:300]
            if "permission denied" in low:
                out["sudo_needed"] = True
                out["label"] = "Docker needs sudo here"
                out["fix"] = "sudo usermod -aG docker $USER, then log out and in again (setup never uses sudo itself)"
            elif code == 124:
                out["label"] = "Docker doesn't answer"
                out["fix"] = "Restart Docker, then check again"
            else:
                out["label"] = "Docker isn't running"
                out["fix"] = ("Start Docker Desktop (open -a Docker), then check again" if p["os"] == "macos" else
                              "sudo systemctl start docker (or start Docker Desktop), then check again")
        code, so, _ = util.run([path, "context", "show"], timeout=5)
        out["context"] = so.strip() if code == 0 else ""
        if out["context"].startswith("desktop-"):
            out["desktop"] = True
        code, so, _ = util.run([path, "compose", "version", "--short"], timeout=10)
        if code == 0 and so.strip():
            out["compose_version"] = so.strip().lstrip("v")
        if out["running"]:
            code, so, _ = util.run([path, "version", "--format", "{{.Server.Platform.Name}}"], timeout=5)
            if code == 0 and "desktop" in so.lower():
                out["desktop"] = True
                v = util.version_tuple(so)
                out["desktop_version"] = ".".join(str(x) for x in v) if v else ""
            if out["desktop"]:
                what = f"Docker Desktop {out['desktop_version']}".strip()
                out["label"] = f"{what} is running (engine {out['version']})"
            else:
                out["label"] = f"Docker Engine {out['version']} is running" + (" (rootless)" if out["rootless"] else "")
        return out

    return _cached("docker", _do)


def network_default(d: dict | None = None, p: dict | None = None) -> str:
    """host on a Linux rootful Engine (and Engine inside WSL); host-access on Docker Desktop and on
    rootless Engines before 29.5, where 'host' is not the user's real localhost."""
    d = d or docker()
    p = p or platform()
    if p.get("os") == "macos" or d.get("desktop"):
        return "host-access"
    if d.get("rootless") and util.version_tuple(d.get("version", "")) < (29, 5, 0):
        return "host-access"
    return "host"


def network_choices(d: dict | None = None, p: dict | None = None) -> list:
    """The three modes with labels; the ones that cannot work here are marked disabled."""
    d = d or docker()
    p = p or platform()
    best = network_default(d, p)
    where = "your Mac" if p.get("os") == "macos" else "this computer"
    host = {"value": "host", "label": "This computer's localhost",
            "detail": "Agents can open your dev servers, like localhost:5173, and any website. "
                      "Both containers share this computer's network (Linux Docker Engine)."}
    access = {"value": "host-access", "label": f"{where[0].upper() + where[1:]}'s localhost, through Docker Desktop",
              "detail": "Agents can open your dev servers, like localhost:5173, and any website. The desk reaches "
                        f"{where} through host.docker.internal; start dev servers on 127.0.0.1, not only ::1."}
    isolated = {"value": "isolated", "label": "Internet only",
                "detail": f"The desk can't reach anything on {where}. Choose this if agents will browse sites "
                          "you don't fully trust."}
    if best == "host":
        access["disabled"] = True
        access["detail"] = "Only for Docker Desktop. On Linux the desk shares this computer's network instead."
    else:
        host["disabled"] = True
        host["detail"] = ("Needs a Linux Docker Engine (rootful, or rootless 29.5 or newer). "
                          "Docker Desktop keeps containers in a VM.")
    for c in (host, access, isolated):
        c.setdefault("disabled", False)
        if c["value"] == best:
            c["label"] += " (recommended)"
    return [host, access, isolated]


def own_stack(repo, project: str) -> dict:
    """Whether this repo's Deskmate (compose project `project`) has containers, and whether they run."""

    def _do():
        d = docker()
        out = {"exists": False, "running": False, "working_dir": "", "containers": [], "other_dir": ""}
        if not d["running"]:
            return out
        fmt = '{{.Names}}\t{{.State}}\t{{.Label "com.docker.compose.project.working_dir"}}'
        code, so, _ = util.run([d["path"], "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                                "--format", fmt], timeout=10)
        if code != 0:
            return out
        here = os.path.realpath(str(repo))
        for line in so.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            name, state, wd = parts[0], parts[1], parts[2]
            if wd and os.path.realpath(wd) != here:
                out["other_dir"] = wd
                continue
            out["exists"] = True
            out["working_dir"] = wd
            out["containers"].append({"name": name, "state": state})
            if state == "running":
                out["running"] = True
        return out

    return _cached(("own_stack", str(repo), project), _do)


def project_name(repo, user: str = "") -> str:
    """'deskmate' (keeps the desk's volumes), or 'deskmate-<user>' when another user's Deskmate already
    uses that name on this Docker daemon (containers and volumes are per daemon, not per user)."""

    def _do():
        d = docker()
        if not d["running"]:
            return "deskmate"
        code, so, _ = util.run([d["path"], "ps", "-a", "--filter", "label=com.docker.compose.project=deskmate",
                                "--format", '{{.Label "com.docker.compose.project.working_dir"}}'], timeout=10)
        if code != 0:
            return "deskmate"
        for wd in {ln.strip() for ln in so.splitlines() if ln.strip()}:
            if not _same_user(wd):
                name = re.sub(r"[^a-z0-9_-]+", "-", (user or _login()).lower()).strip("-") or "user"
                return f"deskmate-{name}"
        return "deskmate"

    return _cached(("project_name", str(repo)), _do)


def _same_user(folder: str) -> bool:
    try:
        return os.stat(folder).st_uid == os.getuid()
    except OSError:
        h = home()
        return folder == h or folder.startswith(h + "/")


def _login() -> str:
    for k in ("USER", "LOGNAME"):
        if os.environ.get(k):
            return os.environ[k]
    if pwd is not None:
        try:
            return pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            pass
    return "user"


# ---------------------------------------------------------------- Claude Code


def claude_cli() -> dict:
    """{installed, path, version}. `claude --version` writes nothing to Claude Code's config."""

    def _do():
        h = Path(home())
        path = util.which("claude", extra=(h / ".local/bin/claude", h / ".claude/local/claude",
                                           "/opt/homebrew/bin/claude", "/usr/local/bin/claude"))
        if not path:
            return {"installed": False, "path": "", "version": ""}
        code, so, _ = util.run([path, "--version"], timeout=20)
        m = re.search(r"\d+\.\d+\.\d+", so)
        return {"installed": True, "path": path, "version": m.group(0) if m else "", "answers": code == 0}

    return _cached("claude", _do)


def default_config_dir() -> str:
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return os.path.abspath(os.path.expanduser(env)).rstrip("/") if env else os.path.join(home(), ".claude")


_PROFILE_RE = re.compile(r"CLAUDE_CONFIG_DIR\s*[= ]\s*[\"']?([^\"'\s;]+)")


def _count_transcripts(config_dir: str) -> int:
    n = 0
    try:
        for folder in Path(config_dir, "projects").iterdir():
            if folder.is_dir():
                try:
                    n += sum(1 for f in folder.iterdir() if f.name.endswith(".jsonl") and f.is_file())
                except OSError:
                    continue
    except OSError:
        return 0
    return n


def claude_config_dirs() -> list:
    """[{path, sessions, has_projects, source}]: $CLAUDE_CONFIG_DIR (or ~/.claude), ~/.claude, and any
    folder a shell profile points CLAUDE_CONFIG_DIR at. Only folders with projects/ get used."""

    def _do():
        h = home()
        cands = []
        if os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
            cands.append((os.environ["CLAUDE_CONFIG_DIR"], "CLAUDE_CONFIG_DIR"))
        cands.append((os.path.join(h, ".claude"), "default"))
        for name in (".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".zshenv", ".profile", ".config/fish/config.fish"):
            try:
                text = Path(h, name).read_text(errors="replace")[:1_000_000]
            except OSError:
                continue
            for m in _PROFILE_RE.finditer(text):
                raw = m.group(1).replace("$HOME", h).replace("${HOME}", h)
                cands.append((raw, "~/" + name))
        out, seen = [], set()
        for raw, source in cands:
            p = os.path.abspath(os.path.expanduser(raw)).rstrip("/")
            if not p or p in seen or "$" in p:
                continue
            seen.add(p)
            has = Path(p, "projects").is_dir()
            out.append({"path": p, "sessions": _count_transcripts(p) if has else 0, "has_projects": has, "source": source})
        return out

    return _cached("config_dirs", _do)


_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def first_cwd(transcript) -> str | None:
    """The working folder a session started in: the first "cwd" within its first 50 lines.
    '' when there is none, None when the file can't be read."""
    try:
        with open(str(transcript), "rb") as fh:
            seen = 0
            for i, raw in enumerate(fh):
                seen += len(raw)
                if i >= 50 or seen > 8 * 1024 * 1024:
                    break
                if b'"cwd"' not in raw:
                    continue
                m = _CWD_RE.search(raw.decode("utf-8", "replace"))
                if m:
                    try:
                        return json.loads('"' + m.group(1) + '"')
                    except ValueError:
                        return m.group(1)
    except OSError:
        return None
    return ""


def _group_of(cwd: str, h: str) -> str:
    """The folder a session's cwd is grouped under: the folder directly under home that holds it,
    home itself, or (outside home) its first two levels."""
    cwd = cwd.rstrip("/") or "/"
    if cwd == h:
        return h
    if cwd.startswith(h + "/"):
        return h + "/" + cwd[len(h) + 1:].split("/")[0]
    parts = [p for p in cwd.split("/") if p]
    return "/" + "/".join(parts[:2]) if parts else "/"


def _scan_sessions(dirs: tuple) -> dict:
    per, skipped, total = {}, 0, 0
    for d in dirs:
        try:
            folders = [f for f in Path(d, "projects").iterdir() if f.is_dir()]
        except OSError:
            continue
        for folder in folders:
            try:  # top-level transcripts only: <id>/subagents/ and workflows/ are not sessions
                files = [f for f in folder.iterdir() if f.name.endswith(".jsonl") and f.is_file()]
            except OSError:
                skipped += 1
                continue
            for f in files:
                total += 1
                cwd = first_cwd(f)
                if cwd is None:
                    skipped += 1
                    continue
                if not cwd:
                    continue
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    mtime = 0.0
                row = per.setdefault(cwd, [0, 0.0])
                row[0] += 1
                row[1] = max(row[1], mtime)
    return {"per": per, "skipped": skipped, "total": total}


def _dirs_of(config_dirs) -> tuple:
    out = []
    for d in config_dirs or []:
        p = d.get("path") if isinstance(d, dict) else str(d)
        if p and p not in out:
            out.append(p)
    return tuple(out)


def session_folders(config_dirs) -> list:
    """[{path, sessions, last_ts, parent}], one per folder sessions started in (from each transcript's
    recorded cwd, never from Claude Code's lossy encoded folder names), sorted by group then path."""
    dirs = _dirs_of(config_dirs)
    scan = _cached(("sessions", dirs), lambda: _scan_sessions(dirs))
    h = home()
    rows = [{"path": cwd, "sessions": n, "last_ts": last, "parent": _group_of(cwd, h)}
            for cwd, (n, last) in scan["per"].items()]
    rows.sort(key=lambda r: (r["parent"], r["path"]))
    return rows


def session_stats(config_dirs) -> dict:
    """{total, skipped}: how many transcripts were read, and how many could not be."""
    dirs = _dirs_of(config_dirs)
    scan = _cached(("sessions", dirs), lambda: _scan_sessions(dirs))
    return {"total": scan["total"], "skipped": scan["skipped"]}


def rank_roots(rows) -> list:
    """Group session folders under their parents, most sessions first:
    [{path, sessions, last_ts, children, home}]. Home itself is a group of its own."""
    h = home()
    groups = {}
    for r in rows:
        g = groups.setdefault(r["parent"], {"path": r["parent"], "sessions": 0, "last_ts": 0.0,
                                            "children": [], "home": r["parent"] == h})
        g["sessions"] += r["sessions"]
        g["last_ts"] = max(g["last_ts"], r["last_ts"])
        if r["path"] != r["parent"]:
            g["children"].append(r["path"])
    return sorted(groups.values(), key=lambda g: (-g["sessions"], g["path"]))


# ---------------------------------------------------------------- repos and docs

_SKIP = {"node_modules", ".venv", "venv", "__pycache__", "vendor", "dist", "build", "target", ".git", ".cache"}


def _git_main(gitfile: Path) -> str:
    """The main repo of a linked worktree, from its .git file ('gitdir: <main>/.git/worktrees/<name>')."""
    try:
        line = gitfile.read_text(errors="replace").strip()
    except OSError:
        return ""
    if not line.startswith("gitdir:"):
        return ""
    target = line[len("gitdir:"):].strip()
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(str(gitfile.parent), target))
    m = re.match(r"(.*)/\.git/worktrees/[^/]+/?$", target)
    return m.group(1) if m else ""


def repos(roots, depth: int = 3, limit: int = 500) -> list:
    """[{path, name, worktree_of}]: git repos in the roots, up to `depth` levels down. Linked worktrees
    name their main repo. Hidden folders, dependency folders and symlinks are not followed."""
    out, seen = [], set()
    deadline = time.monotonic() + 5.0

    def walk(p: Path, level: int):
        if len(out) >= limit or time.monotonic() > deadline:
            return
        rp = os.path.realpath(str(p))
        if rp in seen:
            return
        seen.add(rp)
        git = p / ".git"
        try:
            if git.is_dir():
                out.append({"path": str(p), "name": p.name, "worktree_of": ""})
                return
            if git.is_file():
                out.append({"path": str(p), "name": p.name, "worktree_of": _git_main(git)})
                return
        except OSError:
            return
        if level >= depth:
            return
        try:
            children = sorted(c for c in p.iterdir() if c.is_dir() and not c.is_symlink()
                              and not c.name.startswith(".") and c.name not in _SKIP)
        except OSError:
            return
        for c in children:
            walk(c, level + 1)

    for root in util.split_list(roots):
        walk(Path(root), 0)
    return out


def _count(path: Path, pattern: str) -> int:
    try:
        return sum(1 for _ in path.glob(pattern))
    except OSError:
        return 0


def docs_hubs(roots) -> list:
    """[{path, kind, label, changelog_entries, changelog_repos, design_docs, project_indexes, kb_notes}]:
    folders in the roots (the root itself, its children and grandchildren) that hold a docs hub, which is
    a folder with changelog/README.md or design/_template.md."""
    out, seen = [], set()
    for root in util.split_list(roots):
        base = Path(root)
        cands = [base]
        try:
            for c in sorted(base.iterdir()):
                if c.is_dir() and not c.is_symlink() and c.name not in _SKIP:
                    cands.append(c)
                    try:
                        cands.extend(sorted(g for g in c.iterdir() if g.is_dir() and not g.is_symlink()
                                            and not g.name.startswith(".") and g.name not in _SKIP))
                    except OSError:
                        pass
        except OSError:
            continue
        for c in cands:
            try:
                is_hub = (c / "changelog" / "README.md").is_file() or (c / "design" / "_template.md").is_file()
            except OSError:
                continue
            if not is_hub or str(c) in seen:
                continue
            seen.add(str(c))
            entries = _count(c / "changelog", "*/*.md")
            repos_n = _count(c / "changelog", "*/")
            out.append({"path": str(c), "kind": "hub", "label": util.tilde(str(c), home()),
                        "changelog_entries": entries, "changelog_repos": repos_n,
                        "design_docs": _count(c / "design", "*/"), "project_indexes": _count(c / "projects", "*/INDEX.md"),
                        "kb_notes": _count(c / "knowledge-base", "*.md")})
    return out


# ---------------------------------------------------------------- ports, display, time zone, name


def _port_busy(port: int) -> bool:
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            if sys.platform.startswith("linux"):  # ignore TIME_WAIT leftovers; a listener still blocks
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or exc.errno == getattr(errno, "WSAEADDRINUSE", -1):
                return True
        finally:
            s.close()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _port_owner(port: int) -> str:
    if sys.platform.startswith("linux") and shutil.which("ss"):
        code, out, _ = util.run(["ss", "-Hltnp", f"sport = :{port}"], timeout=5)
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', out)
        if m:
            return f"{m.group(1)} (process {m.group(2)})"
    elif shutil.which("lsof"):
        code, out, _ = util.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], timeout=5)
        pid = re.search(r"^p(\d+)", out, re.M)
        cmd = re.search(r"^c(.+)$", out, re.M)
        if pid:
            return f"{cmd.group(1) if cmd else 'a program'} (process {pid.group(1)})"
    return ""


def ports(port_list) -> dict:
    """{port: {port, free, owner}} from a bind test on 127.0.0.1 and ::1 (owner from ss or lsof)."""
    out = {}
    for p in port_list:
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        busy = _port_busy(p)
        out[p] = {"port": p, "free": not busy, "owner": _port_owner(p) if busy else ""}
    return out


def free_port(start: int, avoid=(), ours=()) -> int:
    """`start` when it is free (or already Deskmate's), else the next free port within 100."""
    for p in range(start, start + 100):
        if p in avoid:
            continue
        if p in ours or not _port_busy(p):
            return p
    return start


def display_free(n: int) -> bool:
    """X display :n is free: no /tmp/.X11-unix/X<n> socket, no lock file, and (Linux) the abstract
    socket is free too, which host networking shares with this computer."""
    if os.path.exists(f"/tmp/.X11-unix/X{n}") or os.path.exists(f"/tmp/.X{n}-lock"):
        return False
    if sys.platform.startswith("linux"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(f"\0/tmp/.X11-unix/X{n}")
        except OSError:
            return False
        finally:
            s.close()
    return True


def free_display(start: int = 87, ours=()) -> int:
    for n in range(start, start + 100):
        if n in ours or display_free(n):
            return n
    return start


_TZ_RE = re.compile(r"^[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$")


def valid_tz(name: str) -> bool:
    name = (name or "").strip()
    if not name or not _TZ_RE.match(name) or name.startswith("posix") or name.startswith("right/"):
        return False
    if name in ("UTC", "Etc/UTC", "GMT"):
        return True
    try:
        import zoneinfo

        zoneinfo.ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001 - no zoneinfo module or no such zone: check the files instead
        pass
    return any(Path(d, name).is_file() for d in ("/usr/share/zoneinfo", "/var/db/timezone/zoneinfo", "/usr/lib/zoneinfo"))


def all_timezones() -> list:
    try:
        import zoneinfo

        return sorted(z for z in zoneinfo.available_timezones() if "/" in z or z == "UTC")
    except Exception:  # noqa: BLE001
        return []


def timezone() -> str:
    """The IANA zone: $TZ, /etc/timezone, the /etc/localtime link (Linux and macOS), timedatectl, else UTC."""

    def _do():
        tz = os.environ.get("TZ", "").strip().lstrip(":")
        if valid_tz(tz):
            return tz
        try:
            name = Path("/etc/timezone").read_text().strip()
            if valid_tz(name):
                return name
        except OSError:
            pass
        try:
            link = os.readlink("/etc/localtime")
            if "zoneinfo/" in link:
                name = link.split("zoneinfo/", 1)[1]
                if valid_tz(name):
                    return name
        except OSError:
            pass
        if shutil.which("timedatectl"):
            code, out, _ = util.run(["timedatectl", "show", "-p", "Timezone", "--value"], timeout=5)
            if code == 0 and valid_tz(out.strip()):
                return out.strip()
        return "UTC"

    return _cached("tz", _do)


def clean_name(name: str) -> str:
    """1-40 characters, no control characters or angle brackets."""
    name = re.sub(r"[\x00-\x1f\x7f<>]", "", name or "").strip()
    return name[:40].strip()


def owner() -> str:
    """The first word of git's user.name, else the account's full name, else the login name."""

    def _do():
        code, out, _ = util.run(["git", "config", "user.name"], timeout=5, cwd=home())
        name = out.strip().split()[0] if code == 0 and out.strip() else ""
        if not name and sys.platform == "darwin":
            code, out, _ = util.run(["id", "-F"], timeout=5)
            name = out.strip().split()[0] if code == 0 and out.strip() else ""
        if not name and pwd is not None:
            try:
                gecos = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
                name = gecos.split()[0] if gecos else ""
            except (KeyError, IndexError):
                name = ""
        if not name:
            login = _login()
            name = login[:1].upper() + login[1:]
        return clean_name(name) or "the user"

    return _cached("owner", _do)


def machine_name() -> str:
    if sys.platform == "darwin":
        code, out, _ = util.run(["scutil", "--get", "ComputerName"], timeout=5)
        if code == 0 and out.strip():
            return out.strip()
    return socket.gethostname().split(".")[0] or "this computer"


def disk_free_gb(path) -> float:
    try:
        return round(shutil.disk_usage(str(util.nearest_existing(path))).free / 1e9, 1)
    except OSError:
        return 0.0


# ---------------------------------------------------------------- tools that compete with the desk

_BROWSER_RE = re.compile(r"playwright|puppeteer|chrome-devtools|browser|selenium", re.I)


def _read_json(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(errors="replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _user_json(config_dir: str) -> Path:
    """Where Claude Code keeps user-scope MCP servers for this config folder."""
    if os.path.realpath(config_dir) == os.path.realpath(os.path.join(home(), ".claude")):
        return Path(home(), ".claude.json")
    return Path(config_dir, ".claude.json")


def _static_auth(server: dict) -> bool:
    headers = server.get("headers") if isinstance(server, dict) else None
    return isinstance(headers, dict) and any(k.lower() == "authorization" for k in headers)


def conflicts(config_dirs) -> list:
    """[{id, kind: mcp|skill|hook|plugin|legacy, name, where, removable, detail}]. Read-only: never shows a
    token, a header value or a command line, only names and places. 'legacy' marks the old Deskmate install
    (its MCP entry with the token in Claude Code's config, the copied skill, the hook.sh hooks)."""
    out = []

    def add(kind, name, where, removable, detail, extra=""):
        cid = f"{kind}:{name}" + (f"@{extra}" if extra else "")
        if all(c["id"] != cid for c in out):
            out.append({"id": cid, "kind": kind, "name": name, "where": where, "removable": removable, "detail": detail})

    for d in _dirs_of(config_dirs):
        uj = _user_json(d)
        data = _read_json(uj)
        servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
        for name, srv in servers.items():
            if name == "deskmate":
                if _static_auth(srv):
                    add("legacy", "deskmate", str(uj), True,
                        "The old Deskmate MCP server, with its token stored in Claude Code's config. "
                        "Connecting replaces it with one that reads the token from Deskmate's own file.")
                continue
            blob = name + " " + json.dumps(srv if isinstance(srv, dict) else {}, default=str)
            if _BROWSER_RE.search(blob):
                add("mcp", name, str(uj), True, "A browser MCP server (user scope). Sessions may use it instead of the desk.")
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        for proj, pdata in projects.items():
            ps = pdata.get("mcpServers") if isinstance(pdata, dict) else None
            for name, srv in (ps or {}).items():
                blob = name + " " + json.dumps(srv if isinstance(srv, dict) else {}, default=str)
                if name != "deskmate" and _BROWSER_RE.search(blob):
                    add("mcp", name, f"{uj} (for {util.tilde(proj, home())})", True,
                        "A browser MCP server for one folder (local scope).", extra=proj)
        skills = Path(d, "skills")
        try:
            entries = sorted(skills.iterdir())
        except OSError:
            entries = []
        for s in entries:
            if not s.is_dir():
                continue
            low = s.name.lower()
            if low == "deskmate":
                try:
                    text = (s / "SKILL.md").read_text(errors="replace")[:4000]
                except OSError:
                    text = ""
                if "name: deskmate" in text:
                    add("legacy", "deskmate skill", str(s), True,
                        "The old copied Deskmate skill. The Deskmate plugin brings it now.")
            elif low.startswith("viway") or low.startswith("deskfish") or _BROWSER_RE.search(low):
                add("skill", s.name, str(s), True, "A skill for another browser or desk tool.")
        settings = _read_json(Path(d, "settings.json"))
        hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
        for event, groups in hooks.items():
            for g in groups if isinstance(groups, list) else []:
                for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                    cmd = str(h.get("command", "")) if isinstance(h, dict) else ""
                    if "/deskmate/hook.sh" in cmd:
                        add("legacy", f"deskmate {event} hook", str(Path(d, "settings.json")), True,
                            "A hook from the old Deskmate install. The Deskmate plugin brings its hooks now.", extra=event)
                    elif "viway" in cmd.lower():
                        add("hook", f"viway {event} hook", str(Path(d, "settings.json")), True,
                            "A hook from viway, which also drives a browser.", extra=event)
        enabled = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
        for plugin, on in enabled.items():
            if on and not plugin.startswith("deskmate@") and not plugin.startswith("habits@") \
                    and (_BROWSER_RE.search(plugin) or "viway" in plugin.lower()):
                add("plugin", plugin, str(Path(d, "settings.json")), False,
                    "A plugin with browser tools. Turn it off in Claude Code's /plugin if sessions should use the desk.")
    return out
