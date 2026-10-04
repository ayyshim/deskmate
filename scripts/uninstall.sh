#!/usr/bin/env bash
# Disconnect Claude Code from Deskmate. Leaves the containers and their data alone (make down for those).
set -euo pipefail
cd "$(dirname "$0")/.."
claude mcp remove --scope user deskmate >/dev/null 2>&1 || true
python3 scripts/hooks.py uninstall
rm -rf "$HOME/.claude/skills/deskmate"
rm -f "$HOME/.local/share/deskmate/token" "$HOME/.local/share/deskmate/hook.sh"
echo "Removed the deskmate MCP server, hooks and skill."
