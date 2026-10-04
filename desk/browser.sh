#!/bin/bash
# Keeps one Chromium running with DevTools on 127.0.0.1:$CDP_PORT. If it exits or crashes, it is
# started again a second later; `deskd` action restart_browser just kills it.
#
# Its own profile directory: Chromium refuses remote debugging on the default one, and the panel's
# Browser launcher opens windows in this same instance.
: "${CDP_PORT:=7802}"
PROFILE="$HOME/.config/deskmate-chromium"
mkdir -p "$PROFILE"

while true; do
  # Never show "Chromium didn't shut down correctly": mark the last exit as clean.
  PREFS="$PROFILE/Default/Preferences"
  if [ -f "$PREFS" ]; then
    sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/; s/"exited_cleanly":false/"exited_cleanly":true/' "$PREFS" 2>/dev/null || true
  fi
  # --no-sandbox: the container is the sandbox (no user namespaces without privileges);
  # --test-type hides the warning bar that flag would otherwise show on every window.
  chromium \
    --user-data-dir="$PROFILE" \
    --remote-debugging-port="$CDP_PORT" \
    --remote-debugging-address=127.0.0.1 \
    --no-first-run --no-default-browser-check \
    --no-sandbox --test-type \
    --password-store=basic \
    --disable-dev-shm-usage \
    --disable-features=Translate,MediaRouter,DialMediaRouteProvider \
    --hide-crash-restore-bubble \
    --start-maximized \
    --window-position=0,0 \
    about:blank >/tmp/chromium.log 2>&1
  sleep 1
done
