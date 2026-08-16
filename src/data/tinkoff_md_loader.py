"""TinkoffInvestMDDataLoader — minute-archive backfill source.

Source
------
Unlike :class:`TinkoffInvestDataLoader` which streams daily candles
through gRPC (``GetCandles``), this loader pulls the **official Tinkoff
market-data archive** at ``https://invest-public-api.tinkoff.ru/history-data``
(the same endpoint documented in
``RussianInvestments/investAPI/src/marketdata/download_md.sh``).

The archive returns one ZIP per FIGI per year, each ZIP containing one
CSV per trading day. Each CSV row is a minute candle:

    <figi>;<ISO8601 UTC>;<open>;<close>;<high>;<low>;<volume>

Minute candles are aggregated in-process into daily OHLCV bars before
returning. The aggregator is **pure**: no external state, no
side-effects, deterministic.

Why a separate loader class
---------------------------
- Different transport (HTTPS download vs gRPC stream).
- Different cache lifetime (the archive is yearly; gRPC is last 6 years).
- Different rate-limit profile (HTTPS allows ~30 req/min with retries;
  gRPC allows ~200 UoM/min).
- Different completeness (archive covers 2018-current for instruments
  Tinkoff has ever listed; gRPC covers last 6 years for currently
  tradable instruments).

Lifetime
--------
This loader is built to be the **primary backfill tool**. Workflow:

1. First-run cold backfill: ``list_instruments()`` returns the full Tinkoff
   share/bond/etf universe, then for each instrument we download
   yearly archives from 2018 (or earliest available year) to current
   year, aggregate, and ``upsert`` into Postgres.
2. Steady-state: the cron ``daily_sync.py`` calls the gRPC loader for
   the last few days. The MD loader is **only re-run** if backfill
   coverage is short of N bars per ticker (restart/recovery case).

Idempotency
-----------
Aggregation is deterministic and ``upsert_ohlcv`` is keyed on
``(ticker, ts)``. Re-running on the same window is a no-op.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .loader import (
    DataLoader,
    LoaderAuthError,
    LoaderError,
    LoaderNotFoundError,
    LoaderRateLimitError,
)
from .models import CorporateAction, OHLCVRow, TickerMeta
from .token_bucket import TokenBucket

logger = logging.getLogger(__name__)

# Tinkoff history-data endpoint — official, documented in
# RussianInvestments/investAPI/src/marketdata/download_md.sh.
_HISTORY_URL = "https://invest-public-api.tinkoff.ru/history-data"

# Per official shell script (download_md.sh):
#   - minimum_year=2017 (but archive returns 404 below earliest known year per FIGI)
#   - rate-limit: 30 req/min documented; we honour 429 + sleep 5s
MIN_YEAR = 2017

# Tinkoff gRPC has the same MIN_YEAR we use here; for older data the
# caller must use MOEXDataLoader (moex_loader.py).
MIN_HISTORY_DATE = date(MIN_YEAR, 1, 1)

# Source tag for TickerMeta.source and audit. Two chars to match the
# SourceType Literal in models.py.
SOURCE = "tkf"

# The MD archive carries OHLCV minute bars aggregated to daily.
# Tinkoff gRPC is the live-tick source — this loader is read-only
# historical aggregation and never places orders.
DAILY_INTERVAL = "CANDLE_INTERVAL_DAY"

# Per-token bucket: 30 req/min => 0.5 r/s. Sleep on 429 adds backoff.
# Anti-bombing: this is the ONLY place that decides request pacing.
# - bucket.acquire() blocks (sleep) until a token is available.
# - 429 from upstream triggers 5s sleep then re-raise LoaderRateLimitError.
# - We never bypass the bucket, even in tests (bucket override via __init__).
# If you raise DEFAULT_BUCKET_RATE, you will hit upstream 429s and the
# script will slow itself down via the backoff path. Keep it <= 30/min.
DEFAULT_BUCKET_RATE = 0.5

# HTTP timeout per archive download — SBER 2020 ≈ 2.2 MB, fits in 30s.
_DOWNLOAD_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Pure aggregation
# ---------------------------------------------------------------------------


def aggregate_minutes_to_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group minute bars by trading date and produce daily OHLCV.

    Input schema (one dict per minute bar)::

        {"ts": datetime, "open": Decimal, "close": Decimal,
         "high": Decimal, "low": Decimal, "volume": int}

    Output schema (one dict per date)::

        {"ts": date, "open": Decimal, "close": Decimal,
         "high": Decimal, "low": Decimal, "volume": int}

    Aggregation rules (standard OHLC convention):
        open   = first minute bar's open price (the session's opening)
        high   = max(high) across all minute bars
        low    = min(low) across all minute bars
        close  = last minute bar's close price (the session's closing)
        volume = sum(volume) across all minute bars

    Notes
    -----
    - The minute archive contains bars in **ascending timestamp order**
      within each daily CSV. We rely on that to pick ``open`` / ``close``
      via first/last wins.
    - Empty input returns ``[]`` — caller treats it as "no data for the
      window", not an error.
    - Pure function: no I/O, no module-level state, no clock reads.
    - Timezone: Tinkoff minute timestamps are ISO-8601 UTC (``...Z``).
      ``aggregate`` normalises to naive UTC ``date`` for the daily bar;
      we don't preserve sub-day granularity.
    """
    if not rows:
        return []
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        ts = r["ts"]
        d = ts.date() if isinstance(ts, datetime) else ts
        by_day[d].append(r)
    daily: list[dict[str, Any]] = []
    for d in sorted(by_day.keys()):
        bars = by_day[d]
        if not bars:
            continue
        # Sort by timestamp ascending so first/last picks the correct
        # open/close within the session.
        bars_sorted = sorted(bars, key=lambda b: b["ts"])
        daily.append(
            {
                "ts": d,
                "open": bars_sorted[0]["open"],
                "close": bars_sorted[-1]["close"],
                "high": max(b["high"] for b in bars_sorted),
                "low": min(b["low"] for b in bars_sorted),
                "volume": sum(int(b["volume"]) for b in bars_sorted),
            }
        )
    return daily


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TinkoffInvestMDDataLoader(DataLoader):
    """Backfill-oriented Tinkoff Invest data loader (MD archive).

    See module docstring for rationale. Compared to
    :class:`TinkoffInvestDataLoader` (gRPC streaming) this loader:

    - uses HTTPS ``GET`` to ``/history-data`` instead of gRPC,
    - returns daily bars aggregated from minute archive CSVs,
    - has a separate cache for yearly downloads,
    - is slower per-instrument but covers 2018-current for any
      instrument Tinkoff has ever listed (delisted included).

    Parameters
    ----------
    token:
        Tinkoff Invest API token (sandbox or real). Falls back to
        ``$TINKOFF_SANDBOX_TOKEN`` then ``$TINKOFF_REAL_TOKEN``.
    bucket:
        Optional token-bucket override. Default 0.5 r/s (30 r/min,
        the documented Tinkoff MD rate limit).
    min_year:
        Override the minimum archive year (testing only). Defaults to
        :data:`MIN_YEAR` (2017).
    """

    SOURCE = SOURCE

    def __init__(
        self,
        token: str | None = None,
        *,
        bucket: TokenBucket | None = None,
        min_year: int = MIN_YEAR,
    ) -> None:
        super().__init__(bucket=bucket or TokenBucket(rate=DEFAULT_BUCKET_RATE, window_seconds=60.0))
        self._token = token or _resolve_token()
        if not self._token:
            raise LoaderAuthError(
                "TinkoffInvestMDDataLoader: no token — pass token=, or set "
                "$TINKOFF_SANDBOX_TOKEN / $TINKOFF_REAL_TOKEN"
            )
        if len(self._token) < 16:
            raise LoaderAuthError("TinkoffInvestMDDataLoader: token looks malformed (length < 16)")
        self._min_year = int(min_year)
        if self._min_year < 2017:
            raise LoaderError(
                f"TinkoffInvestMDDataLoader: min_year {self._min_year} " "below Tinkoff archive minimum (2017)"
            )
        # Per-year ZIP cache: (figi, year) -> bytes. Empty cache on
        # construction; tests assert clean state.
        self._archive_cache: dict[tuple[str, int], bytes | None] = {}

    # ---- universe -------------------------------------------------------

    def list_tickers(self) -> list[TickerMeta]:
        """Universe = every share Tinkoff exposes, regardless of class_code.

        We pull the broker-side ``list_shares_all`` per class_code (TQBR,
        SPBXM, TQBS, TQDE, ...). The union covers ≈1927 Russian shares
        + 1516 SPBX US/foreign names + other boards. NO client-side
        filter on trading_status — delisted tickers still have a FIGI
        and the MD archive honours it (returns 200 + zip for the years
        before delisting).

        For Phase 1.1 we keep this lean: shares only. Bonds/ETFs have
        shorter history windows and are not the Phase 1.1 priority.

        Note: ``figi`` may be ``None`` if the gRPC cache returns a row
        without one — caller must drop such entries before issuing
        archive requests.
        """
        from .tinkoff_loader import TinkoffInvestDataLoader

        grpc_loader = TinkoffInvestDataLoader(token=self._token)
        # Class codes to harvest: broker-side full universe, no client filter.
        # TQBR = MOEX main board Russian shares (incl. delisted/suspended).
        # SPBXM = SPB Exchange US/foreign shares.
        # TQBS/TQDE/TQNO/TQLV/TQPI = MOEX minor boards.
        target_classes = ("TQBR", "SPBXM", "TQBS", "TQDE", "TQNO", "TQLV", "TQPI")
        seen: dict[str, TickerMeta] = {}
        for cls in target_classes:
            try:
                metas = grpc_loader.list_shares_all(class_code=cls)
            except Exception as e:  # noqa: BLE001 — broker may rate-limit one class
                logger.warning("list_shares_all(%s) failed: %s", cls, e)
                continue
            for m in metas:
                if m.figi and m.ticker not in seen:
                    seen[m.ticker] = m
        return list(seen.values())

    def list_tickers_with_figi(self) -> list[TickerMeta]:
        """Same as ``list_tickers`` but drops entries with missing FIGI."""
        return [m for m in self.list_tickers() if m.figi]

    # ---- archive download ------------------------------------------------

    def download_year(self, figi: str, year: int) -> bytes | None:
        """Download and cache one yearly archive (ZIP).

        Returns ``bytes`` of the ZIP on success, ``None`` if the
        upstream has no data for this ``(figi, year)`` (HTTP 404 — the
        download_md.sh script removes empty files in this case).

        Raises
        ------
        LoaderAuthError
            HTTP 401/403 — token is invalid or expired.
        LoaderRateLimitError
            HTTP 429 — backoff is internal, caller should retry later.
        LoaderError
            HTTP 5xx or network error.
        """
        if year < self._min_year:
            return None
        cache_key = (figi, year)
        if cache_key in self._archive_cache:
            return self._archive_cache[cache_key]
        self.bucket.acquire()
        query = urlencode({"figi": figi, "year": year})
        url = f"{_HISTORY_URL}?{query}"
        req = Request(url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
                status = getattr(resp, "status", 200)
                if status == 404:
                    self._archive_cache[cache_key] = None
                    return None
                if status in (401, 403):
                    raise LoaderAuthError(f"Tinkoff MD archive auth failed (HTTP {status}) for {figi}/{year}")
                if status == 429:
                    # Per official script: sleep 5s and bubble up.
                    time.sleep(5.0)
                    raise LoaderRateLimitError(f"Tinkoff MD archive rate-limited for {figi}/{year}")
                if status >= 500:
                    raise LoaderError(f"Tinkoff MD archive HTTP {status} for {figi}/{year}")
                body: bytes = resp.read()
        except HTTPError as e:
            if e.code == 404:
                self._archive_cache[cache_key] = None
                return None
            if e.code in (401, 403):
                raise LoaderAuthError(f"Tinkoff MD archive auth failed (HTTP {e.code}) for {figi}/{year}") from e
            if e.code == 429:
                time.sleep(5.0)
                raise LoaderRateLimitError(f"Tinkoff MD archive rate-limited for {figi}/{year}") from e
            if e.code >= 500:
                raise LoaderError(f"Tinkoff MD archive HTTP {e.code} for {figi}/{year}") from e
            raise LoaderError(f"Tinkoff MD archive HTTP {e.code} for {figi}/{year}") from e
        except URLError as e:
            raise LoaderError(f"Tinkoff MD archive network error for {figi}/{year}: {e}") from e
        self._archive_cache[cache_key] = body
        return body

    def parse_archive(self, zip_bytes: bytes) -> list[dict[str, Any]]:
        """Parse one ZIP archive into a flat list of minute-bar dicts.

        Returns
        -------
        list[dict]
            One dict per minute bar, keys: ``ts`` (datetime UTC),
            ``open``/``close``/``high``/``low`` (Decimal), ``volume``
            (int).

        Raises
        ------
        LoaderError
            Malformed CSV or ZIP — bubble up; caller treats as
            per-archive failure.
        """
        out: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    if not name.endswith(".csv"):
                        continue
                    with zf.open(name) as fh:
                        text = io.TextIOWrapper(fh, encoding="utf-8")
                        reader = csv.reader(text, delimiter=";")
                        for row in reader:
                            if len(row) < 7:
                                continue
                            try:
                                ts_str = row[1]
                                # Tinkoff format: 2020-01-06T07:00:00Z
                                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
                                out.append(
                                    {
                                        "ts": ts,
                                        "open": Decimal(row[2]),
                                        "close": Decimal(row[3]),
                                        "high": Decimal(row[4]),
                                        "low": Decimal(row[5]),
                                        "volume": int(row[6]),
                                    }
                                )
                            except (ValueError, ArithmeticError, IndexError):
                                # Skip malformed row — CSV from upstream
                                # occasionally has trailing blank lines.
                                continue
        except zipfile.BadZipFile as e:
            raise LoaderError(f"Bad ZIP from Tinkoff MD archive: {e}") from e
        return out

    # ---- main entry point ------------------------------------------------

    def iter_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[OHLCVRow]:
        """Yield daily OHLCV bars from MD archive for ``[start, end]``.

        Notes
        -----
        - One ``iter_ohlcv`` call may issue ``years * 1`` HTTP requests
          (one per year in the window). For a full 2018-current
          backfill that is ~9 requests per ticker.
        - 404 (no data) is treated as an empty window — caller sees
          zero bars and decides whether to skip or alert.
        - Pagination happens per-year internally.
        """
        if start > end:
            return
        if start < MIN_HISTORY_DATE:
            # Silently clamp to archive minimum — caller asked for
            # pre-2017; we have no answer.
            start = MIN_HISTORY_DATE
        # Lookup FIGI
        meta = self._figi_for(ticker)
        if meta is None or not meta.figi:
            raise LoaderNotFoundError(f"TinkoffInvestMDDataLoader: no FIGI for ticker {ticker!r}")
        figi = meta.figi
        # Aggregate per year, then yield only the bars in the window.
        per_year: dict[date, dict[str, Any]] = {}
        for year in range(max(self._min_year, start.year), end.year + 1):
            zip_bytes = self.download_year(figi, year)
            if zip_bytes is None:
                continue
            minutes = self.parse_archive(zip_bytes)
            for daily_bar in aggregate_minutes_to_daily(minutes):
                d_ts = daily_bar["ts"]
                assert isinstance(d_ts, date)
                per_year[d_ts] = daily_bar
        for d in sorted(per_year):
            daily = per_year[d]
            if daily["ts"] < start or daily["ts"] > end:
                continue
            yield OHLCVRow(
                ticker=ticker.upper(),
                ts=daily["ts"],
                open=daily["open"],
                high=daily["high"],
                low=daily["low"],
                close=daily["close"],
                volume=Decimal(int(daily["volume"])),
                adj_close=daily["close"],
            )

    def iter_corporate_actions(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> Iterator[CorporateAction]:
        """MD archive does not carry corporate actions; raise explicitly.

        Phase 2 will derive splits from price discontinuities; Phase 1.1
        relies on the gRPC loader for corp-action data. Callers that
        need corp-action history should fall back to
        :class:`TinkoffInvestDataLoader.iter_corporate_actions`.
        """
        raise LoaderError(
            "TinkoffInvestMDDataLoader.iter_corporate_actions: MD archive "
            "has no corporate-action data. Use TinkoffInvestDataLoader "
            "for splits/dividends."
        )

    # ---- internal helpers ------------------------------------------------

    def _figi_for(self, ticker: str) -> TickerMeta | None:
        """Resolve ticker -> TickerMeta via the cached universe.

        Performs a single ``list_tickers`` call (cached by the gRPC
        loader) so we don't hit the upstream twice per backfill tick.
        """
        for m in self.list_tickers():
            if m.ticker.upper() == ticker.upper():
                return m
        return None


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def _resolve_token() -> str | None:
    """Pick the best Tinkoff token from env vars.

    Priority: explicit ``$TINKOFF_SANDBOX_TOKEN`` (sandbox preferred for
    backfill since it never places orders), then ``$TINKOFF_REAL_TOKEN``.
    Returns ``None`` if neither is set — caller raises ``LoaderAuthError``.
    """
    return os.environ.get("TINKOFF_SANDBOX_TOKEN") or os.environ.get("TINKOFF_REAL_TOKEN")


__all__ = [
    "TinkoffInvestMDDataLoader",
    "aggregate_minutes_to_daily",
]
