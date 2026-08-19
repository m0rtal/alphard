#!/usr/bin/env python3
"""Daily sync: pull last N days for top MOEX tickers into Postgres.

Tinkoff Invest gRPC is the PRIMARY source (broker-authoritative).
MOEX ISS REST is reserved for backfill pre-2010 (when Tinkoff API
may not have data) and as a cross-source validation fallback.

Used by cron. Idempotent: upsert on (ticker, ts, source) PK.

Universe (Phase 1.1): top 20 liquid MOEX TQBR shares — bootstrap for
the cross-sectional ML pipeline. Phase 3 will expand to full MOEX.

Side effect on success: ``daily_sync`` re-runs the same completion
formula that ``backfill_history_md`` uses and flips
``ticker_universe.backfill_complete = TRUE`` for any ticker that
now satisfies it. This is what tells the ML/training layer the
ticker is safe to consume. Same logic lives in
``scripts/backfill_history_md._is_complete`` — we re-import it
rather than duplicate.
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
    parser.add_argument("--max-tickers", type=int, default=0, help="Limit number of tickers (0/all)")  # noqa: E501
    parser.add_argument(
        "--min-bars",
        type=int,
        default=1300,
        help="If MD-backfill loader is selected, skip tickers with >= N bars already.",  # noqa: E501
    )
    parser.add_argument(
        "--prefer-md-backfill",
        action="store_true",
        help="Use TinkoffInvestMDDataLoader (history-data ZIPs aggregated to daily) "
        "for tickers short on history, then gRPC for the rest. "
        "This is the production path; the default 'tkf' source remains "
        "the gRPC-only path for incremental updates.",
    )

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

    # Lazy MD loader: only created if --prefer-md-backfill is on.
    md_loader: Any = None
    if args.prefer_md_backfill:
        try:
            from src.data.tinkoff_md_loader import TinkoffInvestMDDataLoader

            md_loader = TinkoffInvestMDDataLoader()
            logger.info("MD backfill loader enabled (TinkoffInvestMDDataLoader)")
        except Exception as e:
            logger.error(f"MD loader init failed (falling back to gRPC): {e}")
            md_loader = None

    # Resolve TickerMeta once
    try:
        meta_cache = {t.ticker: t for t in loader.list_tickers()}
    except Exception as e:
        logger.error(f"Failed to list tickers: {e}")
        store.close()
        return 2

    total_bars = 0
    errors = []
    md_used_count = 0  # BUGFIX (H-6): track how many tickers went through MD archive

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
                # MD-backfill path: if the ticker has < min_bars daily
                # rows in DB AND the MD loader is enabled, fill the gap
                # with the archive BEFORE incremental gRPC.
                used_md = False
                if md_loader is not None and store.count_ohlcv(symbol) < args.min_bars:
                    md_start = date(2018, 1, 1)
                    md_end = end
                    md_bars = list(md_loader.iter_ohlcv(symbol, md_start, md_end))
                    if md_bars:
                        store.upsert_ohlcv(md_bars)
                        logger.info(
                            f"{i}/{len(symbols)} {symbol}: "
                            f"MD backfill +{len(md_bars)} bars (archive 2018→{md_end.year})"
                        )
                        used_md = True
                # BUGFIX (H-6): log when MD was used so the summary shows
                # how many tickers actually went through the archive path.
                if used_md:
                    md_used_count += 1

                if args.source == "tkf":
                    bars = loader.fetch_ohlcv(symbol, start, end)
                else:
                    bars = list(loader.iter_ohlcv(symbol, start, end))

                if not bars:
                    logger.debug(f"{i}/{len(symbols)} {symbol}: no bars")
                    continue

                store.upsert_ticker(meta)

                # BUGFIX (H-5): removed the dead `adapted` block that
                # rebuilt OHLCVRow from dicts. `bars` is already a list
                # of native OHLCVRow objects (Tinkoff loader + MD loader
                # both return OHLCVRow), so passing them straight to
                # ``store.upsert_ohlcv(bars)`` is correct.

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

                # Mark complete if daily-sync topped the ticker up to
                # the expected history range. The same formula the
                # backfill uses, so the two pieces of code agree on
                # what "complete" means. Idempotent: flipping an
                # already-TRUE flag is a no-op.
                try:
                    from scripts.backfill_history_md import _is_complete

                    if _is_complete(store, symbol, args.min_bars):
                        store.mark_backfill_complete(symbol, complete=True)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"could not flip backfill_complete for {symbol}: {exc}")

                if args.batch_sleep > 0:
                    import time

                    time.sleep(args.batch_sleep)
            except Exception as exc:
                logger.error(f"{i}/{len(symbols)} {symbol}: {exc}")
                errors.append((symbol, str(exc)))
    finally:
        store.close()

    logger.info(
        f"=== DONE: {total_bars} bars written, {len(errors)} errors, " f"md_archive_used={md_used_count} tickers ==="
    )

    # Phase 1.6 audit: stamp the watchdog sentinel so src.main's
    # in-process watchdog can detect a stuck daily_sync daemon thread.
    # On success: status='ok'. On non-zero exit: status='failed' with
    # the first error message attached. timeout case is handled by the
    # caller (subprocess.run timeout) — that path also exits non-zero
    # and falls into the failure branch.
    try:
        if not errors:
            store.record_daily_sync_run(
                status="ok",
                bars=total_bars,
                tickers=len(symbols),
            )
        else:
            first_err = errors[0][1] if errors else "unknown"
            store.record_daily_sync_run(
                status="failed",
                bars=total_bars,
                tickers=len(symbols) - len(errors),
                error=f"{len(errors)} tickers failed; first: {first_err}",
            )
    except Exception as exc:  # noqa: BLE001
        # Sentinel write failure must NOT mask the run result. Just log
        # and let the run return code speak for itself.
        logger.warning(f"could not stamp _daily_sync_health: {exc}")

    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
