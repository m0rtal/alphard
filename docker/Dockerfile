FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (UID 1000) for bot execution
# Phase 0 stub doesn't enforce USER yet, but Phase 1+ MUST use:
#   USER 1000:1000
# SECURITY: running as root exposes host if container escapes. For a bot
# trading real money, non-root is a hard requirement (SECURITY.md P0 #3).

# Python deps (Phase 0: requirements.txt; Phase 1+ will use poetry)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Bot code
COPY src/ ./src/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Phase 0: healthcheck отключён (entrypoint — stub, не поднимает HTTP)
# TODO Phase 1: реальный health endpoint через FastAPI

ENTRYPOINT ["/entrypoint.sh"]