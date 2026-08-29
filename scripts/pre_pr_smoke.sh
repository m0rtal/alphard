#!/usr/bin/env bash
# scripts/pre_pr_smoke.sh — mandatory pre-PR local stack smoke gate.
#
# Contract: run this after writing code, before `gh pr create`. The
# companion hook (scripts/hooks/pre-push, install via
# `git config core.hooksPath scripts/hooks`) refuses `git push` unless
# this script has written a fresh sentinel for the current branch.
#
# Why this exists: CI alone cannot catch defects that only appear when
# the code runs against a real Postgres schema inside the container
# (e.g. a SELECT whose column order disagrees with schema.sql — that
# class of bug passes every mocked unit test). This gate exercises the
# branch's src/ and scripts/ inside a live container.
#
# What it does:
#   1. Brings the stack up with src/ + scripts/ bind-mounted, so the
#      branch code runs instead of the baked image.
#   2. Polls alphard-bot /health for up to 180 s.
#   3. Runs the full pytest suite.
#   4. Runs daily_incremental.py --dry-run inside the container.
#   5. Writes a per-branch sentinel, then tears the stack down.
#
# Exit codes: 0 pass | 1 stack unhealthy | 2 pytest failed | 3 dry-run failed

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
REPO_ROOT="$(pwd)"

SENTINEL_TTL_MINUTES=30
HEALTH_ATTEMPTS=60
HEALTH_INTERVAL_SECONDS=3
SMOKE_MAX_TICKERS=3

echo "[pre-pr-smoke] === alphard pre-PR smoke gate ==="
echo "[pre-pr-smoke] branch: $(git rev-parse --abbrev-ref HEAD)"
echo "[pre-pr-smoke] commit: $(git rev-parse --short HEAD)"

# Compose reads .env from the repo root. /root/.env uses `export FOO=bar`
# shell syntax, which compose does not parse, so normalise it if the
# repo-root .env is missing. .env is gitignored.
if [[ ! -f "$REPO_ROOT/.env" ]]; then
    if [[ ! -f /root/.env ]]; then
        echo "[pre-pr-smoke] FAIL: no .env at repo root and no /root/.env to derive it from"
        exit 1
    fi
    echo "[pre-pr-smoke] deriving .env from /root/.env (stripping 'export ' prefixes)"
    sed -E 's/^[[:space:]]*export[[:space:]]+//' /root/.env \
        | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' > "$REPO_ROOT/.env"
fi

OVERRIDE_FILE="/tmp/alphard-pre-pr-smoke-$$.yaml"
COMPOSE=(docker compose -f docker-compose.yaml -f "$OVERRIDE_FILE")

cleanup() {
    "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
    rm -f "$OVERRIDE_FILE"
}
trap cleanup EXIT

# Bind-mount the branch tree so the container runs THIS code. Paths are
# repo-relative: hardcoding an absolute checkout path breaks every
# worktree and every other machine.
cat > "$OVERRIDE_FILE" <<'YAML'
services:
  alphard-bot:
    volumes:
      - ./src:/app/src:ro
      - ./scripts:/app/scripts:ro
YAML

echo "[pre-pr-smoke] [1/4] bringing up stack..."
if ! "${COMPOSE[@]}" up -d postgres pg-init alphard-bot >/dev/null 2>&1; then
    echo "[pre-pr-smoke] FAIL: docker compose up failed"
    exit 1
fi

# Port 8765 is NOT published on the host (it binds only inside
# alphard-net), so `curl localhost:8765` always refuses. Probe from
# inside the container instead.
echo "[pre-pr-smoke] [2/4] waiting for alphard-bot healthy..."
healthy=0
for ((i = 1; i <= HEALTH_ATTEMPTS; i++)); do
    if docker exec alphard-bot python3 -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        echo "[pre-pr-smoke]   healthy after $((i * HEALTH_INTERVAL_SECONDS))s"
        healthy=1
        break
    fi
    sleep "$HEALTH_INTERVAL_SECONDS"
done

if [[ $healthy -ne 1 ]]; then
    echo "[pre-pr-smoke] FAIL: alphard-bot not healthy in $((HEALTH_ATTEMPTS * HEALTH_INTERVAL_SECONDS))s"
    docker logs --tail 30 alphard-bot 2>&1 | sed 's/^/  /'
    exit 1
fi

echo "[pre-pr-smoke] [3/4] running pytest..."
if ! python3 -m pytest tests/ --no-cov -p no:cacheprovider -q 2>&1 | tail -15; then
    echo "[pre-pr-smoke] FAIL: pytest failed"
    exit 2
fi

# Resolve the DSN the same way the container does, then exercise the
# real script against the real schema. This is the step that catches
# schema/column-order defects invisible to mocked tests.
echo "[pre-pr-smoke] [4/4] running daily_incremental --dry-run in container..."
PG_IP="$(docker inspect alphard-postgres --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')"
PG_USER="$(grep -E '^POSTGRES_USER=' "$REPO_ROOT/.env" | cut -d= -f2-)"
PG_DB="$(grep -E '^POSTGRES_DB=' "$REPO_ROOT/.env" | cut -d= -f2-)"
PG_PW="$(grep -E '^POSTGRES_PASSWORD=' "$REPO_ROOT/.env" | cut -d= -f2-)"
SMOKE_DSN="postgresql://${PG_USER}:${PG_PW}@${PG_IP}:5432/${PG_DB}"

if ! docker exec alphard-bot bash -c \
    "cd /app && ALPHARD_PG_DSN='${SMOKE_DSN}' python3 scripts/daily_incremental.py --dry-run --max-tickers ${SMOKE_MAX_TICKERS}" 2>&1 | tail -10; then
    echo "[pre-pr-smoke] FAIL: daily_incremental --dry-run failed"
    exit 3
fi

# Sanitize the branch name: 'fix/331-x' would otherwise be read as a
# subdirectory under /tmp that does not exist.
BRANCH="$(git rev-parse --abbrev-ref HEAD | tr '/' '-')"
SENTINEL="/tmp/.alphard-pr-smoke-pass.${BRANCH}"
touch "$SENTINEL"

echo "[pre-pr-smoke] === PASS ==="
echo "[pre-pr-smoke] sentinel: $SENTINEL (TTL ${SENTINEL_TTL_MINUTES} min)"
echo "[pre-pr-smoke] safe to: git push && gh pr create"
