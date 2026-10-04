"""Git repos under SESSIONS_ROOTS and, when READ_GIT is on, what their history says. Read-only.

Repos are found by looking for `.git` (a folder, or a file for linked worktrees) in each root, its
children and grandchildren, without running git: grouping worktrees under their main repo needs only
the `gitdir:` line. That is enough to label files and sessions by repo even with READ_GIT off.

With READ_GIT on, the sweep runs a few git commands per repo (`--no-optional-locks`, so a read never
writes the index): branches with commits on no remote, commits of the last two weeks by author date,
and for the docs folder the design folders each commit touched.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import threading
import time

from .. import config
from . import util

log = logging.getLogger(__name__)

SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "dist", "build", "target", "vendor", ".git"}
MAX_DIRS = 4000
_cache: dict = {"ts": 0.0, "sig": None, "repos": []}
_lock = threading.Lock()
CACHE_S = 120.0


# ---------------------------------------------------------------- discovery (no git needed)


def _gitdir_of(d: str) -> str | None:
    """The git dir of a working tree: d/.git, or the path in a worktree's `.git` file."""
    g = os.path.join(d, ".git")
    if os.path.isdir(g):
        return g
    if os.path.isfile(g):
        try:
            with open(g, encoding="utf-8", errors="replace") as f:
                line = f.readline().strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            p = line[7:].strip()
            return os.path.normpath(p if os.path.isabs(p) else os.path.join(d, p))
    return None


def _common_dir(gitdir: str) -> str:
    """<main>/.git/worktrees/<name> -> <main>/.git; anything else is its own common dir."""
    parent = os.path.dirname(gitdir)
    if os.path.basename(parent) == "worktrees":
        return os.path.dirname(parent)
    return gitdir


def _candidates(root: str):
    """The root, its children and grandchildren (not below a repo, not hidden or build folders)."""
    seen = 0
    level = [root]
    for depth in range(3):
        nxt = []
        for d in level:
            seen += 1
            if seen > MAX_DIRS:
                return
            if _gitdir_of(d):
                yield d
                continue
            if depth == 2:
                continue
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for n in names:
                if n.startswith(".") or n in SKIP_DIRS:
                    continue
                p = os.path.join(d, n)
                if os.path.isdir(p) and not os.path.islink(p):
                    nxt.append(p)
        level = nxt


def _discover() -> list[dict]:
    groups: dict[str, dict] = {}
    for root in config.SESSIONS_ROOTS:
        for d in _candidates(root):
            gd = _gitdir_of(d)
            if not gd:
                continue
            common = _common_dir(gd)
            main = os.path.dirname(common) if os.path.basename(common) == ".git" else d
            g = groups.setdefault(common, {"common_dir": common, "path": main, "worktrees": [], "root": root})
            if d not in g["worktrees"]:
                g["worktrees"].append(d)
    repos = sorted(groups.values(), key=lambda g: g["path"])
    taken: dict[str, int] = {}
    for g in repos:
        name = os.path.basename(g["path"].rstrip("/")) or "repo"
        if name in taken:
            rel = g["path"][len(g["root"].rstrip("/")) + 1:] if g["path"].startswith(g["root"] + "/") else g["path"]
            name = rel.strip("/") or f"{name}-{taken[name] + 1}"
        taken[name] = taken.get(name, 0) + 1
        g["key"] = name
        g["label"] = name
    docs = config.DOCS_DIR
    if docs:
        dk = docs_key()
        for g in repos:
            if os.path.normpath(g["path"]) == os.path.normpath(docs):
                g["key"], g["label"], g["docs"] = dk, docs_label(), True
    return repos


def repos() -> list[dict]:
    """Every repo under the roots: {key, label, path, worktrees, common_dir, root}. Cached for two minutes."""
    sig = (tuple(config.SESSIONS_ROOTS), config.DOCS_DIR)
    with _lock:
        if _cache["sig"] == sig and time.monotonic() - _cache["ts"] < CACHE_S:
            return _cache["repos"]
    found = _discover()
    with _lock:
        _cache.update(ts=time.monotonic(), sig=sig, repos=found)
    return found


def forget_cache() -> None:
    with _lock:
        _cache.update(ts=0.0, sig=None)


def docs_key() -> str | None:
    if not config.DOCS_DIR:
        return None
    return os.path.basename(config.DOCS_DIR.rstrip("/")) or "docs"


def docs_is_hub() -> bool:
    d = config.DOCS_DIR
    return bool(d) and (os.path.isdir(os.path.join(d, "changelog")) or os.path.isdir(os.path.join(d, "design")))


def docs_label() -> str:
    k = docs_key() or "docs"
    return f"{k} hub" if docs_is_hub() else f"{k} docs"


def _inside(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def repo_of(path: str | None) -> str | None:
    """The repo key of a host path: the longest repo working tree (or the docs folder) that holds it."""
    if not path or not path.startswith("/"):
        return None
    p = os.path.normpath(path)
    best, best_len = None, -1
    if config.DOCS_DIR and _inside(p, config.DOCS_DIR) and len(config.DOCS_DIR) > best_len:
        best, best_len = docs_key(), len(config.DOCS_DIR)
    for r in repos():
        for w in r["worktrees"] + [r["path"]]:
            if _inside(p, w) and len(w) > best_len:
                best, best_len = r["key"], len(w)
    return best or _sibling_of(p)


def _sibling_of(p: str) -> str | None:
    """A path under a folder that sits next to a repo and is named after it, where worktrees and spare clones
    live (<root>/fleet-wt/<branch>/…, <root>/fleet-hotfix/…): that repo's key. This also places files of a
    worktree that has since been removed, which discovery cannot see (it needs the worktree's .git file)."""
    for root in config.SESSIONS_ROOTS:
        base = root.rstrip("/")
        if not p.startswith(base + "/"):
            continue
        rest = p[len(base) + 1:]
        if "/" not in rest:
            continue  # a file in the root itself, not in a folder named after a repo
        top = rest.split("/", 1)[0]
        best = None
        for r in repos():
            name = os.path.basename(r["path"].rstrip("/"))
            if os.path.dirname(r["path"].rstrip("/")) != base or len(top) <= len(name):
                continue
            if top.startswith(name) and top[len(name)] in "-_." and (best is None or len(name) > len(best[0])):
                best = (name, r["key"])
        if best:
            return best[1]
    return None


def labels() -> dict[str, str]:
    out = {r["key"]: r["label"] for r in repos()}
    dk = docs_key()
    if dk:
        out[dk] = docs_label()
    return out


def label(key: str | None) -> str:
    if not key:
        return ""
    return labels().get(key, key)


def find(key: str) -> dict | None:
    return next((r for r in repos() if r["key"] == key), None)


# ---------------------------------------------------------------- git (READ_GIT)


def git(repo_path: str, *args: str, timeout: float = 20.0) -> str:
    """stdout of a read-only git command, or '' on any failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_OPTIONAL_LOCKS="0")
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", repo_path, *args], capture_output=True, text=True,
                           timeout=timeout, env=env, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def has_remote(repo: dict) -> bool:
    return bool(git(repo["path"], "remote").strip())


def user_email(repo: dict) -> str | None:
    return git(repo["path"], "config", "user.email").strip().lower() or None


def worktree_heads(repo: dict) -> list[str]:
    heads = []
    for line in git(repo["path"], "worktree", "list", "--porcelain").splitlines():
        if line.startswith("HEAD "):
            heads.append(line[5:].strip())
    return heads


def unpushed_branches(repo: dict) -> list[dict]:
    """Local branches holding commits that are on no remote. Repos without a remote never count."""
    if not has_remote(repo):
        return []
    out = []
    for line in git(repo["path"], "for-each-ref", "--format=%(refname:short)%09%(committerdate:unix)",
                    "refs/heads").splitlines():
        name, _, cts = line.partition("\t")
        if not name:
            continue
        n = git(repo["path"], "rev-list", "--count", f"refs/heads/{name}", "--not", "--remotes").strip()
        if n.isdigit() and int(n) > 0:
            out.append({"branch": name, "count": int(n),
                        "last_day": util.day_of(float(cts)) if cts.strip().isdigit() else None})
    return out


def commits_since(repo: dict, days: int = 14) -> list[dict]:
    """Commits reachable from any branch, remote, tag or worktree HEAD (not stash or notes), newest first.
    Grouped by AUTHOR date (a rebase keeps it); --since filters on committer date, so it is only a pre-filter."""
    since = (dt.date.today() - dt.timedelta(days=days + 1)).isoformat()
    fmt = "%H%x1f%h%x1f%at%x1f%ae%x1f%P%x1f%s%x1e"
    raw = git(repo["path"], "log", "--no-color", "--branches", "--remotes", "--tags", "HEAD",
              *worktree_heads(repo), f"--since={since} 00:00", f"--format={fmt}", timeout=40)
    me = user_email(repo)
    out, seen = [], set()
    cutoff = time.time() - (days + 1) * 86400
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) != 6:
            continue
        full, short, at, ae, parents, subj = parts
        if full in seen or not at.isdigit() or int(at) < cutoff:
            continue
        seen.add(full)
        out.append({"sha": full, "short": short, "ts": float(at), "day": util.day_of(float(at)),
                    "time": util.hhmm(float(at)), "mine": (not me) or ae.lower() == me,
                    "merge": len(parents.split()) > 1, "subject": subj})
    return out


def docs_changes(days: int = 30) -> list[dict]:
    """Commits in the docs folder's repo with the docs paths they touched: [{ts, day, paths}]."""
    docs = config.DOCS_DIR
    if not docs or not os.path.isdir(docs):
        return []
    top = git(docs, "rev-parse", "--show-toplevel").strip()
    if not top:
        return []
    since = (dt.date.today() - dt.timedelta(days=days + 1)).isoformat()
    raw = git(docs, "log", "--no-color", "--name-only", f"--since={since} 00:00", "--format=%x1e%at", "--", ".",
              timeout=40)
    out = []
    for chunk in raw.split("\x1e"):
        lines = [x for x in chunk.strip("\n").splitlines() if x.strip()]
        if not lines or not lines[0].strip().isdigit():
            continue
        ts = float(lines[0].strip())
        paths = [os.path.join(top, x.strip()) for x in lines[1:]]
        out.append({"ts": ts, "day": util.day_of(ts), "paths": paths})
    return out
