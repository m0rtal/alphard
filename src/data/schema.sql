-- Alphard Data Agent schema (Phase 1.1)
--
-- Conventions:
--   * All identifiers use snake_case.
--   * NUMERIC(18, 8) for prices / amounts: fits MOEX lot prices (3-4 decimals)
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
    delisted     BOOLEAN NOT NULL DEFAULT FALSE,
    delisted_at  DATE,
    listed_at    DATE,
    source       VARCHAR(8) NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ticker_universe_delisted
    ON ticker_universe (delisted);

CREATE INDEX IF NOT EXISTS idx_ticker_universe_figi
    ON ticker_universe (figi) WHERE figi IS NOT NULL;

-- ---------------------------------------------------------------------------
-- ohlcv_daily  (PK = ticker, ts, source — multi-source reconciliation OK)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    ticker       VARCHAR(12) NOT NULL,
    ts           DATE NOT NULL,
    open         NUMERIC(18, 8) NOT NULL,
    high         NUMERIC(18, 8) NOT NULL,
    low          NUMERIC(18, 8) NOT NULL,
    close        NUMERIC(18, 8) NOT NULL,
    volume       NUMERIC(18, 8) NOT NULL,
    adj_close    NUMERIC(18, 8) NOT NULL,
    source       VARCHAR(8) NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, ts, source),
    CONSTRAINT fk_ohlcv_ticker FOREIGN KEY (ticker)
        REFERENCES ticker_universe(ticker) ON DELETE RESTRICT
);

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
-- news_embedding  (RESERVED — Phase 3 will add vector(384) column)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_embedding (
    id            BIGSERIAL PRIMARY KEY,
    ticker        VARCHAR(12),
    ts            TIMESTAMPTZ NOT NULL,
    headline      TEXT NOT NULL,
    embedding     vector(384),          -- requires pgvector (Phase 3)
    source        VARCHAR(8) NOT NULL DEFAULT 'manual',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- seed sample data (optional — comment out for clean install)
-- ---------------------------------------------------------------------------
-- INSERT INTO ticker_universe (ticker, figi, name, lot, isin, source)
-- VALUES
--     ('SBER',  'BBG004730N88', 'Сбер Банк',         10, 'RU0009029540', 'tkf'),
--     ('GAZP',  'BBG004730RP0', 'Газпром',            10, 'RU0007661625', 'tkf'),
--     ('YDEX',  'BBG00QKJVZ03', 'Яндекс',             1, 'NL0009805522', 'tkf')
-- ON CONFLICT (ticker) DO NOTHING;