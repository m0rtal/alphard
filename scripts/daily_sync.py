#!/usr/bin/env python3
"""Daily sync: pull last 5 trading days for top MOEX tickers into Postgres.

Used by cron. Idempotent: upsert based on (ticker, ts) PK.

Universe (Phase 1.1): top 20 liquid MOEX TQBR shares — reasonable bootstrap for
the cross-sectional ML pipeline. Phase 3 will expand to full MOEX.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta

# Make alphard.src importable when run from /app
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.data.moex_loader import MOEXDataLoader
from src.data.pg_store import PostgresDataStore

logger = logging.getLogger("alphard.daily_sync")

# Top 20 MOEX TQBR by ADV (June 2026 estimate). Phase 1.1 bootstrap.
LIQUID_UNIVERSE = [
    "SBER", "GAZP", "LKOH", "GMKN", "NVTK",
    "ROSN", "TATN", "MGNT", "MOEX", "ALRS",
    "MTSS", "SNGS", "NLMK", "CHMF", "YDEX",
    "OZON", "VKCO", "SBERP", "BANE", "BSPB",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5,
                        help="Pull last N days (default 5, includes weekends)")
    parser.add_argument("--universe", nargs="*", default=LIQUID_UNIVERSE,
                        help="Ticker list (default: top 20 MOEX liquid)")
    parser.add_argument("--dsn", default=os.environ.get("ALPHARD_PG_DSN"),
                        help="Postgres DSN (falls back to $ALPHARD_PG_DSN)")
    args = parser.parse_args()

    if not args.dsn:
        logger.error("ALPHARD_PG_DSN not set")
        return 1

    os.environ["ALPHARD_PG_DSN"] = args.dsn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    end = date.today()
    start = end - timedelta(days=args.days)

    loader = MOEXDataLoader(timeout_sec=20.0)
    store = PostgresDataStore()

    # Cache ticker universe once
    universe = loader.list_tickers()
    universe_by_ticker = {t.ticker: t for t in universe}

    total_bars = 0
    errors = []

    try:
        for symbol in args.universe:
            meta = universe_by_ticker.get(symbol.upper())
            if meta is None:
                logger.warning(f"{symbol}: not in MOEX universe, skipping")
                continue

            try:
                store.upsert_ticker(meta)
                bars = list(loader.iter_ohlcv(symbol, start, end))
                if bars:
                    written = store.upsert_ohlcv(bars)
                    logger.info(f"{symbol}: {written} bars ({start} → {end})")
                    total_bars += written
            except Exception as exc:
                logger.error(f"{symbol}: {exc}")
                errors.append((symbol, str(exc)))
    finally:
        store.close()

    logger.info(f"=== DONE: {total_bars} bars written, {len(errors)} errors ===")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
