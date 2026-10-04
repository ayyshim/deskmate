#!/bin/sh
# Claude Code hook → Deskmate hub. Posts the hook's JSON (stdin) to the hub and never holds up a
# turn: it gives up after 1 s, prints nothing, and always exits 0.
#   hook.sh pre-tool | stop | session-end | pre-compact
TOKEN_FILE="$HOME/.local/share/deskmate/token"
[ -r "$TOKEN_FILE" ] || exit 0
curl -s -m 1 -o /dev/null \
  -H "Authorization: Bearer $(cat "$TOKEN_FILE")" -H "Content-Type: application/json" \
  --data-binary @- "http://127.0.0.1:7800/hooks/$1" 2>/dev/null
exit 0
