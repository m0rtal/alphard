"""Standalone DB health probe — Phase 1.6 H-9.

Run from cron every minute to surface silent auth drift before
backfill silently loses hours of work to a password mismatch.

Exit codes
----------
0   auth verified (SELECT 1 + INSERT _auth_probe both succeeded)
1   auth BROKEN — connection failed, auth rejected, or write denied.
    A non-zero exit signals to cron / monitoring that the bot's
    writes are landing nowhere.

Why not just rely on the healthcheck in docker-compose.yml?
pg_isready reports healthy even when pg_authid holds a scram-hash
of an older POSTGRES_PASSWORD. The only way to know the bot's
actual credentials work is to do a real round-trip with them.

Usage
-----
    # In cron, every minute:
    * * * * *  python3 /app/scripts/check_db_health.py >> /app/logs/health.log 2>&1

    # Manual, on the bot or anywhere with $ALPHARD_PG_DSN set:
    python3 scripts/check_db_health.py
    echo $?    # 0 = ok, 1 = broken

Phase 1.6 — wired in by the silent-broken-postgres-on-redeploy fix.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from data.pg_store import PostgresDataStore  # noqa: E402 — after sys.path tweak


def main() -> int:
    try:
        store = PostgresDataStore()
    except Exception as exc:
        print(f"[db_health] DSN missing or invalid: {type(exc).__name__}: {exc}")
        return 1
    try:
        ok = store.auth_probe(source="check_db_health")
    except Exception as exc:
        # Defensive: auth_probe() catches psycopg errors and returns
        # False, but Python-level errors (TypeError, AttributeError
        # from a botched upgrade, etc.) would otherwise escape and
        # leave cron's exit code as 1 from the uncaught exception —
        # which is fine for cron, but we want a clean message in the
        # log so the on-call has a recognisable "db_health" prefix.
        print(f"[db_health] BROKEN: probe raised " f"{type(exc).__name__}: {exc}")
        return 1
    if ok:
        print("[db_health] OK: SELECT 1 + INSERT _auth_probe succeeded")
        return 0
    print("[db_health] BROKEN: see WARNING in container logs for psycopg error")
    return 1


if __name__ == "__main__":
    sys.exit(main())
