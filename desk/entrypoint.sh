#!/bin/bash
# Starts the screen (Xvnc), splits it into monitors, then the window manager, the browser
# supervisor (via Openbox autostart) and deskd.
# Environment:
#   MONITORS      how many monitors, side by side (default 2)
#   MONITOR_SIZE  size of each monitor, default 1280x800
#   DISPLAY_NUM   X display number, default 87 (the desk shares the host's network namespace, so
#                 its abstract X socket must not collide with a display the host already uses)
#   CDP_PORT      Chromium's DevTools port on 127.0.0.1, default 7802
#   CDP_PUBLISH   if set (isolated mode), also forward 0.0.0.0:7812 → 127.0.0.1:$CDP_PORT so a
#                 published port can reach it from outside a bridge network
#   RUN_DIR       where deskd.sock and vnc.sock live; a volume shared with the hub only
set -euo pipefail
: "${MONITORS:=2}"
: "${MONITOR_SIZE:=1280x800}"
: "${DISPLAY_NUM:=87}"
: "${RUN_DIR:=/run/desk}"
MON_W="${MONITOR_SIZE%x*}"
MON_H="${MONITOR_SIZE#*x}"
export DISPLAY=":${DISPLAY_NUM}"
export XAUTHORITY=/tmp/.Xauthority

# File exchange with the sessions: files they send arrive in Uploads, Chromium saves to Downloads.
mkdir -p "$HOME/Downloads" "$HOME/Uploads"
rm -f "$RUN_DIR"/*.sock "/tmp/.X${DISPLAY_NUM}-lock"

# A cookie instead of an open display: with host networking, any host process could otherwise
# open this display through its abstract socket.
touch "$XAUTHORITY"
xauth -f "$XAUTHORITY" add "$DISPLAY" . "$(mcookie 2>/dev/null || head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

# Xvnc is the X server and the VNC server in one. VNC listens on a unix socket only (no TCP port),
# so nothing on the host reaches the live view except through the hub, which checks who you are.
# -noreset: an X server resets when its last client leaves, which would drop the monitor layout.
Xvnc "$DISPLAY" -geometry "$((MON_W * MONITORS))x${MON_H}" -depth 24 \
  -auth "$XAUTHORITY" -nolisten tcp -noreset \
  -rfbport -1 -rfbunixpath "$RUN_DIR/vnc.sock" -rfbunixmode 0666 \
  -SecurityTypes None -AlwaysShared -AcceptSetDesktopSize=1 \
  >/tmp/xvnc.log 2>&1 &

for _ in $(seq 1 100); do
  if [ -S "$RUN_DIR/vnc.sock" ] && xdotool getdisplaygeometry >/dev/null 2>&1; then break; fi
  sleep 0.1
done

# One real output per monitor, so browsers see separate screens (see monitors.py).
if [ "$MONITORS" -gt 1 ]; then
  python3 /opt/desk/monitors.py "$RUN_DIR/vnc.sock" "$MON_W" "$MON_H" "$MONITORS" \
    || echo "warning: could not split the screen into $MONITORS monitors" >&2
fi
printf '%s\n' "$MONITORS" > "$RUN_DIR/monitors"
printf '%s\n' "$MONITOR_SIZE" > "$RUN_DIR/monitor-size"

if command -v dbus-launch >/dev/null; then
  eval "$(dbus-launch --sh-syntax)"
  export DBUS_SESSION_BUS_ADDRESS
fi

openbox-session >/tmp/openbox.log 2>&1 &

if [ -n "${CDP_PUBLISH:-}" ]; then
  socat TCP-LISTEN:7812,bind=0.0.0.0,fork,reuseaddr "TCP:127.0.0.1:${CDP_PORT:-7802}" >/tmp/socat.log 2>&1 &
fi

# Graceful shutdown: close Chromium first so it writes its cookies and logins, then stop deskd.
DESKD_SOCKET="$RUN_DIR/deskd.sock" node /opt/desk/deskd.mjs &
DESKD_PID=$!
shutdown() {
  pkill -TERM -f '/opt/desk/browser.sh' 2>/dev/null || true
  pkill -TERM -x chromium 2>/dev/null || true
  for _ in $(seq 1 50); do pgrep -x chromium >/dev/null 2>&1 || break; sleep 0.1; done
  kill -TERM "$DESKD_PID" 2>/dev/null || true
  wait "$DESKD_PID" 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT
wait "$DESKD_PID"
