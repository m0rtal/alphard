"""Multi-source OHLCV loader.

The primary production backfill (backfill_history_md.py) bootstraps the
Postgres OHLCV universe. Sources are tried in order, falling back to
the next when a source raises (no rows / network error / quota
exceeded). The order is:

    1. Tinkoff Invest ``history-data`` HTTP endpoint
       URL: https://invest-public-api.tinkoff.ru/history-data?figi=…&year=…
       Returns minute bars in a ZIP of CSVs that we aggregate to daily.
       Best for: long history (2018–present), no auth quota cost.
       Same-day support: only yesterday's archive is published, so the
       *current* day's bar must come from elsewhere.

    2. Tinkoff Invest ``GetCandles`` gRPC (broker)
       Best for: most-recent 1-2 days, authoritative live prices.
       Costs: 30 req/min (token bucket shared with history-data).
       6y cap per request, paginated.

    3. MOEX ISS REST (free public)
       Best for: tickers Tinkoff doesn't expose (most bonds and ETFs),
       older history (pre-2018, anything Tinkoff MD-archive doesn't
       cover), and as a final fallback for tickers both Tinkoff
       endpoints failed on.
       Costs: 500-bar pagination, no auth.

The same data-quality gate from ``src.data.quality.validate`` runs
after each source returns its rows. CRITICAL issues reject the batch;
WARNING issues are logged.

Why this is structured as ``chain
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Iterator

from src.data.models import OHLCVRow

logger = logging.getLogger("alphard.fallback_loader")


# Ordered list of fallback strategies. Per the 2026-08-29 contract
# change (issue #331, m0rtal): broker gRPC is now FIRST because it is
# the canonical live source and has full depth (back to
# ``first_1day_candle_date``). MOEX ISS is the backup when broker is
# unreachable. The old Tinkoff history-data HTTP archive
# (``/history-data``) is REMOVED from the chain because that endpoint
# is deprecated and only contains the last ~2y for most FIGI — for any
# ticker listed before ~2024 it returns 0 bytes and silently burned a
# network round-trip per year before falling through.
FALLBACK_ORDER: tuple[str, ...] = ("tinkoff_grpc", "moex_iss")


class FallbackDataLoader:
    """Multi-source OHLCV loader with automatic fallback chain.

    Wraps two production loaders behind a single ``iter_ohlcv`` /
    ``list_tickers`` interface that mirrors the per-source loaders
    already used by ``backfill_history_md.py`` and ``daily_sync.py``.

    Contract change 2026-08-29 (issue #331, m0rtal): the chain is now
    ``tinkoff_grpc → moex_iss``. The Tinkoff history-data HTTP archive
    (``/history-data``) is no longer wired into the chain. The legacy
    ``tinkoff_md`` parameter is preserved as a deprecated keyword-only
    argument for backward compatibility with code that still imports
    ``TinkoffInvestMDDataLoader``; it is stored on the instance but
    is **never** tried as part of the chain.

    Parameters
    ----------
    tinkoff_grpc
        ``TinkoffInvestDataLoader`` instance (broker gRPC). Required.
    moex_iss
        ``MOEXDataLoader`` instance. Required.
    order
        Tuple of source names in the order they should be tried.
        Defaults to ``FALLBACK_ORDER``.
    tinkoff_md
        Deprecated. Kept as a keyword-only argument so legacy callers
        that still pass ``tinkoff_md=...`` don't raise; ignored.
    """

    def __init__(
        self,
        tinkoff_grpc: Any,
        moex_iss: Any,
        order: tuple[str, ...] = FALLBACK_ORDER,
        *,
        tinkoff_md: Any = None,  # deprecated, kept for back-compat; no longer in chain
    ) -> None:
        self.tinkoff_md = tinkoff_md
        self.tinkoff_grpc = tinkoff_grpc
        self.moex_iss = moex_iss
        self.order = order
        self._stats: dict[str, dict[str, int]] = {
            "tinkoff_grpc": {"ok": 0, "fallback": 0, "error": 0},
            "moex_iss": {"ok": 0, "fallback": 0, "error": 0},
        }

    @property
    def stats(self) -> dict[str, dict[str, int]]:
        """Per-source success / fallback / error counts for the run."""
        return self._stats

    # -- universe --------------------------------------------------------

    def list_tickers(self) -> list[Any]:  # noqa: D401
        """Resolve ticker universe from the broker-first fallback chain.

        Contract change 2026-08-29 (issue #331, m0rtal): the chain is now
        ``tinkoff_grpc → moex_iss``. The Tinkoff history-data HTTP archive
        (``/history-data``) is removed from the universe-discovery path
        because that endpoint is deprecated and exposes no ticker-listing
        API — ``tinkoff_md.list_tickers_with_figi()`` was actually a
        wrapper that delegated to broker gRPC anyway (see
        ``src/data/tinkoff_md_loader.py:_fill_universe_cache``), so
        including it as a separate chain step just created an extra
        round-trip that returned the same data the broker gRPC step
        would have returned on its own.

        Per source:

          1. tinkoff_grpc (broker gRPC live data). ``list_tickers()``
             walks every tradable class (TQBR + SPBXM + TQCB + TQOB +
             ...) and returns ~3259 tickers with FIGI. Single point of
             failure: token health. The first non-empty result wins.
          2. moex_iss (no auth, MOEX web endpoint, last resort). Returns
             the same ``TickerMeta`` shape.

        ``tinkoff_md`` is preserved on the instance as an attribute (for
        ``backfill_history_md.py`` which still uses it as a stand-alone
        loader for one-off downloads), but it is no longer wired into
        the chain. The ``_stats`` dict no longer contains a
        ``tinkoff_md`` slot.

        If both sources fail the function returns ``[]`` and the
        supervisor treats this as a clean exit; the per-source error
        counts show up in logs and Prometheus so the operator can
        diagnose.
        """
        # 1. tinkoff_grpc (broker gRPC; same REAL token as daily_sync).
        try:
            metas = self.tinkoff_grpc.list_tickers()
            if metas:
                self._stats["tinkoff_grpc"]["ok"] += 1
                logger.info(
                    f"FallbackDataLoader.list_tickers: tinkoff_grpc OK ({len(metas)} tickers)"
                )
                return list(metas)
            self._stats["tinkoff_grpc"]["fallback"] += 1
            logger.warning(
                "FallbackDataLoader.list_tickers: tinkoff_grpc returned 0 tickers, trying moex_iss"
            )
        except Exception as e:
            self._stats["tinkoff_grpc"]["error"] += 1
            logger.warning(
                f"FallbackDataLoader.list_tickers: tinkoff_grpc failed ({type(e).__name__}: {e}); trying moex_iss"
            )

        # 2. moex_iss (no auth, MOEX web endpoint, 1825d cap, last resort).
        try:
            metas = self.moex_iss.list_tickers()
            if metas:
                self._stats["moex_iss"]["ok"] += 1
                logger.info(
                    f"FallbackDataLoader.list_tickers: moex_iss OK ({len(metas)} tickers)"
                )
                return list(metas)
            self._stats["moex_iss"]["fallback"] += 1
            logger.warning("FallbackDataLoader.list_tickers: moex_iss returned 0 tickers")
        except Exception as e:
            self._stats["moex_iss"]["error"] += 1
            logger.warning(
                f"FallbackDataLoader.list_tickers: moex_iss failed ({type(e).__name__}: {e})"
            )

        logger.error(
            "FallbackDataLoader.list_tickers: ALL SOURCES returned 0 tickers or failed"
        )
        return []

    # -- OHLCV ------------------------------------------------------------

    def iter_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[OHLCVRow]:
        """Yield OHLCV bars for ``ticker`` in ``[start, end]``.

        Tries each source in order. If a source returns zero rows or
        raises, falls through to the next. Stops at the first source
        that returned a non-empty result.
        """
        for source_name in self.order:
            source = self._resolve(source_name)
            if source is None:
                continue
            try:
                rows = list(source.iter_ohlcv(ticker, start, end))
            except Exception as exc:  # noqa: BLE001 — fallback contract
                logger.warning(
                    f"{ticker}: {source_name} raised {type(exc).__name__}: {exc}; " f"falling back to next source"
                )
                self._stats[source_name]["error"] += 1
                self._stats[source_name]["fallback"] += 1
                continue
            if rows:
                logger.info(f"{ticker}: {source_name} returned {len(rows)} bars")
                self._stats[source_name]["ok"] += 1
                yield from rows
                return
            logger.info(f"{ticker}: {source_name} returned 0 bars; falling back")
            self._stats[source_name]["fallback"] += 1
        logger.warning(f"{ticker}: ALL sources returned 0 bars")

    def _resolve(self, name: str) -> Any:
        if name == "tinkoff_grpc":
            return self.tinkoff_grpc
        if name == "moex_iss":
            return self.moex_iss
        return None

    # -- corporate actions ------------------------------------------------

    def iter_corporate_actions(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[Any]:
        """Corporate actions = same fallback contract as OHLCV. Tinkoff
        gRPC is authoritative; MOEX ISS fills gaps for tickers that
        only MOEX knows about (most bonds/ETFs delisted).
        """
        for source_name in self.order:
            source = self._resolve(source_name)
            if source is None or not hasattr(source, "iter_corporate_actions"):
                continue
            try:
                rows = list(source.iter_corporate_actions(ticker, start, end))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{ticker} corp-actions {source_name}: {type(exc).__name__}: {exc}; falling back")
                continue
            if rows:
                yield from rows
                return
