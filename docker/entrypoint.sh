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
if [ "${DISABLE_BACKFILL:-false}" != "true" ]; then
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
