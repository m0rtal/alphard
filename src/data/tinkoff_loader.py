"""TinkoffDataLoader — T-Bank Invest REST API, OAuth token required.

API surface used
----------------
- POST https://invest-public-api.tinkoff.ru/rest/tinkoff.public.invest.api.contract.v1.
  InstrumentsService/Shares — paginated share universe (ticker, FIGI, lot, ISIN).
- POST .../MarketDataService/GetCandles — daily candles (figi, from, to, interval=DAY).
- POST .../InstrumentsService/GetDividends — dividend history (used for Phase 2
  total-return index). Phase 1.1 only consumes it if available; falls back
  gracefully otherwise.

Why REST and not the ``tinkoff-investments`` SDK?
-------------------------------------------------
Phase 1.1 budget is stdlib + requests + pydantic — pulling the gRPC SDK
would add 15+ transitive deps (grpcio, protobuf, ...) and pin us to a
particular Python version. REST is fine: ~4 endpoints, JSON over HTTPS,
Bearer-token auth.

Auth
----
A sandbox token is required to talk to the sandbox endpoint; a real
token (or production) is required for the production endpoint. The
``auth_token`` constructor argument accepts either — we trust the
caller to pair it with ``sandbox=True/False``.

Rate limit
----------
Tinkoff public docs: 60 rps per token. We enforce that exactly so two
loaders sharing a token don't trample each other.

Corporate actions
-----------------
Tinkoff exposes dividends via ``GetDividends`` (Phase 2); splits are
reported in the ``Shares`` payload as ``lot`` changes. Phase 1.1 emits
``kind='dividend'`` rows; ``kind='split'`` is added in Phase 1.3 when
the lot-change detector is wired.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
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

DEFAULT_RATE_PER_SEC = 60.0  # Tinkoff SLA
MAX_LOOKBACK = timedelta(days=10 * 365)  # ISS retention is shorter than that, but Tinkoff retains ~10y

PROD_BASE = "https://invest-public-api.tinkoff.ru"
SANDBOX_BASE = "https://sandbox-invest-public-api.tinkoff.ru"


class TinkoffDataLoader(DataLoader):
    """Synchronous loader for the T-Bank Invest REST API."""

    SOURCE = "tkf"

    def __init__(
        self,
        auth_token: str | None = None,
        *,
        sandbox: bool = True,
        session: requests.Session | None = None,
        timeout_sec: float = 30.0,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
    ) -> None:
        from .token_bucket import TokenBucket

        super().__init__(bucket=TokenBucket(rate=rate_per_sec, window_seconds=1.0))
        # Token resolution order: explicit arg > TINKOFF_SANDBOX_TOKEN
        # (if sandbox=True) > TINKOFF_REAL_TOKEN (if sandbox=False) >
        # TINKOFF_INVEST_TOKEN (last resort, respects sandbox flag).
        token = auth_token
        if token is None:
            if sandbox:
                token = os.environ.get("TINKOFF_SANDBOX_TOKEN")
            else:
                token = os.environ.get("TINKOFF_REAL_TOKEN") or os.environ.get(
                    "TINKOFF_INVEST_TOKEN"
                )
        self._token = token
        self._sandbox = sandbox
        self._base = SANDBOX_BASE if sandbox else PROD_BASE
        self._session = session or requests.Session()
        self._timeout = timeout_sec
        self._universe_cache: list[TickerMeta] | None = None

    # --------------------------------------------------------------- public

    @property
    def is_configured(self) -> bool:
        """True when an auth token is set. False means integration tests must skip."""
        return bool(self._token)

    def list_tickers(self) -> list[TickerMeta]:
        if not self.is_configured:
            raise LoaderAuthError(
                "Tinkoff auth token not set "
                "(TINKOFF_SANDBOX_TOKEN / TINKOFF_REAL_TOKEN / explicit arg)"
            )
        if self._universe_cache is not None:
            return self._universe_cache
        url = f"{self._base}/tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares"
        body: dict[str, Any] = {"instrumentStatus": "INSTRUMENT_STATUS_ALL"}
        payload = self._post_json(url, body)
        instruments = payload.get("instruments") or []
        metas = [self._instrument_to_meta(i) for i in instruments]
        out: list[TickerMeta] = [m for m in metas if m is not None]
        self._universe_cache = out
        return out

    def iter_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[OHLCVRow]:
        self._validate_range(start, end, max_lookback=MAX_LOOKBACK)
        if not self.is_configured:
            raise LoaderAuthError("Tinkoff auth token not set")
        ticker = ticker.upper().strip()
        figi = self._figi_for(ticker)
        if figi is None:
            raise LoaderNotFoundError(f"unknown ticker {ticker!r} on Tinkoff")

        # Tinkoff's GetCandles expects RFC-3339 instants.
        from_ts = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        to_ts = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)

        url = f"{self._base}/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"
        body = {
            "figi": figi,
            "from": from_ts.isoformat(),
            "to": to_ts.isoformat(),
            "interval": "CANDLE_INTERVAL_DAY",
        }
        payload = self._post_json(url, body)
        candles = payload.get("candles") or []
        for c in candles:
            bar = self._candle_to_ohlcv(ticker, c)
            if bar is not None and start <= bar.ts <= end:
                yield bar

    def iter_corporate_actions(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[CorporateAction]:
        if not self.is_configured:
            raise LoaderAuthError("Tinkoff auth token not set")
        self._validate_range(start, end, max_lookback=MAX_LOOKBACK)
        ticker = ticker.upper().strip()
        figi = self._figi_for(ticker)
        if figi is None:
            return
        url = (
            f"{self._base}/tinkoff.public.invest.api.contract.v1.InstrumentsService/GetDividends"
        )
        body = {"figi": figi, "from": _to_instant(start), "to": _to_instant(end, end_of_day=True)}
        try:
            payload = self._post_json(url, body)
        except LoaderNotFoundError:
            return  # no dividends → no rows → done
        for div in payload.get("dividends") or []:
            action = self._dividend_to_action(ticker, div)
            if action is not None:
                yield action

    # ------------------------------------------------------------ internal

    def _figi_for(self, ticker: str) -> str | None:
        for m in self.list_tickers():
            if m.ticker == ticker:
                return m.figi
        return None

    def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bucket.acquire()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = self._session.post(
                url, headers=headers, json=body, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise LoaderError(f"network error posting {url}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise LoaderAuthError(
                f"Tinkoff auth rejected (HTTP {resp.status_code}); "
                f"check token / sandbox flag (sandbox={self._sandbox})"
            )
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

    # ----------------------------------------------------- row -> model

    @staticmethod
    def _instrument_to_meta(row: dict[str, Any]) -> TickerMeta | None:
        try:
            ticker = row.get("ticker")
            figi = row.get("figi")
            if not ticker or not figi:
                return None
            lot = int(row.get("lot") or 1)
            name = row.get("name") or ticker
            isin = row.get("isin")
            return TickerMeta(
                ticker=str(ticker).upper(),
                figi=str(figi),
                name=str(name),
                lot=lot,
                isin=str(isin) if isin else None,
                currency=str(row.get("currency") or "RUB"),
                delisted=bool(row.get("blocked") or row.get("delisted")),
                delisted_at=None,
                listed_at=None,
                source="tkf",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed Tinkoff instrument row %r: %s", row, exc)
            return None

    @staticmethod
    def _candle_to_ohlcv(ticker: str, row: dict[str, Any]) -> OHLCVRow | None:
        try:
            ts_raw = row.get("time")
            if not ts_raw:
                return None
            ts = _parse_instant(str(ts_raw)).date()
            o = _d(row.get("open"))
            h = _d(row.get("high"))
            lo = _d(row.get("low"))
            c = _d(row.get("close"))
            vol = _d(row.get("volume"))
            return OHLCVRow(
                ticker=ticker,
                ts=ts,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=vol,
                adj_close=c,  # Phase 1.3 will compute from splits
                source="tkf",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed Tinkoff candle %r: %s", row, exc)
            return None

    @staticmethod
    def _dividend_to_action(ticker: str, row: dict[str, Any]) -> CorporateAction | None:
        try:
            ts_raw = row.get("lastBuyDate") or row.get("payDate") or row.get("declaredDate")
            if not ts_raw:
                return None
            ts = _parse_instant(str(ts_raw)).date()
            value = _d(row.get("dividend"))
            return CorporateAction(
                ticker=ticker,
                ts=ts,
                kind="dividend",
                value=value,
                source="tkf",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping malformed Tinkoff dividend %r: %s", row, exc)
            return None


# ---------------------------------------------------------------------- utils


def _d(v: Any) -> Decimal:
    if v is None or v == "":
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    # Tinkoff returns numbers as JSON strings sometimes; coerce via str.
    # ``float`` would lose precision — Decimal is required by NUMERIC(18,8).
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    return Decimal(str(v))


def _parse_instant(s: str) -> datetime:
    """Parse ISO-8601 with or without trailing 'Z'."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _to_instant(d: date, *, end_of_day: bool = False) -> str:
    """Convert ``date`` to an RFC-3339 instant (UTC midnight / 23:59:59)."""
    hh = 23 if end_of_day else 0
    mm = 59 if end_of_day else 0
    ss = 59 if end_of_day else 0
    return (
        datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


__all__ = ["TinkoffDataLoader", "PROD_BASE", "SANDBOX_BASE"]
