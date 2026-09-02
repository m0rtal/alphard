#!/bin/sh
# init_postgres.sh - apply pg_hba.conf trust line for our private subnet.
#
# Run this AFTER postgres first starts (i.e., after docker stack deploy).
# Why? The default pg_hba.conf from postgres:16-alpine uses scram-sha-256
# for all TCP connections. If the bot's password is wrong, every auth
# attempt fails with FATAL: password authentication failed.
#
# This script adds a trust line for our private Docker-bridge subnet as
# the FIRST rule so that any connection from 172.16.0.0/12 (the RFC1918
# range Docker uses for default and user-defined bridges) is accepted
# without password. The legacy 192.168.0.0/16 rule covered ~65k LAN
# addresses - narrowed in issue #97 to keep defence-in-depth intact
# (LAN peers should NOT reach Postgres without credentials).
#
# Override the trust range with POSTGRES_TRUST_SUBNET (e.g.
# POSTGRES_TRUST_SUBNET=172.18.0.0/16 if you know the bridge subnet).
# Set it to an unreachable value like 127.0.0.0/32 to disable trust
# entirely - make sure password auth works first, then remove.
#
# Idempotent: only adds the line if it isn't already present.
#
# Usage:
#   docker exec alphard-postgres bash /usr/local/bin/init_postgres.sh
#
# This is the LEGACY manual bootstrap path. For normal container
# deploys, the bot's own `init_schema()` (called from
# `docker/entrypoint.sh` BEFORE `auth_probe()`, see issue #347 /
# PR #351 - `pg-init` was dropped because its single-file
# bind-mounts rendered as directories on PVE LXC, breaking
# schema application) is the active path. Use this script only
# when running postgres outside compose or recovering from a
# state where the bot's entrypoint cannot be re-invoked.

set -e

HBA=/var/lib/postgresql/data/pg_hba.conf
BACKUP="${HBA}.bak.$$"

cp "$HBA" "$BACKUP"
echo "Backup: $BACKUP"

# BUGFIX (2026-08-18): old version prepended 'host all all 0.0.0.0/0 trust',
# which would have opened the postgres port to the entire internet if
# port forwarding were ever configured. Bind the trust line to our
# internal subnet instead.

# BUGFIX (issue #97, 2026-08-21): the historical 192.168.0.0/16 range
# covered ~65k LAN addresses. Narrow to 172.16.0.0/12 (Docker bridge
# range) and strip the legacy 192.168.0.0/16 rule if present.
LEGACY_PATTERN='^host all all 192\.168\.0\.0/16 trust$'
if grep -q "$LEGACY_PATTERN" "$HBA" 2>/dev/null; then
    echo "Legacy 192.168.0.0/16 trust line found, removing (issue #97)"
    sed -i "/$LEGACY_PATTERN/d" "$HBA"
fi

# Operator-supplied trust range (default: Docker bridge range).
TRUST_CIDR="${POSTGRES_TRUST_SUBNET:-172.16.0.0/12}"
TRUST_RULE="host all all ${TRUST_CIDR} trust"

if grep -F "$TRUST_RULE" "$HBA" >/dev/null 2>&1; then
    echo "Trust line ($TRUST_CIDR) already present, skipping"
    exit 0
fi

# Remove any prior 0.0.0.0/0 trust line that may exist (idempotent re-runs
# on an upgraded-from-prior-version cluster).
sed -i '/^host all all 0\.0\.0\.0\/0 trust$/d' "$HBA"

# Prepend scoped trust line before everything
sed -i "1i $TRUST_RULE" "$HBA"
echo "Trust line added (${TRUST_CIDR})"

# Reload config
# Issue #73: the trust line above makes password irrelevant on localhost
# (psql will succeed with ANY password or none). Drop the literal
# `PGPASSWORD=alphard` so the script does not pin the historical credential
# - `docker-compose.yaml` sources ${POSTGRES_PASSWORD:?...required} from
# .env and this script should mirror that posture instead of hardcoding a
# value that contradicts the compose path.
psql -h localhost -U "${POSTGRES_USER:-alphard}" -d "${POSTGRES_DB:-alphard}" -w \
  -c 'SELECT pg_reload_conf()' > /dev/null
echo "Postgres config reloaded"
