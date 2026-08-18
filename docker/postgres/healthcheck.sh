#!/bin/sh
# Postgres container healthcheck (Phase 1.6 H-9).
#
# Real authentication check — pg_isready on its own does not verify
# our credentials (it only checks that the socket responds). For a
# volume-preserved cluster whose pg_authid scram hash is stale, the
# socket responds fine while every real auth attempt fails. The old
# healthcheck (`pg_isready -U alphard`) would have returned "healthy"
# in that state.
#
# We use `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'SELECT 1'`
# which performs a full auth round-trip under the username + password
# set by POSTGRES_USER / POSTGRES_PASSWORD env on first initdb. If
# the connection or auth fails, psql exits non-zero and the
# container is marked unhealthy (depends_on condition fails, bot
# does not start).
#
# We avoid the .pgpass file because postgres:16-alpine doesn't ship
# with one and creating one in this script would be brittle on
# entrypoint ordering. Instead we trust the env vars the postgres
# container was started with — they are the only authentication path
# that pg_authid knows about.
set -e

PGUSER="${POSTGRES_USER:-alphard}"
PGDATABASE="${POSTGRES_DB:-alphard}"

# `psql -c 'SELECT 1'` does: connect, auth, send simple query, fetch
# 1 row, return. Any failure (connection refused, auth wrong,
# permission denied on pg_catalog, etc.) exits non-zero → unhealthy.
psql -v ON_ERROR_STOP=1 \
     -U "$PGUSER" \
     -d "$PGDATABASE" \
     -h localhost \
     -c 'SELECT 1' \
     >/dev/null 2>&1
