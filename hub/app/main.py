"""Run the hub on 127.0.0.1 only."""

import logging
import re

import uvicorn

from . import config


class _HideToken(logging.Filter):
    """The sign-in link carries the token (/login?t=…); access logs must never show it."""

    _pattern = re.compile(r"([?&]t=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._pattern.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_HideToken())
    uvicorn.run("app.web:app", host="127.0.0.1", port=config.PORT, log_level="info", proxy_headers=False)
