-- Alphard postgres init bootstrap (Phase 1.6)
--
-- Runs only on first `initdb` (empty data dir). If volume already
-- contains a cluster, this file is NOT executed and the existing
-- auth chain is preserved.
--
-- All other schema migrations live in src/data/schema.sql and are
-- applied at startup by src/data/pg_store.py::init_schema().

-- Ensure the user the bot connects as always exists in a known state.
-- (initdb already creates the POSTGRES_USER; this is a no-op for
-- freshly-init'd clusters but documents the contract.)

-- Create the auth-probe table. We use a one-row INSERT/DELETE in
-- _auth_probe() to verify the bot can actually write, not just SELECT.
-- The table is created here so the probe works the moment the bot
-- starts, without a separate DDL round-trip.
CREATE TABLE IF NOT EXISTS _auth_probe (
    id         SMALLINT PRIMARY KEY,
    probed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source     VARCHAR(32) NOT NULL
);

-- One seed row so the probe can do INSERT ... ON CONFLICT DO UPDATE.
INSERT INTO _auth_probe (id, probed_at, source)
    VALUES (1, NOW(), 'init')
    ON CONFLICT (id) DO NOTHING;

-- Permissions: the bot user (POSTGRES_USER, default 'alphard') owns
-- this table — but we still grant explicitly so a future user rename
-- doesn't break the probe.
-- (initdb has already granted schema usage to the user.)
GRANT ALL PRIVILEGES ON TABLE _auth_probe TO CURRENT_USER;
GRANT USAGE, SELECT ON SEQUENCE _auth_probe_id_seq TO CURRENT_USER;

-- pg_hba.conf trust line for the Docker-bridge subnet. See
-- scripts/init_postgres.sh (and the pg-init one-shot in
-- docker-compose.yaml) for the active rule. As of issue #97 the trust
-- range defaults to 172.16.0.0/12 (Docker bridge range), not the
-- legacy 192.168.0.0/16 LAN range and explicitly NOT 0.0.0.0/0.
