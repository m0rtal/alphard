"""TinkoffInvestDataLoader — primary source for MOEX data.

SDK: t-tech-investments (T-Bank official, Apache-2.0).
Install: pip install t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple  # noqa: E501

API surface used
----------------
- t_tech.invest.Client(token) — sync gRPC client (context manager)
- client.instruments.shares() — full MOEX share universe (1927+ instruments)
- client.instruments.bonds() — full MOEX bond universe (OFZ + corp via class_code filter)
- client.instruments.etfs() — full MOEX ETF universe (TQTE-class)
- client.instruments.find_instrument(query='SBER') — find single instrument by ticker
- client.market_data.get_candles(figi, from_, to, interval) — historical OHLCV

Capabilities matrix
-------------------
- Full TQBR share universe (1927+ instruments via shares())
- Full TQOB+TQCB bond universe (OFZ + corporate, ~1601 instruments via bonds())
- Full TQTE ETF universe (~272 instruments via etfs())
- Historical daily candles (gRPC) — primary path (≈5 years)
- Order placement — Phase 6 (broker Connector)
- Portfolio snapshot — Phase 5

Russian market class codes
--------------------------
Tinkoff exposes every instrument with a MOEX ``class_code`` that we use to
slice the bond universe:
- ``TQOB`` — OFZ (federal) bonds
- ``TQCB`` — corporate + municipal + sub-federal bonds
- ``TQTE`` — exchange-traded funds (BPIFs)
- ``TQBR`` — equities (main board)

For Phase 1 we return both ``TQOB`` and ``TQCB`` so a fixed-income backtest
can choose the OFZ slice downstream via ``isin`` prefix (RU000A0..0 = OFZ).

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
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator, cast

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

# MOEX class codes we want to surface from the bond universe.
#   TQOB — OFZ (federal government bonds)
#   TQCB — corporate, municipal, sub-federal bonds
# Tinkoff's bonds() endpoint returns both; any other class_code is
# exchange-internal / non-tradeable and gets filtered.
_BOND_CLASS_CODES: frozenset[str] = frozenset({"TQOB", "TQCB"})

# MOEX class code for exchange-traded funds (BPIFs included).
_ETF_CLASS_CODE: str = "TQTE"

# Single source of truth for every ``class_code`` that the bot treats as
# tradable at the broker layer (issue #187). Both the data loaders and
# the broker FIGI-mapping helper must agree on this set, otherwise
# RiskGate-approved orders get rejected at the broker with a misleading
# "not found in TQBR/TQOB instrument universe" error.
#
# Composition:
#   TQBR  — main-board equities (Russian shares)
#   TQOB  — OFZ federal bonds
#   TQCB  — corporate / municipal / sub-federal bonds
#   TQTE  — ETFs / BPIFs
#   SPBXM — SPB Exchange US/foreign shares (AAPL, MSFT, etc.)
#   TQBS  — SPB foreign shares, British style
#   TQDE  — SPB foreign shares, German style
#   TQNO  — SPB foreign shares, Norwegian style
#   TQLV  — SPB foreign shares, Latvian style
#   TQPI  — SPB foreign shares, Polish style
#
# Mirrors the per-class search in ``TinkoffInvestMDDataLoader._collect_tickers``
# (src/data/tinkoff_md_loader.py:308) and ``TinkoffInvestDataLoader.get_ticker``
# (src/data/tinkoff_loader.py:244).
_TRADABLE_CLASS_CODES: frozenset[str] = frozenset(
    {
        "TQBR",
        "TQOB",
        "TQCB",
        "TQTE",
        "SPBXM",
        "TQBS",
        "TQDE",
        "TQNO",
        "TQLV",
        "TQPI",
    }
)


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
            # Prefer REAL token if present (full universe, 200 req/min)
            # Falls back to sandbox (150-share universe, 15 req/min)
            token = env.get("TINKOFF_REAL_TOKEN") or env.get("TINKOFF_SANDBOX_TOKEN")
        if not token:
            raise LoaderAuthError("Tinkoff token not set: pass token= or export TINKOFF_SANDBOX_TOKEN")  # noqa: E501
        self._token = token
        self._universe_cache: dict[str, TickerMeta] | None = None
        self._bonds_cache: dict[str, TickerMeta] | None = None
        self._etfs_cache: dict[str, TickerMeta] | None = None
        # BUGFIX (issue #174): each cache fill is guarded by a lock so two
        # threads calling list_tickers() / list_bonds() / list_etfs() /
        # list_shares_all(<cls>) concurrently don't both walk through the
        # gRPC instruments.shares() / bonds() / etfs() harvest (which costs
        # a self.bucket slot per call), race on the cache assignment, and
        # silently discard the slow builder's results. Mirrors the
        # self._universe_lock pattern from issue #152 in the sibling file
        # src/data/tinkoff_md_loader.py:252. One lock per cache category
        # (universe, bonds, etfs, shares_all-per-class) so that a gRPC
        # harvest on one category doesn't serialise the others. Production
        # today is sequential, so the bug is latent — no live failure —
        # but apply_corporate_actions.py:240-260 already constructs many
        # short-lived TinkoffInvestDataLoader instances and Phase 2.6 may
        # parallelise backfills, at which point the race becomes live.
        self._universe_lock = threading.Lock()
        self._bonds_lock = threading.Lock()
        self._etfs_lock = threading.Lock()
        # Per-class_code shares_all cache lives in a dict (was: ad-hoc
        # ``setattr(self, "_shares_all_<cls>", ...)`` attribute); guarded
        # by a single lock that covers the whole dict so the
        # "if cached: return" early-out stays consistent with the
        # assignment under the same critical section.
        self._shares_all_cache: dict[str, list[TickerMeta]] = {}
        self._shares_all_lock = threading.Lock()

    # --------------------------------------------------------------- public

    def list_tickers(self) -> list[TickerMeta]:
        # BUGFIX (issue #174): lock around the cache fill; see __init__
        # for the full rationale. Double-check after acquiring the lock
        # so the common case (cache already populated) skips the lock
        # entirely on the second-and-later call.
        if self._universe_cache is not None:
            return list(self._universe_cache.values())
        with self._universe_lock:
            if self._universe_cache is not None:
                return list(self._universe_cache.values())
            self._universe_cache = self._fill_universe()
        return list(self._universe_cache.values())

    def list_shares_all(self, class_code: str = "TQBR") -> list[TickerMeta]:
        """Full share universe INCLUDING DELISTED (1772 + 150 live = 1927).

        NO filter on trading_status — captures live AND delisted/suspended
        TQBR tickers via the same gRPC ``instruments.shares()`` endpoint
        the broker uses internally. Each TickerMeta has ``delisted`` set
        based on the SecurityTradingStatus enum value.
        """
        # BUGFIX (issue #174): per-class_code cache is now a dict field
        # (``_shares_all_cache``) rather than ad-hoc ``setattr(self,
        # "_shares_all_<cls>", ...)`` attributes. The dict keeps the
        # cache co-located for iteration by ``get_ticker()`` and the
        # ``_shares_all_lock`` serialises both the early-return check and
        # the assignment so two concurrent ``list_shares_all("TQBR")``
        # callers can't both run the gRPC harvest.
        with self._shares_all_lock:
            cached = self._shares_all_cache.get(class_code)
            if cached is not None:
                return cached
            self._shares_all_cache[class_code] = self._fill_shares_all(class_code)
            return self._shares_all_cache[class_code]

    def list_bonds(self) -> list[TickerMeta]:
        """Return tradeable MOEX bonds (TQOB OFZ + TQCB corporate/muni).

        Cached on first call: the gRPC ``bonds()`` endpoint returns the full
        bond universe (~1601 instruments) in one round-trip. Both OFZ and
        corporate bonds are returned; callers that need the OFZ-only slice
        should filter on the ``isin`` prefix (RU000A0..0 = OFZ).
        """
        # BUGFIX (issue #174): lock around the cache fill. See
        # list_tickers() for the rationale and the double-check pattern.
        if self._bonds_cache is not None:
            return list(self._bonds_cache.values())
        with self._bonds_lock:
            if self._bonds_cache is not None:
                return list(self._bonds_cache.values())
            self._bonds_cache = self._fill_bonds()
        return list(self._bonds_cache.values())

    def list_etfs(self) -> list[TickerMeta]:
        """Return tradeable MOEX ETFs / BPIFs (TQTE class).

        Cached on first call. No class-code filter is required because the
        ``etfs()`` endpoint already returns only ETF instruments.
        """
        # BUGFIX (issue #174): lock around the cache fill. See
        # list_tickers() for the rationale and the double-check pattern.
        if self._etfs_cache is not None:
            return list(self._etfs_cache.values())
        with self._etfs_lock:
            if self._etfs_cache is not None:
                return list(self._etfs_cache.values())
            self._etfs_cache = self._fill_etfs()
        return list(self._etfs_cache.values())

    def get_ticker(self, ticker: str) -> TickerMeta:
        """Find a ticker across all instrument universes (shares, bonds, etfs).

        Search order: full-share-universe across ALL class codes (TQBR, SPBXM,
        TQOB, TQCB, TQTE) → live shares → bonds → ETFs. This ensures delisted
        tickers (e.g., VSMO) AND cross-board tickers (e.g., AAPL on SPBXM)
        resolve to a TickerMeta with FIGI, allowing fetch_ohlcv() to retrieve
        history.
        """
        t = ticker.upper()
        # 1) Full share universe (live + delisted) — try ALL cached class codes.
        # BUGFIX (issue #174): iterate the per-class_code dict
        # (``_shares_all_cache``) instead of ``vars(self)`` filtered by
        # the ``_shares_all_`` prefix. The old code picked up any
        # future attribute whose name happened to start with
        # ``_shares_all_`` (e.g. a test fixture or a subclass attribute)
        # and walked it as if it were a cached ticker list — a latent
        # TypeError waiting to happen if anyone introduced such an
        # attribute.
        for metas in self._shares_all_cache.values():
            for meta in metas:
                if meta.ticker == t:
                    return meta
        # 2) Live-only universe, bonds, ETFs
        for cache_getter in (self._ensure_universe, self._ensure_bonds, self._ensure_etfs):
            cache = cast(dict[str, TickerMeta], cache_getter())  # type: ignore[redundant-cast]
            fetched: TickerMeta | None = cache.get(t)
            if fetched is not None:
                return fetched
        raise LoaderNotFoundError(f"Ticker {ticker} not found in Tinkoff universe")

    def fetch_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[OHLCVRow]:
        """Fetch historical daily candles for [start, end].

        Note: the gRPC ``get_candles`` API rejects request periods > ~1 year
        with INVALID_ARGUMENT 30014. We transparently split the requested
        range into 1-year chunks. Works for both live and delisted tickers
        (verified with VSMO which returned 1746 candles for 2020-2026).
        """
        # Validate range: gRPC cap is 5y, but chunked requests let us exceed it.
        self._validate_range(start, end, max_lookback=timedelta(days=30 * 365))
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

        # Split into ≤1-year chunks (API rejects longer periods).
        one_year = timedelta(days=365)
        chunks: list[tuple[datetime, datetime]] = []
        cursor = start_dt
        while cursor < end_dt:
            chunk_end = min(cursor + one_year, end_dt)
            chunks.append((cursor, chunk_end))
            cursor = chunk_end

        bars: list[OHLCVRow] = []
        with Client(self._token) as client:
            for chunk_start, chunk_end in chunks:
                self.bucket.acquire()
                response = client.market_data.get_candles(
                    instrument_id=meta.figi,
                    from_=chunk_start,
                    to=chunk_end,
                    interval=CandleInterval.CANDLE_INTERVAL_DAY,
                )
                for c in response.candles:
                    bars.append(_candle_to_row(ticker.upper(), c))
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
        # BUGFIX (issue #174): _ensure_universe is now a thin wrapper around
        # _fill_universe(); the lock lives in the public list_tickers()
        # call site (and the lock-free variant is kept here for callers
        # like get_ticker() that already hold no other lock and want
        # one-shot access). When called outside the lock, _fill_universe()
        # is still safe because the assignment happens here (not in the
        # helper) and we set the cache before any other thread can
        # observe a half-built dict.
        if self._universe_cache is not None:
            return self._universe_cache
        self._universe_cache = self._fill_universe()
        return self._universe_cache

    def _fill_shares_all(self, class_code: str) -> list[TickerMeta]:
        """Harvest one share class_code via a gRPC ``instruments.shares()`` call.

        BUGFIX (issue #174): extracted from ``list_shares_all`` so the
        caller holds ``self._shares_all_lock`` across the assignment
        into ``_shares_all_cache``. Without this split, two threads
        calling ``list_shares_all("TQBR")`` would both pass the cache
        check (which was an ad-hoc ``getattr(self, "_shares_all_TQBR",
        None)``), both pay one ``self.bucket`` slot for the gRPC
        ``shares()`` harvest, and one would silently overwrite the
        other's per-class_code result.

        Includes delisted tickers (``delisted=True`` is set based on the
        SecurityTradingStatus enum value: NOT_AVAILABLE_FOR_TRADING /
        DELISTED / EXCLUDED). The ``listed_at`` field is the earliest
        of first_1day_candle_date / first_1min_candle_date / ipo_date
        that the proto exposes; delisted_at stays None because Tinkoff
        does not surface a delisting date — ``delist_source.py`` fills
        that separately via MOEX ISS.
        """
        from t_tech.invest import Client, SecurityTradingStatus

        with Client(self._token) as client:
            self.bucket.acquire()
            response = client.instruments.shares()
            out: list[TickerMeta] = []
            for inst in response.instruments:
                if inst.class_code != class_code:
                    continue
                ts_int = int(getattr(inst, "trading_status", 14) or 14)
                try:
                    status_name = SecurityTradingStatus(ts_int).name
                except (ValueError, TypeError):
                    status_name = "UNKNOWN"
                delisted = (
                    "NOT_AVAILABLE_FOR_TRADING" in status_name
                    or "DELISTED" in status_name
                    or "EXCLUDED" in status_name  # noqa: E501
                )
                # Date fields from Tinkoff Instrument:
                # - ``first_1day_candle_date`` = first daily bar (proxy for listing)
                # - ``ipo_date`` = IPO (may be pre-listing for some classes)
                # - no explicit ``delisting_date`` on Instrument protobuf —
                #   delisted_at stays None; delist_source.py fills it via MOEX ISS.
                from datetime import date as _date

                # Tinkoff proto emits timestamps as ``datetime`` objects
                # (often tz-aware UTC), not as ISO strings. Normalise.
                from datetime import datetime as _dt

                listed_at_attr = None
                # Issue #319 (2026-08-29): ``listed_at`` for backfill-
                # completion purposes MUST anchor on ``first_1day_candle_date``
                # — the earliest date the broker API actually has daily bars
                # for this instrument. Falling back to ``first_1min_candle_date``
                # or ``ipo_date`` (which can predate actual data availability
                # by years for IPO'd-but-not-traded instruments like some
                # SPBXM ETFs) yields an optimistic completion floor that
                # _is_complete() formula cannot satisfy — the ticker is
                # perpetually "missing data" even when all available data
                # has been pulled (98% of universe on .107 was stuck at 0
                # bars with this bug).
                # Per Tinkoff docs, ``first_1day_candle_date`` is the
                # canonical "data from this date" marker returned by every
                # Instrument in Shares/Bonds/ETFs list responses. Falling
                # back to ``first_1min_candle_date`` / ``ipo_date`` would
                # be misleading; leave ``listed_at_attr = None`` so the
                # backfill ``_resolve_universe`` / ``_is_complete`` formulas
                # probe ``earliest_ts`` from the OHLCV table to derive
                # a real floor.
                for _attr in ("first_1day_candle_date",):
                    _raw = getattr(inst, _attr, None)
                    if _raw is None:
                        continue
                    try:
                        if isinstance(_raw, _dt):
                            listed_at_attr = _raw.date()
                        elif isinstance(_raw, _date):
                            listed_at_attr = _raw
                        else:
                            listed_at_attr = _date.fromisoformat(str(_raw)[:10])
                        break
                    except (TypeError, ValueError):
                        continue

                out.append(
                    TickerMeta(
                        ticker=inst.ticker,
                        figi=getattr(inst, "figi", None) or None,
                        name=inst.name,
                        lot=inst.lot,
                        isin=getattr(inst, "isin", None),
                        currency="RUB",
                        class_code=getattr(inst, "class_code", None),
                        delisted=delisted,
                        listed_at=listed_at_attr,
                        delisted_at=None,  # populated by delist_source via MOEX ISS
                        source=self.SOURCE,  # type: ignore[arg-type]
                    )
                )
        return out

    def _fill_universe(self) -> dict[str, TickerMeta]:
        """Harvest the TQBR share universe via one gRPC ``instruments.shares()`` call.

        BUGFIX (issue #174): extracted from ``_ensure_universe`` so the
        fill can be assigned under ``self._universe_lock`` without the
        gRPC client ``with`` block leaking the lock across the network
        round-trip. The caller (``list_tickers``) is responsible for
        holding the lock; this helper does no locking of its own.

        Filter contract: keep class_code == "TQBR" with trading_status
        in {5, 14, 15} (OPENING / NORMAL_TRADING / CLOSING) and
        ``api_trade_available_flag is not False``. Everything else is
        dropped — delisted/blocked, non-MOEX classes, exchange-internal.
        """
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
                    class_code=getattr(inst, "class_code", None),
                    delisted=False,
                    source=self.SOURCE,  # type: ignore[arg-type]
                )
        return universe

    def _ensure_bonds(self) -> dict[str, TickerMeta]:
        # BUGFIX (issue #174): see _ensure_universe() rationale. Thin
        # wrapper — the lock lives at the list_bonds() call site.
        if self._bonds_cache is not None:
            return self._bonds_cache
        self._bonds_cache = self._fill_bonds()
        return self._bonds_cache

    def _fill_bonds(self) -> dict[str, TickerMeta]:
        """Harvest the TQOB + TQCB bond universe via one gRPC ``instruments.bonds()`` call.

        BUGFIX (issue #174): extracted from ``_ensure_bonds`` so the
        caller (``list_bonds``) holds ``self._bonds_lock`` across the
        assignment. See ``_fill_universe`` for the full pattern.

        Filter contract: keep class_code in ``_BOND_CLASS_CODES``
        (TQOB OFZ + TQCB corporate/muni) with trading_status in
        {5, 14, 15} and ``api_trade_available_flag is not False``.
        """
        from t_tech.invest import Client

        with Client(self._token) as client:
            self.bucket.acquire()
            response = client.instruments.bonds()
            universe: dict[str, TickerMeta] = {}
            for inst in response.instruments:
                # bonds() returns both OFZ (TQOB) and corporate (TQCB).
                # Drop exchange-internal / non-tradeable classes.
                if inst.class_code not in _BOND_CLASS_CODES:
                    continue
                # Mirror shares() filter: NORMAL_TRADING + DEALER_NORMAL_TRADING + auctions.
                try:
                    ts = int(getattr(inst, "trading_status", 14) or 14)
                    if ts not in (5, 14, 15):
                        continue
                except (ValueError, TypeError):
                    pass
                if getattr(inst, "api_trade_available_flag", None) is False:
                    continue
                ticker = inst.ticker
                universe[ticker] = TickerMeta(
                    ticker=ticker,
                    figi=getattr(inst, "figi", None) or None,
                    name=inst.name,
                    lot=inst.lot,
                    isin=inst.isin,
                    currency=getattr(inst, "currency", "RUB") or "RUB",
                    class_code=getattr(inst, "class_code", None),
                    delisted=False,
                    source=self.SOURCE,  # type: ignore[arg-type]
                )
        return universe

    def _ensure_etfs(self) -> dict[str, TickerMeta]:
        # BUGFIX (issue #174): see _ensure_universe() rationale. Thin
        # wrapper — the lock lives at the list_etfs() call site.
        if self._etfs_cache is not None:
            return self._etfs_cache
        self._etfs_cache = self._fill_etfs()
        return self._etfs_cache

    def _fill_etfs(self) -> dict[str, TickerMeta]:
        """Harvest the TQTE ETF universe via one gRPC ``instruments.etfs()`` call.

        BUGFIX (issue #174): extracted from ``_ensure_etfs`` so the
        caller (``list_etfs``) holds ``self._etfs_lock`` across the
        assignment. See ``_fill_universe`` for the full pattern.

        Filter contract: keep class_code == "TQTE" with trading_status
        in {5, 14, 15} and ``api_trade_available_flag is not False``.
        """
        from t_tech.invest import Client

        with Client(self._token) as client:
            self.bucket.acquire()
            response = client.instruments.etfs()
            universe: dict[str, TickerMeta] = {}
            for inst in response.instruments:
                # etfs() returns only ETF instruments — TQTE on MOEX main board.
                # Defensive: skip any non-MOEX or sub-class entries.
                if inst.class_code != _ETF_CLASS_CODE:
                    continue
                try:
                    ts = int(getattr(inst, "trading_status", 14) or 14)
                    if ts not in (5, 14, 15):
                        continue
                except (ValueError, TypeError):
                    pass
                if getattr(inst, "api_trade_available_flag", None) is False:
                    continue
                ticker = inst.ticker
                universe[ticker] = TickerMeta(
                    ticker=ticker,
                    figi=getattr(inst, "figi", None) or None,
                    name=inst.name,
                    lot=inst.lot,
                    isin=inst.isin,
                    currency=getattr(inst, "currency", "RUB") or "RUB",
                    class_code=getattr(inst, "class_code", None),
                    delisted=False,
                    source=self.SOURCE,  # type: ignore[arg-type]
                )
        return universe
