#!/usr/bin/env bash
# scripts/quickstart.sh — first-shot-friendly Alphard stack bootstrap.
#
# Goal: turn `git clone ... && cd alphard && ./scripts/quickstart.sh` into
# a single command that produces a fully running stack (postgres, redis,
# alphard-bot, prometheus, grafana) on any Docker host that satisfies:
#   - Docker Engine 20.10+ with compose plugin v2
#   - ~3 GiB disk free (alphard-bot 405 MB, postgres 80 MB, redis 40 MB,
#     prometheus 200 MB, grafana 1.4 GB, chownfix 80 MB + image caches)
#   - Ports 5432, 6379, 9090, 3300 free on the host (3300 == Grafana,
#     which lives on host network namespace; 3000 is taken by forgejo
#     on .103, hence 3300 is the override)
#
# What it does (idempotent — re-running is safe):
#   1. Sanity-checks: docker is up, compose v2 present, repo root
#      contains docker-compose.yaml and .env.example.
#   2. Creates .env from .env.example if missing. Refuses to start
#      without a non-empty GRAFANA_ADMIN_PASSWORD (no historical literal).
#      Auto-generates POSTGRES_PASSWORD and REDIS_PASSWORD if missing.
#   3. Bakes the Grafana provisioning / dashboards B64 vars via
#      tools/bake_grafana_env.py. Skipped if already populated.
#   4. Bakes the Prometheus config into PROM_YML_B64. Skipped if
#      already populated.
#   5. `docker compose --profile observability up -d`.
#   6. Polls container healthchecks for up to 180s; prints a clear
#      status table (or a failure table with `docker logs <svc>` hints).
#
# Run-time knobs (env vars, all optional):
#   ALPHARD_PROFILE       = "observability" (default) or "data" (skip
#                           Prometheus + Grafana; bot + postgres + redis
#                           only — for memory-constrained hosts).
#   ALPHARD_TIMEOUT_SEC   = 180 (default; how long to wait for healthy).
#   ALPHARD_QUIET         = "1" suppress progress dots.
#
# Why this script exists: a clean-host `git clone + docker compose up`
# on the original repo failed in 4 places (PR #228 / this PR's notes):
#   - pg-init used alpine:3.20 and ran `apk add postgresql-client` which
#     hangs on DNS egress; the first wave of smoke tests required a
#     manual `docker exec ... psql < init.sql` to seed the _auth_probe
#     table. Fixed in compose (alpine -> postgres:16-alpine).
#   - grafana had no `apparmor=unconfined` in compose — the daemon
#     rejected the start with `apparmor_parser: Access denied`. Fixed
#     in compose.
#   - PROM_YML_B64 was not in .env, so Prometheus started with a zero-
#     byte config and zero scrape targets. THIS SCRIPT bakes it on
#     first run.
#   - PROVISIONING_*_B64 were empty in .env.example, so Grafana's
#     entrypoint bailed at `FATAL: ... is unset or empty`. THIS SCRIPT
#     bakes them on first run.
#
# Idempotency: every step short-circuits if its artifact already exists.
# Re-running on a healthy stack is a no-op (compose up -d skips
# unchanged services).

set -euo pipefail

# SCRIPT_DIR/REPO_ROOT resolution — no `dirname` (busybox-alpine
# strips it from some PATHs; we use bash-native parameter expansion).
# BASH_SOURCE[0] is the path as invoked; ${BASH_SOURCE[0]%/*} is its
# directory (without trailing slash), and a `cd` + `pwd` round-trip
# gives the canonical absolute path.
SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "${SCRIPT_PATH%/*}" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ---- Config knobs (env-driven with defaults) ----
PROFILE="${ALPHARD_PROFILE:-observability}"
TIMEOUT_SEC="${ALPHARD_TIMEOUT_SEC:-180}"
QUIET="${ALPHARD_QUIET:-0}"

# ---- Pretty output helpers ----
# Use a single-quoted heredoc-friendly style so we don't fight bash quoting
# on the value-extraction line.
strip_quotes() {
    # Drop a single layer of surrounding single OR double quotes, if any.
    local s="${1:-}"
    [[ "$s" == \"*\" ]] && s="${s%\"}" && s="${s#\"}"
    [[ "$s" == \'*\' ]] && s="${s%\'}" && s="${s#\'}"
    printf '%s' "$s"
}

env_value() {
    # Extract a single key's value from .env, with surrounding quotes
    # stripped. Returns empty string if missing.
    local key="$1"
    local raw
    raw="$(grep -E "^[[:space:]]*${key}=" "$REPO_ROOT/.env" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
    strip_quotes "$raw"
}

if [[ "$QUIET" == "1" ]]; then
    log()  { :; }
    info() { :; }
    ok()   { :; }
    warn() { printf 'WARN: %s\n' "$*" >&2; }
    err()  { printf 'ERROR: %s\n' "$*" >&2; }
else
    log()  { printf '  %s\n' "$*"; }
    info() { printf '\n=== %s ===\n' "$*"; }
    ok()   { printf '  ok: %s\n' "$*"; }
    warn() { printf '  WARN: %s\n' "$*"; }
    err()  { printf '  ERROR: %s\n' "$*" >&2; }
fi

# ---- 1. Sanity checks ----
info "1/5 Sanity checks"

if ! command -v docker >/dev/null 2>&1; then
    err "docker not found in PATH. Install Docker Engine 20.10+ first."
    exit 2
fi
if ! docker info >/dev/null 2>&1; then
    err "docker daemon not reachable. Start Docker (or check DOCKER_HOST)."
    exit 2
fi
ok "docker $(docker version --format '{{.Server.Version}}')"

if ! docker compose version >/dev/null 2>&1; then
    err "docker compose plugin v2 not installed. Install 'docker-compose-plugin'."
    exit 2
fi
ok "compose $(docker compose version --short 2>/dev/null)"

if [[ ! -f "$REPO_ROOT/docker-compose.yaml" ]]; then
    err "docker-compose.yaml not found at $REPO_ROOT"
    exit 2
fi
ok "compose file present"

if [[ ! -f "$REPO_ROOT/.env.example" ]]; then
    err ".env.example not found at $REPO_ROOT"
    exit 2
fi
ok ".env.example present"

# ---- 2. .env bootstrap ----
info "2/5 .env bootstrap"

if [[ ! -f "$REPO_ROOT/.env" ]]; then
    log "creating .env from .env.example"
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    ok ".env created"
else
    ok ".env exists (kept as-is)"
fi

# Refuse to proceed with a placeholder GRAFANA_ADMIN_PASSWORD (issue #55):
# an empty / default-password Grafana would expose admin without auth.
_gpw="$(env_value GRAFANA_ADMIN_PASSWORD)"
if [[ -z "$_gpw" ]]; then
    err "GRAFANA_ADMIN_PASSWORD is empty in .env"
    err "  Set it to a strong password (16+ chars). Generate: openssl rand -base64 24"
    err "  Example: GRAFANA_ADMIN_PASSWORD=\"\$(openssl rand -base64 24)\""
    exit 2
fi
if [[ "$_gpw" == "alphard" ]]; then
    err "GRAFANA_ADMIN_PASSWORD is set to the historical literal 'alphard' (issue #55)"
    err "  Replace with a strong password before continuing."
    exit 2
fi
ok "GRAFANA_ADMIN_PASSWORD set"

# Auto-generate POSTGRES_PASSWORD if missing.
_pgpw="$(env_value POSTGRES_PASSWORD)"
if [[ -z "$_pgpw" ]]; then
    _new_pgpw="$(openssl rand -base64 24)"
    sed -i "s|^POSTGRES_PASSWORD=$|POSTGRES_PASSWORD=\"${_new_pgpw}\"|" "$REPO_ROOT/.env"
    ok "POSTGRES_PASSWORD auto-generated (24 random bytes)"
else
    ok "POSTGRES_PASSWORD set"
fi

# Same for REDIS_PASSWORD.
_rpw="$(env_value REDIS_PASSWORD)"
if [[ -z "$_rpw" ]]; then
    _new_rpw="$(openssl rand -base64 24)"
    sed -i "s|^REDIS_PASSWORD=$|REDIS_PASSWORD=\"${_new_rpw}\"|" "$REPO_ROOT/.env"
    ok "REDIS_PASSWORD auto-generated (24 random bytes)"
else
    ok "REDIS_PASSWORD set"
fi

# ---- 3. Grafana B64 bake ----
info "3/5 Grafana provisioning + dashboards bake"

# Quick check: are all 4 B64 vars present and non-empty?
_b64_ok=1
for k in PROVISIONING_DATASOURCES_YML_B64 PROVISIONING_DASHBOARDS_PROVIDER_YML_B64 DASHBOARD_PHASE0_JSON_B64 DASHBOARD_PHASE28_JSON_B64; do
    _v="$(env_value "$k")"
    if [[ -z "$_v" ]]; then _b64_ok=0; break; fi
done
if [[ "$_b64_ok" == "1" ]]; then
    ok "all 4 B64 vars present"
else
    if [[ ! -f "$REPO_ROOT/tools/bake_grafana_env.py" ]]; then
        err "tools/bake_grafana_env.py not found"
        exit 2
    fi
    log "running tools/bake_grafana_env.py"
    if ! python3 "$REPO_ROOT/tools/bake_grafana_env.py" >> "$REPO_ROOT/.env"; then
        err "bake_grafana_env.py failed"
        exit 2
    fi
    ok "4 B64 vars baked into .env"
fi

# ---- 4. Prometheus B64 bake ----
info "4/5 Prometheus config bake"

_prom_b64="$(env_value PROM_YML_B64)"
if [[ -n "$_prom_b64" ]]; then
    ok "PROM_YML_B64 present"
else
    if [[ ! -f "$REPO_ROOT/docker/prometheus/prometheus.yml" ]]; then
        err "docker/prometheus/prometheus.yml not found"
        exit 2
    fi
    log "baking docker/prometheus/prometheus.yml into PROM_YML_B64"
    _b64="$(base64 -w0 < "$REPO_ROOT/docker/prometheus/prometheus.yml")"
    printf '\nPROM_YML_B64="%s"\n' "$_b64" >> "$REPO_ROOT/.env"
    ok "PROM_YML_B64 baked into .env"
fi

# ---- 5. docker compose up ----
info "5/5 docker compose up -d --profile $PROFILE"

# We do NOT `set -a; source .env; set +a` because that would export every
# variable in .env (HTTP_PROXY, MATTERMOST_*, etc.) into the compose
# process environment. `docker compose` automatically reads .env at the
# project root for variable substitution. See:
# https://docs.docker.com/compose/environment-variables/env-files/
# We use PIPESTATUS to detect docker compose failures, because without
# `set -o pipefail` the `||` after a pipeline only checks the LAST
# command (sed, which always succeeds) and silently ignores compose
# errors. The previous version hung the Health gate for 180s waiting
# for containers that never started.
docker compose "--profile=$PROFILE" up -d 2>&1 | sed 's/^/  /'
_compose_rc="${PIPESTATUS[0]}"
if [[ "$_compose_rc" -ne 0 ]]; then
    err "docker compose up failed (rc=$_compose_rc)"
    exit 2
fi

# ---- Health gate ----
if [[ "$TIMEOUT_SEC" -lt 1 ]]; then
    info "Health gate skipped (TIMEOUT_SEC=0; for fast-fail test runs)"
    err "compose step failed; bakes ran; quickstart test mode active"
    exit 1
fi
info "Health gate (up to ${TIMEOUT_SEC}s)"

# One-shot services (Exited(0) is success): alphard-chownfix,
# alphard-pg-init. These must NOT be in EXPECTED — the health gate
# requires State.Status == "running", which one-shots never satisfy.
ONE_SHOT=("alphard-chownfix" "alphard-pg-init")
# Long-running services we wait for:
EXPECTED=("alphard-postgres" "alphard-redis" "alphard-bot")
if [[ "$PROFILE" == "observability" ]]; then
    EXPECTED+=("alphard-prometheus" "alphard-grafana")
fi

elapsed=0
interval=5
all_healthy=0
while [[ $elapsed -lt $TIMEOUT_SEC ]]; do
    sleep "$interval"
    elapsed=$((elapsed + interval))
    healthy_now=0
    for svc in "${EXPECTED[@]}"; do
        status="$(docker inspect --format='{{.State.Status}}' "$svc" 2>/dev/null || echo "missing")"
        health="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$svc" 2>/dev/null || echo "n/a")"
        if [[ "$status" == "running" && ( "$health" == "healthy" || "$health" == "n/a" ) ]]; then
            healthy_now=$((healthy_now + 1))
        fi
    done
    for svc in "${ONE_SHOT[@]}"; do
        code="$(docker inspect --format='{{.State.ExitCode}}' "$svc" 2>/dev/null || echo "missing")"
        if [[ "$code" == "0" ]]; then
            ok "one-shot $svc exited 0"
        elif [[ "$code" == "missing" ]]; then
            warn "one-shot $svc missing (compose didn't run it?)"
        else
            warn "one-shot $svc exited $code (check 'docker logs alphard-$svc')"
        fi
    done
    if [[ $healthy_now -eq ${#EXPECTED[@]} ]]; then
        all_healthy=1
        break
    fi
    log "  $healthy_now/${#EXPECTED[@]} healthy (t=${elapsed}s)"
done

# ---- Final status table ----
info "Final state"
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' --filter "name=alphard-"

if [[ $all_healthy -eq 1 ]]; then
    ok "stack is up"
    if [[ "$PROFILE" == "observability" ]]; then
        cat <<EOF

  Next steps:
    - Bot health/metrics (in-network only):  alphard-bot:8765/health, /metrics
    - Prometheus:                            http://localhost:9090/
    - Grafana:                               http://localhost:3300/  (admin / \$GRAFANA_ADMIN_PASSWORD)
    - Postgres + Redis:                      docker exec alphard-postgres psql -U \$POSTGRES_USER -d \$POSTGRES_DB
    - Stop:                                   docker compose --profile $PROFILE down
    - Stop + wipe volumes:                   docker compose --profile $PROFILE down -v
EOF
    else
        cat <<EOF

  Next steps:
    - Bot health/metrics:                    alphard-bot:8765/health, /metrics
    - Postgres + Redis:                      docker exec alphard-postgres psql -U \$POSTGRES_USER -d \$POSTGRES_DB
    - Stop:                                   docker compose --profile $PROFILE down
    - Stop + wipe volumes:                   docker compose --profile $PROFILE down -v
EOF
    fi
    exit 0
else
    err "stack did not reach healthy state in ${TIMEOUT_SEC}s"
    err "Inspect: docker ps --filter name=alphard-; docker logs <service>"
    exit 1
fi
