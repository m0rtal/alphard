FROM python:3.11-slim

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Poetry
RUN pip install --no-cache-dir poetry==1.8.2

# Копируем pyproject.toml и poetry.lock (если есть)
COPY pyproject.toml ./
COPY poetry.lock* ./

# Устанавливаем зависимости (без dev в production)
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --only=main --no-root \
    || poetry install --no-interaction --no-root

# Код бота
COPY src/ ./src/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
