# The hosted, multi-user DocProof web build. The desktop .app is built with
# PyInstaller (see DocProof.spec) and has nothing to do with this image.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first, off the packaging metadata, so a code-only change
# doesn't reinstall the world on every deploy.
COPY pyproject.toml ./
COPY docproof/__init__.py ./docproof/__init__.py
RUN pip install --no-cache-dir ".[app]" || true

# Now the source, and a real install so the console scripts exist.
COPY . .
RUN pip install --no-cache-dir ".[app]"

# Accounts, jobs and settings live on a mounted volume, not in the image, so a
# redeploy never wipes them. The server reads DOCPROOF_HOME for all of it.
ENV DOCPROOF_HOME=/data/docproof
ENV PORT=8000
EXPOSE 8000

# Binds 0.0.0.0, gate on. Session secret and API key come from the environment
# (set them as secrets on the host) — the server refuses to boot without them.
CMD ["docproof-serve"]
