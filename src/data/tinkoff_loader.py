"""TinkoffInvestDataLoader — primary source for MOEX data.

SDK: t-tech-investments (T-Bank official, Apache-2.0).
Install: pip install t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple

API surface used
----------------
- t_tech.invest.Client(token) — sync gRPC client (context manager)
- client.instruments.shares() — full MOEX share universe (1927+ instruments)
- client.instruments.find_instrument(query='SBER') — find single instrument by ticker
- client.market_data.get_candles(figi, from_, to, interval) — historical OHLCV

Capabilities matrix
-------------------
- Full TQBR share universe (1927+ instruments via shares())
- Historical daily candles (gRPC) — primary path (≈5 years)
- Order placement — Phase 6 (broker Connector)
- Portfolio snapshot — Phase 5

Why gRPC (not REST HTTPS)
Tinkoff Invest REST HTTPS endpoint requires a Russian-trusted CA chain
not present in minimal Python containers. gRPC uses its own TLS and
ships with the SDK, so it works out of the box. We therefore use the
SDK's gRPC for ALL operations. REST fallback is documented as a TODO
for Phase 2/3 if broker Connector needs POST orders (which use gRPC
anyway).

Sandbox vs Real
----------------
- Sandbox token prefix: t. (88 chars)
- Real token prefix: t. (88 chars, identical shape)
- Detect via TINKOFF_SANDBOX_TOKEN vs TINKOFF_REAL_TOKEN env vars
- This loader refuses to silently fall back: if TINKOFF_SANDBOX_TOKEN is
  set but invalid, raise. If neither is set, raise.

History depth
---------------
Tinkoff Invest API keeps ~5 years of daily candles reliably. For longer
history (pre-2021) use MOEXDataLoader (moex_loader.py) which exposes
the same OHLCVRow interface. Phase 2 will add MOEX AlgoPack for
>5-year history.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator

from .loader import (
    DataLoader,
    LoaderAuthError,
    LoaderError,
    LoaderNotFoundError,
)
from .models import CorporateAction, OHLCVRow, TickerMeta

logger = logging.getLogger(__name__)

# Tinkoff gRPC rate limit: 200/min per token (per official docs).
# We enforce 100/min to leave headroom for concurrent consumers.
DEFAULT_RATE_PER_MIN = 100

# Tinkoff's CandleInterval enum is the only knob we have. Daily comes
# built-in. Intraday is available but Phase 1.1 ships daily only.
DAILY_INTERVAL = "CANDLE_INTERVAL_DAY"


def _money_to_decimal(money: Any) -> Decimal:
    """Convert tinkoff invest ``Money`` (units + nano) to Decimal."""
    units = getattr(money, "units", 0)
    nano = getattr(money, "nano", 0)
    return Decimal(units) + Decimal(nano) / Decimal(1_000_000_000)


def _candle_to_row(ticker: str, candle: Any) -> OHLCVRow:
    """Convert tinkoff HistoricCandle to OHLCVRow."""
    ts = candle.time.date() if hasattr(candle.time, "date") else candle.time
    return OHLCVRow(
        ticker=ticker,
        ts=ts,
        open=_money_to_decimal(candle.open),
        high=_money_to_decimal(candle.high),
        low=_money_to_decimal(candle.low),
        close=_money_to_decimal(candle.close),
        volume=candle.volume,
        adj_close=_money_to_decimal(candle.close),
        source="tkf",
    )


class TinkoffInvestDataLoader(DataLoader):
    """Synchronous gRPC loader for Tinkoff Invest API (MOEX)."""

    SOURCE = "tkf"

    def __init__(
        self,
        *,
        token: str | None = None,
        rate_per_min: float = DEFAULT_RATE_PER_MIN,
    ) -> None:
        from .token_bucket import TokenBucket

        super().__init__(bucket=TokenBucket(rate=rate_per_min, window_seconds=60.0))
        env = os.environ
        if token is None:
            token = env.get("TINKOFF_SANDBOX_TOKEN") or env.get("TINKOFF_REAL_TOKEN")
        if not token:
            raise LoaderAuthError("Tinkoff token not set: pass token= or export TINKOFF_SANDBOX_TOKEN")
        self._token = token
        self._universe_cache: dict[str, TickerMeta] | None = None

    # --------------------------------------------------------------- public

    def list_tickers(self) -> list[TickerMeta]:
        if self._universe_cache is not None:
            return list(self._universe_cache.values())
        return list(self._ensure_universe().values())

    def get_ticker(self, ticker: str) -> TickerMeta:
        universe = self._ensure_universe()
        meta = universe.get(ticker.upper())
        if meta is None:
            raise LoaderNotFoundError(f"Ticker {ticker} not found in Tinkoff universe")
        return meta

    def fetch_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[OHLCVRow]:
        """Fetch historical daily candles for [start, end]."""
        self._validate_range(start, end, max_lookback=timedelta(days=5 * 365))
        meta = self.get_ticker(ticker)
        if not meta.figi:
            raise LoaderError(f"Ticker {ticker} has no FIGI; cannot fetch from Tinkoff")

        # Tinkoff requires timezone-aware datetimes in UTC
        if isinstance(start, datetime):
            start_dt = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        else:
            start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        if isinstance(end, datetime):
            end_dt = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        else:
            end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)

        from t_tech.invest import CandleInterval, Client

        with Client(self._token) as client:
            self.bucket.acquire()
            response = client.market_data.get_candles(
                instrument_id=meta.figi,
                from_=start_dt,
                to=end_dt,
                interval=CandleInterval.CANDLE_INTERVAL_DAY,
            )
        bars = [_candle_to_row(ticker.upper(), c) for c in response.candles]
        return bars

    def iter_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[OHLCVRow]:
        yield from self.fetch_ohlcv(ticker, start, end)

    def iter_corporate_actions(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[CorporateAction]:
        # Phase 1.1 stub: Tinkoff has no historical corporate-action REST;
        # Phase 2 will reconstruct via dividends/events feed.
        return
        yield  # noqa: unreachable

    # --------------------------------------------------------------- private

    def _ensure_universe(self) -> dict[str, TickerMeta]:
        if self._universe_cache is not None:
            return self._universe_cache
        from t_tech.invest import Client

        with Client(self._token) as client:
            self.bucket.acquire()
            response = client.instruments.shares()
            universe: dict[str, TickerMeta] = {}
            for inst in response.instruments:
                if inst.class_code != "TQBR":
                    continue
                # trading_status is a SecurityTradingStatus enum (integer).
                # 5=OPENING, 14=NORMAL_TRADING, 15=CLOSING. Filter out delisted/blocked.
                try:
                    ts = int(getattr(inst, "trading_status", 14) or 14)
                    if ts not in (5, 14, 15):
                        continue
                except (ValueError, TypeError):
                    pass
                if inst.api_trade_available_flag is False:
                    continue
                ticker = inst.ticker
                universe[ticker] = TickerMeta(
                    ticker=ticker,
                    figi=getattr(inst, "figi", None) or None,
                    name=inst.name,
                    lot=inst.lot,
                    isin=inst.isin,
                    currency="RUB",  # Tinkoff shares MOEX are always RUB
                    delisted=False,
                    source=self.SOURCE,  # type: ignore[arg-type]
                )
        self._universe_cache = universe
        return universe
