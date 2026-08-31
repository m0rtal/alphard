#!/bin/sh
# Alphard bot entrypoint

set -e

# S-H5: support long-token env injection via bind-mounted file.
# Portainer StackUpdate Env-parameter has a 60-char Go-unmarshal limit,
# so we cannot pass Tinkoff tokens (64+ chars) inline. The /root/.env file
# is bind-mounted at /root/.env (see docker-compose stack BUGFIX #122).
# S-H5: source long-token env from file. Order (first existing wins):
#   1. ${ENV_FILE:-}                   — explicit override (highest priority)
#   2. /root/.env                      — bind-mounted compose local-dev path
#   3. /run/secrets/alphard.env        — compose secrets (.107 production)
#   4. /run/secrets/alphard_env        — alternative compose secrets path
#   5. /tmp/alphard.env                — manual fallback for Docker 29.x
#                                        bind-mount bug that creates a
#                                        directory at the leaf when
#                                        /run/secrets/ doesn't pre-exist.
#
# Why /root/.env: the compose file mounts it at /root/.env:ro with the
# "BUGFIX (#122)" comment that says entrypoint.sh should source it.
# Production (.107) hits path #3 first (compose secrets) so adding #2
# is purely additive — local dev bring-ups without an explicit ENV_FILE
# now pick up the bind-mounted file instead of silently running with
# ALPHARD_PG_DSN=None and all universe-coverage gauges stuck at 0.
# Issue #295.
for ENV_FILE_CANDIDATE in     "${ENV_FILE:-}"     "/root/.env"     "/run/secrets/alphard.env"     "/run/secrets/alphard_env"     "/tmp/alphard.env"; do
    if [ -n "${ENV_FILE_CANDIDATE}" ] && [ -f "${ENV_FILE_CANDIDATE}" ]; then
        set -a
        . "${ENV_FILE_CANDIDATE}"
        set +a
        # Issue #298: POSIX `for` loops leave the iteration variable
        # set to its LAST iterated value even when the loop exits
        # without `break`. Downstream code (and tests) cannot tell
        # whether the loop actually sourced a candidate or just iterated
        # past every missing file. Record the chosen path in a separate
        # variable that's only set on a real hit. This also makes
        # `docker logs` show which env file the bot actually sourced,
        # which is useful ops telemetry.
        SOURCED_ENV_FILE="${ENV_FILE_CANDIDATE}"
        export SOURCED_ENV_FILE
        break
    fi
done
# Defensive: clear the iteration variable so accidental later reads
# (or test snippets that echo it) cannot leak the last-tried value
# as if it had been sourced.
unset ENV_FILE_CANDIDATE

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
    # before init_schema because psycopg.OperationalError("connection
    # refused") vs auth drift look the same to the bot.
    for i in $(seq 1 30); do
        if python -c "import socket,sys; s=socket.socket(); s.settimeout(1); s.connect(('alphard-postgres', 5432)); s.close(); sys.exit(0)" 2>/dev/null; then
            break
        fi
        sleep 2
    done

    # BUGFIX (issue #347): init_schema() MUST run BEFORE auth_probe().
    # auth_probe() does INSERT INTO _auth_probe ... ON CONFLICT DO UPDATE,
    # and _auth_probe is created by src/data/schema.sql which is read by
    # init_schema(). On a fresh volume (or any volume where the bot has
    # never run) the table does not exist yet, so the probe fails with
    # UndefinedTable and the entrypoint exits fail-closed.
    #
    # Pre-#347 ordering was: probe → init_schema. That was OK because
    # the pg-init sidecar (since removed in #347) had already created
    # _auth_probe via its docker/postgres/init.sql replay. With pg-init
    # gone, init_schema() is the ONLY path that creates _auth_probe, so
    # it must run first.
    #
    # init_schema() is idempotent (CREATE TABLE IF NOT EXISTS / ADD
    # COLUMN IF NOT EXISTS throughout src/data/schema.sql), so re-running
    # it on every boot is safe and picks up any ADD COLUMN migrations
    # that have landed since the volume was last initialized.
    echo "Applying schema migrations (before auth probe)..."
    python -c "
import sys
# BUGFIX (issue #363): use absolute /app/src instead of relative 'src'.
# When the smoke script bind-mounts ./src → /app/src:ro on top of the
# baked image, the relative path resolved a stale ZFS subvolume view
# that exposed only a few low-layer files (e.g. schema.sql) and
# triggered ModuleNotFoundError: No module named 'data.pg_store' on
# every restart cycle. Absolute path is bind-mount safe and image-layer
# independent. WORKDIR=/app is hardcoded in docker/Dockerfile.
sys.path.insert(0, '/app/src')
from data.pg_store import PostgresDataStore
PostgresDataStore().init_schema()
print('schema OK')
" || { echo "SCHEMA INIT FAILED: $?" >&2; exit 1; }
    echo "  schema OK"

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
# BUGFIX (issue #363): absolute /app/src (see comment in init_schema block).
sys.path.insert(0, '/app/src')
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
    # NOTE (2026-08-20): backfill_history_md.py is now owned by the
    # _backfill_supervisor_loop thread inside src/main.py. The thread
    # spawns, waitpids, and respawns on death — exactly the lifecycle
    # control we needed when the old `setsid ... &` shell pattern left
    # a zombie PID 19 with no reaper and no respawner (the 17-hour
    # "network stall" that wasn't). The shell no longer launches the
    # backfill; main.py does. We just touch the log file so the log
    # stream exists before the supervisor writes to it, and drop a
    # marker so the next operator can tell supervisor-managed vs
    # shell-launched apart at a glance.
    install -d -m 0755 "$(dirname "${BACKFILL_LOG}")"
    # touch — DO NOT truncate. The backfill log is the only forensic record
    # of supervisor-driven child behaviour, and a restart that wipes it
    # defeats the whole point. See issue #49.
    #
    # Issue #120 (Defect 2): PR #119 moved /app/logs to a 100M tmpfs to
    # work around the .107 PVE LXC userns bug, which means the backfill
    # log evaporates on every container restart — the exact moment an
    # operator needs it most. Emit a single banner on every boot to
    # /app/logs/backfill_history_md.log AND stderr so the operator
    # always knows whether a restart wiped prior forensics. The banner
    # is itself inside the log, so even if the log is gone post-restart
    # the stderr line is the only surviving record.
    touch "${BACKFILL_LOG}"
    {
        echo "  backfill: owned by alphard-backfill-supervisor thread in src/main.py (boot $(date -u +%FT%TZ))"
        echo "  backfill log=${BACKFILL_LOG} (supervisor-managed; appended, not truncated)"
        echo "  WARN [issue #120]: /app/logs is tmpfs on this host (PR #119) — container restart wipes prior forensics."
    } >>"${BACKFILL_LOG}"
    # Mirror the banner to stderr so it's visible in `docker logs` even
    # after the in-container log has evaporated.
    echo "WARN [issue #120]: /app/logs is tmpfs on this host (PR #119) — container restart wipes prior forensics." >&2

    # H-NETWORK-DETECT (2026-08-20): wire SIGUSR1 -> faulthandler dump
    # so that if the backfill Python process ever sits idle in a
    # deadlock again (the symptom that surfaced on sha-bc867a2: the
    # process was alive, the Postgres connection was open, but Python
    # was not sending any query), the operator (or a future cron
    # watchdog) can grab a Python stack trace without killing the
    # daemon. The dump is appended to the same backfill log so all
    # forensics live in one file. install() is idempotent and cheap;
    # re-installing it on every container start is the simplest way to
    # guarantee the signal handler is in place regardless of whether
    # backfill_history_md.py itself grows to ignore SIGUSR1.
    #
    # The faulthandler registers in THIS throwaway python subprocess, so
    # the live backfill_history_md.py child needs its own register call
    # (handled at module-import time in scripts/backfill_history_md.py).
    # This entrypoint hook is kept as a defense-in-depth for the main
    # src.main heartbeat loop and any future daemons that don't carry
    # their own registration.
    python3 -c "
import faulthandler, signal, sys
faulthandler.enable()
faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
sys.stdout.write('faulthandler SIGUSR1 enabled (entrypoint shim)\n')
sys.stdout.flush()
" >>"${BACKFILL_LOG}" 2>&1 || echo 'faulthandler init failed (non-fatal)'
fi

# Health endpoint simple version (Phase 0)
# TODO: replace with FastAPI app in Phase 1
# Пока запускаем main loop (Phase 1+)
exec python -m src.main
