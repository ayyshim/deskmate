#!/usr/bin/env bash
# Connect Claude Code to Deskmate: the MCP server for every session (user scope), the hooks, and
# the deskmate skill. Safe to run again. Undo with scripts/uninstall.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="$HOME/.local/share/deskmate"
TOKEN=$(grep '^DESKMATE_TOKEN=' .env | cut -d= -f2-)
[ -n "$TOKEN" ] || { echo "No DESKMATE_TOKEN in .env. Run make env first." >&2; exit 1; }
mkdir -p "$DATA"
(umask 077 && printf '%s' "$TOKEN" > "$DATA/token")
install -m 755 scripts/hook.sh "$DATA/hook.sh"

claude mcp remove --scope user deskmate >/dev/null 2>&1 || true
# --header takes several values, so it goes last or it swallows the name and URL.
claude mcp add --scope user --transport http deskmate http://127.0.0.1:7800/mcp --header "Authorization: Bearer $TOKEN"

# The secretary's hooks (stop, session-end, pre-compact) are added once it exists (milestone M4).
python3 scripts/hooks.py install "$DATA/hook.sh" ${HOOK_EVENTS:-pre-tool}

mkdir -p "$HOME/.claude/skills/deskmate"
cp skill/deskmate/SKILL.md "$HOME/.claude/skills/deskmate/SKILL.md"
echo "Done. New Claude Code sessions get the deskmate tools; restart running ones to pick them up."
