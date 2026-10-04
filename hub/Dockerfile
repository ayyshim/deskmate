# The hub: MCP server, web UI with the live views, and the secretary.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# git reads the repos' history for the hub sweep; ripgrep backs the secretary's Grep tool.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git ripgrep ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# noVNC (MPL-2.0), served at /novnc for the live views: ES modules straight from the release tarball,
# checked against its sha256 so a changed download fails the build. To move to a new release, set both
# arguments; the hash is `curl -sL <url> | sha256sum`.
ARG NOVNC_VERSION=1.6.0
ARG NOVNC_SHA256=5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa
RUN python -c "import sys, urllib.request; open('/tmp/novnc.tar.gz', 'wb').write(urllib.request.urlopen(sys.argv[1], timeout=120).read())" \
        "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
    && echo "${NOVNC_SHA256}  /tmp/novnc.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/novnc.tar.gz -C /opt \
    && mv "/opt/noVNC-${NOVNC_VERSION}" /opt/novnc \
    && rm /tmp/novnc.tar.gz \
    && test -f /opt/novnc/core/rfb.js

# git refuses to read repos owned by another uid unless told they are safe; they are mounted read-only.
RUN git config --system --add safe.directory '*'

# Not installed: only the test run installs it (see the file).
COPY requirements-test.txt .
COPY app ./app
CMD ["python", "-m", "app.main"]
