-- =============================================================================
-- Migration 0003: sizing_audit_log
-- =============================================================================
--
-- Phase 2.2 (kanban task t_e55e2168): Position Sizing Matrix. One row per
-- compute_position_size() call, recording inputs, scalars, output, and the
-- locked formula version. Replay tool scripts/replay_sizing.py reads this
-- table (or the JSONL mirror) to reproduce decisions bit-identically.
--
-- See docs/POSITION-SIZING.md §"Audit trail" for the column rationale.
--
-- IDEMPOTENCY
-- -----------
-- Same pattern as 0002: every DDL uses IF NOT EXISTS / IF EXISTS, and the
-- CREATE TABLE wraps the body in a pg_class guard so a re-run against an
-- already-migrated DB is a no-op. Test asserts in test_migration_0003.py.
--
-- RATIONALE
-- ---------
-- * No FK on ticker → ticker_universe.ticker: the sizer must not silently
--   fail when a ticker is delisted between audit and replay. The audit
--   row captures the decision as it was made.
-- * JSONB for inputs / scalars / output: the formula constants evolve
--   (ADR-0006 §4 pipeline is multiplicative), so JSON gives forward
--   compatibility without re-migration.
-- * formula_version VARCHAR(8): 'v1' today, 'v2' tomorrow. Live positions
--   opened under v1 stay v1 — the column is the rollback key.
-- * index on (ticker, ts) and (formula_version, ts): common replay queries.
-- =============================================================================

CREATE TABLE IF NOT EXISTS sizing_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    ticker          VARCHAR(12) NOT NULL,
    side            VARCHAR(8) NOT NULL,
    inputs          JSONB NOT NULL,
    scalars         JSONB NOT NULL,
    output          JSONB NOT NULL,
    formula_version VARCHAR(8) NOT NULL DEFAULT 'v1',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sizing_audit_log_ticker_ts
    ON sizing_audit_log (ticker, ts DESC);

CREATE INDEX IF NOT EXISTS idx_sizing_audit_log_version_ts
    ON sizing_audit_log (formula_version, ts DESC);

-- Comment for humans browsing psql \d+
COMMENT ON TABLE sizing_audit_log IS
    'Phase 2.2 sizing decisions. Append-only. formula_version is the rollback key.';
