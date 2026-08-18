#!/bin/sh
# Alphard bot entrypoint

set -e

# S-H5: support long-token env injection via bind-mounted file.
# Portainer StackUpdate Env-parameter has a 60-char Go-unmarshal limit,
# so we cannot pass Tinkoff tokens (64+ chars) inline. The /root/.env file
# is bind-mounted at /run/secrets/alphard.env (see docker-compose stack).
if [ -f "${ENV_FILE:-/run/secrets/alphard.env}" ]; then
    set -a
    . "${ENV_FILE:-/run/secrets/alphard.env}"
    set +a
fi

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

# Health endpoint simple version (Phase 0)
# TODO: replace with FastAPI app in Phase 1
# Пока запускаем main loop (Phase 1+)
exec python -m src.main
