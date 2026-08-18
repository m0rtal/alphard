# Postgres container

- **Dockerfile / image**: `postgres:16-alpine` (official). No custom
  Dockerfile; we lean on the official image's initdb hooks.

- **init.sql** is bind-mounted at `/docker-entrypoint-initdb.d/init.sql`
  by docker-compose. Postgres only runs it on first `initdb` (empty
  data dir). Volume-preserved clusters do NOT re-run init.sql — see
  `src/data/schema.sql` for the migrations that DO run on every bot
  start.

- **healthcheck.sh** is bind-mounted at
  `/usr/local/bin/alphard-pg-healthcheck.sh` and used by the
  compose healthcheck `test`. Why a script and not pg_isready:
  pg_isready only verifies that the socket responds and the
  server process is up. It does NOT verify our credentials.
  After a StackUpdate that rotated POSTGRES_PASSWORD against a
  preserved volume, pg_isready still returns "healthy" while every
  real query fails. The script does a full auth round-trip.

- The smoke test in the bot's `entrypoint.sh` is the LAST line of
  defense; if it ever fails, the bot refuses to start the backfill.
