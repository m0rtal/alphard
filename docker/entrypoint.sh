#!/bin/sh
# Alphard bot entrypoint

set -e

# S-H5: support long-token env injection via bind-mounted file.
# Portainer StackUpdate Env-parameter has a 60-char Go-unmarshal limit,
# so we cannot pass Tinkoff tokens (64+ chars) inline. The /root/.env file
# is bind-mounted at /run/secrets/alphard.env (see docker-compose stack).
# S-H5: source long-token env from file. Order: ENV_FILE override,
# /run/secrets/alphard.env (compose secrets), /tmp/alphard.env
# (manual cp fallback for Docker 29.x bind-mount bug that creates
# a directory at the leaf when /run/secrets/ doesn't pre-exist).
for ENV_FILE_CANDIDATE in     "${ENV_FILE:-}"     "/run/secrets/alphard.env"     "/run/secrets/alphard_env"     "/tmp/alphard.env"; do
    if [ -n "${ENV_FILE_CANDIDATE}" ] && [ -f "${ENV_FILE_CANDIDATE}" ]; then
        set -a
        . "${ENV_FILE_CANDIDATE}"
        set +a
        break
    fi
done

echo "Starting Alphard..."
# S-H5: do NOT echo $ENV value into docker logs (it may carry secrets in
# misconfigured deployments). Print only the name of the active profile.
ENV_PROFILE="${ENV:-production}"
case "$ENV_PROFILE" in
    production|development|staging|test) ;;
    *) ENV_PROFILE="unknown" ;;
esac
echo "ENV profile: $ENV_PROFILE"
echo "Python: $(python --version)"

# Phase 0 sanity gate: refuse to start without explicit override if neither
# sandbox nor real token is set AND ALLOW_NO_BROKER isn't set. This prevents
# a future Phase 1+ bug from silently running "the heartbeat" with real
# broker credentials in production.
if [ -z "${TINKOFF_SANDBOX_TOKEN:-}" ] && [ -z "${TINKOFF_REAL_TOKEN:-}" ]; then
    if [ "${ALLOW_NO_BROKER:-false}" != "true" ]; then
        echo "ERROR: Neither TINKOFF_SANDBOX_TOKEN nor TINKOFF_REAL_TOKEN is set." >&2
        echo "Set TINKOFF_SANDBOX_TOKEN (recommended) or TINKOFF_REAL_TOKEN in your .env." >&2
        echo "Or set ALLOW_NO_BROKER=true for Phase 0 stub mode only." >&2
        exit 1
    fi
    echo "WARNING: ALLOW_NO_BROKER=true — running Phase 0 stub without broker connectivity."
fi

# S-H6: launch backfill_history_md.py as a supervisor-managed background
# service within this container. Backfill must run inside the same process
# tree as the broker connection so it can read TINKOFF_* from the env we
# sourced above. A previous run kept backfill outside the entrypoint tree
# (manual docker exec), which meant env vars were never inherited and
# backfill failed silently with "no token" errors. Running it here, as a
# background service, makes backfill automatic on every container start.
#
# The backfill script is idempotent (skip-complete on every restart) so
# restarting the container only resumes from where it left off.
#
# BUGFIX (H-9): before launching backfill we do a real auth probe against
# the postgres container. pg_isready in the compose healthcheck does NOT
# verify our credentials — only that the socket is open. Without this
# smoke test, a stale scram hash in pg_authid (volume-preserved across
# redeploys while POSTGRES_PASSWORD env was rotated) would let the bot
# start, then backfill would silently log "no data in window" for every
# ticker without writing anything. We hit this in production 2026-08-18.
#
# We also enforce a stable DSN password by hashing the current password
# to a fingerprint file on first successful probe; on subsequent restarts,
# if the fingerprint diverges from the live password, the operator is
# warned to re-init the postgres volume (or run ALTER USER) before the
# stack can safely pass writes.
if [ "${DISABLE_BACKFILL:-false}" != "true" ]; then
    echo "Auth-probing postgres before launching backfill..."

    # Wait up to 60s for the database to be reachable on TCP. We do this
    # before auth_probe because psycopg.OperationalError("connection
    # refused") vs auth drift look the same to the bot.
    for i in $(seq 1 30); do
        if python -c "import socket,sys; s=socket.socket(); s.settimeout(1); s.connect(('alphard-postgres', 5432)); s.close(); sys.exit(0)" 2>/dev/null; then
            break
        fi
        sleep 2
    done

    # Detect password drift: if a fingerprint exists from a prior run
    # and the current DSN password differs, alert loudly. The fingerprint
    # is computed by scripts/check_db_password.py (not yet in repo) or
    # simply captured here as a sha256 of the password component.
    if [ -n "${ALPHARD_PG_DSN:-}" ]; then
        DSN_PW=$(echo "$ALPHARD_PG_DSN" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')
        if [ -n "$DSN_PW" ]; then
            NEW_FP=$(printf '%s' "$DSN_PW" | sha256sum | cut -d' ' -f1)
            OLD_FP_FILE=/tmp/alphard-dsn-fp
            if [ -f "$OLD_FP_FILE" ]; then
                OLD_FP=$(cat "$OLD_FP_FILE")
                if [ "$NEW_FP" != "$OLD_FP" ]; then
                    echo "WARNING: ALPHARD_PG_DSN password changed since last boot." >&2
                    echo "  Old fingerprint: $OLD_FP" >&2
                    echo "  New fingerprint: $NEW_FP" >&2
                    echo "  If postgres volume was preserved across this rotation, run:" >&2
                    echo "  ALTER USER alphard PASSWORD '$(echo "$DSN_PW" | sed "s/'/''/g")' in the postgres container, OR wipe the volume to re-init." >&2
                fi
            fi
            printf '%s' "$NEW_FP" > "$OLD_FP_FILE"
        fi
    fi

    # Real auth probe: SELECT 1 + INSERT _auth_probe. Must succeed before
    # we let backfill start — backfill without working writes = hours of
    # wasted compute, AND silent loss of newbars history.
    #
    # Note: `|| true` swallows python's exit-code, then we capture it
    # explicitly via $?. Without `|| true`, `set -e` would abort the
    # script the moment auth_probe() returns False, never printing the
    # failure message. The cron job (check_db_health.py) and the
    # backfill_history_md.py pre-run guard cover the same check — this
    # entrypoint guard is the LAST chance to surface auth drift, so
    # it must be noisy.
    AUTH_RESULT=$(python -c "
import os, sys
sys.path.insert(0, 'src')
from data.pg_store import PostgresDataStore
s = PostgresDataStore()
ok = s.auth_probe(source='entrypoint_smoke')
print('OK' if ok else 'BROKEN')
sys.exit(0 if ok else 1)
" 2>&1) || AUTH_RESULT="$AUTH_RESULT (python exited non-zero)"
    AUTH_EXIT=$?

    if [ $AUTH_EXIT -ne 0 ] || [ "$AUTH_RESULT" != "OK" ]; then
        echo "AUTH PROBE FAILED: backfill would silently write to nowhere." >&2
        echo "Probe error: ${AUTH_RESULT}" >&2
        echo "Aborting container start to prevent silent backfill failure." >&2
        exit 1
    fi
    echo "  postgres auth OK"

    echo "Launching backfill_history_md as background service..."
    BACKFILL_LOG="${BACKFILL_LOG:-/app/logs/backfill_history_md.log}"
    # Run in background; redirect output; use setsid so backfill survives
    # any signal sent to this entrypoint. PID goes to a file so health
    # checks / debug exec can find it.
    setsid python3 scripts/backfill_history_md.py \
        --classes TQBR TQOB TQCB TQTE \
        --limit 3254 \
        --start-year 2018 \
        --min-bars 1300 \
        >>"${BACKFILL_LOG}" 2>&1 &
    BACKFILL_PID=$!
    echo "  backfill PID=${BACKFILL_PID}, log=${BACKFILL_LOG}"
    echo "${BACKFILL_PID}" > /tmp/alphard-backfill.pid
fi

# Health endpoint simple version (Phase 0)
# TODO: replace with FastAPI app in Phase 1
# Пока запускаем main loop (Phase 1+)
exec python -m src.main
