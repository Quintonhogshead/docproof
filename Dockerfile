# The hosted, multi-user DocProof web build. The desktop .app is built with
# PyInstaller (see DocProof.spec) and has nothing to do with this image.
FROM python:3.12-slim

WORKDIR /app

# The optional LanguageTool mechanical-floor pass runs as a local Java server, so
# the image carries a headless JRE and installs the [languagetool] extra. This
# only makes the image CAPABLE of running the pass — it stays off by default
# (languagetool.enabled: false). The ~260 MB LanguageTool jar is NOT baked in; it
# downloads on first use to LTP_JAR_DIR_PATH, which fly.toml points at the mounted
# /data volume so it downloads once and survives redeploys. Before enabling the
# pass, bump the machine memory (see fly.toml) — the JVM needs its own heap.
RUN apt-get update \
    && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first, off the packaging metadata, so a code-only change
# doesn't reinstall the world on every deploy.
COPY pyproject.toml ./
COPY docproof/__init__.py ./docproof/__init__.py
RUN pip install --no-cache-dir ".[app,languagetool]" || true

# Now the source, and a real install so the console scripts exist.
COPY . .
RUN pip install --no-cache-dir ".[app,languagetool]"

# Accounts, jobs and settings live on a mounted volume, not in the image, so a
# redeploy never wipes them. The server reads DOCPROOF_HOME for all of it.
ENV DOCPROOF_HOME=/data/docproof
ENV PORT=8000
EXPOSE 8000

# Binds 0.0.0.0, gate on. Session secret and API key come from the environment
# (set them as secrets on the host) — the server refuses to boot without them.
CMD ["docproof-serve"]
