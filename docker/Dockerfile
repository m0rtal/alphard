FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

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