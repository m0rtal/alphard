-- =============================================================================
-- Migration 0002: add source column to ohlcv_daily
-- =============================================================================
--
-- Phase 2.6 step 2 (issue #68). Lifts the v1 PK from (ticker, ts) to
-- (ticker, ts, source) so two different sources (Tinkoff MD vs MOEX ISS)
-- can write bars for the same (ticker, date) without UPSERT collision.
--
-- See scripts/cross_source_smoke.py header docstring for the Phase 2.6
-- step 1 / 2 / 3 split rationale.
--
-- IDEMPOTENCY
-- -----------
-- Every ALTER uses ``ADD COLUMN IF NOT EXISTS`` / ``DROP CONSTRAINT IF EXISTS``.
-- A second invocation against an already-migrated DB is a no-op:
--   - ALTER TABLE ... ADD COLUMN IF NOT EXISTS — no-op if column exists
--   - ALTER TABLE ... DROP CONSTRAINT IF EXISTS — no-op if constraint gone
--   - CREATE INDEX IF NOT EXISTS — no-op if index exists
--   - ALTER TABLE ... ADD PRIMARY KEY — guarded by a SELECT on
--     pg_constraint to avoid "multiple primary keys" / "pk already exists"
--
-- LEGACY DEFAULT
-- --------------
-- Every pre-existing row was written by Tinkoff MD (Phase 1.1 had no other
-- source wired). Backfilling ``source = 'tkf'`` is therefore an exact match
-- for the historical reality — no row carries incorrect provenance.
--
-- ROLLBACK
-- --------
-- This migration is not auto-reversible from this file. If a rollback is
-- ever needed (e.g. a Phase 2.6 step 3 breakage), the procedure is:
--   1. Stop the bot.
--   2. For each source != 'tkf' in ohlcv_daily:
--        DELETE FROM ohlcv_daily WHERE source != 'tkf';
--      (or merge back into tkf rows per project policy)
--   3. Reverse the operations:
--        DROP INDEX IF EXISTS idx_ohlcv_daily_ticker_ts_source;
--        CREATE INDEX IF NOT EXISTS idx_ohlcv_daily_ticker_ts ON ohlcv_daily (ticker, ts);
--        ALTER TABLE ohlcv_daily DROP CONSTRAINT ohlcv_daily_pkey;
--        ALTER TABLE ohlcv_daily ADD PRIMARY KEY (ticker, ts);
--        ALTER TABLE ohlcv_daily DROP COLUMN IF EXISTS source;
--   4. Restart the bot.

-- -----------------------------------------------------------------------------
-- 1. Add the source column (idempotent via IF NOT EXISTS).
-- -----------------------------------------------------------------------------
ALTER TABLE ohlcv_daily
    ADD COLUMN IF NOT EXISTS source VARCHAR(8) NOT NULL DEFAULT 'tkf';

-- -----------------------------------------------------------------------------
-- 2. Drop the old v1 primary key if it still exists.
--    IF EXISTS guard makes the step idempotent on a re-run.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        WHERE t.relname = 'ohlcv_daily'
          AND c.contype = 'p'
    ) THEN
        ALTER TABLE ohlcv_daily DROP CONSTRAINT ohlcv_daily_pkey;
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 3. Add the v2 primary key (ticker, ts, source) — but only if no PK exists yet.
--    The DO-block + information_schema check is what makes this idempotent on
--    a fresh-schema deploy (where schema.sql already created the v2 PK) and
--    on an upgrade-from-v1 (where schema.sql created the v1 PK first).
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        WHERE t.relname = 'ohlcv_daily'
          AND c.contype = 'p'
    ) THEN
        ALTER TABLE ohlcv_daily
            ADD PRIMARY KEY (ticker, ts, source);
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 4. Replace the v1 covering index with the v2 (ticker, ts, source) index.
--    ``IF EXISTS`` / ``IF NOT EXISTS`` keep the step idempotent.
-- -----------------------------------------------------------------------------
DROP INDEX IF EXISTS idx_ohlcv_daily_ticker_ts;
CREATE INDEX IF NOT EXISTS idx_ohlcv_daily_ticker_ts_source
    ON ohlcv_daily (ticker, ts, source);
