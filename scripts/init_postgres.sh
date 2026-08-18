#!/bin/sh
# init_postgres.sh — apply pg_hba.conf trust line for our private subnet.
#
# Run this AFTER postgres first starts (i.e., after docker stack deploy).
# Why? The default pg_hba.conf from postgres:16-alpine uses scram-sha-256
# for all TCP connections. If the bot's password is wrong, every auth
# attempt fails with FATAL: password authentication failed.
#
# This script adds a trust line for our private subnet as the FIRST rule so
# that any connection from .107's internal 192.168.0.0/16 network is
# accepted without password. .107 is a private Docker host NOT exposed to
# the public internet — explicitly NOT 0.0.0.0/0 (which would allow
# arbitrary internet connections if the port were ever forwarded).
#
# Idempotent: only adds the line if it isn't already present.
#
# Usage:
#   docker exec alphard-postgres bash /usr/local/bin/init_postgres.sh

set -e

HBA=/var/lib/postgresql/data/pg_hba.conf
BACKUP="${HBA}.bak.$$"

cp "$HBA" "$BACKUP"
echo "Backup: $BACKUP"

# BUGFIX (2026-08-18): old version prepended 'host all all 0.0.0.0/0 trust',
# which would have opened the postgres port to the entire internet if
# port forwarding were ever configured. Bind the trust line to our
# internal subnet instead.
if grep -q '192.168.0.0/16 trust' "$HBA"; then
    echo "Trust line already present, skipping"
    exit 0
fi

# Remove any prior 0.0.0.0/0 trust line that may exist (idempotent re-runs
# on an upgraded-from-prior-version cluster).
sed -i '/^host all all 0\.0\.0\.0\/0 trust$/d' "$HBA"

# Prepend scoped trust line before everything
sed -i '1i host all all 192.168.0.0/16 trust' "$HBA"
echo "Trust line added (192.168.0.0/16)"

# Reload config
PGPASSWORD=alphard psql -h localhost -U alphard -d alphard -c 'SELECT pg_reload_conf()' > /dev/null
echo "Postgres config reloaded"
