"""Backfill delisted/archived tickers via Tinkoff gRPC.

MOEX ISS only covers 505 active TQBR. Delisted/archived tickers (1576 SPBXM
+ 90 cross-listed US) live in Tinkoff universe and have full historical
OHLCV accessible via gRPC market_data.get_candles with yearly chunking.

This script:
1. Reads tickers from ticker_universe where class_code IS NULL (== no MOEX data)
2. Filters to delisted=True OR live but missing class_code (foreign shares)
3. Fetches via Tinkoff gRPC in 1-year chunks
4. Upserts to ohlcv_daily with source='tkf' (primary_source stays 'tkf')

Usage:
    python -m scripts.backfill_delisted_via_tinkoff [--max-tickers N]
                                                     [--years N]
                                                     [--start-after T]
                                                     [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

import psycopg
from src.data.models import TickerMeta
from src.data.pg_store import PostgresDataStore
from src.data.tinkoff_loader import TinkoffInvestDataLoader

logger = logging.getLogger("alphard.backfill_tkf_delisted")


def _dsn() -> str:
    dsn = os.environ.get("ALPHARD_PG_DSN")
    if not dsn:
        raise RuntimeError("ALPHARD_PG_DSN not set. See docker-compose.yaml.")
    return dsn


def _missing_tickers(start_after: str | None = None) -> list[str]:
    """Get all tickers in ticker_universe that have ZERO bars in ohlcv_daily.

    Sorted by ticker ASC for resumability.
    """
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tu.ticker
                FROM ticker_universe tu
                LEFT JOIN (
                    SELECT DISTINCT ticker FROM ohlcv_daily
                ) o ON tu.ticker = o.ticker
                WHERE o.ticker IS NULL
                  AND tu.ticker ~ '^[A-Z0-9]+$'  -- filter out non-MOEX garbage
                ORDER BY tu.ticker
                """,
            )
            rows = [r[0] for r in cur.fetchall()]
    if start_after:
        rows = [t for t in rows if t > start_after]
    return rows


def backfill(
    max_tickers: int = 0,
    years: int = 10,
    start_after: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Backfill delisted/archived tickers via Tinkoff gRPC.

    Args:
        max_tickers: 0 = all, else limit.
        years: backfill window (default 10 — Tinkoff keeps ~10y for shares).
        start_after: resume lexicographically.
        dry_run: only print plan, don't write.
    """
    if dry_run:
        logger.info("DRY RUN — no writes")

    store = PostgresDataStore() if not dry_run else None
    loader = TinkoffInvestDataLoader(rate_per_min=200)  # real token = 200/min

    # Aggregate ALL share boards from Tinkoff (TQBR + SPBXM + SPBHKEX +
    # A27 + MTQR + SPBEQRU + SPBRU). The previous version only used
    # TQBR (255 tickers) which left 1513 SPBXM/archived tickers uncovered.
    # Tinkoff gRPC instruments.shares() returns the FULL universe (~1927
    # TQBR + ~1516 SPBXM + smaller boards); list_shares_all(board) queries
    # each board and dedup is handled by ticker PK in ticker_universe.
    BOARDS = ("TQBR", "SPBXM", "SPBHKEX", "A27", "MTQR", "SPBEQRU", "SPBRU")
    universe_meta: dict[str, TickerMeta] = {}
    for board in BOARDS:
        try:
            for m in loader.list_shares_all(board):
                universe_meta[m.ticker] = m
        except Exception as exc:
            logger.warning(f"list_shares_all({board}) failed: {exc}")
    logger.info(f"Tinkoff universe (all boards): {len(universe_meta)} tickers")

    # Add bonds, ETFs
    try:
        for m in loader.list_bonds():
            universe_meta[m.ticker] = m
    except Exception:
        pass
    try:
        for m in loader.list_etfs():
            universe_meta[m.ticker] = m
    except Exception:
        pass

    missing = _missing_tickers(start_after=start_after)
    logger.info(f"Missing tickers (no bars in DB): {len(missing)}")

    # Filter to those that exist in Tinkoff universe
    tickers_to_backfill = [t for t in missing if t in universe_meta]
    skipped_no_meta = [t for t in missing if t not in universe_meta]
    logger.info(
        f"In Tinkoff universe: {len(tickers_to_backfill)}; "
        f"no meta (skip): {len(skipped_no_meta)}"
    )

    if max_tickers > 0:
        tickers_to_backfill = tickers_to_backfill[:max_tickers]

    end = date.today()
    start = end - timedelta(days=years * 365)

    stats = {
        "universe_total": len(tickers_to_backfill),
        "bars_total": 0,
        "errors": 0,
        "no_data": 0,
        "rate_limited": 0,
    }

    for i, ticker in enumerate(tickers_to_backfill, 1):
        meta = universe_meta[ticker]
        t0 = time.time()
        try:
            bars = list(loader.fetch_ohlcv(meta.ticker, start, end))
            if bars and not dry_run and store is not None:
                store.upsert_ohlcv(bars)
                stats["bars_total"] += len(bars)
            elif not bars:
                stats["no_data"] += 1
            elapsed = time.time() - t0
            logger.info(
                f"PROGRESS {i}/{stats['universe_total']} {ticker} "
                f"bars={len(bars)} elapsed={elapsed:.2f}s"
            )
        except Exception as exc:
            stats["errors"] += 1
            msg = str(exc).lower()
            if "rate" in msg or "limit" in msg:
                stats["rate_limited"] += 1
            logger.warning(
                f"PROGRESS {i}/{stats['universe_total']} {ticker} "
                f"ERROR {type(exc).__name__}: {str(exc)[:120]}"
            )
        # Progress every 50
        if i % 50 == 0:
            logger.info(
                f"=== {i}/{stats['universe_total']} | "
                f"bars={stats['bars_total']} | err={stats['errors']} | "
                f"empty={stats['no_data']} ==="
            )

    if store:
        store.close()
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill delisted via Tinkoff gRPC")
    p.add_argument("--max-tickers", type=int, default=0)
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--start-after", type=str, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    stats = backfill(
        max_tickers=args.max_tickers,
        years=args.years,
        start_after=args.start_after,
        dry_run=args.dry_run,
    )
    logger.info("=== BACKFILL COMPLETE ===")
    logger.info(f"  Universe:  {stats['universe_total']}")
    logger.info(f"  Bars:      {stats['bars_total']}")
    logger.info(f"  Errors:    {stats['errors']}")
    logger.info(f"  No data:   {stats['no_data']}")
    logger.info(f"  Rate-limit hits: {stats['rate_limited']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
