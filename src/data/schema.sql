-- Alphard Data Agent schema (Phase 1.1)
--
-- Conventions:
--   * All identifiers use snake_case.
--   * NUMERIC(20, 8) for prices / amounts: fits MOEX values up to ~1e12
--     (e.g. AFKS volume 193M, AFKS value 1.6B RUB on busy days).
--     and equity prices (~10^6) with 8 fractional digits.
--   * VARCHAR(12) for tickers (Tinkoff / MOEX both fit; Phase 1.3 may add
--     qualifier tickers like 'SBER@SPB' which are still < 12 chars).
--   * VARCHAR(8) for source tags ('tkf' | 'moex' | 'manual').
--   * DATE for OHLCV timestamps (no intraday in Phase 1.1).
--   * TEXT for JSONB-like audit columns; we use JSONB on Postgres for
--     querying the audit log efficiently.
--
-- Extensions:
--   * Phase 3 adds the pgvector extension. We DO NOT enable it here —
--     requiring it in Phase 1.1 would break the env without pgvector
--     installed. The ``news_embedding`` table is reserved for it.

-- ---------------------------------------------------------------------------
-- Extensions (reserved for later phases)
-- ---------------------------------------------------------------------------
-- CREATE EXTENSION IF NOT EXISTS vector;       -- pgvector (Phase 3)
-- CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy ticker search (Phase 2)

-- ---------------------------------------------------------------------------
-- ticker_universe
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticker_universe (
    ticker       VARCHAR(12) PRIMARY KEY,
    figi         VARCHAR(12),
    name         TEXT NOT NULL,
    lot          INTEGER NOT NULL CHECK (lot > 0),
    isin         VARCHAR(12),
    currency     VARCHAR(3) NOT NULL DEFAULT 'RUB',
    -- MOEX class code: TQBR (shares), TQOB (OFZ), TQCB (corp/muni), TQTE (ETFs), CETS (currencies), SPBFUT (futures)
    class_code   VARCHAR(12),
    delisted     BOOLEAN NOT NULL DEFAULT FALSE,
    delisted_at  DATE,
    listed_at    DATE,
    source       VARCHAR(8) NOT NULL,
    -- Backfill-completion flag: TRUE when the data-agent has pulled
    -- the expected bar count for this ticker's listed_at..today|
    -- delisted_at range (see scripts/backfill_history_md._HALTS_PCT
    -- and the formula). ML and backtest layers filter on this to
    -- avoid training on partial history.
    backfill_complete BOOLEAN NOT NULL DEFAULT FALSE,
    backfill_complete_at TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Forward-compat for columns that may have been added by older images.
-- Idempotent: ADD COLUMN IF NOT EXISTS does nothing if the column is
-- already there. RUN 'init_schema' to apply.
ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS lot INTEGER;
ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS isin VARCHAR(12);
ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS class_code VARCHAR(12);
ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS delisted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS delisted_at DATE;
ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS listed_at DATE;
ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS backfill_complete BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS backfill_complete_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_ticker_universe_delisted
    ON ticker_universe (delisted);

CREATE INDEX IF NOT EXISTS idx_ticker_universe_backfill_complete
    ON ticker_universe (backfill_complete);


CREATE INDEX IF NOT EXISTS idx_ticker_universe_figi
    ON ticker_universe (figi) WHERE figi IS NOT NULL;

-- ---------------------------------------------------------------------------
-- _auth_probe  (Phase 1.6 H-9: detect silent auth drift after redeploy)
-- ---------------------------------------------------------------------------
-- One-row table used by PostgresDataStore.auth_probe() and by the bot
-- entrypoint smoke test. The probe performs INSERT ... ON CONFLICT DO
-- UPDATE to verify the connection can BOTH read AND write, not just
-- SELECT 1 (which pg_isready -- but does NOT -- verifies). If this
-- table does not exist yet, init_schema() will create it on the next
-- bot startup. If it does, the probe is non-destructive.
--
-- Why not in docker/postgres/init.sql? init.sql only runs on first
-- `initdb` (empty data dir). On a volume that has been preserved
-- across redeploys, init.sql is NEVER executed — which is exactly the
-- case we want to detect. Putting the probe in src/data/schema.sql
-- (which init_schema() applies on every bot start) means the probe
-- always works, regardless of volume history.
CREATE TABLE IF NOT EXISTS _auth_probe (
    id         SMALLINT PRIMARY KEY,
    probed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source     VARCHAR(32) NOT NULL
);

-- Seed the single probe row. The probe is INSERT ... ON CONFLICT DO
-- UPDATE so the row must already exist for the probe to be non-destructive.
INSERT INTO _auth_probe (id, probed_at, source)
    VALUES (1, NOW(), 'schema_init')
    ON CONFLICT (id) DO NOTHING;


-- ---------------------------------------------------------------------------
-- ohlcv_daily  (PK = ticker, ts)
-- -----------------------------------------------------------------------------
-- Each (ticker, ts) is stored ONCE. Source provenance is not tracked at row
-- level — observability (logs, decision_log) is sufficient for Phase 2+.
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    ticker           VARCHAR(12) NOT NULL,
    ts               DATE NOT NULL,
    open             NUMERIC(20, 8) NOT NULL,
    high             NUMERIC(20, 8) NOT NULL,
    low              NUMERIC(20, 8) NOT NULL,
    close            NUMERIC(20, 8) NOT NULL,
    volume           NUMERIC(20, 0) NOT NULL,
    adj_close        NUMERIC(20, 8) NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, ts),
    CONSTRAINT fk_ohlcv_ticker FOREIGN KEY (ticker)
        REFERENCES ticker_universe(ticker) ON DELETE RESTRICT
);

-- Forward-compat for older images that may have skipped columns.
ALTER TABLE ohlcv_daily ADD COLUMN IF NOT EXISTS adj_close NUMERIC(20, 8);

CREATE INDEX IF NOT EXISTS idx_ohlcv_daily_ticker_ts
    ON ohlcv_daily (ticker, ts);

CREATE INDEX IF NOT EXISTS idx_ohlcv_daily_ts
    ON ohlcv_daily (ts);

-- ---------------------------------------------------------------------------
-- corporate_actions  (splits, dividends, ticker renames)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corporate_actions (
    ticker       VARCHAR(12) NOT NULL,
    ts           DATE NOT NULL,
    kind         VARCHAR(12) NOT NULL,  -- 'split' | 'dividend' | 'change'
    value        NUMERIC(18, 8) NOT NULL,
    source       VARCHAR(8) NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, ts, kind, source)
);

CREATE INDEX IF NOT EXISTS idx_corp_actions_ticker_ts
    ON corporate_actions (ticker, ts);

-- ---------------------------------------------------------------------------
-- delisting_log  (append-only audit trail for survivorship-aware backtests)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS delisting_log (
    id            SERIAL PRIMARY KEY,
    ticker        VARCHAR(12) NOT NULL,
    delisted_at   DATE NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    source        VARCHAR(8) NOT NULL DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_delisting_log_ticker
    ON delisting_log (ticker);

-- ---------------------------------------------------------------------------
-- -- news_embedding  (RESERVED — Phase 3 will add vector(384) column) (pgvector disabled in Phase 1, Phase 3 will enable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_embedding (
    id            BIGSERIAL PRIMARY KEY,
    ticker        VARCHAR(12),
    ts            TIMESTAMPTZ NOT NULL,
    headline      TEXT NOT NULL,
--     embedding     vector(384),          -- requires pgvector (Phase 3) (pgvector disabled in Phase 1, Phase 3 will enable)
    source        VARCHAR(8) NOT NULL DEFAULT 'manual',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ---------------------------------------------------------------------------
-- decision_log  (Phase 1.5: Coordinator pipeline audit trail)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_log (
    id            BIGSERIAL PRIMARY KEY,
    kind          VARCHAR(64) NOT NULL DEFAULT 'coordinator_pipeline',
    ticker        VARCHAR(12),
    decision      JSONB NOT NULL,
    source        VARCHAR(8) NOT NULL DEFAULT 'alphard',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decision_log_kind_ticker
    ON decision_log (kind, ticker);

CREATE INDEX IF NOT EXISTS idx_decision_log_created
    ON decision_log (created_at);

-- ---------------------------------------------------------------------------
-- seed sample data (optional — comment out for clean install)
-- ---------------------------------------------------------------------------
-- INSERT INTO ticker_universe (ticker, figi, name, lot, isin, source)
-- VALUES
--     ('SBER',  'BBG004730N88', 'Сбер Банк',         10, 'RU0009029540', 'tkf'),
--     ('GAZP',  'BBG004730RP0', 'Газпром',            10, 'RU0007661625', 'tkf'),
--     ('YDEX',  'BBG00QKJVZ03', 'Яндекс',             1, 'NL0009805522', 'tkf')
-- ON CONFLICT (ticker) DO NOTHING;