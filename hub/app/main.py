"""Start the hub.

It listens on HUB_BIND: 127.0.0.1 in host mode, where it shares this machine's network; 0.0.0.0 in
the bridge modes, inside its own network namespace, where Docker publishes only 127.0.0.1:<port> of
this machine. Either way it answers only requests addressed to 127.0.0.1, localhost or [::1] with its
port (auth.TrustedHosts).
"""

import logging
import os
import re

import uvicorn

from . import config

log = logging.getLogger("app.main")


class _HideToken(logging.Filter):
    """The sign-in link carries the token (/login?t=…); access logs must never show it."""

    _pattern = re.compile(r"([?&]t=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._pattern.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


def _private(path, mode: int) -> None:
    """Tighten a folder an older hub left 0755. Best effort: some file systems ignore modes."""
    try:
        if path.is_dir() and (path.stat().st_mode & 0o777) != mode:
            path.chmod(mode)
    except OSError:
        pass


def prepare() -> None:
    """What the hub writes (the activity journal, files from the desk, the secretary's state) is for the
    user alone: files 0600 and folders 0700, whatever umask the image or Docker gave the process."""
    os.umask(0o077)
    for folder in (config.DATA, config.DATA / "home", config.EXCHANGE_DIR):
        _private(folder, 0o700)


if __name__ == "__main__":
    prepare()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_HideToken())
    log.info("Deskmate at %s (network mode %s, listening on %s)", config.HUB_URL, config.NETWORK, config.BIND)
    uvicorn.run("app.web:app", host=config.BIND, port=config.PORT, log_level="info", proxy_headers=False)
