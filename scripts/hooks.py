"""Add or remove Deskmate's hooks in ~/.claude/settings.json without touching anyone else's.

    python3 scripts/hooks.py install <path to hook.sh> <event>...   (pre-tool, stop, session-end, pre-compact)
    python3 scripts/hooks.py uninstall

Deskmate's entries are the ones whose command runs deskmate/hook.sh. A copy of the file is kept
next to it as settings.json.deskmate-backup before every change.
"""

import json
import shutil
import sys
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
MARK = "deskmate/hook.sh"


def ours(group: dict) -> bool:
    return any(MARK in h.get("command", "") for h in group.get("hooks", []))


def main() -> None:
    action = sys.argv[1]
    data = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, SETTINGS.with_name("settings.json.deskmate-backup"))
    hooks = data.setdefault("hooks", {})
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not ours(g)]
        if not hooks[event]:
            del hooks[event]
    if action == "install":
        script, wanted = sys.argv[2], set(sys.argv[3:])

        def entry(arg: str, matcher: str | None = None) -> dict:
            g = {"hooks": [{"type": "command", "command": f"{script} {arg}", "timeout": 5}]}
            if matcher:
                g["matcher"] = matcher
            return g

        # Links an MCP session to its Claude Code session and folder (design Q1).
        if "pre-tool" in wanted:
            hooks.setdefault("PreToolUse", []).append(entry("pre-tool", "mcp__deskmate__.*"))
        # The secretary's triggers (§3.3): a turn ended, a session ended, a compaction is coming.
        for event, arg in (("Stop", "stop"), ("SessionEnd", "session-end"), ("PreCompact", "pre-compact")):
            if arg in wanted:
                hooks.setdefault(event, []).append(entry(arg))
    if not hooks:
        del data["hooks"]
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")
    print(f"{action}ed Deskmate hooks in {SETTINGS}")


main()
