"""Abstract ``DataLoader`` interface — Tinkoff / MOEX implement it.

Why an ABC?
-----------
1. Phase 1.3 will wire the Risk Agent to a ``DataLoader`` for live NAV /
   price checks. Swapping providers (Tinkoff sandbox vs. real token,
   Tinkoff vs. MOEX ISS, paper vs. live) is a one-line constructor swap
   if the interface is fixed.
2. Contract tests in ``tests/test_data_loader.py`` parameterise over
   ``FakeLoader`` and concrete classes — that's how we keep both honest
   about the same wire format.
3. Phase 2 will add a third loader (Finam / T-Bank backup). Without an
   ABC we'd duplicate the rate-limiter + retry logic.

Lifecycle
---------
- ``__init__`` validates config and constructs the rate-limiter. NO I/O.
- ``iter_ohlcv`` is a generator that paginates internally — callers can
  bail out on partial data without us buffering the whole year.
- ``list_tickers`` is one-shot and cached by the concrete loader.
- All HTTP errors are converted to ``LoaderError`` — never raw
  ``requests.RequestException`` — so callers only handle one exception
  hierarchy.
"""

from __future__ import annotations

import abc
from datetime import date, timedelta
from typing import Iterator

from .models import CorporateAction, OHLCVRow, TickerMeta
from .token_bucket import TokenBucket


class LoaderError(Exception):
    """Base for all data-loader failures. Wraps HTTP / parse / config errors."""


class LoaderAuthError(LoaderError):
    """Raised when credentials are missing or rejected (HTTP 401/403)."""


class LoaderRateLimitError(LoaderError):
    """Raised when the upstream rate-limits us (HTTP 429) after our bucket drained."""


class LoaderNotFoundError(LoaderError):
    """Raised when a ticker / date range is unknown to the upstream."""


class DataLoader(abc.ABC):
    """Abstract base class for OHLCV + corporate-action providers.

    Subclasses MUST implement the four methods marked ``@abc.abstractmethod``.
    The default rate-limiter is the constructor's responsibility — the ABC
    exposes it via ``self.bucket`` so callers / tests can drain it.
    """

    SOURCE: str = ""

    def __init__(self, *, bucket: TokenBucket | None = None) -> None:
        # Default: 60 r/s. Concrete loaders override per their SLA.
        self.bucket = bucket or TokenBucket(rate=60.0, window_seconds=1.0)

    # ------------------------------------------------------------------ API

    @abc.abstractmethod
    def list_tickers(self) -> list[TickerMeta]:
        """Return the full ticker universe (including delisted)."""

    @abc.abstractmethod
    def iter_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[OHLCVRow]:
        """Yield OHLCV bars between ``start`` (inclusive) and ``end`` (inclusive).

        Pagination happens internally. If ``start > end`` the iterator is
        empty (NOT an error). If the date range exceeds the upstream's
        retention window, ``LoaderNotFoundError`` is raised on the first
        offending page — partial results are NOT returned.
        """

    @abc.abstractmethod
    def iter_corporate_actions(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[CorporateAction]:
        """Yield corporate actions for ``ticker`` in ``[start, end]``.

        Splits are required for the backtester to compute ``adj_close``.
        Phase 1.1 only consumes ``kind='split'``; the other kinds are
        parsed and stored for Phase 2.
        """

    # ------------------------------------------------------------- helpers

    def load_ohlcv(self, ticker: str, start: date, end: date) -> list[OHLCVRow]:
        """Materialise the iterator into a list.

        Convenience for callers that don't care about streaming.
        """
        return list(self.iter_ohlcv(ticker, start, end))

    def load_corporate_actions(self, ticker: str, start: date, end: date) -> list[CorporateAction]:
        """Materialise corporate actions into a list."""
        return list(self.iter_corporate_actions(ticker, start, end))

    @staticmethod
    def _validate_range(start: date, end: date, *, max_lookback: timedelta) -> None:
        """Reject empty or absurdly large ranges up-front."""
        if start > end:
            raise LoaderError(f"start {start} > end {end}")
        if (end - start) > max_lookback:
            raise LoaderError(f"range {start}..{end} exceeds upstream max lookback {max_lookback.days}d")


__all__ = [
    "DataLoader",
    "LoaderAuthError",
    "LoaderError",
    "LoaderNotFoundError",
    "LoaderRateLimitError",
]
