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


# Ordered list of fallback strategies. Tinkoff MD first because it has
# the longest history at the lowest quota cost; Tinkoff gRPC next
# because it's the broker-authoritative live source; MOEX ISS last
# because pagination is slow but covers everything else.
FALLBACK_ORDER: tuple[str, ...] = ("tinkoff_md", "tinkoff_grpc", "moex_iss")


class FallbackDataLoader:
    """Multi-source OHLCV loader with automatic fallback chain.

    Wraps three existing loaders behind a single ``iter_ohlcv`` /
    ``list_tickers`` interface that mirrors the per-source loaders
    already used by ``backfill_history_md.py`` and ``daily_sync.py``.

    Parameters
    ----------
    tinkoff_md
        ``TinkoffInvestMDDataLoader`` instance (history-data HTTP).
        Required.
    tinkoff_grpc
        ``TinkoffInvestDataLoader`` instance (broker gRPC).
        Required.
    moex_iss
        ``MOEXDataLoader`` instance. Required.
    order
        Tuple of source names in the order they should be tried.
        Defaults to ``FALLBACK_ORDER``.
    """

    def __init__(
        self,
        tinkoff_md: Any,
        tinkoff_grpc: Any,
        moex_iss: Any,
        order: tuple[str, ...] = FALLBACK_ORDER,
    ) -> None:
        self.tinkoff_md = tinkoff_md
        self.tinkoff_grpc = tinkoff_grpc
        self.moex_iss = moex_iss
        self.order = order
        self._stats: dict[str, dict[str, int]] = {
            "tinkoff_md": {"ok": 0, "fallback": 0, "error": 0},
            "tinkoff_grpc": {"ok": 0, "fallback": 0, "error": 0},
            "moex_iss": {"ok": 0, "fallback": 0, "error": 0},
        }

    @property
    def stats(self) -> dict[str, dict[str, int]]:
        """Per-source success / fallback / error counts for the run."""
        return self._stats

    # -- universe --------------------------------------------------------

    def list_tickers(self) -> list[Any]:  # noqa: D401
        """Live universe = full Tinkoff MD universe (already includes
        shares + bonds + ETFs from gRPC). Used as the primary source
        for the ticker list; the per-year fallback chain only affects
        how bars are fetched.
        """
        metas = self.tinkoff_md.list_tickers_with_figi()
        return list(metas)

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
        if name == "tinkoff_md":
            return self.tinkoff_md
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
