#!/bin/bash
# Starts the screen (Xvnc), splits it into monitors, then the window manager, the browser
# supervisor (via Openbox autostart) and deskd.
# Environment (compose passes them from .env):
#   DESK_MONITORS      how many monitors, side by side, 1-4 (default 2)
#   DESK_MONITOR_SIZE  size of each monitor, default 1280x800
#   DESK_DISPLAY       X display number, default 87. In host mode the desk shares this machine's
#                      network namespace, so its abstract X socket must not collide with a display
#                      the machine already uses (setup picks a free one). Any number works otherwise.
#   TZ                 time zone for the panel clock and every page's Date (default UTC)
#   DESK_NETWORK       host | host-access | isolated (see deploy/net-*.yaml): what the desk's browser
#                      may reach, turned into Chromium --host-resolver-rules below
#   CDP_PORT           Chromium's DevTools port on the desk's 127.0.0.1, default 7802
#   HOST_RULES         optional extra resolver rules, appended after the ones built here
#   RUN_DIR            where deskd.sock and vnc.sock live; a volume shared with the hub only
set -euo pipefail
: "${DESK_MONITORS:=2}"
: "${DESK_MONITOR_SIZE:=1280x800}"
: "${DESK_DISPLAY:=87}"
: "${DESK_NETWORK:=host}"
: "${CDP_PORT:=7802}"
: "${RUN_DIR:=/run/desk}"

case "$DESK_MONITORS" in
  [1-4]) ;;
  *) echo "warning: DESK_MONITORS=$DESK_MONITORS is not 1-4; using 2" >&2; DESK_MONITORS=2 ;;
esac
case "$DESK_MONITOR_SIZE" in
  [1-9][0-9][0-9]x[1-9][0-9][0-9] | [1-9][0-9][0-9][0-9]x[1-9][0-9][0-9] | [1-9][0-9][0-9][0-9]x[1-9][0-9][0-9][0-9]) ;;
  *) echo "warning: DESK_MONITOR_SIZE=$DESK_MONITOR_SIZE is not WIDTHxHEIGHT; using 1280x800" >&2; DESK_MONITOR_SIZE=1280x800 ;;
esac
case "$DESK_DISPLAY" in
  '' | *[!0-9]*) echo "warning: DESK_DISPLAY=$DESK_DISPLAY is not a number; using 87" >&2; DESK_DISPLAY=87 ;;
esac
if [ -n "${TZ:-}" ] && [ ! -f "/usr/share/zoneinfo/$TZ" ]; then
  echo "warning: unknown time zone $TZ; using UTC" >&2
  TZ=UTC
fi
export TZ="${TZ:-UTC}"

MONITORS="$DESK_MONITORS"
MONITOR_SIZE="$DESK_MONITOR_SIZE"
DISPLAY_NUM="$DESK_DISPLAY"
MON_W="${MONITOR_SIZE%x*}"
MON_H="${MONITOR_SIZE#*x}"
export DISPLAY=":${DISPLAY_NUM}"
export XAUTHORITY=/tmp/.Xauthority
export CDP_PORT

# ---- what the desk's browser may reach -------------------------------------------------------
# host         nothing to do: the desk shares this machine's network, localhost included.
# host-access  this machine is host.docker.internal (Docker Desktop provides the name; on Linux the
#              compose file adds it). localhost, *.localhost, 127.0.0.1 and ::1 are mapped to its IPv4
#              address, resolved here so Chromium never tries IPv6 first. Pages keep their Host header
#              (localhost:5173) and stay secure contexts.
# isolated     the internet only: refuse the names Docker gives this machine, their addresses, the
#              desk's gateway and private address ranges. Best effort, and for the browser only.
resolve4() { timeout 3 getent ahostsv4 "$1" 2>/dev/null | awk 'NR == 1 { print $1 }' || true; }

gateway4() {
  # The default route's gateway, read from /proc/net/route (hex, little-endian); no iproute2 needed.
  local g
  g=$(awk '$2 == "00000000" { print $3; exit }' /proc/net/route 2>/dev/null || true)
  [ "${#g}" -eq 8 ] || return 0
  printf '%d.%d.%d.%d\n' "0x${g:6:2}" "0x${g:4:2}" "0x${g:2:2}" "0x${g:0:2}"
}

RULES=""
add_rule() { RULES="${RULES:+$RULES, }$1"; }

case "$DESK_NETWORK" in
  host) ;;
  host-access)
    target=$(resolve4 host.docker.internal)
    if [ -z "$target" ]; then
      echo "warning: host.docker.internal does not resolve: localhost in the desk's browser will not reach this machine" >&2
      target=host.docker.internal
    fi
    for name in localhost '*.localhost' 127.0.0.1 ::1; do add_rule "MAP $name $target"; done
    ;;
  isolated)
    # Rules match the host as written in the URL, so names and IP literals both need one. A pattern
    # like 172.2?.* matches IP literals only in practice: real host names end in a top-level domain.
    for name in host.docker.internal gateway.docker.internal docker.for.mac.localhost docker.for.mac.host.internal \
      docker.for.win.localhost docker.for.win.host.internal host.containers.internal host.lima.internal \
      host.orb.internal kubernetes.docker.internal hub; do
      add_rule "MAP $name ^NOTFOUND"
    done
    # Docker Desktop resolves these two to this machine; the hub starts after the desk, and its
    # address is in a private range anyway.
    for name in host.docker.internal gateway.docker.internal; do
      ip=$(resolve4 "$name")
      if [ -n "$ip" ]; then add_rule "MAP $ip ^NOTFOUND"; fi
    done
    gw=$(gateway4)
    if [ -n "$gw" ]; then add_rule "MAP $gw ^NOTFOUND"; fi
    # 10/8, 172.16/12, 192.168/16, link-local, carrier-grade NAT 100.64/10 (Tailscale), and their IPv6 kin.
    for range in '10.*' '172.16.*' '172.17.*' '172.18.*' '172.19.*' '172.2?.*' '172.30.*' '172.31.*' '192.168.*' \
      '169.254.*' '100.64.*' '100.65.*' '100.66.*' '100.67.*' '100.68.*' '100.69.*' '100.7?.*' '100.8?.*' '100.9?.*' \
      '100.10?.*' '100.11?.*' '100.120.*' '100.121.*' '100.122.*' '100.123.*' '100.124.*' '100.125.*' '100.126.*' \
      '100.127.*' 'fc*:*' 'fd*:*' 'fe8?:*' 'fe9?:*' 'fea?:*' 'feb?:*' '::ffff:*'; do
      add_rule "MAP $range ^NOTFOUND"
    done
    ;;
  *)
    echo "warning: DESK_NETWORK=$DESK_NETWORK is not host, host-access or isolated; treating it as host" >&2
    DESK_NETWORK=host
    ;;
esac
if [ -n "${HOST_RULES:-}" ]; then add_rule "$HOST_RULES"; fi
export HOST_RULES="$RULES"
echo "desk: network $DESK_NETWORK, display $DISPLAY, $MONITORS x $MONITOR_SIZE, TZ $TZ"
if [ -n "$HOST_RULES" ]; then echo "desk: browser resolver rules: $HOST_RULES"; fi

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

# The hub reaches DevTools directly in host mode (127.0.0.1:$CDP_PORT of this machine). In the bridge
# modes it comes over the private compose network, and headed Chromium binds DevTools to 127.0.0.1
# whatever it is told, so socat forwards 0.0.0.0:7812 inside the desk to it; nothing publishes 7812.
# Never in host mode: there 0.0.0.0 would be every interface of this machine.
case "$DESK_NETWORK" in
  host-access | isolated)
    socat TCP-LISTEN:7812,bind=0.0.0.0,fork,reuseaddr "TCP:127.0.0.1:${CDP_PORT}" >/tmp/socat.log 2>&1 &
    ;;
  *)
    if [ -n "${CDP_PUBLISH:-}" ]; then
      echo "warning: CDP_PUBLISH is ignored in host mode (it would open DevTools on every interface of this machine)" >&2
    fi
    ;;
esac

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
