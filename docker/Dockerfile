FROM python:3.11-slim

WORKDIR /app

# Pre-create /run/secrets so bind-mount of an env file at
# /run/secrets/alphard.env works on any Docker version (some create
# the parent as a directory when it does not exist).
RUN mkdir -p /run/secrets && chmod 755 /run/secrets && mkdir -p /var/run

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (UID 1000) — SECURITY.md P0 #3
# Bot trading real money MUST NOT run as root; reduces attack surface
# if container escapes.
RUN groupadd -r -g 1000 alphard && \
    useradd -r -u 1000 -g alphard -d /app -s /bin/bash alphard

# Python deps (Phase 0: requirements.txt; Phase 1+ will use poetry)
COPY requirements.txt ./
RUN pip install --no-cache-dir t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple

RUN pip install --no-cache-dir -r requirements.txt

# Bot code (with correct ownership before USER switch)
COPY --chown=alphard:alphard src/ ./src/
COPY --chown=alphard:alphard scripts/ ./scripts/
COPY --chown=alphard:alphard docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && \
    mkdir -p /app/logs && chown -R alphard:alphard /app

# Phase 0: healthcheck отключён (entrypoint — stub, не поднимает HTTP)
# TODO Phase 1: реальный health endpoint через FastAPI

USER alphard
WORKDIR /app

ENTRYPOINT ["/entrypoint.sh"]