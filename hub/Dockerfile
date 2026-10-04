# The hub: MCP server, web UI with the live views, and the secretary.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

# git reads the repos' history for the hub sweep; ripgrep backs the secretary's Grep tool.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git ripgrep ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# noVNC (MPL-2.0), served at /novnc for the live views. ES modules straight from the release.
ARG NOVNC_VERSION=1.6.0
RUN python - <<PY
import io, tarfile, urllib.request
url = "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as t:
    t.extractall("/opt")
PY
RUN mv /opt/noVNC-${NOVNC_VERSION} /opt/novnc && test -f /opt/novnc/core/rfb.js

# git refuses to read repos owned by another uid unless told they are safe; they are mounted read-only.
RUN git config --system --add safe.directory '*'

COPY app ./app
CMD ["python", "-m", "app.main"]
