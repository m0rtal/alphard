#!/usr/bin/env python3
"""Daily sync: pull last N days for top MOEX tickers into Postgres.

Tinkoff Invest gRPC is the PRIMARY source (broker-authoritative).
MOEX ISS REST is reserved for backfill pre-2010 (when Tinkoff API
may not have data) and as a cross-source validation fallback.

Used by cron. Idempotent: upsert on (ticker, ts, source) PK.

Universe (Phase 1.1): top 20 liquid MOEX TQBR shares — bootstrap for
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

from src.data.tinkoff_loader import TinkoffInvestDataLoader  # noqa: E402
from src.data.pg_store import PostgresDataStore  # noqa: E402
from typing import Any  # noqa: E402

logger = logging.getLogger("alphard.daily_sync")

# Top 20 MOEX TQBR by ADV (June 2026 estimate). Phase 1.1 bootstrap.
LIQUID_UNIVERSE = [
    "SBER",
    "GAZP",
    "LKOH",
    "GMKN",
    "NVTK",
    "ROSN",
    "TATN",
    "MGNT",
    "MOEX",
    "ALRS",
    "MTSS",
    "SNGS",
    "NLMK",
    "CHMF",
    "YDEX",
    "OZON",
    "VKCO",
    "SBERP",
    "BANE",
    "BSPB",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--days", type=int, default=5, help="Pull last N days (default 5, includes weekends)"
    )  # noqa: E501
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        help="If > 0, pull N days for the full universe (one-time backfill)",  # noqa: E501
    )
    parser.add_argument(
        "--universe", nargs="*", default=None, help="Ticker list (default: top 20 MOEX liquid)"
    )  # noqa: E501
    parser.add_argument(
        "--dsn",
        default=os.environ.get("ALPHARD_PG_DSN"),
        help="Postgres DSN (falls back to $ALPHARD_PG_DSN)",  # noqa: E501
    )
    parser.add_argument(
        "--source",
        default="tkf",
        choices=["tkf", "moex"],
        help="Primary source: tkf (Tinkoff, default) or moex (MOEX ISS)",
    )
    parser.add_argument(
        "--mode",
        default="daily",
        choices=["daily", "weekly", "full", "universe"],
        help="daily=top20 5d; weekly=top20 7d; full=all TQBR 5y; universe=all TQBR N d",
    )
    parser.add_argument("--batch-sleep", type=float, default=0, help="Sleep between tickers (rate-limit)")  # noqa: E501
    parser.add_argument("--quality-gate", action="store_true", help="Run Ingestion Gate before upsert")  # noqa: E501
    parser.add_argument("--max-tickers", type=int, default=0, help="Limit number of tickers (0=all)")  # noqa: E501
    args = parser.parse_args()

    if not args.dsn:
        logger.error("ALPHARD_PG_DSN not set")
        return 1

    os.environ["ALPHARD_PG_DSN"] = args.dsn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.backfill > 0:
        end = date.today()
        start = end - timedelta(days=args.backfill)
        symbols = args.universe or LIQUID_UNIVERSE
    elif args.mode == "full":
        end = date.today()
        start = end - timedelta(days=5 * 365)  # 5 years
        symbols = None  # resolve from loader
    elif args.mode == "weekly":
        end = date.today()
        start = end - timedelta(days=7)
        symbols = args.universe or LIQUID_UNIVERSE
    elif args.mode == "universe":
        end = date.today()
        start = end - timedelta(days=args.days)
        symbols = None  # resolve from loader
    else:  # daily
        end = date.today()
        start = end - timedelta(days=args.days)
        symbols = args.universe or LIQUID_UNIVERSE

    logger.info(f"=== Sync: {args.source} {start} → {end} (mode={args.mode}) ===")

    # Loader selection
    loader: Any = None
    if args.source == "tkf":
        try:
            loader = TinkoffInvestDataLoader()
        except Exception as e:
            logger.error(f"Failed to init Tinkoff loader: {e}")
            return 2
    else:
        from src.data.moex_loader import MOEXDataLoader

        loader = MOEXDataLoader()

    # Resolve full universe if mode required (store is created below)
    if symbols is None:
        try:
            symbols = [m.ticker for m in loader.list_tickers()]
        except Exception as e:
            logger.error(f"Failed to list tickers: {e}")
            return 2

    if args.max_tickers > 0:
        symbols = symbols[: args.max_tickers]
    logger.info(f"=== Sync: {args.source} {start} → {end} (mode={args.mode}, {len(symbols)} tickers) ===")  # noqa: E501

    store = PostgresDataStore()

    # Resolve TickerMeta once
    try:
        meta_cache = {t.ticker: t for t in loader.list_tickers()}
    except Exception as e:
        logger.error(f"Failed to list tickers: {e}")
        store.close()
        return 2

    total_bars = 0
    errors = []

    try:
        from src.data.models import TickerMeta as _TickerMeta

        for i, symbol in enumerate(symbols, start=1):
            meta = meta_cache.get(symbol.upper())
            if meta is None:
                meta = _TickerMeta(
                    ticker=symbol,
                    name=symbol,
                    lot=1,
                    source=args.source,
                )

            try:
                if args.source == "tkf":
                    bars = loader.fetch_ohlcv(symbol, start, end)
                else:
                    bars = list(loader.iter_ohlcv(symbol, start, end))  # type: ignore[attr-defined]

                if not bars:
                    logger.debug(f"{i}/{len(symbols)} {symbol}: no bars")
                    continue

                store.upsert_ticker(meta)

                # Adapt loader output to OHLCVRow with source flags.
                # PK is now (ticker, ts); source flags track coverage.
                adapted: list[Any] = []
                for b in bars:
                    if hasattr(b, "primary_source"):  # already adapted
                        adapted.append(b)
                    elif hasattr(b, "source"):  # old shape
                        # Old OHLCVRow had .source; convert to new shape
                        from src.data.models import OHLCVRow as _OHLCV

                        adapted.append(
                            _OHLCV(
                                ticker=b.ticker,
                                ts=b.ts,
                                open=b.open,
                                high=b.high,
                                low=b.low,
                                close=b.close,
                                volume=b.volume,
                                adj_close=b.adj_close,
                                primary_source=b.source,
                                covered_by_tkf=(b.source == "tkf"),
                                covered_by_moex=(b.source == "moex"),
                            )
                        )
                    else:
                        # Bare dict or pydantic loader row — build from scratch
                        from src.data.models import OHLCVRow as _OHLCV

                        adapted.append(
                            _OHLCV(
                                ticker=getattr(b, "ticker", symbol),
                                ts=getattr(b, "ts", None),
                                open=getattr(b, "open", 0),
                                high=getattr(b, "high", 0),
                                low=getattr(b, "low", 0),
                                close=getattr(b, "close", 0),
                                volume=getattr(b, "volume", 0),
                                adj_close=getattr(b, "adj_close", getattr(b, "close", 0)),
                                primary_source=args.source,
                                covered_by_tkf=(args.source == "tkf"),
                                covered_by_moex=(args.source == "moex"),
                            )
                        )

                if args.quality_gate and args.source == "tkf":
                    from src.data.quality.ingestion_gate import check_ingestion, IngestionParams, Bar  # noqa: E501
                    from src.data.quality.severity import Severity

                    # check_ingestion expects list[Bar] where Bar.primary_key=date and
                    # OHLC fields are floats. Map our OHLCVRow to Bar.
                    bar_list = [
                        Bar(
                            primary_key=b.ts,
                            open=float(b.open),
                            high=float(b.high),
                            low=float(b.low),
                            close=float(b.close),
                            volume=int(b.volume),
                        )
                        for b in bars
                    ]
                    report = check_ingestion(symbol, bar_list, params=IngestionParams())
                    worst = report.worst_severity()
                    if worst == Severity.CRITICAL:
                        logger.warning(f"{i}/{len(symbols)} {symbol}: GATE_CRITICAL — {report.issues}")  # noqa: E501
                        continue
                    elif worst == Severity.HIGH:
                        logger.warning(
                            f"{i}/{len(symbols)} {symbol}: GATE_HIGH (skipped) — {report.issues}"
                        )  # noqa: E501
                        continue
                    elif worst is not None:
                        logger.debug(f"{i}/{len(symbols)} {symbol}: GATE_{worst.value} — {report.issues}")  # noqa: E501

                    written = store.upsert_ohlcv(bars)
                else:
                    written = store.upsert_ohlcv(bars)

                logger.info(f"{i}/{len(symbols)} {symbol}: {written} bars")
                total_bars += written

                if args.batch_sleep > 0:
                    import time

                    time.sleep(args.batch_sleep)
            except Exception as exc:
                logger.error(f"{i}/{len(symbols)} {symbol}: {exc}")
                errors.append((symbol, str(exc)))
    finally:
        store.close()

    logger.info(f"=== DONE: {total_bars} bars written, {len(errors)} errors ===")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
