"""The MCP sessions that use the desk, one per Claude Code session, and how they are labelled.

A session is first known only by its MCP session id. The PreToolUse hook (matcher
`mcp__deskmate__.*`) tells the hub which Claude Code session and working directory is about to
call which tool with which arguments; the next MCP call with the same tool and arguments links
the two (design question Q1). After that the session is shown as "my-app · <first why>".
"""

from __future__ import annotations

import json
import os
import time

from . import db, journal

_pending: list[dict] = []
_LINK_WINDOW = 20.0


def letter(n: int) -> str:
    n -= 1
    return chr(65 + n % 26) + (str(n // 26) if n >= 26 else "")


def _row(sid: str) -> dict:
    row = db.one("select * from mcp_sessions where id = ?", (sid,))
    if row:
        return dict(row)
    n = db.one("select coalesce(max(number), 0) as n from mcp_sessions")["n"] + 1
    now = time.time()
    db.run(
        "insert into mcp_sessions(id, number, first_seen, last_seen) values(?, ?, ?, ?)",
        (sid, n, now, now),
    )
    return dict(db.one("select * from mcp_sessions where id = ?", (sid,)))


def _norm(args: dict) -> dict:
    return json.loads(json.dumps(args or {}, default=str))


def hook_pre_tool(payload: dict) -> None:
    """Remember a PreToolUse hook for a deskmate tool until its MCP call arrives."""
    now = time.time()
    _pending[:] = [p for p in _pending if now - p["ts"] < _LINK_WINDOW]
    tool = str(payload.get("tool_name") or "")
    if "__" not in tool:
        return
    _pending.append(
        {
            "ts": now,
            "tool": tool.rsplit("__", 1)[-1],
            "args": _norm(payload.get("tool_input") or {}),
            "claude_session": payload.get("session_id"),
            "cwd": payload.get("cwd"),
        }
    )


def _link(info: dict, tool: str, args: dict) -> None:
    if info.get("claude_session"):
        return
    mine = _norm(args)
    now = time.time()
    for p in reversed(_pending):
        # The MCP call also carries defaults the hook never saw, so compare only what the hook saw.
        same = all(mine.get(k) == v for k, v in p["args"].items())
        if p["tool"] == tool and same and now - p["ts"] < _LINK_WINDOW:
            _pending.remove(p)
            db.run(
                "update mcp_sessions set claude_session = ?, cwd = ? where id = ?",
                (p["claude_session"], p["cwd"], info["id"]),
            )
            info["claude_session"], info["cwd"] = p["claude_session"], p["cwd"]
            return


def touch(sid: str, tool: str, args: dict, why: str | None) -> dict:
    """Record a call from this session; returns its row."""
    info = _row(sid)
    _link(info, tool, args)
    if not info.get("label") and why:
        info["label"] = why.strip()[:60]
        db.run("update mcp_sessions set label = ? where id = ?", (info["label"], sid))
    db.run("update mcp_sessions set last_seen = ? where id = ?", (time.time(), sid))
    journal.publish("sessions", None)
    return info


def short(sid: str | None) -> str:
    if not sid:
        return "?"
    info = _row(sid)
    return letter(info["number"])


def label(sid: str | None) -> str:
    if not sid:
        return "nobody"
    if sid == "human":
        return "you"
    info = _row(sid)
    name = os.path.basename(info["cwd"].rstrip("/")) if info.get("cwd") else f"session {letter(info['number'])}"
    return f"{name} · {info['label']}" if info.get("label") else name


def active(within: float = 6 * 3600) -> list[dict]:
    rows = db.q("select * from mcp_sessions where last_seen > ? order by number", (time.time() - within,))
    out = []
    for r in rows:
        d = dict(r)
        d["letter"] = letter(d["number"])
        d["display"] = label(d["id"])
        d["project"] = os.path.basename(d["cwd"].rstrip("/")) if d.get("cwd") else None
        out.append(d)
    return out
