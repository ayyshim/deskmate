"""Working habits: the rules block, the docs hub, team agents and the end-of-turn check (the habits pack).

`./deskmate setup` can give every Claude Code session in a picked folder the same working habits: a docs
hub (a new one from pack/hub-skeleton, an existing folder, or none), a "Working
habits" block in the folder's CLAUDE.md (pack/rules.md.tmpl), team subagents in <folder>/.claude/agents/,
and the habits plugin (plugin/habits: three skills and the end-of-turn changelog check, whose config is
${XDG_CONFIG_HOME:-~/.config}/deskmate/habits.json).

Everything this module changes outside Deskmate's own folders is recorded in <data>/installed.json under
"habits", and remove() undoes exactly that. It never touches text outside its markers, copies a file into
<data>/backups/ before it edits it, writes through a temp file and a rename (keeping the file's mode), and
never deletes a hub or a file the user edited. Python 3.9 stdlib only (macOS system Python).

Values read (all optional; detect() fills in what is missing):
  HABITS                on | off             off: apply() removes what was installed
  HABITS_FOLDERS        paths                the folders to set up (a list or ':'-separated); default: the
                                             folders set up before, else SESSIONS_ROOTS
  HABITS_FOLDER         path                 only this folder (`habits apply --folder`); others stay as they are
  HABITS_CHECK          off|remind|require   the end-of-turn check; default as installed, else the hub's
                                             team manifest's check_mode, else remind
  HABITS_FOLDER_OPTIONS {folder: {...}}      per-folder choices, see FOLDER_OPTIONS (a dict or its JSON)
  HABITS_ALLOW_NOTIFY   bool                 allow mcp__deskmate__notify without a prompt (default: on when
                                             notifications are on)
  HABITS_MEMORY_ON      bool                 turn Claude Code's auto memory on if it is off (default off)
  HABITS_PLUGIN         bool                 install the habits plugin (default on)
  HABITS_UPDATE         bool                 fetch team agents from git again
  DESKMATE_OWNER, NOTIFY_KIND, SESSIONS_ROOTS, DOCS_DIR, DESKMATE_DATA_DIR are read, never written.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

PHASE = "Working habits"
BLOCK_VERSION = 1
SKILL_PREFIX = "habits:"
PLUGIN = "habits"
NOTIFY_TOOL = "mcp__deskmate__notify"
MODES = ("off", "remind", "require")
DEFAULT_HUB_NAME = "claude"
DEFAULT_CHANGELOG_DIR = "changelog"
MANIFEST = "deskmate-team.json"
AGENTS_MANIFEST = ".deskmate.json"

# Per-folder choices (HABITS_FOLDER_OPTIONS[<folder>]) and what they mean. detect() fills in defaults.
FOLDER_OPTIONS = {
    "block": "write the rules block (default: off when the folder already has its own rules)",
    "hub": "new | existing | none",
    "hub_path": "the existing hub, or where to create the new one (default <folder>/<hub_name>)",
    "hub_name": "folder name for a new hub (default claude)",
    "git_init": "new hub: git init and a first commit (default on; off inside a git work tree)",
    "fill_missing": "existing hub: add the skeleton files it lacks, never overwriting (default off)",
    "skip_repos": "repo names the end-of-turn check ignores and the rules leave out",
    "agents": "none | manifest | git | folder",
    "agents_source": "git URL or folder of the team agents",
    "agents_path": "subfolder that holds the agent .md files (default agents)",
    "agents_accept": "import even though the lint found something (default off)",
    "on_edit": "skip | overwrite | keep, when the block was edited by hand (default skip)",
}

BEGIN_RE = re.compile(r"^<!-- deskmate:habits:begin\b.*-->$")
END_RE = re.compile(r"^<!-- deskmate:habits:end -->$")
MARK_SHA = re.compile(r"\bblock=([0-9a-f]+)")
MARK_SINCE = re.compile(r"\bsince (\d{4}-\d{2}-\d{2})")
MARKER_LINE = re.compile(r"<!--\s*deskmate:habits:(begin|end)\b")

# A folder "already has its own rules" when its instructions name a changelog folder and design docs.
OWN_CHANGELOG = re.compile(r"changelog/", re.I)
OWN_DESIGN = re.compile(r"design[ _-]?docs?\b|design/", re.I)

SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}


class TemplateError(ValueError):
    """A template that does not render cleanly: an unknown {name}, an unbalanced {?flag} section."""


class BlockError(ValueError):
    """Markers that cannot be trusted: one of them twice, or the end before the begin."""


# ---------------------------------------------------------------- paths


def repo_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def pack_dir() -> Path:
    return repo_dir() / "pack"


def plugin_dir() -> Path:
    return repo_dir() / "plugin" / PLUGIN


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _xdg(name: str, default: str) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value) if value and os.path.isabs(value) else _home() / default


def config_path() -> Path:
    """The end-of-turn check's config, read by plugin/habits/bin/habits-stop."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "deskmate" / "habits.json"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "deskmate" / "habits"


def claude_dir() -> Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(os.path.expanduser(value)) if value else _home() / ".claude"


def data_dir(values: dict | None = None) -> Path:
    """Deskmate's data folder. The environment wins (as everywhere in setup), then the values, then the
    DATA_DIR that connect wrote to client.env (so uninstall finds it without .env), then
    settings.data_dir(), then the XDG default."""
    env = os.environ.get("DESKMATE_DATA_DIR", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    if values and str(values.get("DESKMATE_DATA_DIR") or "").strip():
        return Path(os.path.expanduser(str(values["DESKMATE_DATA_DIR"]).strip()))
    if values is None:
        try:
            for line in (_xdg("XDG_CONFIG_HOME", ".config") / "deskmate" / "client.env").read_text().splitlines():
                if line.startswith("DATA_DIR=") and line[9:].strip():
                    return Path(line[9:].strip())
        except OSError:
            pass
    try:
        from . import settings  # A1's module; absent while it is being written

        return Path(settings.data_dir(values))
    except Exception:
        return _xdg("XDG_DATA_HOME", ".local/share") / "deskmate"


def installed_path(values: dict | None = None) -> Path:
    return data_dir(values) / "installed.json"


# ---------------------------------------------------------------- small helpers


def _norm(p) -> str:
    return os.path.normpath(os.path.expanduser(str(p).strip()))


def _real(p) -> str:
    return os.path.realpath(_norm(p))


def _same(a, b) -> bool:
    return _real(a) == _real(b)


def _inside(child, parent) -> bool:
    """Whether child is parent or below it (symlinks resolved)."""
    c, p = _real(child), _real(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _label(p) -> str:
    """A path with the home folder shown as ~."""
    s, home = _norm(p), str(_home())
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def _paths(value) -> list:
    """A list of folders from a list or a ':'-separated string; normalised, without duplicates."""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[:\n]", str(value))
    out = []
    for item in items:
        item = str(item).strip()
        if item:
            p = _norm(item).rstrip(os.sep) or os.sep
            if p not in out:
                out.append(p)
    return out


def _bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("on", "1", "true", "yes", "y"):
        return True
    if s in ("off", "0", "false", "no", "n"):
        return False
    return default


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _sha_file(path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _read_json(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _today() -> str:
    return dt.date.today().isoformat()


def _now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _umask() -> int:
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def atomic_write(path, data, mode: int | None = None) -> None:
    """Write through a temp file in the same folder and a rename, so a crash never leaves half a file.
    An existing file keeps its mode; a new one gets `mode` (default 0666 minus the umask). A symlink is
    followed, so the link stays and its target changes."""
    target = Path(os.path.realpath(str(path)))
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        keep = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        keep = None
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".deskmate-tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, keep if keep is not None else (mode if mode is not None else 0o666 & ~_umask()))
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _git(args: list, cwd=None, timeout: float = 30):
    """Run git without prompts (a wizard has no terminal). None when git is missing or hangs."""
    exe = shutil.which("git")
    if not exe:
        return None
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        return subprocess.run([exe] + list(args), cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_error(r) -> str:
    if r is None:
        return "git is not installed or did not answer"
    lines = [ln.strip() for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
    return lines[-1] if lines else f"git exited with {r.returncode}"


class Run:
    """One apply or remove: where events go, the backup folder, and what went wrong."""

    def __init__(self, values: dict | None, emit=None):
        self.values = values or {}
        self.emit = emit
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.backups = None
        self.warnings: list = []
        self.errors: list = []
        self.done: list = []

    def say(self, level: str, text: str) -> None:
        if level == "warn":
            self.warnings.append(text)
        elif level == "error":
            self.errors.append(text)
        elif level == "ok":
            self.done.append(text)
        if self.emit:
            try:
                self.emit({"phase": PHASE, "level": level, "text": text})
            except Exception:
                pass

    def backup(self, path) -> str | None:
        """Copy a file into this run's backup folder before the first edit, under its full path."""
        src = Path(os.path.realpath(str(path)))
        if not src.is_file():
            return None
        if self.backups is None:
            self.backups = data_dir(self.values) / "backups" / f"habits-{self.stamp}"
            os.makedirs(str(self.backups), mode=0o700, exist_ok=True)
        dest = self.backups / str(src).lstrip(os.sep)
        if dest.exists():
            return str(dest)  # the first copy is the one from before this run
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))
        os.chmod(str(dest), 0o600)
        return str(dest)


# ---------------------------------------------------------------- templates


_SECTION = re.compile(r"^\{([?/])(\w+)\}$")
_INLINE_SECTION = re.compile(r"\{[?/]\w+\}")
_NAME = re.compile(r"\{(\w+)\}")


def render(template: str, values: dict, flags: dict | None = None) -> str:
    """The pack's template syntax, without a dependency. `{name}` is replaced by its value; a line that is
    exactly `{?flag}` opens a section kept only when the flag is true and `{/flag}` closes it; runs of
    blank lines become one. An unknown name or flag, or an unbalanced section, is an error. Values are
    inserted once and never scanned again, so braces inside them are safe."""
    flags = flags or {}
    out, stack = [], []
    for line in template.split("\n"):
        m = _SECTION.match(line.strip())
        if m:
            kind, name = m.groups()
            if name not in flags:
                raise TemplateError(f"no flag for section {{?{name}}}")
            if kind == "?":
                stack.append((name, bool(flags[name])))
            elif not stack or stack[-1][0] != name:
                raise TemplateError(f"section {{/{name}}} closes nothing")
            else:
                stack.pop()
            continue
        if _INLINE_SECTION.search(line):
            raise TemplateError(f"a section marker must be on a line of its own: {line.strip()[:60]}")
        if all(on for _, on in stack):
            out.append(line)
    if stack:
        raise TemplateError(f"section {{?{stack[-1][0]}}} is not closed")

    def value(m):
        if m.group(1) not in values:
            raise TemplateError(f"no value for {{{m.group(1)}}}")
        return str(values[m.group(1)])

    text = _NAME.sub(value, "\n".join(out))
    text = "\n".join(ln.rstrip() for ln in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text)


def template_names(template: str) -> tuple:
    """(value names, flag names) a template uses."""
    flags = sorted({m.group(2) for m in (_SECTION.match(ln.strip()) for ln in template.split("\n")) if m})
    names = sorted({n for n in _NAME.findall(template)} - set(flags))
    return names, flags


def rules_template() -> str:
    return (pack_dir() / "rules.md.tmpl").read_text(encoding="utf-8")


def render_block(ctx: dict, flags: dict, template: str | None = None) -> tuple:
    """(the block's lines, markers included, and the hash of its inner text). The begin marker carries the
    hash, so a later run can tell whether someone edited inside the block."""
    lines = (template if template is not None else rules_template()).rstrip("\n").split("\n")
    if not BEGIN_RE.match(lines[0].strip()) or not END_RE.match(lines[-1].strip()):
        raise TemplateError("the rules template must start and end with the deskmate:habits markers")
    inner = render("\n".join(lines[1:-1]), ctx, flags).strip("\n")
    sha = _sha(inner)
    begin = render(lines[0], dict(ctx, block_sha=sha), {})
    return [begin] + inner.split("\n") + [lines[-1]], sha


# ---------------------------------------------------------------- the block inside a file


def _split(text: str) -> list:
    """Lines with their own endings, so text outside the block is written back byte for byte."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _bare(line: str) -> str:
    return line.rstrip("\r\n")


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def find_block(lines: list) -> tuple | None:
    """(begin, end) line indexes of the block, None when there is none. BlockError when the markers
    cannot be trusted; then nothing may be changed."""
    begins = [i for i, ln in enumerate(lines) if BEGIN_RE.match(_bare(ln).strip())]
    ends = [i for i, ln in enumerate(lines) if END_RE.match(_bare(ln).strip())]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise BlockError("Deskmate's markers are broken (begin on line %s, end on line %s); fix them by hand"
                         % (", ".join(str(b + 1) for b in begins) or "none", ", ".join(str(e + 1) for e in ends) or "none"))
    return begins[0], ends[0]


def block_info(text: str) -> dict:
    """What a file holds: {state: none|current|edited|broken, since, sha, inner, detail}."""
    lines = _split(text)
    try:
        span = find_block(lines)
    except BlockError as exc:
        return {"state": "broken", "detail": str(exc)}
    if not span:
        return {"state": "none"}
    b, e = span
    begin = _bare(lines[b])
    inner = "\n".join(_bare(ln) for ln in lines[b + 1:e])
    sha = (MARK_SHA.search(begin) or [None, None])[1]
    since = MARK_SINCE.search(begin)
    return {
        "state": "current" if sha == _sha(inner) else "edited",
        "since": since.group(1) if since else None,
        "sha": sha,
        "inner": inner,
        "lines": (b + 1, e + 1),
    }


def place_block(text: str, block: list, prefix: list | None = None) -> str:
    """The file's text with the block in place: replaced where it is, else appended after one blank
    line. `prefix` lines open a file that is empty or new (the @AGENTS.md line)."""
    nl = _newline(text)
    lines = _split(text)
    span = find_block(lines)
    new = [ln + nl for ln in block]
    if span:
        b, e = span
        if not lines[e].endswith("\n"):
            new[-1] = new[-1].rstrip("\r\n")
        lines[b:e + 1] = new
        return "".join(lines)
    if not "".join(lines).strip():
        head = [p + nl for p in (prefix or [])]
        return "".join(head + ([nl] if head else []) + new)
    if not lines[-1].endswith("\n"):
        lines[-1] += nl
    return "".join(lines + [nl] + new)


def strip_block(text: str, keep_text: bool = False) -> tuple:
    """(text without the block, whether there was one). Removes the begin..end lines plus one blank
    line next to them. keep_text removes only the two marker lines (the user keeps the rules as theirs)."""
    lines = _split(text)
    span = find_block(lines)
    if not span:
        return text, False
    b, e = span
    if keep_text:
        del lines[e]
        del lines[b]
        return "".join(lines), True
    if b > 0 and not _bare(lines[b - 1]).strip():
        b -= 1
    elif e + 1 < len(lines) and not _bare(lines[e + 1]).strip():
        e += 1
    del lines[b:e + 1]
    return "".join(lines), True


def _read_doc(path) -> tuple:
    """(text, bom, exists). A missing file is empty. A UTF-8 BOM is kept aside and written back."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return "", b"", False
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    return raw[len(bom):].decode("utf-8", errors="surrogateescape"), bom, True


def _encode(text: str, bom: bytes) -> bytes:
    return bom + text.encode("utf-8", errors="surrogateescape")


# ---------------------------------------------------------------- detection (read-only)


def git_work_tree(path) -> dict | None:
    """The git work tree that holds `path`: {top, exclude}, or None. Uses git when it is installed (it
    knows linked worktrees and $GIT_COMMON_DIR); otherwise walks up looking for .git."""
    path = Path(_norm(path))
    r = _git(["rev-parse", "--is-inside-work-tree", "--show-toplevel", "--git-path", "info/exclude"], cwd=path, timeout=10)
    if r is not None:
        out = [ln.strip() for ln in r.stdout.splitlines()]
        if r.returncode != 0 or len(out) < 3 or out[0] != "true":
            return None
        excl = out[2] if os.path.isabs(out[2]) else os.path.join(str(path), out[2])
        return {"top": os.path.normpath(out[1]), "exclude": os.path.normpath(excl)}
    for d in [path] + list(path.parents):
        g = d / ".git"
        if g.is_dir():
            return {"top": str(d), "exclude": str(g / "info" / "exclude")}
        if g.is_file():
            m = re.match(r"gitdir:\s*(.+)", g.read_text(errors="replace"))
            if not m:
                return None
            gd = Path(m.group(1).strip())
            gd = gd if gd.is_absolute() else (d / gd)
            common = gd
            try:
                common = (gd / (gd / "commondir").read_text().strip()).resolve()
            except OSError:
                pass
            return {"top": str(d), "exclude": str(common / "info" / "exclude")}
    return None


def _git_kind(folder: Path) -> tuple:
    """("repo" | "worktree" | None, main checkout for a worktree)."""
    g = folder / ".git"
    if g.is_dir():
        return "repo", None
    if g.is_file():
        try:
            m = re.match(r"gitdir:\s*(.+)", g.read_text(errors="replace"))
        except OSError:
            return None, None
        if m:
            gd = Path(m.group(1).strip())
            gd = gd if gd.is_absolute() else (folder / gd)
            parts = gd.parts
            if "worktrees" in parts:
                main = Path(*parts[: len(parts) - 1 - parts[::-1].index("worktrees")])
                if main.name == ".git":
                    return "worktree", main.parent
            return "repo", None
    return None, None


def _children(folder: Path) -> list:
    try:
        return sorted((p for p in folder.iterdir() if p.is_dir() and not p.is_symlink()), key=lambda p: p.name)
    except OSError:
        return []


def find_repos(root, hub=None) -> list:
    """Repos in a folder, read-only: the folder's own repo when it is inside one; else child folders with
    a .git, and repos or linked worktrees one level further down, worktrees grouped under their main
    repo's name. Hidden folders and the hub are skipped."""
    root = Path(_norm(root))
    tree = git_work_tree(root)
    if tree:
        top = Path(tree["top"])
        kind, main = _git_kind(top)
        name = main.name if kind == "worktree" and main else top.name
        return [{"name": name, "path": str(top), "label": "", "worktrees": [], "single": True}]
    hub_real = _real(hub) if hub else None
    repos: dict = {}

    def add(folder: Path, label: str) -> bool:
        kind, main = _git_kind(folder)
        if kind is None:
            return False
        name = main.name if kind == "worktree" and main else folder.name
        if name not in repos:
            path = str(main) if kind == "worktree" and main else str(folder)
            inside = kind != "worktree" or _inside(path, root)
            repos[name] = {"name": name, "path": path, "single": False, "worktrees": [],
                           "label": (os.path.relpath(path, str(root)) + "/") if inside else ""}
        if kind == "worktree":
            repos[name]["worktrees"].append(label)
        elif not repos[name]["label"]:
            repos[name]["label"] = label
            repos[name]["path"] = str(folder)
        return True

    for child in _children(root):
        if child.name.startswith(".") or child.name in SKIP_DIRS or (hub_real and _real(child) == hub_real):
            continue
        if add(child, child.name + "/"):
            continue
        for grand in _children(child):
            if not grand.name.startswith(".") and grand.name not in SKIP_DIRS:
                if not (hub_real and _real(grand) == hub_real):
                    add(grand, f"{child.name}/{grand.name}/")
    return sorted(repos.values(), key=lambda r: r["name"].lower())


def is_hub(folder) -> bool:
    f = Path(folder)
    return (f / MANIFEST).is_file() or ((f / "changelog" / "README.md").is_file()
                                        and ((f / "design" / "_template.md").is_file() or (f / "design" / "README.md").is_file()))


def find_hubs(root) -> list:
    """Child folders that look like a docs hub: a changelog README and a design template, or a team manifest."""
    return [{"path": str(c), "label": _label(c), "manifest": (c / MANIFEST).is_file()}
            for c in _children(Path(_norm(root))) if not c.name.startswith(".") and is_hub(c)]


def read_manifest(hub) -> dict | None:
    """The team manifest (deskmate-team.json) at a hub's root, or None. Only known keys are used."""
    if not hub:
        return None
    return _read_json(Path(hub) / MANIFEST)


def _instruction_files(root: Path) -> list:
    return [root / "CLAUDE.md", root / ".claude" / "CLAUDE.md", root / "CLAUDE.local.md"]


def own_rules(root) -> tuple:
    """(True, why) when the folder's own instructions already name a changelog folder and design docs.
    Deskmate's own block does not count."""
    root = Path(_norm(root))
    for f in _instruction_files(root):
        text, _, exists = _read_doc(f)
        if not exists:
            continue
        try:
            text, _ = strip_block(text)
        except BlockError:
            pass
        if OWN_CHANGELOG.search(text) and OWN_DESIGN.search(text):
            rel = os.path.relpath(str(f), str(root))
            return True, f"this folder already has its own rules ({rel} names a changelog folder and design docs)"
    return False, ""


def _agents_md(root: Path) -> str | None:
    """The AGENTS.md to import when Deskmate creates the folder's first instruction file. Claude Code
    reads AGENTS.md only where a project has no CLAUDE.md of its own, so a new CLAUDE.md (or
    CLAUDE.local.md) starts with an @import of it."""
    if any(f.exists() for f in _instruction_files(root)):
        return None
    for name in ("AGENTS.md", ".claude/AGENTS.md"):
        if (root / name).is_file():
            return name
    return None


def _frontmatter(text: str) -> dict:
    """The simple YAML frontmatter of an agent or skill file: key: value lines, quoted or folded values."""
    lines = text.lstrip("\ufeff").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict = {}
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m and not line[:1].isspace():
            key, val = m.group(1), m.group(2).strip()
            if val in ("|", ">", "|-", ">-", "|+", ">+"):
                val = ""
            elif len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            out[key] = val
        elif key and line[:1].isspace() and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    return {}  # no closing ---


def _first_sentence(text: str, limit: int = 160) -> str:
    s = re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def read_agents(folder) -> list:
    """Agent files in a folder: *.md whose frontmatter has a name and a description."""
    out = []
    try:
        files = sorted(Path(folder).glob("*.md"))
    except OSError:
        return out
    for p in files:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        fm = _frontmatter(data.decode("utf-8", errors="replace"))
        if fm.get("name") and fm.get("description"):
            out.append({"file": p.name, "path": str(p), "name": fm["name"], "description": fm["description"],
                        "sha": hashlib.sha256(data).hexdigest(), "data": data})
    return out


# The lint for imported agents: things that do not belong in files shared between people.
_HOME_PATH = re.compile(r"(?<![\w.-])(/home|/Users)/([A-Za-z0-9._-]+)")
_LOCAL_HOST = re.compile(r"(?:(?<=//)|(?<=@)|(?<![\w./~-]))((?:[A-Za-z0-9-]+\.)+(?:lan|local|home\.arpa))(?![\w-])", re.I)
_SECRETS = [
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an Anthropic token", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")),
    ("an API key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("a GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("an AWS access key", re.compile(r"\bA(?:KIA|SIA)[A-Z0-9]{16}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("a Slack token", re.compile(r"\bxox[abposr]-[0-9A-Za-z-]{10,}")),
    ("a Slack webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/_-]{10,}")),
    ("a Discord webhook", re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}")),
    ("a JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("a password in a URL", re.compile(r"://[^\s:/@]+:[^\s/@]{3,}@")),
    ("a secret value", re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\b[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{12,}")),
]


def lint_agents(agents: list, deny=None, user: str | None = None) -> list:
    """Findings in agent files: home paths that are not this user's, .lan/.local hosts, the team's
    lint_deny words, and anything that looks like a secret. A secret is never shown, only its kind."""
    user = user or os.environ.get("USER") or _home().name
    deny = [str(w) for w in (deny or []) if str(w).strip()]
    found = []
    for a in agents:
        text = a["data"].decode("utf-8", errors="replace") if "data" in a else Path(a["path"]).read_text(errors="replace")
        for n, line in enumerate(text.split("\n"), 1):
            for m in _HOME_PATH.finditer(line):
                if m.group(2) != user:
                    found.append({"file": a["file"], "line": n, "kind": "home path", "text": m.group(0) + "/"})
            for m in _LOCAL_HOST.finditer(line):
                found.append({"file": a["file"], "line": n, "kind": "local host name", "text": m.group(1)})
            for word in deny:
                if word.lower() in line.lower():
                    found.append({"file": a["file"], "line": n, "kind": "team lint word", "text": word})
            for kind, rx in _SECRETS:
                if rx.search(line):
                    found.append({"file": a["file"], "line": n, "kind": "secret", "text": f"looks like {kind}"})
                    break
    return found


def codegraph() -> bool:
    return shutil.which("codegraph") is not None


def memory_state() -> dict:
    """Whether Claude Code's auto memory is on: the env switch, then user settings."""
    if _bool(os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), False):
        return {"enabled": False, "why": "CLAUDE_CODE_DISABLE_AUTO_MEMORY is set in the environment"}
    s = _read_json(claude_dir() / "settings.json") or {}
    if s.get("autoMemoryEnabled") is False:
        return {"enabled": False, "why": "autoMemoryEnabled is false in " + _label(claude_dir() / "settings.json")}
    return {"enabled": True, "why": ""}


PLUGIN_ID = PLUGIN + "@deskmate"


def plugin_version() -> str:
    """The habits plugin's version in this clone."""
    return str((_read_json(plugin_dir() / ".claude-plugin" / "plugin.json") or {}).get("version") or "")


def plugin_installed() -> str | None:
    """The installed version of habits@deskmate ('' when unknown), or None when it is not installed."""
    data = _read_json(claude_dir() / "plugins" / "installed_plugins.json") or {}
    entries = (data.get("plugins") or {}).get(PLUGIN_ID)
    if entries is None:
        return None
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return str(entries[0].get("version") or "")
    return ""


# ---------------------------------------------------------------- the plan


def _record(values: dict | None = None) -> dict:
    data = _read_json(installed_path(values)) or {}
    rec = data.get("habits")
    return rec if isinstance(rec, dict) else {}


def _save_record(rec: dict | None, values: dict | None = None) -> None:
    """Write our key of installed.json; the other keys (other roles') are kept as they are."""
    path = installed_path(values)
    data = _read_json(path) or {}
    if rec:
        data["habits"] = rec
    else:
        data.pop("habits", None)
    if not path.parent.exists():
        os.makedirs(str(path.parent), mode=0o700, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n", mode=0o600)


def _options_map(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except ValueError:
            value = {}
    if not isinstance(value, dict):
        return {}
    return {_norm(k).rstrip(os.sep) or os.sep: (v if isinstance(v, dict) else {}) for k, v in value.items()}


def _abs(p, base: Path) -> Path:
    p = os.path.expanduser(str(p).strip())
    return Path(os.path.normpath(p if os.path.isabs(p) else os.path.join(str(base), p)))


def _hub_display(hub: Path, root: Path) -> str:
    """How the rules name the hub: relative inside the folder, absolute outside it."""
    if _inside(hub, root) and not _same(hub, root):
        return os.path.relpath(_real(hub), _real(root))
    return str(hub)


def _skeleton_files() -> list:
    """(source, relative output path) for every file of pack/hub-skeleton."""
    skel = pack_dir() / "hub-skeleton"
    out = []
    for src in sorted(skel.rglob("*")):
        if src.is_file():
            rel = src.relative_to(skel)
            out.append((src, rel.with_suffix("") if rel.suffix == ".tmpl" else rel))
    return out


def _slug(url: str) -> str:
    base = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1]) or "pack"
    base = re.sub(r"[^A-Za-z0-9._-]", "-", base)[:40]
    return f"{base}-{hashlib.sha256(url.encode()).hexdigest()[:8]}"


def _folder_defaults(root: Path, rec: dict | None, own: bool, hubs: list, values: dict) -> dict:
    """A folder's choices before the user changes any: what was installed, else what detection suggests."""
    if rec and isinstance(rec.get("options"), dict):
        return dict(rec["options"])
    docs = str(values.get("DOCS_DIR") or "").strip()
    if hubs:
        hub = {"hub": "existing", "hub_path": hubs[0]["path"]}
    elif docs and os.path.isdir(_norm(docs)) and _inside(docs, root) and is_hub(_norm(docs)):
        hub = {"hub": "existing", "hub_path": _norm(docs)}
    else:
        hub = {"hub": "new", "hub_name": DEFAULT_HUB_NAME}
    return dict(hub, block=not own, git_init=True, fill_missing=False, skip_repos=[], agents="manifest",
                agents_source="", agents_path="agents", agents_accept=False, on_edit="skip")


def _folder(path: str, given: dict, rec: dict | None, values: dict, ctx: dict) -> dict:
    """Everything about one folder that does not need the network: where the block goes, what is there
    now, the hub, the repos, and the options in effect."""
    root = Path(path)
    f = {"path": path, "label": _label(path), "exists": root.is_dir(), "errors": [], "warnings": [],
         "installed": bool(rec)}
    if not f["exists"]:
        f["errors"].append(f"{_label(path)} is not a folder")
        f["options"] = dict(given)
        return f
    tree = git_work_tree(root)
    f["git_top"] = tree["top"] if tree else None
    target = root / ("CLAUDE.local.md" if tree else "CLAUDE.md")
    f["target"] = str(target)
    f["target_name"] = target.name
    f["exclude"] = None
    if tree:
        rel = os.path.relpath(_real(target), _real(tree["top"])).replace(os.sep, "/")
        f["exclude"] = {"file": tree["exclude"], "line": "/" + rel}
    f["agents_md"] = None if target.exists() else _agents_md(root)
    own, why = own_rules(root)
    f["own_rules"], f["own_rules_why"] = own, why
    text, _, exists = _read_doc(target)
    f["block_now"] = block_info(text) if exists else {"state": "none"}
    hubs = find_hubs(root)
    f["hubs_found"] = hubs
    opts = _folder_defaults(root, rec, own, hubs, values)
    opts.update({k: v for k, v in given.items() if k in FOLDER_OPTIONS})
    opts["block"] = _bool(opts.get("block"), not own)
    opts["git_init"] = _bool(opts.get("git_init"), True)
    opts["fill_missing"] = _bool(opts.get("fill_missing"), False)
    opts["agents_accept"] = _bool(opts.get("agents_accept"), False)
    opts["skip_repos"] = [str(s) for s in (opts.get("skip_repos") or []) if str(s).strip()]
    if opts.get("hub") not in ("existing", "new", "none"):
        opts["hub"] = "new"
    if opts.get("agents") not in ("none", "manifest", "git", "folder"):
        opts["agents"] = "none"
    if opts.get("on_edit") not in ("skip", "overwrite", "keep"):
        opts["on_edit"] = "skip"
    f["options"] = opts
    f["block_default"] = not own
    f["block_reason"] = why
    f["hub"] = _hub(root, opts, hubs, f)
    f["repos"] = find_repos(root, f["hub"].get("path"))
    _hub_details(f)
    return f


def _hub(root: Path, opts: dict, hubs: list, f: dict) -> dict:
    kind = opts["hub"]
    h = {"kind": kind, "path": None, "exists": False}
    if kind == "none":
        return h
    if kind == "existing":
        raw = opts.get("hub_path") or (hubs[0]["path"] if hubs else "")
        if not raw:
            f["errors"].append("pick the docs hub folder, or choose another hub option")
            return h
        p = _abs(raw, root)
    else:
        p = _abs(opts.get("hub_path") or opts.get("hub_name") or DEFAULT_HUB_NAME, root)
    h.update(path=str(p), label=_label(p), exists=p.is_dir(), inside=_inside(p, root) and not _same(p, root),
             display=_hub_display(p, root))
    if kind == "existing" and not p.is_dir():
        f["errors"].append(f"the hub {_label(p)} does not exist")
    elif kind == "new" and p.exists():
        if p.is_dir() and is_hub(p):
            h["kind"] = "existing"  # created by an earlier run (or by hand): fill the gaps only
            h["was_new"] = True
        elif p.is_dir() and any(p.iterdir()):
            f["errors"].append(f"{_label(p)} already exists and is not a docs hub; pick another name")
        elif not p.is_dir():
            f["errors"].append(f"{_label(p)} is a file")
    if h["kind"] == "existing" and p.is_dir():
        h["missing"] = [str(rel) for _, rel in _skeleton_files() if not (p / rel).exists()]
    if h["kind"] == "new":
        h["git_init"] = opts["git_init"] and not f.get("git_top") and not git_work_tree(p.parent)
    return h


def _hub_details(f: dict) -> None:
    """The hub-dependent part: the team manifest, changelog folder and repo names. Called again after a
    new hub is created."""
    h = f["hub"]
    p = Path(h["path"]) if h.get("path") else None
    man = read_manifest(p) if p and p.is_dir() else None
    if p and p.is_dir() and (p / MANIFEST).exists() and man is None:
        f["warnings"].append(f"{_label(p / MANIFEST)} is not valid JSON; it was ignored")
    f["manifest"] = man or {}
    m = f["manifest"]
    f["changelog_dir"] = str(m.get("changelog_dir") or DEFAULT_CHANGELOG_DIR).strip("/") or DEFAULT_CHANGELOG_DIR
    names = {}
    for r in m.get("repos") or []:
        if isinstance(r, dict) and r.get("name") and r.get("folder"):
            names[str(r["folder"])] = str(r["name"])
    f["repo_names"] = names
    if not f["options"].get("skip_repos") and isinstance(m.get("skip_repos"), list) and not f.get("installed"):
        f["options"]["skip_repos"] = [str(s) for s in m["skip_repos"]]


def _repo_rows(f: dict) -> str:
    h = f["hub"]
    skip = set(f["options"]["skip_repos"])
    rows = []
    for r in f["repos"]:
        name = f["repo_names"].get(r["name"], r["name"])
        if r["name"] in skip or name in skip:
            continue
        docs = "—"
        if h.get("path"):
            index = Path(h["path"]) / "projects" / name / "INDEX.md"
            docs = f"`{h['display']}/projects/{name}/INDEX.md`" if index.is_file() else "not documented yet"
        if r.get("single"):
            label = f"`{name}` (this folder)"
        else:
            label = f"`{r['label'] or r['path']}`"
            if r["worktrees"]:
                parents = {os.path.dirname(w.rstrip("/")) for w in r["worktrees"]}
                if len(parents) == 1 and "" not in parents:
                    label += f" (worktrees in `{next(iter(parents))}/`)"
                else:
                    label += " (worktrees: " + ", ".join(f"`{w}`" for w in r["worktrees"]) + ")"
            if name != r["name"]:
                label += f" (logged as `{name}`)"
        rows.append(f"| {label} | {docs} |")
    return "\n".join(rows) or "| _none found yet_ | |"


def _block_lines(f: dict, ctx: dict, agents: list) -> tuple:
    """Render this folder's block. agents: [{name, description}] to list."""
    h = f["hub"]
    hub = bool(h.get("path"))
    since = (f.get("block_now") or {}).get("since") or _today()
    values = {
        "version": BLOCK_VERSION, "date": since,
        "hub": h.get("display", ""), "changelog_dir": f.get("changelog_dir", DEFAULT_CHANGELOG_DIR),
        "repo_rows": _repo_rows(f), "owner": ctx["owner"], "skill_prefix": SKILL_PREFIX,
        "check_mode": ctx["mode"],
        "agent_rows": "\n".join(f"- `{a['name']}` — {_first_sentence(a['description'])}" for a in agents),
    }
    single = any(r.get("single") for r in f["repos"])
    flags = {"hub": hub, "nohub": not hub, "notify": ctx["notify"], "check": ctx["mode"] != "off" and hub,
             "codegraph": ctx["codegraph"], "agents": bool(agents), "single": single, "multi": not single}
    return render_block(values, flags)


def _context(values: dict, rec: dict) -> dict:
    owner = str(values.get("DESKMATE_OWNER") or os.environ.get("DESKMATE_OWNER") or "").strip() or "the user"
    kind = str(values.get("NOTIFY_KIND") or "none").strip().lower()
    cfg = _read_json(config_path()) or {}
    mode = str(values.get("HABITS_CHECK") or "").strip().lower()
    return {"owner": owner, "notify": kind not in ("", "none", "off"), "codegraph": codegraph(),
            "mode": mode if mode in MODES else "", "cfg": cfg}


def _mode_from_manifest(ctx: dict, folders: list) -> None:
    """A hub's team manifest's check_mode is the default when nobody chose a mode."""
    if ctx.get("mode_from") != "default":
        return
    for f in folders:
        mode = str((f.get("manifest") or {}).get("check_mode") or "")
        if mode in MODES:
            ctx["mode"], ctx["mode_from"] = mode, "manifest"
            return


def _plan(values: dict | None) -> dict:
    """The folders to set up and every choice for them, from values, the current install and detection."""
    v = dict(values or {})
    rec = _record(v)
    ctx = _context(v, rec)
    rec_folders = {f["path"]: f for f in rec.get("folders") or [] if isinstance(f, dict) and f.get("path")}
    only = str(v.get("HABITS_FOLDER") or "").strip()
    if only:
        folders = [_norm(only).rstrip(os.sep) or os.sep]
    else:
        folders = (_paths(v.get("HABITS_FOLDERS")) or list(rec_folders)
                   or [_norm(r["path"]) for r in ctx["cfg"].get("roots") or [] if isinstance(r, dict) and r.get("path")]
                   or _paths(v.get("SESSIONS_ROOTS")))
    given = _options_map(v.get("HABITS_FOLDER_OPTIONS"))
    plans = [_folder(p, given.get(p, {}), rec_folders.get(p), v, ctx) for p in folders]
    # A folder inside another picked folder would load both CLAUDE.md files.
    others = [p for p in rec_folders if p not in folders] if only else []
    for f in plans:
        for g in folders + others:
            if g != f["path"] and _inside(f["path"], g) and not _same(f["path"], g):
                f["errors"].append(f"{f['label']} is inside {_label(g)}, which is also set up; both CLAUDE.md files would load")
    if ctx["mode"]:
        ctx["mode_from"] = "values"
    elif ctx["cfg"].get("mode") in MODES:
        ctx["mode"], ctx["mode_from"] = ctx["cfg"]["mode"], "config"
    else:
        ctx["mode"], ctx["mode_from"] = "remind", "default"
        _mode_from_manifest(ctx, plans)
    notify_default = ctx["notify"]
    return {
        "folders": plans, "only": bool(only), "record": rec, "ctx": ctx,
        "allow_notify": _bool(v.get("HABITS_ALLOW_NOTIFY"), notify_default) and ctx["notify"],
        "memory_on": _bool(v.get("HABITS_MEMORY_ON"), False),
        "plugin": _bool(v.get("HABITS_PLUGIN"), True),
        "update": _bool(v.get("HABITS_UPDATE"), False),
        "values": v,
    }


# ---------------------------------------------------------------- agents


def _agents_source(f: dict, values: dict) -> dict:
    """Where this folder's team agents come from: {kind: none|git|folder, url, folder, via}."""
    opts = f["options"]
    kind = opts["agents"]
    if kind == "manifest":
        m = f.get("manifest", {}).get("agents")
        hub = f["hub"].get("path")
        if isinstance(m, dict) and m.get("git"):
            return {"kind": "git", "url": str(m["git"]), "path": str(m.get("path") or "agents"), "via": "the team manifest"}
        if isinstance(m, dict) and (m.get("folder") or m.get("path")) and hub:
            return {"kind": "folder", "folder": str(_abs(m.get("folder") or m.get("path"), Path(hub))), "path": "",
                    "via": "the team manifest"}
        return {"kind": "none"}
    if kind == "git":
        url = str(opts.get("agents_source") or "").strip()
        if not url or url.startswith("-"):
            f["errors"].append("type the git URL of the team agents")
            return {"kind": "none"}
        return {"kind": "git", "url": url, "path": str(opts.get("agents_path") or "agents"), "via": url}
    if kind == "folder":
        src = str(opts.get("agents_source") or "").strip()
        if not src:
            f["errors"].append("pick the folder that holds the team agents")
            return {"kind": "none"}
        return {"kind": "folder", "folder": _norm(src), "path": str(opts.get("agents_path") or ""), "via": _label(src)}
    return {"kind": "none"}


def _agents_folder(src: dict, values: dict) -> Path | None:
    if src["kind"] == "git":
        base = data_dir(values) / "packs" / _slug(src["url"])
    elif src["kind"] == "folder":
        base = Path(src["folder"])
    else:
        return None
    sub = base / src["path"] if src.get("path") else base
    return sub if sub.is_dir() else (base if base.is_dir() else None)


def _fetch_agents(run: Run, src: dict) -> str:
    """Clone (or fast-forward) a git agents source into <data>/packs/. Returns the commit, or ''."""
    dest = data_dir(run.values) / "packs" / _slug(src["url"])
    if (dest / ".git").exists():
        r = _git(["-C", str(dest), "pull", "--ff-only", "-q"], timeout=120)
        if r is None or r.returncode != 0:
            run.say("warn", f"Could not update the team agents from {src['url']}: {_git_error(r)}")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = _git(["clone", "-q", "--depth", "1", "--", src["url"], str(dest)], timeout=300)
        if r is None or r.returncode != 0:
            run.say("error", f"Could not fetch the team agents from {src['url']}: {_git_error(r)}")
            shutil.rmtree(str(dest), ignore_errors=True)
            return ""
    r = _git(["-C", str(dest), "rev-parse", "HEAD"], timeout=10)
    return r.stdout.strip() if r is not None and r.returncode == 0 else ""


def _agents_plan(f: dict, values: dict) -> dict:
    """The agents a folder would get: source, files, lint findings, clashes. Reads only local files (a
    git source is read from its last fetch)."""
    src = _agents_source(f, values)
    out = {"source": src, "files": [], "lint": [], "clashes": [], "same": False, "fetched": False}
    if src["kind"] == "none":
        return out
    folder = _agents_folder(src, values)
    target = Path(f["path"]) / ".claude" / "agents"
    out["target"] = str(target)
    if folder is None:
        return out
    out["fetched"] = True
    out["folder"] = str(folder)
    out["same"] = _same(folder, target)
    out["files"] = read_agents(folder)
    deny = f.get("manifest", {}).get("lint_deny") or []
    out["lint"] = [] if out["same"] else lint_agents(out["files"], deny)
    ours = (_read_json(target / AGENTS_MANIFEST) or {}).get("files") or {}
    for a in out["files"]:
        if not out["same"] and (target / a["file"]).exists() and a["file"] not in ours:
            out["clashes"].append(a["file"])
    return out


def _import_agents(run: Run, f: dict, plan: dict, commit: str) -> dict | None:
    """Copy the agents into <folder>/.claude/agents/ and write the manifest. Never overwrites a file
    Deskmate did not install or one edited here; drops files the source no longer has, unless edited."""
    target = Path(plan["target"])
    claude_existed = target.parent.exists()
    dir_existed = target.exists()
    prev = _read_json(target / AGENTS_MANIFEST) or {}
    prev_files = prev.get("files") or {}
    rec_prev = ((f.get("rec") or {}).get("agents") or {})
    files: dict = {}
    written = []
    target.mkdir(parents=True, exist_ok=True)
    for a in plan["files"]:
        dest = target / a["file"]
        if dest.exists():
            cur = _sha_file(dest)
            if a["file"] not in prev_files:
                run.say("warn", f"{_label(dest)} is not Deskmate's; the team's {a['file']} was not copied")
                continue
            if cur != prev_files[a["file"]]:
                run.say("warn", f"{_label(dest)} was edited here; kept it (the team's version was not copied)")
                files[a["file"]] = prev_files[a["file"]]
                continue
            if cur == a["sha"]:
                files[a["file"]] = cur
                continue
        atomic_write(dest, a["data"], mode=0o644)
        files[a["file"]] = a["sha"]
        written.append(a["file"])
    for name, sha in prev_files.items():
        if name in files:
            continue
        dest = target / name
        if dest.exists() and _sha_file(dest) == sha:
            dest.unlink()
            run.say("info", f"Removed {_label(dest)}; the team no longer has it")
        elif dest.exists():
            run.say("warn", f"{_label(dest)} is gone from the team agents but was edited here; kept it")
    src = plan["source"]
    manifest = {"version": 1, "source": {k: src.get(k) for k in ("kind", "url", "folder", "path") if src.get(k)},
                "commit": commit, "files": files, "updated": _now()}
    atomic_write(target / AGENTS_MANIFEST, json.dumps(manifest, indent=2) + "\n", mode=0o644)
    excluded = list(rec_prev.get("exclude_lines") or [])
    if f.get("exclude"):
        top = f["git_top"]
        lines = ["/" + os.path.relpath(_real(target / n), _real(top)).replace(os.sep, "/") for n in list(files) + [AGENTS_MANIFEST]]
        excluded = sorted(set(excluded) | set(_exclude_add(run, f["exclude"]["file"], lines)))
    if written:
        run.say("ok", f"Imported {len(written)} team agent(s) into {_label(target)}")
    return {"dir": str(target), "dir_created": not dir_existed if not rec_prev else rec_prev.get("dir_created", False),
            "claude_dir_created": (not claude_existed) if not rec_prev else rec_prev.get("claude_dir_created", False),
            "source": manifest["source"], "commit": commit, "files": files, "exclude_lines": excluded}


def _remove_agents(run: Run, rec: dict) -> None:
    target = Path(rec["dir"])
    for name, sha in (rec.get("files") or {}).items():
        dest = target / name
        if dest.exists() and _sha_file(dest) == sha:
            dest.unlink()
        elif dest.exists():
            run.say("warn", f"Kept {_label(dest)}: it was edited after Deskmate copied it")
    try:
        (target / AGENTS_MANIFEST).unlink()
    except OSError:
        pass
    for d, made in ((target, rec.get("dir_created")), (target.parent, rec.get("claude_dir_created"))):
        if made:
            try:
                d.rmdir()  # only when empty
            except OSError:
                pass


# ---------------------------------------------------------------- .git/info/exclude


def _exclude_add(run: Run, path: str, lines: list) -> list:
    """Add lines to a git exclude file; returns the lines this call added."""
    p = Path(path)
    text, bom, exists = _read_doc(p)
    have = {_bare(ln).strip() for ln in _split(text)}
    add = [ln for ln in lines if ln not in have]
    if not add:
        return []
    if exists:
        run.backup(p)
    nl = _newline(text) if text else "\n"
    if text and not text.endswith("\n"):
        text += nl
    atomic_write(p, _encode(text + "".join(ln + nl for ln in add), bom), mode=0o644)
    return add


def _exclude_remove(run: Run, path: str, lines: list) -> None:
    p = Path(path)
    text, bom, exists = _read_doc(p)
    if not exists:
        return
    drop = set(lines)
    kept = [ln for ln in _split(text) if _bare(ln).strip() not in drop]
    if len(kept) != len(_split(text)):
        run.backup(p)
        atomic_write(p, _encode("".join(kept), bom))


# ---------------------------------------------------------------- the block, applied and removed


def _write_block(run: Run, f: dict, lines: list, sha: str) -> dict | None:
    """Put the block in the folder's file. Returns the record, or the old one when nothing could be
    done (a broken marker, or an edit inside the block the user did not decide about)."""
    target = Path(f["target"])
    rec_prev = (f.get("rec") or {}).get("block")
    text, bom, exists = _read_doc(target)
    info = block_info(text) if exists else {"state": "none"}
    if info["state"] == "broken":
        run.say("error", f"{_label(target)}: {info['detail']}. Nothing was changed there.")
        return rec_prev
    if info["state"] == "edited":
        choice = f["options"]["on_edit"]
        if choice == "keep":
            new, _ = strip_block(text, keep_text=True)
            run.backup(target)
            atomic_write(target, _encode(new, bom))
            run.say("warn", f"{_label(target)}: the edited rules are yours now; Deskmate no longer manages them")
            return None
        if choice != "overwrite":
            run.say("warn", f"{_label(target)}: the rules block was edited by hand, so it was left as it is. "
                            "Choose overwrite or keep in the habits step to settle it.")
            return rec_prev
    prefix = ["@" + f["agents_md"]] if (not exists or not text.strip()) and f.get("agents_md") else None
    new = place_block(text, lines, prefix)
    created = (rec_prev or {}).get("created", not exists)
    agents_line = (rec_prev or {}).get("agents_line") or (prefix[0] if prefix else None)
    if new != text or not exists:
        if exists:
            run.backup(target)
        atomic_write(target, _encode(new, bom))
        how = "updated" if info["state"] != "none" else ("created" if not exists else "added to")
        run.say("ok", f"Working-habits block {how} {_label(target)}" + (f" (it imports {prefix[0][1:]})" if prefix else ""))
    rec = {"file": str(target), "created": bool(created), "agents_line": agents_line, "block_sha": sha,
           "exclude": (rec_prev or {}).get("exclude")}
    if f.get("exclude"):
        added = _exclude_add(run, f["exclude"]["file"], [f["exclude"]["line"]])
        if added or not rec.get("exclude"):
            had = ((rec_prev or {}).get("exclude") or {}).get("added")
            rec["exclude"] = {"file": f["exclude"]["file"], "line": f["exclude"]["line"], "added": bool(added or had)}
    return rec


def _remove_block(run: Run, rec: dict) -> bool:
    """Take the block out of its file. True when the file no longer holds Deskmate's text."""
    target = Path(rec["file"])
    text, bom, exists = _read_doc(target)
    if exists:
        info = block_info(text)
        if info["state"] == "broken":
            run.say("error", f"{_label(target)}: {info['detail']}. The block was not removed.")
            return False
        if info["state"] != "none":
            edited = info["state"] == "edited"
            new, _ = strip_block(text, keep_text=edited)
            if edited:
                run.say("warn", f"{_label(target)}: the rules block was edited by hand, so its text was kept "
                                "and only Deskmate's markers were removed")
            agents_line = rec.get("agents_line")
            rest = new
            if agents_line:
                first = _split(new)[:1]
                if first and _bare(first[0]).strip() == agents_line:
                    rest = "".join(_split(new)[1:])
            run.backup(target)
            if rec.get("created") and not rest.strip():
                target.unlink()
                run.say("ok", f"Removed {_label(target)} (Deskmate had created it)")
            else:
                atomic_write(target, _encode(new, bom))
                run.say("ok", f"Removed the working-habits block from {_label(target)}")
    ex = rec.get("exclude") or {}
    if ex.get("added") and ex.get("file") and ex.get("line"):
        _exclude_remove(run, ex["file"], [ex["line"]])
    return True


# ---------------------------------------------------------------- the hub


def _skeleton_values(f: dict) -> dict:
    root = Path(f["path"])
    repos = [r for r in f.get("repos") or [] if r["name"] not in set(f["options"]["skip_repos"])]
    names = [f.get("repo_names", {}).get(r["name"], r["name"]) for r in repos]
    return {
        "title": f"{root.name} docs hub",
        "repo_list": "\n".join(f"- `{n}`: `projects/{n}/INDEX.md` (not written yet)" for n in names)
        or "_No repos yet. Add a line per repo when you start one._",
        "changelog_rows": "\n".join(f"| {n} | [{n}/]({n}/) |" for n in names) or "| _none yet_ | |",
        "today": _today(),
    }


def _fill_hub(run: Run, f: dict, hub: Path, only_missing_of: list | None = None) -> list:
    """Copy skeleton files into a hub; never overwrites. Returns the relative paths added."""
    values = _skeleton_values(f)
    added = []
    for src, rel in _skeleton_files():
        if only_missing_of is not None and str(rel) not in only_missing_of:
            continue
        dest = hub / rel
        if dest.exists():
            continue
        data = render(src.read_text(encoding="utf-8"), values) if src.suffix == ".tmpl" else src.read_bytes()
        atomic_write(dest, data, mode=0o644)
        added.append(str(rel))
    return added


def _prepare_hub(run: Run, f: dict) -> dict | None:
    """Make the folder's hub real: create it from the skeleton, or fill its gaps. Returns the hub record,
    or None when there is no hub."""
    h = f["hub"]
    rec_prev = (f.get("rec") or {}).get("hub") or {}
    if h["kind"] == "none" or not h.get("path"):
        return None
    p = Path(h["path"])
    if h["kind"] == "new":
        existed = p.exists()
        added = _fill_hub(run, f, p)
        rec = {"path": str(p), "kind": "new", "created": not existed, "files_added": added}
        msg = f"Created the docs hub {_label(p)} from the skeleton ({len(added)} files)"
        if h.get("git_init") and not (p / ".git").exists():
            ok = _git(["init", "-q"], cwd=p)
            if ok is not None and ok.returncode == 0:
                _git(["add", "-A"], cwd=p)
                c = _git(["commit", "-q", "-m", "docs: start the docs hub"], cwd=p, timeout=60)
                if c is not None and c.returncode == 0:
                    msg += ", with git init and a first commit"
                else:
                    run.say("warn", f"git init worked in {_label(p)}, but the first commit did not ({_git_error(c)}). "
                                    "Commit it yourself.")
            else:
                run.say("warn", f"git init failed in {_label(p)}: {_git_error(ok)}")
        elif not h.get("git_init"):
            msg += "; it is not a git repo of its own" + (" (it is inside one)" if f.get("git_top") else "")
        run.say("ok", msg)
        return rec
    # existing
    rec = {"path": str(p), "kind": rec_prev.get("kind") or ("new" if h.get("was_new") else "existing"),
           "created": bool(rec_prev.get("created")),
           "files_added": list(rec_prev.get("files_added") or [])}
    if f["options"].get("fill_missing") and h.get("missing"):
        added = _fill_hub(run, f, p, h["missing"])
        if added:
            rec["files_added"] = sorted(set(rec["files_added"]) | set(added))
            run.say("ok", f"Added {', '.join(added)} to {_label(p)} from the skeleton")
    elif h.get("missing") and "changelog/README.md" in h["missing"]:
        run.say("warn", f"{_label(p)} has no changelog/README.md; sessions will use the default entry format")
    return rec


# ---------------------------------------------------------------- Claude Code settings


def _settings_file() -> Path:
    return claude_dir() / "settings.json"


def _edit_settings(run: Run, change) -> bool:
    """Apply change(data) -> bool to the user's settings.json as a JSON merge, with a backup."""
    p = _settings_file()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        run.say("error", f"{_label(p)} is not valid JSON, so it was left alone")
        return False
    if not isinstance(data, dict) or not change(data):
        return False
    run.backup(p)
    atomic_write(p, json.dumps(data, indent=2) + "\n", mode=0o600)
    return True


def _list_add(data: dict, key: str, value: str) -> bool:
    perms = data.setdefault("permissions", {})
    items = perms.setdefault(key, [])
    if value in items:
        return False
    items.append(value)
    return True


def _list_drop(data: dict, key: str, value: str) -> bool:
    perms = data.get("permissions")
    if not isinstance(perms, dict) or value not in (perms.get(key) or []):
        return False
    perms[key] = [x for x in perms[key] if x != value]
    if not perms[key]:
        del perms[key]
    if not perms:
        del data["permissions"]
    return True


def _sync_settings(run: Run, want: dict, had: list) -> list:
    """Make Deskmate's entries in settings.json match `want` ({allow: [..], dirs: [..], memory: bool}).
    Only entries Deskmate added are ever removed. Returns the new record."""
    p = str(_settings_file())
    rec = []
    for key, wanted in (("allow", want.get("allow") or []), ("additionalDirectories", want.get("dirs") or [])):
        kept = [r for r in had if r.get("key") == "permissions." + key]
        for r in kept:
            if r["value"] not in wanted:
                _edit_settings(run, lambda d, r=r: _list_drop(d, key, r["value"]))
                run.say("ok", f"Removed {r['value']} from permissions.{key} in {_label(p)}")
            else:
                rec.append(r)
        mine = {r["value"] for r in rec if r["key"] == "permissions." + key}
        for value in wanted:
            if value in mine:
                continue
            if _edit_settings(run, lambda d, value=value: _list_add(d, key, value)):
                rec.append({"file": p, "key": "permissions." + key, "value": value})
                run.say("ok", f"Added {value} to permissions.{key} in {_label(p)}")
    mem = [r for r in had if r.get("key") == "autoMemoryEnabled"]
    if want.get("memory") and not mem:
        before = {}

        def turn_on(d):
            if d.get("autoMemoryEnabled") is not False:
                return False
            before["v"] = d["autoMemoryEnabled"]
            d["autoMemoryEnabled"] = True
            return True

        if _edit_settings(run, turn_on):
            rec.append({"file": p, "key": "autoMemoryEnabled", "before": before["v"]})
            run.say("ok", f"Turned Claude Code's auto memory on in {_label(p)}")
    elif mem and not want.get("memory"):
        _restore_memory(run, mem[0])
    else:
        rec += mem
    return rec


def _restore_memory(run: Run, r: dict) -> None:
    def back(d):
        if d.get("autoMemoryEnabled") is not True:
            return False
        if r.get("before") is None:
            del d["autoMemoryEnabled"]
        else:
            d["autoMemoryEnabled"] = r["before"]
        return True

    if _edit_settings(run, back):
        run.say("ok", "Put autoMemoryEnabled back as it was")


# ---------------------------------------------------------------- the plugin


def _plugin_ops():
    """(install, uninstall) for the habits plugin, from the Claude Code connector (claude_connect.py)."""
    from . import claude_connect

    return claude_connect.install_plugin, claude_connect.uninstall_plugin


def _plugin(run: Run, install: bool) -> bool:
    try:
        ops = _plugin_ops()
    except Exception as exc:
        run.say("warn", f"Could not load the Claude Code connector to {'install' if install else 'remove'} "
                        f"the habits plugin ({exc.__class__.__name__}). The rules work without it; the skills "
                        "and the end-of-turn check need it.")
        return False
    try:
        (ops[0] if install else ops[1])(PLUGIN, run.emit)
        return True
    except Exception as exc:
        run.say("warn", f"The habits plugin was not {'installed' if install else 'removed'}: {exc}")
        return False


# ---------------------------------------------------------------- habits.json


def _config_roots(plan_folders: list) -> list:
    roots = []
    for f in plan_folders:
        h = f.get("hub") or {}
        if not f.get("exists") or f.get("errors") or not h.get("path"):
            continue
        top = f.get("git_top")
        roots.append({
            "path": f["path"], "hub": h["path"], "changelog_dir": f.get("changelog_dir", DEFAULT_CHANGELOG_DIR),
            "skip_repos": sorted(set(f["options"]["skip_repos"])), "repo_names": f.get("repo_names") or {},
            "work_tree": top if top and not _same(top, f["path"]) else None,
        })
    return roots


def _write_config(run: Run, mode: str, roots: list, keep_others: bool) -> bool:
    """Write habits.json for the Stop hook. Returns whether the file existed before Deskmate wrote it."""
    p = config_path()
    old = _read_json(p)
    existed = old is not None or p.exists()
    if keep_others and old:
        mine = {_real(r["path"]) for r in roots}
        roots = [r for r in old.get("roots") or [] if isinstance(r, dict) and _real(r.get("path", "")) not in mine] + roots
    cfg = {"version": 1, "mode": mode, "max_blocks": int((old or {}).get("max_blocks") or 2),
           "skill": SKILL_PREFIX + "changelog-entry", "roots": roots}
    if old != cfg:
        if not p.parent.exists():
            os.makedirs(str(p.parent), mode=0o700, exist_ok=True)
        atomic_write(p, json.dumps(cfg, indent=2) + "\n", mode=0o600)
        run.say("ok", f"End-of-turn check: {mode}, {len(roots)} folder(s), config in {_label(p)}")
    return existed


# ---------------------------------------------------------------- public API


def detect(values: dict | None = None) -> dict:
    """Read-only: the folders, what is in them, and the defaults for the habits step. Never raises."""
    try:
        plan = _plan(values)
    except Exception as exc:  # detection must never break the wizard
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "folders": []}
    ctx = plan["ctx"]
    folders = []
    for f in plan["folders"]:
        item = {k: f.get(k) for k in ("path", "label", "exists", "errors", "warnings", "installed", "target",
                                     "target_name", "git_top", "agents_md", "own_rules", "block_default",
                                     "block_reason", "hubs_found", "changelog_dir", "options")}
        item["block_now"] = {k: v for k, v in (f.get("block_now") or {}).items() if k != "inner"}
        h = f.get("hub") or {}
        item["hub"] = {k: h.get(k) for k in ("kind", "path", "label", "exists", "inside", "missing", "git_init")}
        item["manifest"] = bool(f.get("manifest"))
        item["repos"] = [{"name": r["name"], "label": r["label"], "path": r["path"], "worktrees": r["worktrees"],
                          "skipped": r["name"] in f["options"]["skip_repos"]} for r in f.get("repos") or []]
        item["selected"] = f.get("exists", False) and not f.get("errors")
        if f.get("exists"):
            ag = _agents_plan(f, plan["values"])
            item["agents"] = {"source": ag["source"], "files": [a["name"] for a in ag["files"]],
                              "lint": ag["lint"], "clashes": ag["clashes"], "fetched": ag["fetched"]}
        folders.append(item)
    return {
        "ok": True, "owner": ctx["owner"], "check_mode": ctx["mode"], "notify": ctx["notify"],
        "codegraph": ctx["codegraph"], "memory": memory_state(), "plugin_available": plugin_dir().is_dir(),
        "plugin_installed": plugin_installed() is not None, "plugin_version": plugin_version(),
        "installed": bool(plan["record"]), "config": str(config_path()),
        "allow_notify": plan["allow_notify"], "folders": folders, "options_help": FOLDER_OPTIONS,
    }


def preview(values: dict | None = None) -> dict:
    """What apply() would change, without changing anything: a diff per CLAUDE.md, the files to create, the
    settings to edit. {ok, changes: [{what, where, detail, diff?}], folders, lint, warnings, errors}."""
    v = dict(values or {})
    changes, warnings, errors, lint = [], [], [], []
    if not _bool(v.get("HABITS"), True):
        rec = _record(v)
        if rec:
            changes.append({"what": "Remove the working habits", "where": "every folder set up before",
                            "detail": "blocks, imported agents, the check's config and the plugin; hubs are kept"})
        return {"ok": True, "changes": changes, "folders": [], "lint": [], "warnings": [], "errors": []}
    try:
        plan = _plan(v)
    except Exception as exc:
        return {"ok": False, "changes": [], "folders": [], "lint": [], "warnings": [], "errors": [str(exc)]}
    ctx = plan["ctx"]
    rec_folders = {f["path"]: f for f in plan["record"].get("folders") or []}
    out_folders = []
    for f in plan["folders"]:
        errors += [f"{f['label']}: {e}" for e in f["errors"]]
        warnings += [f"{f['label']}: {w}" for w in f["warnings"]]
        if f["errors"]:
            out_folders.append({"path": f["path"], "block": "error", "errors": f["errors"]})
            continue
        h = f["hub"]
        if h["kind"] == "new":
            n = len(_skeleton_files())
            changes.append({"what": "Create a docs hub from the skeleton", "where": h["label"],
                            "detail": f"{n} files" + (", git init and a first commit" if h.get("git_init") else "")})
        elif h["kind"] == "existing" and h.get("missing") and f["options"]["fill_missing"]:
            changes.append({"what": "Add the skeleton files the hub lacks", "where": h["label"],
                            "detail": ", ".join(h["missing"])})
        elif h["kind"] == "existing" and h.get("missing"):
            warnings.append(f"{f['label']}: the hub lacks {', '.join(h['missing'])} (fill_missing adds them)")
        ag = _agents_plan(f, plan["values"])
        names = [a for a in ag["files"]]
        if ag["source"]["kind"] != "none":
            if not ag["fetched"]:
                changes.append({"what": "Import the team agents", "where": _label(ag.get("target") or f["path"]),
                                "detail": f"from {ag['source'].get('via')}; listed and checked after fetching"})
            elif ag["same"]:
                changes.append({"what": "Use the team agents in place", "where": _label(ag["target"]),
                                "detail": ", ".join(a["name"] for a in names)})
            else:
                changes.append({"what": f"Import {len(names)} team agent(s)", "where": _label(ag["target"]),
                                "detail": ", ".join(a["name"] for a in names)})
                for c in ag["clashes"]:
                    warnings.append(f"{f['label']}: .claude/agents/{c} is not Deskmate's and will be left alone")
                for x in ag["lint"]:
                    lint.append(dict(x, folder=f["path"]))
        state = "skip"
        diff = ""
        target = Path(f["target"])
        text, _, exists = _read_doc(target)
        now = f["block_now"]["state"]
        if f["options"]["block"]:
            agents = [{"name": a["name"], "description": a["description"]} for a in ag["files"]]
            if not agents and (rec_folders.get(f["path"]) or {}).get("agents"):
                agents = _installed_agent_rows(rec_folders[f["path"]]["agents"])
            lines, _sha_ = _block_lines(f, ctx, agents)
            if now == "broken":
                state = "error"
                errors.append(f"{_label(target)}: {f['block_now']['detail']}")
            elif now == "edited" and f["options"]["on_edit"] == "skip":
                state = "conflict"
                cur = f["block_now"]["inner"].split("\n")
                diff = "".join(difflib.unified_diff([ln + "\n" for ln in cur], [ln + "\n" for ln in lines[1:-1]],
                                                    "your edited block", "Deskmate's block", n=1))
                warnings.append(f"{_label(target)}: the rules block was edited by hand; choose overwrite or keep")
            elif now == "edited" and f["options"]["on_edit"] == "keep":
                state = "keep"
                changes.append({"what": "Stop managing the edited rules block", "where": _label(target),
                                "detail": "Deskmate's two marker lines are removed; the text stays, as yours"})
            else:
                prefix = ["@" + f["agents_md"]] if (not exists or not text.strip()) and f.get("agents_md") else None
                new = place_block(text, lines, prefix)
                state = "unchanged" if new == text and exists else ("replace" if now != "none" else "add")
                if state != "unchanged":
                    diff = "".join(difflib.unified_diff(_split(text), _split(new), _label(target), _label(target), n=2))
                    what = {"add": "Add the working-habits block", "replace": "Update the working-habits block"}[state]
                    detail = "new file" if not exists else ("replaced in place" if state == "replace" else "after your text")
                    if prefix:
                        detail += f"; it starts with {prefix[0]} so {f['agents_md']} still loads"
                    changes.append({"what": what, "where": _label(target), "detail": detail, "diff": diff})
                if f.get("exclude"):
                    have = {_bare(x).strip() for x in _split(_read_doc(f["exclude"]["file"])[0])}
                    if f["exclude"]["line"] not in have:
                        changes.append({"what": "Keep the rules file out of git", "where": _label(f["exclude"]["file"]),
                                        "detail": "adds " + f["exclude"]["line"]})
        elif now in ("current", "edited"):
            state = "remove"
            changes.append({"what": "Remove the working-habits block", "where": _label(target),
                            "detail": f["block_reason"] or "the block is switched off for this folder"})
        out_folders.append({"path": f["path"], "block": state, "reason": f["block_reason"], "diff": diff,
                            "target": f["target"], "hub": h.get("path"), "warnings": f["warnings"]})
    if not plan["only"]:
        keep = {f["path"] for f in plan["folders"]}
        for path in rec_folders:
            if path not in keep:
                changes.append({"what": "Remove the working habits from a folder", "where": _label(path),
                                "detail": "it is no longer picked; its hub is kept"})
    roots = _config_roots(plan["folders"])
    changes.append({"what": "Write the end-of-turn check's config", "where": _label(config_path()),
                    "detail": f"mode {ctx['mode']}; {len(roots)} folder(s) with a hub"})
    have = plugin_installed()
    if plan["plugin"] and have is None:
        changes.append({"what": "Install the habits plugin", "where": "Claude Code, user scope",
                        "detail": "skills habits:changelog-entry, habits:design-doc, habits:wrap-up; the Stop hook"})
    elif plan["plugin"] and have != plugin_version():
        changes.append({"what": "Update the habits plugin", "where": "Claude Code, user scope",
                        "detail": f"{have or 'an older version'} to {plugin_version()}"})
    elif not plan["plugin"] and (plan["record"].get("plugin") or {}).get("installed"):
        changes.append({"what": "Remove the habits plugin", "where": "Claude Code, user scope", "detail": PLUGIN_ID})
    settings = _label(_settings_file())
    if plan["allow_notify"]:
        cur = ((_read_json(_settings_file()) or {}).get("permissions") or {}).get("allow") or []
        if NOTIFY_TOOL not in cur:
            changes.append({"what": "Let sessions post milestones without a prompt", "where": settings,
                            "detail": f"permissions.allow += {NOTIFY_TOOL}"})
    for d in _outside_hubs(plan["folders"]):
        cur = ((_read_json(_settings_file()) or {}).get("permissions") or {}).get("additionalDirectories") or []
        if d not in cur:
            changes.append({"what": "Let sessions write to a hub outside the folder", "where": settings,
                            "detail": f"permissions.additionalDirectories += {d}"})
    mem = memory_state()
    if not mem["enabled"]:
        if plan["memory_on"]:
            changes.append({"what": "Turn auto memory on", "where": settings, "detail": "autoMemoryEnabled: true"})
        else:
            warnings.append(f"Claude Code's auto memory is off ({mem['why']}); the rules' memory advice will not apply")
    return {"ok": not errors, "changes": changes, "folders": out_folders, "lint": lint, "warnings": warnings,
            "errors": errors, "check_mode": ctx["mode"]}


def _installed_agent_rows(rec: dict) -> list:
    rows = []
    for name in sorted(rec.get("files") or {}):
        p = Path(rec["dir"]) / name
        try:
            fm = _frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if fm.get("name") and fm.get("description"):
            rows.append({"name": fm["name"], "description": fm["description"]})
    return rows


def _outside_hubs(folders: list) -> list:
    out = []
    for f in folders:
        h = f.get("hub") or {}
        if f.get("exists") and not f.get("errors") and h.get("path") and not h.get("inside"):
            if h["path"] not in out:
                out.append(h["path"])
    return out


def apply(values: dict | None, emit=None) -> dict:
    """Set up the working habits as planned. Safe to run again: unchanged files are not rewritten.
    Returns {ok, folders: [{path, block, hub, agents}], lint, warnings, errors, backups}."""
    v = dict(values or {})
    if not _bool(v.get("HABITS"), True):
        return remove(emit)
    run = Run(v, emit)
    try:
        plan = _plan(v)
    except Exception as exc:
        run.say("error", f"Could not plan the working habits: {exc}")
        return {"ok": False, "folders": [], "lint": [], "warnings": run.warnings, "errors": run.errors}
    ctx = plan["ctx"]
    rec = plan["record"] or {}
    rec_folders = {f["path"]: f for f in rec.get("folders") or [] if isinstance(f, dict) and f.get("path")}
    new_folders = dict(rec_folders) if plan["only"] else {}
    lint_all = []
    results = []
    items = {}
    # Pass 1: the hubs, so every hub (and its manifest, which may set the default check mode) is ready
    # before any block is rendered.
    for f in plan["folders"]:
        f["rec"] = rec_folders.get(f["path"]) or {}
        for w in f["warnings"]:
            run.say("warn", f"{f['label']}: {w}")
        if f["errors"]:
            for e in f["errors"]:
                run.say("error", f"{f['label']}: {e}")
            if f["rec"]:
                new_folders[f["path"]] = f["rec"]
            results.append({"path": f["path"], "block": "error"})
            continue
        item = items[f["path"]] = {"path": f["path"], "options": dict(f["options"])}
        hub_rec = _prepare_hub(run, f)
        if hub_rec:
            if f["hub"]["kind"] == "new":
                f["hub"] = dict(f["hub"], kind="existing", exists=True)
                _hub_details(f)
            item["hub"] = hub_rec
            item["options"].update(hub="existing", hub_path=hub_rec["path"])
        elif f["hub"]["kind"] != "none":
            f["hub"] = {"kind": "none", "path": None}
            run.say("warn", f"{f['label']}: no hub, so the rules leave out the hub rules and the check skips this folder")
    _mode_from_manifest(ctx, plan["folders"])
    # Pass 2: the agents, then the block that lists them.
    for f in plan["folders"]:
        if f["path"] not in items:
            continue
        item = items[f["path"]]
        ag = _agents_plan(f, run.values)
        if ag["source"]["kind"] == "git" and (not ag["fetched"] or plan["update"] or not f["rec"].get("agents")):
            commit = _fetch_agents(run, ag["source"])
            ag = _agents_plan(f, run.values)
        else:
            commit = (f["rec"].get("agents") or {}).get("commit", "")
            if ag["source"]["kind"] == "folder" and ag.get("folder"):
                r = _git(["-C", ag["folder"], "rev-parse", "HEAD"], timeout=10)
                commit = r.stdout.strip() if r is not None and r.returncode == 0 else ""
        listed = []
        if ag["source"]["kind"] != "none" and ag["fetched"]:
            if ag["same"]:
                listed = ag["files"]
                item["agents"] = {"dir": ag["target"], "same": True, "files": {}}
            elif ag["lint"] and not f["options"]["agents_accept"]:
                lint_all += [dict(x, folder=f["path"]) for x in ag["lint"]]
                run.say("warn", f"{f['label']}: the team agents were not imported; the check found "
                                f"{len(ag['lint'])} thing(s) to look at first (see the list, then accept to import)")
                if f["rec"].get("agents"):
                    item["agents"] = f["rec"]["agents"]
                    listed = _installed_agent_rows(f["rec"]["agents"])
            else:
                item["agents"] = _import_agents(run, f, ag, commit)
                listed = [a for a in ag["files"] if a["file"] in (item["agents"] or {}).get("files", {})]
        elif f["rec"].get("agents"):
            if ag["source"]["kind"] == "none":
                _remove_agents(run, f["rec"]["agents"])
            else:
                item["agents"] = f["rec"]["agents"]
                listed = _installed_agent_rows(f["rec"]["agents"])
        if f["options"]["block"]:
            lines, sha = _block_lines(f, ctx, [{"name": a["name"], "description": a["description"]} for a in listed])
            block = _write_block(run, f, lines, sha)
            if block:
                item["block"] = block
            elif f["rec"].get("block") and f["options"]["on_edit"] == "keep":
                item["options"]["block"] = False
        elif f["rec"].get("block"):
            if _remove_block(run, f["rec"]["block"]):
                run.say("info", f"{f['label']}: {f['block_reason'] or 'the block is switched off for this folder'}")
            else:
                item["block"] = f["rec"]["block"]
        elif f["own_rules"]:
            run.say("info", f"{f['label']}: no block written; {f['block_reason']}")
        new_folders[f["path"]] = item
        results.append({"path": f["path"], "block": "written" if item.get("block") else "none",
                        "hub": (item.get("hub") or {}).get("path"),
                        "agents": sorted((item.get("agents") or {}).get("files") or {})})
    # Folders no longer picked (a full plan only) lose their block and agents; their hub stays.
    if not plan["only"]:
        for path, old in rec_folders.items():
            if path not in new_folders:
                _remove_folder(run, old)
    # The check's config, from every folder that is set up.
    by_path = {f["path"]: f for f in plan["folders"]}
    cfg_folders = [by_path[p] for p in new_folders if p in by_path and new_folders[p].get("hub")]
    config_existed = _write_config(run, ctx["mode"], _config_roots(cfg_folders), keep_others=plan["only"])
    # Claude Code settings: notify permission, hubs outside their folder, auto memory.
    dirs = []
    for p, item in new_folders.items():
        f = by_path.get(p)
        h = (f or {}).get("hub") or {}
        if f and h.get("path") and not h.get("inside"):
            dirs.append(h["path"])
        elif not f and item.get("hub") and not _inside(item["hub"]["path"], p):
            dirs.append(item["hub"]["path"])
    settings_rec = _sync_settings(run, {"allow": [NOTIFY_TOOL] if plan["allow_notify"] else [],
                                        "dirs": sorted(set(dirs)), "memory": plan["memory_on"]},
                                  rec.get("settings_changed") or [])
    # The plugin: skills and the Stop hook.
    plugin_rec = rec.get("plugin") or {}
    have = plugin_installed()
    if plan["plugin"] and (have is None or not plugin_rec.get("installed") or have != plugin_version() or plan["update"]):
        if _plugin(run, True):  # installs, or updates to this clone's version
            plugin_rec = {"name": PLUGIN, "installed": True}
    elif not plan["plugin"] and plugin_rec.get("installed"):
        if _plugin(run, False):
            plugin_rec = {}
    new_rec = {
        "version": 1, "installed_at": rec.get("installed_at") or _now(), "updated_at": _now(),
        "config": {"path": str(config_path()), "created": rec.get("config", {}).get("created", not config_existed)},
        "state_dir": str(state_dir()), "check_mode": ctx["mode"],
        "folders": list(new_folders.values()), "settings_changed": settings_rec, "plugin": plugin_rec,
    }
    try:
        _save_record(new_rec, v)
    except OSError as exc:
        run.say("error", f"Could not record what was installed ({exc}); `./deskmate uninstall` cannot undo it")
    if run.backups:
        run.say("info", f"Backups of the files edited are in {_label(run.backups)}")
    ok = not run.errors
    run.say("ok" if ok else "error", "Working habits are set up" if ok else "Working habits: some parts failed (see above)")
    return {"ok": ok, "folders": results, "lint": lint_all, "warnings": run.warnings, "errors": run.errors,
            "backups": str(run.backups) if run.backups else None, "check_mode": ctx["mode"]}


def _remove_folder(run: Run, rec: dict) -> bool:
    ok = True
    if rec.get("block"):
        ok = _remove_block(run, rec["block"]) and ok
    if rec.get("agents") and not rec["agents"].get("same"):
        _remove_agents(run, rec["agents"])
        if rec["agents"].get("exclude_lines"):
            tree = git_work_tree(rec["path"])
            if tree:
                _exclude_remove(run, tree["exclude"], rec["agents"]["exclude_lines"])
    hub = rec.get("hub") or {}
    if hub.get("created") or hub.get("files_added"):
        run.say("info", f"Kept the docs hub {_label(hub['path'])}: it holds your docs")
    return ok


def remove(emit=None, folder: str | None = None) -> dict:
    """Undo what apply() recorded: for one folder, or everything (folder=None). Hubs and files the user
    edited are kept, and named. Returns {ok, removed: [folders], kept: [...], warnings, errors}."""
    run = Run({}, emit)
    rec = _record()
    if not rec:
        run.say("info", "Working habits are not set up; nothing to remove")
        return {"ok": True, "removed": [], "kept": [], "warnings": [], "errors": []}
    folders = [f for f in rec.get("folders") or [] if isinstance(f, dict) and f.get("path")]
    targets = [f for f in folders if folder is None or _same(f["path"], folder)]
    if folder is not None and not targets:
        run.say("warn", f"{_label(folder)} is not set up with the working habits")
        return {"ok": True, "removed": [], "kept": [], "warnings": run.warnings, "errors": []}
    removed, kept_records = [], []
    for f in targets:
        if _remove_folder(run, f):
            removed.append(f["path"])
        else:
            kept_records.append(f)
    left = [f for f in folders if f not in targets] + kept_records
    # habits.json: drop those roots; delete it when none is left and Deskmate created it.
    p = config_path()
    cfg = _read_json(p)
    if cfg is not None:
        gone = {_real(x) for x in removed}
        roots = [r for r in cfg.get("roots") or [] if isinstance(r, dict) and _real(r.get("path", "")) not in gone]
        if not left and rec.get("config", {}).get("created", True):
            p.unlink()
            run.say("ok", f"Removed {_label(p)}")
        elif roots != cfg.get("roots"):
            cfg["roots"] = roots
            atomic_write(p, json.dumps(cfg, indent=2) + "\n", mode=0o600)
    settings_rec = rec.get("settings_changed") or []
    plugin_rec = rec.get("plugin") or {}
    if not left:
        settings_rec = _sync_settings(run, {"allow": [], "dirs": [], "memory": False}, settings_rec)
        if plugin_rec.get("installed") and _plugin(run, False):
            plugin_rec = {}
        shutil.rmtree(str(state_dir()), ignore_errors=True)
    else:
        dirs = [f["hub"]["path"] for f in left if f.get("hub") and not _inside(f["hub"]["path"], f["path"])]
        allow = [r["value"] for r in settings_rec if r.get("key") == "permissions.allow"]
        settings_rec = _sync_settings(run, {"allow": allow, "dirs": dirs,
                                            "memory": any(r.get("key") == "autoMemoryEnabled" for r in settings_rec)},
                                      settings_rec)
    if left or settings_rec or plugin_rec:
        rec.update(folders=left, settings_changed=settings_rec, plugin=plugin_rec, updated_at=_now())
        _save_record(rec)
    else:
        _save_record(None)
    if run.backups:
        run.say("info", f"Backups of the files edited are in {_label(run.backups)}")
    return {"ok": not run.errors, "removed": removed, "kept": [f["path"] for f in kept_records],
            "warnings": run.warnings, "errors": run.errors}


def status() -> dict:
    """What is installed and whether it is still in shape, for `./deskmate habits status` and doctor."""
    rec = _record()
    cfg = _read_json(config_path())
    out = {"installed": bool(rec), "mode": (cfg or {}).get("mode") or "off", "config": str(config_path()),
           "config_exists": cfg is not None, "plugin": {"recorded": bool((rec.get("plugin") or {}).get("installed")),
                                                        "installed": plugin_installed() is not None,
                                                        "version": plugin_installed(), "clone_version": plugin_version()},
           "folders": [], "settings": rec.get("settings_changed") or [], "problems": []}
    for f in rec.get("folders") or []:
        item = {"path": f.get("path"), "label": _label(f.get("path", "")), "block": "off",
                "hub": (f.get("hub") or {}).get("path"), "hub_exists": False, "agents": {}}
        b = f.get("block")
        if b:
            text, _, exists = _read_doc(b["file"])
            info = block_info(text) if exists else {"state": "missing"}
            item["block"] = info["state"] if info["state"] != "none" else "missing"
            item["file"] = b["file"]
            if item["block"] in ("missing", "broken", "edited"):
                out["problems"].append(f"{_label(b['file'])}: the rules block is {item['block']}")
        if item["hub"]:
            hub = Path(item["hub"])
            item["hub_exists"] = hub.is_dir()
            if not item["hub_exists"]:
                out["problems"].append(f"the hub {_label(hub)} is gone")
        a = f.get("agents") or {}
        if a.get("files"):
            counts = {"unchanged": 0, "edited": [], "missing": []}
            for name, sha in a["files"].items():
                cur = _sha_file(Path(a["dir"]) / name)
                if cur is None:
                    counts["missing"].append(name)
                elif cur == sha:
                    counts["unchanged"] += 1
                else:
                    counts["edited"].append(name)
            item["agents"] = counts
        out["folders"].append(item)
    plug = out["plugin"]
    if rec and plug["recorded"] and not plug["installed"]:
        out["problems"].append("the habits plugin is recorded but Claude Code does not list it")
    elif plug["installed"] and plug["version"] and plug["version"] != plug["clone_version"]:
        out["problems"].append(f"the habits plugin is {plug['version']}, this clone has {plug['clone_version']}; "
                               "`./deskmate update` updates it")
    return out


def format_status(st: dict | None = None) -> str:
    """status() as a few plain lines for the terminal."""
    st = st or status()
    if not st["installed"]:
        return "Working habits: not set up. Run `./deskmate habits apply` or `./deskmate setup`."
    lines = [f"Working habits: end-of-turn check {st['mode']} ({_label(st['config'])})",
             f"  plugin: {'installed' if st['plugin']['installed'] else 'not installed'}"]
    for f in st["folders"]:
        agents = f.get("agents") or {}
        a = ""
        if agents:
            a = f", agents {agents['unchanged']} unchanged"
            a += f", {len(agents['edited'])} edited" if agents["edited"] else ""
            a += f", {len(agents['missing'])} missing" if agents["missing"] else ""
        hub = _label(f["hub"]) if f.get("hub") else "no hub"
        lines.append(f"  {f['label']}: block {f['block']}, hub {hub}{a}")
    for p in st["problems"]:
        lines.append(f"  ! {p}")
    return "\n".join(lines)
