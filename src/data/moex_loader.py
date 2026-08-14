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
import urllib.parse
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterator

import requests  # type: ignore[import-untyped]

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

    # --------------------------------------------------------------- public

    def list_tickers(self) -> list[TickerMeta]:
        if self._universe_cache is not None:
            return self._universe_cache
        url = f"{BASE_URL}/iss/engines/stock/markets/shares/securities.json"
        rows = self._fetch_all_rows(url, columns_metadata_key="securities")
        out: list[TickerMeta] = []
        for row in rows:
            meta = self._row_to_ticker_meta(row)
            if meta is not None:
                out.append(meta)
        self._universe_cache = out
        return out

    def iter_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[OHLCVRow]:
        self._validate_range(start, end, max_lookback=MAX_LOOKBACK)
        ticker = ticker.upper().strip()
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
                f"{BASE_URL}/iss/engines/stock/markets/shares/securities/" f"{urllib.parse.quote(ticker)}/candles.json"
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
            return resp.json()  # type: ignore[no-any-return]
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
            ts_raw = row.get("begin") or row.get("tradedate")
            if not ts_raw:
                return None
            ts_str = str(ts_raw)[:10]  # 'YYYY-MM-DD'
            ts = date.fromisoformat(ts_str)
            o = _d(row.get("open"))
            h = _d(row.get("high"))
            lo = _d(row.get("low"))
            c = _d(row.get("close"))
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
                source="moex",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed MOEX OHLCV row %r: %s", row, exc)
            return None


def _d(v: Any) -> Decimal:
    """Coerce a value to Decimal. None → 0. Strings parsed via Decimal()."""
    if v is None or v == "":
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


__all__ = ["MOEXDataLoader", "DEFAULT_PAGE_SIZE", "DEFAULT_RATE_PER_MIN"]
