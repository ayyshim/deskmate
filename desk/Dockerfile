# The desk: the smallest useful computer for Claude Code sessions to share.
#
#   debian:bookworm-slim                      base
#   Xvnc (TigerVNC)                           the screen and the live view in one, VNC on a unix socket
#   Openbox + tint2 + feh                     window manager, bottom panel, wallpaper
#   chromium (CDP on 127.0.0.1:7802) + xterm  the apps
#   xdotool + scrot + xclip                   hands and eyes for deskd
#   deskd.mjs (Node)                          the control API, on a unix socket
#
# Built from Deskfish's docker/desktop (Apache-2.0, see NOTICE). Firefox and its page bridge are
# replaced by Chromium, because CDP gives each session its own tab and a cheap page outline. Xvfb and
# x11vnc are replaced by Xvnc, because only Xvnc gives one screen several real outputs (monitors.py).
#
# The bot user is uid 10001, not 1000: the desk shares the host's network namespace, and a uid
# that maps to a real host user could pass X11 "localuser" checks on the host's own display.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      tigervnc-standalone-server xauth openbox xdotool scrot xclip xterm x11-xserver-utils x11-utils xinput \
      tint2 feh librsvg2-bin \
      chromium fonts-dejavu-core fonts-liberation2 fonts-noto-color-emoji dbus-x11 \
      nodejs ca-certificates procps socat \
      curl python3 jq git zip unzip nano less poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 -s /bin/bash bot \
    && mkdir -p /run/desk && chmod 1777 /run/desk

COPY chromium-policies.json /etc/chromium/policies/managed/deskmate.json
COPY openbox/menu.xml openbox/autostart openbox/rc.xml /etc/xdg/openbox/
COPY tint2rc /etc/xdg/tint2/tint2rc
COPY browser.desktop terminal.desktop /usr/share/applications/
COPY wallpaper.svg terminal.svg /usr/share/deskmate/
RUN rsvg-convert -w 64 -h 64 /usr/share/deskmate/terminal.svg -o /usr/share/deskmate/terminal.png
COPY deskd.mjs /opt/desk/deskd.mjs
COPY browser.sh entrypoint.sh monitors.py /opt/desk/
RUN chmod +x /opt/desk/*.sh /opt/desk/monitors.py /etc/xdg/openbox/autostart

USER bot
WORKDIR /home/bot
ENV HOME=/home/bot \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    MONITORS=2 \
    MONITOR_SIZE=1280x800 \
    DISPLAY_NUM=87 \
    CDP_PORT=7802 \
    RUN_DIR=/run/desk

ENTRYPOINT ["/opt/desk/entrypoint.sh"]
