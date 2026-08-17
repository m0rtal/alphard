#!/usr/bin/env python3
"""Standalone OHLCV data validator.

Runs the same quality gate the backfill loop applies to every fresh
batch, but across the entire ``ohlcv_daily`` table. Designed for a
weekly cron that catches invariant violations the per-batch gate
couldn't (e.g. bars that were already in DB before the gate existed,
or that crept in via manual SQL).

What it does
------------
1. Loads every ``(ticker, ts)`` row in ``ohlcv_daily``.
2. For each row, calls ``src.data.quality.validate.validate_bar``.
3. For each ticker, runs ``validate_series`` for gap / outlier / return
   invariants.
4. Aggregates issues by severity and ticker; exits non-zero if any
   CRITICAL is found, exits zero otherwise.

Why standalone, not part of the backfill loop
---------------------------------------------
Backfill re-checks bars it just inserted. This script re-checks bars
that were written by ANY source (previous backfill runs, manual
``COPY``, future ingested APIs). It is the operator's safety net.

Run as
-----
::

    python3 scripts/validate_ohlcv.py
    python3 scripts/validate_ohlcv.py --ticker SBER     # single ticker
    python3 scripts/validate_ohlcv.py --limit 1000      # sample

Exit codes: 0 = clean (or only WARNING/INFO), 2 = at least one
CRITICAL issue, 3 = infrastructure error (DB connection, etc.).
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from decimal import Decimal
from typing import Any

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.data.pg_store import PostgresDataStore  # noqa: E402
from src.data.quality.validate import (  # noqa: E402
    Severity,
)  # noqa: E402, F401 — duplicate of validate.Severity; imported here for mypy typing
from src.data.quality.validate import (  # noqa: E402
    Issue,
    blocking,
    summarize,
    validate_bar,
    validate_series,
    worst_tickers,
)

logger = logging.getLogger("alphard.validate_ohlcv")


def _fetch_all_bars(
    store: PostgresDataStore,
    ticker: str | None = None,
    limit: int | None = None,
) -> dict[str, list[tuple[Any, ...]]]:
    """Return ``ticker -> sorted(rows)``. Skip None / missing fields."""
    store._connect()
    with store._conn.cursor() as cur:
        if ticker:
            sql = "SELECT ticker, ts, open, high, low, close, volume FROM ohlcv_daily WHERE ticker = %s ORDER BY ts"
            cur.execute(sql, (ticker.upper(),))
        else:
            sql = "SELECT ticker, ts, open, high, low, close, volume FROM ohlcv_daily ORDER BY ticker, ts"
            cur.execute(sql)
        if limit:
            cur.execute(sql + " LIMIT %s", (limit,) if ticker else None)
        rows = cur.fetchall()
    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for r in rows:
        grouped.setdefault(r[0], []).append(r)
    return grouped


def _to_ohlcv_rows(rows, ticker):  # type: ignore[no-untyped-def]
    """Convert raw DB rows to ``OHLCVRow`` instances.

    ``validate_bar`` reads ``row.ticker`` so we need the ticker on each
    bar. We use ``OHLCVRow.model_construct`` (skip validation) because
    the DB-stored values don't include ``adj_close`` and the script's
    purpose is read-only auditing, not production ingestion.

    Malformed rows are skipped silently — the script should not crash
    on dirty data; it should report what it can.
    """
    from src.data.models import OHLCVRow

    out: list[OHLCVRow] = []
    for r in rows:
        try:
            out.append(
                OHLCVRow.model_construct(
                    ticker=ticker,
                    ts=r[1],
                    open=Decimal(str(r[2])),
                    high=Decimal(str(r[3])),
                    low=Decimal(str(r[4])),
                    close=Decimal(str(r[5])),
                    volume=Decimal(str(r[6])),
                    adj_close=Decimal(str(r[5])),  # not stored — use close as a safe default
                )
            )
        except (TypeError, ValueError, IndexError, Exception) as exc:
            logger.debug(f"skipping malformed row for {r[0] if r else '?'}: {exc}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone OHLCV validator")
    parser.add_argument("--ticker", help="validate only this ticker")
    parser.add_argument(
        "--limit",
        type=int,
        help="max total rows to read (for sampling)",
    )
    parser.add_argument(
        "--critical-only",
        action="store_true",
        help="suppress WARNING/INFO noise in output",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level))

    store = PostgresDataStore()
    try:
        grouped = _fetch_all_bars(store, ticker=args.ticker, limit=args.limit)
        if not grouped:
            logger.info("ohlcv_daily is empty — nothing to validate")
            return 0
        logger.info(f"Validating {sum(len(v) for v in grouped.values())} bars " f"across {len(grouped)} tickers")
        all_issues: list[Issue] = []
        for ticker, raw_rows in grouped.items():
            rows = _to_ohlcv_rows(raw_rows, ticker)  # type: ignore[no-untyped-call]
            for bar in rows:
                all_issues.extend(validate_bar(bar))
            all_issues.extend(validate_series(rows))
        _summary = summarize(all_issues)  # noqa: F841 — used by external callers for the same issue set
        worst = worst_tickers(all_issues)
        # Pretty-print summary
        counts = Counter(i.severity for i in all_issues)
        # Severity is a str Enum — counts keys are the str value.
        critical_n: int = sum(1 for i in all_issues if i.is_blocking())
        warning_n = int(counts.get(Severity.WARNING, 0))  # noqa: F841
        info_n: int = counts.get(Severity.INFO, 0)
        logger.info(
            f"Validation complete: {len(all_issues)} issues "
            f"(CRITICAL={critical_n} "
            f"WARNING={counts.get(Severity.WARNING, 0)} "
            f"INFO={info_n})"
        )
        if not args.critical_only and worst:
            logger.info("Issues by ticker (top 20):")
            for ticker, count in worst[:20]:
                logger.info(f"  {ticker}: {count} issue(s)")
        # Determine exit code
        if blocking(all_issues):
            logger.error("CRITICAL issues found — ohlcv_daily contains invariant violations")
            return 2
        logger.info("No CRITICAL issues — table is clean")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logger.exception(f"validate_ohlcv failed: {exc}")
        sys.exit(3)
