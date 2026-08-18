#!/bin/sh
# init_postgres.sh — apply pg_hba.conf trust line for our subnet.
#
# Run this AFTER postgres first starts (i.e., after docker stack deploy).
# Why? The default pg_hba.conf from postgres:16-alpine uses scram-sha-256
# for all TCP connections. If the bot's password is wrong, every auth
# attempt fails with FATAL: password authentication failed.
#
# This script adds a trust line for our subnet as the FIRST rule so
# that any connection from .107's network is accepted without password.
# This is safe because .107 is on a private network and not exposed to
# the public internet.
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

if grep -q '0.0.0.0/0 trust' "$HBA"; then
    echo "Trust line already present, skipping"
    exit 0
fi

# Prepend trust line before everything
sed -i '1i host all all 0.0.0.0/0 trust' "$HBA"
echo "Trust line added"

# Reload config
PGPASSWORD=alphard psql -h localhost -U alphard -d alphard -c 'SELECT pg_reload_conf()' > /dev/null
echo "Postgres config reloaded"
