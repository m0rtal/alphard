#!/usr/bin/env bash
# scripts/pre_pr_smoke.sh - mandatory pre-PR local stack smoke gate.
#
# Contract: run this after writing code, before `gh pr create`. The
# companion hook (scripts/hooks/pre-push, install via
# `git config core.hooksPath scripts/hooks`) refuses `git push` unless
# this script has written a fresh sentinel for the current branch.
#
# Why this exists: CI alone cannot catch defects that only appear when
# the code runs against a real Postgres schema inside the container
# (e.g. a SELECT whose column order disagrees with schema.sql - that
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

# BUGFIX (cycle146, issue #371): REFUSE to run `docker compose down -v` against
# any non-local Docker daemon - not just tcp://. The original cycle145 guard
# (issue #363 follow-up) only matched DOCKER_HOST=tcp://...; an SSH Docker
# context (DOCKER_HOST=ssh://user@host) bypassed it and would still wipe the
# remote volume. Invert to deny-by-default: only DOCKER_HOST=unix://... is
# local, everything else (unset, tcp://, ssh://, fd://, npipe://, future
# schemes) requires explicit ALLOW_NONLOCAL_SMOKE=1.
if [[ -n "${DOCKER_HOST:-}" ]] && [[ ! "${DOCKER_HOST:-}" =~ ^unix:// ]]; then
    if [[ "${ALLOW_NONLOCAL_SMOKE:-0}" != "1" ]]; then
        echo "[pre-pr-smoke] REFUSED: DOCKER_HOST=${DOCKER_HOST} points at a non-local daemon."
        echo "[pre-pr-smoke] Refusing to run 'docker compose down -v' against a remote stack."
        echo "[pre-pr-smoke] Set DOCKER_HOST=unix:///var/run/docker.sock for local smoke,"
        echo "[pre-pr-smoke] or ALLOW_NONLOCAL_SMOKE=1 only if you intentionally want"
        echo "[pre-pr-smoke] to exercise the gate against a remote stack."
        exit 9
    fi
    echo "[pre-pr-smoke] WARNING: DOCKER_HOST=${DOCKER_HOST} (non-local) - proceeding because ALLOW_NONLOCAL_SMOKE=1"
fi

echo "[pre-pr-smoke] === alphard pre-PR smoke gate ==="
echo "[pre-pr-smoke] branch: $(git rev-parse --abbrev-ref HEAD)"
echo "[pre-pr-smoke] commit: $(git rev-parse --short HEAD)"

# BUGFIX (2026-09-02): refresh Russian GOST CA bundle before stack up.
# Tinkoff / MOEX chains can rotate (CA expiries, intermediate replacements).
# Auto-refresh here so the smoke gate catches SSL handshake failures
# *before* push, not after.
if [ -f "$REPO_ROOT/scripts/fetch_tinkoff_gost_ca.py" ]; then
    echo "[pre-pr-smoke] refreshing Russian GOST CA bundle"
    python3 "$REPO_ROOT/scripts/fetch_tinkoff_gost_ca.py" \
        --out "$REPO_ROOT/docker/certs/tinkoff-gost-ca-bundle.pem" || true
fi

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

# BUGFIX (cycle148/149, issue #374 + #379): scope every compose
# invocation to a per-PID project name AND redefine every hardcoded
# `container_name:` in docker-compose.yaml, so the smoke stack gets
# unique per-PID container names (alphard-smoke-<PID>-alphard-bot-1,
# alphard-smoke-<PID>-postgres-1, etc.) and never collides with the
# operator's running alphard-* stack on the same daemon.
#
# Why the override is necessary: docker-compose.yaml hardcodes
# `container_name: alphard-bot`, `alphard-postgres`, `alphard-redis`.
# Docker
# Compose honours these literal names and does NOT prefix them with
# the project name - the `-p alphard-smoke-<PID>` flag scopes
# volumes/networks only, not hardcoded container names. On a host
# where the operator's stack is already running, `compose up` fails at
# step [1/4] with "Conflict. The container name '/alphard-bot' is
# already in use".
#
# The override below redefines each hardcoded container_name with the
# per-PID-scoped name (matching Compose's default project-scoped
# naming convention `<project>-<service>-1`). This is the safe,
# non-destructive fix: no operator containers are touched.
#
# We also re-add `alphard-postgres` as a network alias for the
# postgres service. Without `container_name: alphard-postgres`,
# Compose only adds the service key (`postgres`) as a DNS alias on
# the alphard-net network - the bot's entrypoint hardcodes the
# hostname `alphard-postgres` (docker/entrypoint.sh:106), so the bot
# would fail to resolve postgres. The smoke's alphard-net is a
# separate Docker network (named `alphard-smoke-<PID>_alphard-net`),
# so the alias resolves only inside the smoke stack and never leaks
# into the operator's stack.
#
# `!reset null` would be cleaner but is not supported by Compose v2.40
# on this host - verified via `docker compose ... config` that `!reset`
# is silently ignored. We redefine explicitly instead.
OVERRIDE_FILE="/tmp/alphard-pre-pr-smoke-$$.yaml"
COMPOSE_PROJECT_NAME="alphard-smoke-$$"
COMPOSE=(docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yaml -f "$OVERRIDE_FILE")
# Convenience aliases for docker exec / docker inspect calls. These
# now match Compose's project-scoped naming convention (the override
# below sets container_name to exactly these values), so there is no
# leak from docker-compose.yaml's hardcoded names into the script's
# own docker invocations.
SMOKE_BOT="$COMPOSE_PROJECT_NAME-alphard-bot-1"
SMOKE_PG="$COMPOSE_PROJECT_NAME-postgres-1"
SMOKE_WEB="$COMPOSE_PROJECT_NAME-alphard-web-1"

cleanup() {
    "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
    rm -f "$OVERRIDE_FILE"
}
trap cleanup EXIT

# Bind-mount the branch tree so the container runs THIS code. Paths are
# repo-relative: hardcoding an absolute checkout path breaks every
# worktree and every other machine.
#
# ALSO redefine every hardcoded container_name from docker-compose.yaml
# to its per-PID-scoped equivalent. See the BUGFIX comment above.
cat > "$OVERRIDE_FILE" <<YAML
services:
  alphard-bot:
    container_name: ${COMPOSE_PROJECT_NAME}-alphard-bot-1
    volumes:
      - ./src:/app/src:ro
      - ./scripts:/app/scripts:ro
  alphard-web:
    container_name: ${COMPOSE_PROJECT_NAME}-alphard-web-1
    volumes:
      - ./src:/app/src:ro
  postgres:
    container_name: ${COMPOSE_PROJECT_NAME}-postgres-1
    networks:
      alphard-net:
        aliases:
          - alphard-postgres
  redis:
    container_name: ${COMPOSE_PROJECT_NAME}-redis-1
YAML

echo "[pre-pr-smoke] [1/4] bringing up stack..."
# BUGFIX (issue #347): bring up only postgres + alphard-bot. The previous
# pg-init sidecar was dropped from docker-compose.yaml because its
# single-file bind-mounts render as directories on LXC, so the schema
# never applied and _auth_probe was missing. Schema application is now
# handled by the bot's entrypoint via init_schema() before auth_probe()
# - see tests/test_347_pg_init_removal.py.
#
# alphard-web (issue #393, PR #394) is also brought up so the wire-up
# is exercised in smoke. It bind-mounts ./src:ro via the override above.
if ! "${COMPOSE[@]}" up -d postgres alphard-bot alphard-web >/dev/null 2>&1; then
    echo "[pre-pr-smoke] FAIL: docker compose up failed"
    exit 1
fi

# Port 8765 is NOT published on the host (it binds only inside
# alphard-net), so `curl localhost:8765` always refuses. Probe from
# inside the container instead.
echo "[pre-pr-smoke] [2/4] waiting for alphard-bot healthy..."
healthy=0
for ((i = 1; i <= HEALTH_ATTEMPTS; i++)); do
    if docker exec "$SMOKE_BOT" python3 -c "
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
    docker logs --tail 30 "$SMOKE_BOT" 2>&1 | sed 's/^/  /'
    exit 1
fi

echo "[pre-pr-smoke] [2.5/4] lint+format gate (CI parity: flake8 + black on src/ tests/ scripts/)..."
# BUGFIX (cycle154, issue #389): CI lints scripts/ too; local smoke gate
# only lints src/ tests/, which let the PR #388 style() commit slip
# through and cost an extra round trip. Mirror CI exactly here so a
# flake8/black violation on any script/ file fails the smoke the same
# way CI would - before we waste a PR cycle on it.
if ! python3 -m flake8 src/ tests/ scripts/ \
        --max-line-length=120 --extend-ignore=E203,W503; then
    echo "[pre-pr-smoke] FAIL: flake8 reported issues"
    exit 2
fi
if ! python3 -m black --check src/ tests/ scripts/; then
    echo "[pre-pr-smoke] FAIL: black format check failed"
    exit 2
fi

# alphard-web (issue #393, PR #394): also probe the dashboard service so
# the wire-up is verified in smoke. start_period=30s in compose; we give
# the same grace window. If /api/health 5xx's (DSN missing, server
# crashed), dump logs and fail the gate - there's no point shipping a
# PR that green-lights while the dashboard is dead.
echo "[pre-pr-smoke] [2.5/4] waiting for alphard-web healthy..."
web_healthy=0
for ((i = 1; i <= HEALTH_ATTEMPTS; i++)); do
    if docker exec "$SMOKE_WEB" python3 -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        echo "[pre-pr-smoke]   web healthy after $((i * HEALTH_INTERVAL_SECONDS))s"
        web_healthy=1
        break
    fi
    sleep "$HEALTH_INTERVAL_SECONDS"
done

if [[ $web_healthy -ne 1 ]]; then
    echo "[pre-pr-smoke] FAIL: alphard-web not healthy in $((HEALTH_ATTEMPTS * HEALTH_INTERVAL_SECONDS))s"
    docker logs --tail 30 "$SMOKE_WEB" 2>&1 | sed 's/^/  /'
    exit 1
fi

# Smoke a few representative endpoints so a future wire-up regression
# (e.g. typo in a route name) is caught here, not in production. The
# HTML root, /api/summary, and /api/health are all probed from inside
# the alphard-net. Failures are non-fatal at this gate - we only
# block merge if the server itself is unhealthy. The point is to
# surface the wire-up in `docker logs alphard-web` for the next QA pass.
echo "[pre-pr-smoke] [2.75/4] probing wire-up endpoints..."
for probe_path in /api/summary "/api/tickers?limit=1" /api/backfill /api/settings /api/backups; do
    if ! docker exec "$SMOKE_WEB" python3 -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://127.0.0.1:8080${probe_path}', timeout=3)
    sys.exit(0 if r.status == 200 else 1)
except Exception as e:
    print(f'probe ${probe_path} failed: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1 | sed 's/^/  /'; then
        echo "[pre-pr-smoke] WARN: ${probe_path} not 200 (continuing; see logs above)"
    fi
done
echo "[pre-pr-smoke] [3/4] running pytest..."
if ! python3 -m pytest tests/ --no-cov -p no:cacheprovider -q 2>&1 | tail -15; then
    echo "[pre-pr-smoke] FAIL: pytest failed"
    exit 2
fi

# Resolve the DSN the same way the container does, then exercise the
# real script against the real schema. This is the step that catches
# schema/column-order defects invisible to mocked tests.
echo "[pre-pr-smoke] [4/4] running daily_incremental --dry-run in container..."
PG_IP="$(docker inspect "$SMOKE_PG" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')"
PG_USER="$(grep -E '^POSTGRES_USER=' "$REPO_ROOT/.env" | cut -d= -f2-)"
PG_DB="$(grep -E '^POSTGRES_DB=' "$REPO_ROOT/.env" | cut -d= -f2-)"
PG_PW="$(grep -E '^POSTGRES_PASSWORD=' "$REPO_ROOT/.env" | cut -d= -f2-)"
SMOKE_DSN="postgresql://${PG_USER}:${PG_PW}@${PG_IP}:5432/${PG_DB}"

if ! docker exec "$SMOKE_BOT" bash -c \
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
