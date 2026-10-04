#!/usr/bin/env python3
"""Split the desk's screen into side-by-side monitors.

Xvnc creates a real RandR output per screen when a VNC client asks for a multi-screen layout
(the ExtendedDesktopSize extension). Real outputs matter: Chromium's getScreenDetails() reads
outputs, not RandR 1.5 "monitors", so `xrandr --setmonitor` on Xvfb is not enough for apps such as
edm_react that put their second window on a second screen.

    monitors.py <vnc unix socket> <monitor width> <monitor height> <count>
"""

import socket
import struct
import sys
import time

path, mon_w, mon_h, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])

sock = socket.socket(socket.AF_UNIX)
sock.connect(path)
sock.settimeout(5)


def recv(n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("Xvnc closed the connection")
        buf += chunk
    return buf


# RFB 3.8 handshake with security type None (the socket is the boundary).
recv(12)
sock.sendall(b"RFB 003.008\n")
types = recv(recv(1)[0])
if 1 not in types:
    sys.exit(f"Xvnc offers no 'None' security type: {list(types)}")
sock.sendall(b"\x01")
if struct.unpack(">I", recv(4))[0] != 0:
    sys.exit("Xvnc refused the connection")
sock.sendall(b"\x01")  # ClientInit: shared
recv(20)
recv(struct.unpack(">I", recv(4))[0])

# SetEncodings: only ExtendedDesktopSize (-308), then SetDesktopSize with one screen per monitor.
sock.sendall(struct.pack(">BBHi", 2, 0, 1, -308))
msg = struct.pack(">BBHHBB", 251, 0, mon_w * count, mon_h, count, 0)
for i in range(count):
    msg += struct.pack(">IHHHHI", i + 1, i * mon_w, 0, mon_w, mon_h, 0)
sock.sendall(msg)
sock.sendall(struct.pack(">BBHHHH", 3, 1, 0, 0, 1, 1))

# Wait for the server's answer to our request (reason 1 = "requested by this client").
deadline = time.time() + 5
while time.time() < deadline:
    if recv(1)[0] != 0:
        continue
    recv(1)
    for _ in range(struct.unpack(">H", recv(2))[0]):
        reason, status, _w, _h, enc = struct.unpack(">HHHHi", recv(12))
        if enc != -308:
            sys.exit(0)  # a pixel update means the layout already took; good enough
        screens = recv(4)[0]
        recv(16 * screens)
        if reason == 1:
            if status != 0:
                sys.exit(f"Xvnc refused the layout (status {status})")
            print(f"desk: {screens} monitors of {mon_w}x{mon_h}")
            sys.exit(0)
sys.exit("Xvnc did not answer the layout request")
