#!/bin/sh
# Alphard bot entrypoint

set -e

echo "Starting Alphard..."
echo "ENV: ${ENV:-production}"
echo "Python: $(python --version)"

# Health endpoint (TODO: добавить когда будет FastAPI)
# exec python -m uvicorn src.api:app --host 0.0.0.0 --port 8080

# Пока запускаем main loop (Phase 1+)
exec python -m src.main
