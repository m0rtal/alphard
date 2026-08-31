"""MOEXDataLoader — MOEX ISS REST API, free, no auth required.

API surface used
----------------
- GET https://iss.moex.com/iss/engines/stock/markets/shares/securities.json
  — paginated ticker universe (TICKERS, lot, ISIN, status).
- GET https://iss.moex.com/iss/engines/stock/markets/shares/securities/
  {ticker}/candles.json?from=YYYY-MM-DD&till=YYYY-MM-DD&interval=24
  — daily OHLCV (open, high, low, close, value, volume).
  Returns at most 500 rows per page (next_page cursor).

Volume semantics
----------------
ISS ``volume`` is in *lots*. We multiply by the lot size from
``list_tickers()`` to expose shares (matches Tinkoff convention).

Corporate actions
-----------------
MOEX ISS does not have a first-class "corporate actions" endpoint for
equities; split history is reconstructed from the ``STATUS`` field on
the ticker endpoint and from delisting dates. Phase 1.1 ships the
minimum — yield ``CorporateAction(kind='change')`` for ticker renames
and ``kind='split'`` only if a SPLITFAC row appears (rare on MOEX).
Phase 2 will backfill from a public corporate-action CSV (Finlab /
Smart-Lab).
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterator, cast

import requests

from .loader import (
    DataLoader,
    LoaderAuthError,
    LoaderError,
    LoaderNotFoundError,
    LoaderRateLimitError,
)
from .models import CorporateAction, OHLCVRow, TickerMeta

logger = logging.getLogger(__name__)

# MOEX ISS policy: "reasonable rate". The de-facto community limit is
# 100 req/min. We enforce 30/min to be a good neighbour and to leave
# headroom for concurrent consumers.
DEFAULT_RATE_PER_MIN = 30

# MOEX candles API caps a page at 500 rows; we use the same cap.
DEFAULT_PAGE_SIZE = 500

# ISS retains ~5 years of daily candles reliably. Anything older is a
# LoaderNotFoundError rather than a silent empty result.
MAX_LOOKBACK = timedelta(days=5 * 365)

BASE_URL = "https://iss.moex.com"


class MOEXDataLoader(DataLoader):
    """Synchronous loader for MOEX ISS REST."""

    SOURCE = "moex"

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_sec: float = 30.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        rate_per_min: float = DEFAULT_RATE_PER_MIN,
    ) -> None:
        from .token_bucket import TokenBucket

        super().__init__(bucket=TokenBucket(rate=rate_per_min, window_seconds=60.0))
        self._session = session or requests.Session()
        self._timeout = timeout_sec
        self._page_size = page_size
        # Cache of ticker universe — list_tickers is one-shot but called
        # many times per session (store init, scheduler, etc.).
        self._universe_cache: list[TickerMeta] | None = None
        self._board_filter: str | None | bool = None  # None|True if cached without filter
        # BUGFIX (issue #193): lock around the cache fill so two threads
        # calling ``list_tickers(board_id)`` concurrently don't both walk
        # through ``_fetch_all_rows(...)`` (which costs a self.bucket slot
        # per call), race on the cache assignment, and silently discard
        # the slow builder's results. Mirrors the per-category lock
        # pattern from issue #175 in the sibling file
        # ``src/data/tinkoff_loader.py:195-204`` and the universe_lock
        # from issue #152 in ``src/data/tinkoff_md_loader.py:252``.
        # Production today is sequential per-loader-instance, so the bug
        # is latent — no live failure — but ``fallback_loader.py:68``
        # composes MOEX into chains and Phase 2.6 may parallelise the
        # chain, at which point the race becomes live. The
        # ``self._board_filter == board_id`` invariant from issue #162
        # stays inside both guards (cache-set AND filter-match), because
        # a mismatched board_id must still force a refetch.
        self._universe_lock = threading.Lock()

    # --------------------------------------------------------------- public

    def list_tickers(self, board_id: str | None = "TQBR") -> list[TickerMeta]:
        """Return MOEX share universe, optionally filtered by board_id.

        Default ``board_id="TQBR"`` returns ~770 live + ~1157 archived TQBR
        tickers (1927 total). Pass ``None`` for all boards.

        Caching contract
        ----------------
        The cache key is the *exact* ``board_id`` value the cache was
        filled with. A subsequent call with a different ``board_id``
        (including None vs. a string) forces a refetch.

        Issue #162: the previous version used the short-circuit
        ``board_id is None or self._board_filter is None`` as a
        cache-hit guard, which silently returned the wrong (cached)
        list whenever the requested ``board_id`` and the cached
        ``_board_filter`` differed. The fix is a single equality
        comparison — any mismatch refetches.

        Issue #193: the read at ``self._universe_cache is not None``
        and the write at ``self._universe_cache = out`` were not
        protected by a lock, so two concurrent first-time callers
        both walked through ``_fetch_all_rows(...)`` (duplicate HTTP
        traffic, duplicate bucket-slot consumption, slow builder's
        results silently discarded). Fixed with double-checked
        locking around the fill — same pattern as issue #175 in
        ``tinkoff_loader.py:208-219``.
        """
        if self._universe_cache is not None and self._board_filter == board_id:
            return self._universe_cache
        with self._universe_lock:
            if self._universe_cache is not None and self._board_filter == board_id:
                return self._universe_cache
            url = f"{BASE_URL}/iss/engines/stock/markets/shares/securities.json"
            rows = self._fetch_all_rows(url, columns_metadata_key="securities")
            out: list[TickerMeta] = []
            for row in rows:
                # MOEX ISS returns one row per (secid, boardid). Filter by board.
                if board_id is not None and row.get("BOARDID") != board_id:
                    continue
                meta = self._row_to_ticker_meta(row)
                if meta is not None:
                    out.append(meta)
            self._universe_cache = out
            self._board_filter = board_id
            return out

    def iter_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[OHLCVRow]:
        self._validate_range(start, end, max_lookback=MAX_LOOKBACK)
        ticker = ticker.upper().strip()
        # Routing: ISIN-prefixed tickers go to the bonds history endpoint.
        # MOEX ISS serves bonds under /iss/history/engines/stock/markets/bonds/
        # whereas shares are under /iss/engines/stock/markets/shares/. The
        # two endpoints have different shapes (different columns, different
        # pagination contract) so we dispatch by ticker prefix.
        if self._looks_like_isin(ticker):
            yield from self._iter_ohlcv_bonds(ticker, start, end)
            return
        yield from self._iter_ohlcv_shares(ticker, start, end)

    @staticmethod
    def _looks_like_isin(ticker: str) -> bool:
        """True when ticker starts with an MOEX bond ISIN prefix (``SU`` or ``RU``).

        ``SU...`` is the legacy Soviet-era prefix still in use for some
        corporate and government bonds (e.g. ``SU46020RMFS2`` — ОФЗ 46020).
        ``RU...`` is the modern ISIN prefix for OFZ and corporate bonds
        (e.g. ``RU000A100FE5``).
        """
        return ticker.startswith("SU") or ticker.startswith("RU")

    def _iter_ohlcv_bonds(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[OHLCVRow]:
        """Iterate daily OHLCV via the MOEX ISS bonds history endpoint.

        Endpoint::
            GET https://iss.moex.com/iss/history/engines/stock/markets/
                bonds/securities/{secid}.json?from=YYYY-MM-DD&till=YYYY-MM-DD
                &start=N

        Pagination uses ``?start=N`` offset; we stop when a page returns
        fewer than ``_page_size`` rows (consistent with the shares branch).
        Returns ``LoaderNotFoundError`` on 404 (ticker is not a bond) so
        the FallbackDataLoader can record it and fall through.

        Lot size is irrelevant for bonds (bond denomination is captured
        via ``FACEVALUE`` in the securities endpoint, not in the history
        endpoint); we emit bars with ``volume == lot_count`` and skip the
        lot-multiplication step. The shares branch's ``_lot_for()`` is
        also skipped because the bonds endpoint has no BOARDID/TQBR
        dependency and the shares-universe cache doesn't cover bonds.
        """
        self._validate_range(start, end, max_lookback=MAX_LOOKBACK)
        bonds_prefix = f"{BASE_URL}/iss/history/engines/stock/markets/bonds/securities/"
        url = f"{bonds_prefix}{urllib.parse.quote(ticker)}.json"
        page = 0
        while True:
            params = {
                "from": start.isoformat(),
                "till": end.isoformat(),
                "start": page * self._page_size,
            }
            payload = self._get_json(url, params=params)
            history = self._extract_block(payload, "history")
            if not history.get("columns"):
                return
            data = self._rows_from_block(history)
            if not data:
                return
            for row in data:
                bar = self._row_to_ohlcv_bonds(ticker, row)
                if bar is not None and start <= bar.ts <= end:
                    yield bar
            if len(data) < self._page_size:
                return
            page += 1

    def _iter_ohlcv_shares(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[OHLCVRow]:
        """Iterate daily OHLCV via the MOEX ISS shares candles endpoint.

        Endpoint::
            GET https://iss.moex.com/iss/engines/stock/markets/shares/
                securities/{ticker}/candles.json?from=YYYY-MM-DD
                &till=YYYY-MM-DD&interval=24&start=N

        Pagination uses ``?start=N`` offset. ``_page_size`` defaults to
        ``DEFAULT_PAGE_SIZE`` (500).
        """
        self._validate_range(start, end, max_lookback=MAX_LOOKBACK)
        lot = self._lot_for(ticker)
        page = 0
        while True:
            params = {
                "from": start.isoformat(),
                "till": end.isoformat(),
                "interval": "24",
                "start": page * self._page_size,
            }
            url = (
                f"{BASE_URL}/iss/engines/stock/markets/shares/securities/"
                f"{urllib.parse.quote(ticker)}/candles.json"  # noqa: E501
            )
            payload = self._get_json(url, params=params)
            candles = self._extract_block(payload, "candles")
            if not candles.get("columns"):
                # Empty response — no data in this page. We've run out.
                return
            data = self._rows_from_block(candles)
            if not data:
                return
            for row in data:
                bar = self._row_to_ohlcv(ticker, lot, row)
                if bar is not None and start <= bar.ts <= end:
                    yield bar
            if len(data) < self._page_size:
                return
            page += 1

    def iter_corporate_actions(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[CorporateAction]:
        # Phase 1.1 stub: MOEX has no clean corporate-action endpoint,
        # but we DO emit a synthetic 'change' event when a ticker is
        # delisted within the range, so the backtester can flag the
        # ticker as no longer tradeable.
        self._validate_range(start, end, max_lookback=MAX_LOOKBACK)
        ticker = ticker.upper().strip()
        meta = self._meta_for(ticker)
        if meta is None:
            return
        if meta.delisted and meta.delisted_at and start <= meta.delisted_at <= end:
            yield CorporateAction(
                ticker=ticker,
                ts=meta.delisted_at,
                kind="change",
                value=Decimal("0"),
                source="moex",
            )

    # ------------------------------------------------------------ internal

    def _lot_for(self, ticker: str) -> int:
        meta = self._meta_for(ticker)
        if meta is None:
            # Unknown ticker — assume 1 to avoid producing zero-volume
            # bars. The caller will see a LoaderNotFoundError on store.
            return 1
        return meta.lot

    def _meta_for(self, ticker: str) -> TickerMeta | None:
        # list_tickers() returns the cached universe; for tests that pass
        # board_id=None, the cached list contains test tickers without BOARDID.
        for m in self.list_tickers():
            if m.ticker == ticker:
                return m
        return None

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.bucket.acquire()
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
        except requests.RequestException as exc:
            raise LoaderError(f"network error fetching {url}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise LoaderAuthError(f"auth failed for {url}: HTTP {resp.status_code}")
        if resp.status_code == 404:
            raise LoaderNotFoundError(f"not found: {url}")
        if resp.status_code == 429:
            raise LoaderRateLimitError(f"rate limited: {url}")
        if not resp.ok:
            raise LoaderError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")
        try:
            return cast(dict[str, Any], resp.json())
        except json.JSONDecodeError as exc:
            raise LoaderError(f"non-JSON response from {url}: {exc}") from exc

    def _fetch_all_rows(self, url: str, *, columns_metadata_key: str) -> list[dict[str, Any]]:
        """Fetch a metadata-style payload and return rows as dicts.

        ISS metadata responses look like::

            { "<block>": { "columns": [...], "data": [[...], ...] } }

        We grab the first non-empty block that has a ``columns`` array.
        """
        payload = self._get_json(url)
        # ISS puts everything under the requested block name; tolerate
        # older payloads where the wrapper key is omitted.
        block = payload.get(columns_metadata_key) or self._first_block_with_columns(payload)
        if block is None:
            return []
        return self._rows_from_block(block)

    def _first_block_with_columns(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        for v in payload.values():
            if isinstance(v, dict) and v.get("columns"):
                return v
        return None

    def _extract_block(self, payload: dict[str, Any], name: str) -> dict[str, Any]:
        block = payload.get(name)
        if not isinstance(block, dict):
            raise LoaderError(f"missing block {name!r} in ISS response")
        return block

    @staticmethod
    def _rows_from_block(block: dict[str, Any]) -> list[dict[str, Any]]:
        cols = block.get("columns") or []
        data = block.get("data") or []
        # If the server returned rows but no column metadata, we have no way
        # to map positions → names — return [] rather than a list of empty
        # dicts (which would silently swallow the data downstream).
        if not cols:
            return []
        out: list[dict[str, Any]] = []
        for row in data:
            if not isinstance(row, list):
                continue
            out.append({c: row[i] if i < len(row) else None for i, c in enumerate(cols)})
        return out

    # ----------------------------------------------------- row -> model

    @staticmethod
    def _row_to_ticker_meta(row: dict[str, Any]) -> TickerMeta | None:
        try:
            secid = row.get("SECID")
            if not secid or not isinstance(secid, str):
                return None
            lot_raw = row.get("LOTSIZE") or 1
            lot = int(lot_raw) if lot_raw else 1
            name = row.get("SHORTNAME") or row.get("SECNAME") or secid
            isin = row.get("ISIN")
            status = (row.get("STATUS") or "").upper()
            delisted = status in ("DELISTED", "EXCLUDED", "HALTED")
            # ISS does not expose listed/delisted dates in this endpoint;
            # we leave them as None — Phase 1.3 will pull from the
            # delisting_log table seeded by Finlab.
            return TickerMeta(
                ticker=secid.upper(),
                figi=None,
                name=str(name),
                lot=lot,
                isin=str(isin) if isin else None,
                currency="RUB",
                delisted=delisted,
                delisted_at=None,
                listed_at=None,
                source="moex",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed MOEX ticker row %r: %s", row, exc)
            return None

    @staticmethod
    def _row_to_ohlcv(
        ticker: str,
        lot: int,
        row: dict[str, Any],
    ) -> OHLCVRow | None:
        try:
            # ISS candles: open, close, high, low, value, volume, begin, end.
            # ``begin`` is ISO timestamp string; we keep the date portion.
            ts_raw = row.get("begin") or row.get("tradedate") or row.get("TRADEDATE")
            if not ts_raw:
                return None
            ts_str = str(ts_raw)[:10]  # 'YYYY-MM-DD'
            ts = date.fromisoformat(ts_str)
            o = _d(row.get("open") or row.get("OPEN"))
            h = _d(row.get("high") or row.get("HIGH"))
            lo = _d(row.get("low") or row.get("LOW"))
            c = _d(row.get("close") or row.get("CLOSE"))
            # Issue #364: NUMTRADES is *count of executed trades*, not traded
            # volume. Falling back to it when VOLUME is 0/absent silently
            # fabricates volume values for illiquid sessions, last-trade-day
            # entries, and delisted-ticker trailing bars. Drop NUMTRADES from
            # the chain — accurate silence (0) is better than a wrong value.
            vol_raw = _d(row.get("volume") or row.get("VOLUME"))
            # volume is lots; multiply by lot size.
            vol_shares = vol_raw * Decimal(lot)
            return OHLCVRow(
                ticker=ticker,
                ts=ts,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=vol_shares,
                adj_close=c,  # Phase 1.1: no split adjustments for MOEX
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed MOEX OHLCV row %r: %s", row, exc)
            return None

    @staticmethod
    def _row_to_ohlcv_bonds(
        ticker: str,
        row: dict[str, Any],
    ) -> OHLCVRow | None:
        """Map a single row from the bonds history endpoint to ``OHLCVRow``.

        The bonds endpoint returns UPPERCASE column names (``TRADEDATE``,
        ``OPEN``, ``HIGH``, ``LOW``, ``CLOSE``, ``VOLUME``, ``NUMTRADES``).
        We look up either case (the shares branch uses lowercase) so a
        future column rename on the ISS side doesn't break parsing.

        Volume semantics for bonds: ISS reports ``VOLUME`` as a count of
        *paper units* traded (not number of trades, not RUB volume —
        ``VALUE`` and ``NUMTRADES`` cover those). We pass it through
        without lot-multiplication (bonds are 1-paper-per-record at the
        ISS level; ``FACEVALUE`` handles par-value scaling at the
        portfolio level).
        """
        try:
            ts_raw = row.get("TRADEDATE") or row.get("tradedate")
            if not ts_raw:
                return None
            ts_str = str(ts_raw)[:10]  # 'YYYY-MM-DD'
            ts = date.fromisoformat(ts_str)
            o = _d(row.get("OPEN") or row.get("open"))
            h = _d(row.get("HIGH") or row.get("high"))
            lo = _d(row.get("LOW") or row.get("low"))
            c = _d(row.get("CLOSE") or row.get("close"))
            # Issue #364: NUMTRADES is *count of executed trades*, not paper
            # units traded. Substituting it when VOLUME is 0/absent silently
            # fabricates bond volume values for illiquid sessions. Drop
            # NUMTRADES from the chain — accurate silence (0) beats wrong.
            vol_raw = _d(row.get("VOLUME") or 0)
            return OHLCVRow(
                ticker=ticker,
                ts=ts,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=vol_raw,
                adj_close=c,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed MOEX bonds OHLCV row %r: %s", row, exc)
            return None


def _d(v: Any) -> Decimal:
    """Coerce a value to Decimal. None → 0. Strings parsed via Decimal()."""
    if v is None or v == "":
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


__all__ = ["MOEXDataLoader", "DEFAULT_PAGE_SIZE", "DEFAULT_RATE_PER_MIN"]
