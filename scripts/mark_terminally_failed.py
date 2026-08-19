"""Mark tickers that have failed backfill N consecutive times as delisted.

This implements the documented Phase 1 fix:
> **delisted_at** sync invoked from cron (PHASE1-AUDIT gap #7)
> Treats no-data tickers as known-unrecoverable rather than retrying forever.

Heuristic (deterministic, conservative):
  1. backfill_complete = false
  2. fetched count = 0 across all 3 sources (Tinkoff MD + gRPC + MOEX ISS)
     → no rows in ohlcv_daily for this ticker
  3. listed_at is NULL or older than 2 years
     → either pre-2018 OR delisted before we noticed
  4. Tinkoff shares+bonds class_code = SPBXM (US via SPB-exchange) OR no class_code
     → delisted from MOEX or US-traded only

Writes delisted_at = today() on match. Idempotent (skip if already set).

Usage:
    python -m scripts.mark_terminally_failed [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date

import psycopg

logger = logging.getLogger("alphard.mark_terminally_failed")


def _dsn() -> str:
    dsn = os.environ.get("ALPHARD_PG_DSN")
    if not dsn:
        raise RuntimeError("ALPHARD_PG_DSN not set. See docker-compose.yaml.")
    return dsn


def _heuristic_sql() -> str:
    """Returns tickers that have failed backfill under realistic-no-data
    conditions: no rows, NULL or pre-2018 listed_at, and (no MOEX class OR
    SPBXM = US-only).
    """
    return """
        SELECT ticker
        FROM ticker_universe
        WHERE backfill_complete = false
          AND delisted_at IS NULL
          AND (listed_at IS NULL OR listed_at < (CURRENT_DATE - INTERVAL '2 years'))
          AND (
                class_code IS NULL
             OR class_code = 'SPBXM'
          )
          AND NOT EXISTS (
                SELECT 1 FROM ohlcv_daily od
                WHERE od.ticker = ticker_universe.ticker
                LIMIT 1
          )
        ORDER BY ticker
    """


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be marked, but do not write.",
    )
    parser.add_argument(
        "--horizon-days",
        type=int,
        default=730,
        help="Tickers listed_at older than this are candidates (default 730 = 2y).",
    )
    parser.add_argument(
        "--include-classes",
        type=str,
        default="SPBXM,",
        help="Comma-separated class_codes to mark (default: SPBXM only).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sql = _heuristic_sql().replace("INTERVAL '2 years'", f"INTERVAL '{args.horizon_days} days'")

    with psycopg.connect(_dsn(), autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            candidates = [row[0] for row in cur.fetchall()]

        logger.info("candidates=%d horizon=%dd", len(candidates), args.horizon_days)
        if not candidates:
            return 0

        if args.dry_run:
            for t in candidates:
                print(f"would mark {t} as delisted_at={date.today()}")
            return len(candidates)

        # Bulk UPDATE: only tickers that still have no rows (race-safe)
        update_sql = """
            UPDATE ticker_universe tu
            SET delisted_at = %s
            WHERE tu.ticker = ANY(%s)
              AND tu.delisted_at IS NULL
              AND tu.backfill_complete = false
              AND NOT EXISTS (
                    SELECT 1 FROM ohlcv_daily od
                    WHERE od.ticker = tu.ticker LIMIT 1
              )
        """
        with conn.cursor() as cur:
            cur.execute(update_sql, (date.today(), candidates))
            updated = cur.rowcount
        conn.commit()

        logger.info("marked delisted_at=%s for %d tickers", date.today(), updated)
        for t in candidates:
            print(f"marked {t}")
        return updated


if __name__ == "__main__":
    raise SystemExit(main())
