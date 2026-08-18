FROM python:3.11-slim

WORKDIR /app

# Pre-create /run/secrets so bind-mount of an env file at
# /run/secrets/alphard.env works on any Docker version (some create
# the parent as a directory when it does not exist).
RUN mkdir -p /run/secrets && chmod 755 /run/secrets && mkdir -p /var/run

# System dependencies. ca-certificates is REQUIRED for HTTPS to
# invest-public-api.tinkoff.ru — without it, all Tinkoff API calls
# fail with CERTIFICATE_VERIFY_FAILED (self-signed in the chain)
# and backfill silently falls through to no-data. (Phase 1.6 audit
# 2026-08-18 — root cause of 268 tickers returning 0 bars.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

# Create non-root user (UID 1000) — SECURITY.md P0 #3
# Bot trading real money MUST NOT run as root; reduces attack surface
# if container escapes.
RUN groupadd -r -g 1000 alphard && \
    useradd -r -u 1000 -g alphard -d /app -s /bin/bash alphard

# Python deps (Phase 0: requirements.txt; Phase 1+ will use poetry)
COPY requirements.txt ./
RUN pip install --no-cache-dir t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple

# Install Russian Trusted Root CA into the system trust store so the
# container can verify invest-public-api.tinkoff.ru TLS handshake
# (their chain is rooted at the Russian Ministry of Digital
# Development's CA, not in the default certifi bundle). Without
# this, EVERY HTTPS call to Tinkoff fails with
# CERTIFICATE_VERIFY_FAILED — silently masking as a 180s timeout
# that drives backfill to no-data on every ticker.
# (Phase 1.6 audit, 2026-08-18.)
RUN T_TECH_PATH=$(pip show t-tech-investments 2>/dev/null | awk '/^Location:/{print $2}') && \
    if [ -n "$T_TECH_PATH" ] && [ -f "$T_TECH_PATH/t_tech/invest/certs/RussianTrustedRootCA.pem" ]; then \
        cp "$T_TECH_PATH/t_tech/invest/certs/RussianTrustedRootCA.pem" \
           /usr/local/share/ca-certificates/tbank-root-ca.crt && \
        update-ca-certificates && \
        echo "OK: Russian Trusted Root CA installed"; \
    else \
        echo "WARN: t-tech-investments cert not found, tinkoff.ru TLS will fail"; \
    fi

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