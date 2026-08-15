"""Full TQBR backfill via MOEX ISS (1927 tickers, ~5y daily OHLCV).

This script backfills the COMPLETE TQBR universe from MOEX ISS REST:
- Live + delisted + archived tickers (any STATUS: N, D, X, etc.)
- ~5 years of daily OHLCV (or whatever ISS retains)
- Classifies tickers into source='moex', class_code='TQBR'

Honest gaps documented in README:
- MOEX ISS daily backfill = ~5 years (1300 bars/ticker average)
- Delisted tickers have ~80% coverage, archived have less
- This backfill is INDEPENDENT of Tinkoff (which only has 150 live shares)

Usage:
    python -m scripts.backfill_full_universe [--max-tickers N] [--years N]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Iterable

import psycopg
from src.data.models import TickerMeta
from src.data.moex_loader import MOEXDataLoader
from src.data.pg_store import PostgresDataStore

logger = logging.getLogger("alphard.backfill")


def _dsn() -> str:
    """Get Postgres DSN from env or hardcoded fallback."""
    return os.environ.get(
        "ALPHARD_PG_DSN",
        "host=192.168.48.3 port=5432 dbname=alphard user=alphard password=kJ8sP2vR5mN9wX4tY7qL3zA6bC1dE0fH",
    )


def _ensure_class_code_column(store: PostgresDataStore) -> None:
    """Create class_code column if missing (idempotent)."""
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS class_code VARCHAR(12)")
            cur.execute("ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS delisted BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS delisted_at DATE")
            cur.execute("ALTER TABLE ticker_universe ADD COLUMN IF NOT EXISTS listed_at DATE")
        conn.commit()


def _persist_universe_meta(store: PostgresDataStore, ticker_meta: TickerMeta) -> None:
    """Upsert ticker into ticker_universe with class_code=TQBR and delisted flag."""
    store.upsert_ticker(ticker_meta)
    # Patch class_code + delisted (upsert_ticker doesn't write those)
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE ticker_universe
                   SET class_code = 'TQBR', delisted = %s
                   WHERE ticker = %s""",
                (ticker_meta.delisted, ticker_meta.ticker),
            )
        conn.commit()


def _persist_ohlcv(store: PostgresDataStore, bars: list) -> int:
    """Upsert OHLCV bars with source='moex'."""
    return store.upsert_ohlcv(bars)


def _log_progress(
    ticker: str,
    bars: int,
    elapsed: float,
    error: str | None = None,
    status: str = "OK",
) -> None:
    """Structured per-ticker progress log line for `tail -f` monitoring."""
    msg = {
        "ts": datetime.utcnow().isoformat(),
        "ticker": ticker,
        "bars": bars,
        "elapsed_sec": round(elapsed, 2),
        "status": status,
    }
    if error:
        msg["error"] = error
    print(f"PROGRESS {msg}", flush=True)


def backfill(
    max_tickers: int = 0,
    years: int = 5,
    start_after: str | None = None,
) -> dict[str, int]:
    """Backfill MOEX ISS TQBR universe + OHLCV into Postgres.

    Args:
        max_tickers: 0 = all, else limit (for testing).
        years: backfill window in years (default 5, max ISS retains).
        start_after: if set, skip tickers <= this lexicographically (resume).

    Returns:
        stats dict with counts.
    """
    store = PostgresDataStore()
    _ensure_class_code_column(store)

    loader = MOEXDataLoader(rate_per_min=30)  # ~2s per ticker
    logger.info("Fetching TQBR universe from MOEX ISS...")
    t0 = time.time()
    universe = loader.list_tickers(board_id="TQBR")
    logger.info(f"Universe fetched: {len(universe)} tickers in {time.time()-t0:.1f}s")

    # Filter start_after (for resume)
    if start_after:
        universe = [t for t in universe if t.ticker > start_after]
        logger.info(f"After resume filter (> {start_after}): {len(universe)}")

    if max_tickers > 0:
        universe = universe[:max_tickers]

    end = date.today()
    start = end - timedelta(days=years * 365)

    stats = {
        "universe_total": len(universe),
        "bars_total": 0,
        "errors": 0,
        "delisted_count": sum(1 for t in universe if t.delisted),
        "live_count": sum(1 for t in universe if not t.delisted),
    }

    for i, meta in enumerate(universe, 1):
        t0 = time.time()
        try:
            _persist_universe_meta(store, meta)
            bars = list(loader.iter_ohlcv(meta.ticker, start, end))
            if bars:
                # Convert to OHLCVRow list — store.upsert_ohlcv accepts list
                written = _persist_ohlcv(store, bars)
                stats["bars_total"] += written
            _log_progress(meta.ticker, len(bars), time.time() - t0, status="OK")
        except Exception as exc:
            stats["errors"] += 1
            _log_progress(
                meta.ticker,
                0,
                time.time() - t0,
                error=f"{type(exc).__name__}: {str(exc)[:80]}",
                status="ERR",
            )
        # Progress every 25
        if i % 25 == 0:
            logger.info(
                f"PROGRESS: {i}/{stats['universe_total']} | " f"bars={stats['bars_total']} | errors={stats['errors']}"
            )

    store.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Full TQBR backfill via MOEX ISS")
    parser.add_argument("--max-tickers", type=int, default=0, help="0 = all")
    parser.add_argument("--years", type=int, default=5, help="backfill window in years")
    parser.add_argument("--start-after", type=str, default=None, help="resume from ticker")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    stats = backfill(
        max_tickers=args.max_tickers,
        years=args.years,
        start_after=args.start_after,
    )
    logger.info(f"=== BACKFILL COMPLETE ===")
    logger.info(f"  Universe: {stats['universe_total']} tickers")
    logger.info(f"  Bars:     {stats['bars_total']}")
    logger.info(f"  Live:     {stats['live_count']}")
    logger.info(f"  Delisted: {stats['delisted_count']}")
    logger.info(f"  Errors:   {stats['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
