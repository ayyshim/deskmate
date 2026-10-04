#!/bin/sh
# A new window of the desk's one Chromium: the panel's Browser launcher, the Openbox menu and deskd's
# `application` action all come through here. While browser.sh keeps Chromium running this only hands
# the window over to it. The resolver rules are passed anyway, so a Chromium started from here (the
# second between two runs of browser.sh) follows the same network rules as every other.
exec chromium --user-data-dir="$HOME/.config/deskmate-chromium" --no-sandbox --test-type \
  ${HOST_RULES:+"--host-resolver-rules=$HOST_RULES"} "$@"
