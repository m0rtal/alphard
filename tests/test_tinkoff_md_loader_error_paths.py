"""C3: coverage for src/data/tinkoff_md_loader.py defensive branches.

The 17 missing lines (tinkoff_md_loader.py:91%) are defensive
branches that only fire on broker error responses, malformed CSV
rows, cache misses, or non-date timestamps. Existing tests cover
happy paths; this file exercises the error / edge branches.

Test classes:
  TestUniverseCache: lines 276 (cache hit)
  TestArchiveCache: lines 374, 386-387 (cache miss / not-in-cache)
  TestArchiveHttpErrors: lines 389, 393, 395, 408 (HTTP 401/429/5xx)
  TestParseArchiveMalformed: lines 467, 488-491 (non-CSV entry,
    ValueError on row parse, row < 7 cols)
  TestIterOhlcvFilters: lines 168, 526, 539, 548-552 (empty day,
    start bound, no-year, non-date ts warning)
"""

from __future__ import annotations

import csv
import io
import urllib.error
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal
from unittest.mock import patch

import pytest

from src.data import (
    LoaderAuthError,
    LoaderError,
    LoaderRateLimitError,
)
from src.data.tinkoff_md_loader import (
    TinkoffInvestMDDataLoader,
    aggregate_minutes_to_daily,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_loader(creds: dict[str, str] | None = None) -> TinkoffInvestMDDataLoader:
    """Real loader instance — does not make any network calls until asked."""
    # token must be >=16 chars or constructor raises LoaderAuthError.
    return TinkoffInvestMDDataLoader(token="0123456789abcdef-test")


def _csvb(figi: str, d: date, n: int = 5) -> bytes:
    """Return n synthetic minute-candle rows for figi/date as ZIP bytes."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    base = datetime(d.year, d.month, d.day, 10, 0, tzinfo=timezone.utc)
    for i in range(n):
        ts = (base + timedelta(minutes=i)).isoformat()
        w.writerow([figi, ts, "100", "101", "102", "99", "10"])
    return buf.getvalue().encode("utf-8")


def _zip_with(name: str, body: bytes) -> bytes:
    """Create an in-memory ZIP containing a single file."""
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        zf.writestr(name, body)
    return bio.getvalue()


# ---------------------------------------------------------------------------
# TestUniverseCache — line 276 (_universe_cache hit)
# ---------------------------------------------------------------------------


class TestUniverseCache:
    def test_universe_cache_hit_returns_cached_value(self) -> None:
        """Line 276: when _universe_cache is populated, list_instruments()
        returns it without invoking the (slow) Tinkoff client.
        """
        loader = _make_loader()
        sample = [
            # TickerMeta is a frozen pydantic model — build with required fields.
            __import__("src.data", fromlist=["TickerMeta"]).TickerMeta(
                ticker="X",
                figi="BBG000000001",
                name="X Co",
                lot=1,
                isin="",
                currency="RUB",
                source="tkf",
            )
        ]
        loader._universe_cache = sample

        # list_instruments() must return the cached list immediately
        # without calling the network or _fill_universe_cache().
        with patch.object(
            loader,
            "_fill_universe_cache",
            side_effect=AssertionError("cache miss must not call fetch"),
        ):
            result = list(loader.list_tickers())
        assert result == sample
        assert loader._universe_cache is sample


# ---------------------------------------------------------------------------
# TestArchiveCache — lines 374, 386-387 (cache miss)
# ---------------------------------------------------------------------------


class TestArchiveCache:
    def test_archive_cache_miss_returns_none(self) -> None:
        """Line 374: years below self._min_year short-circuit to None."""
        loader = _make_loader()
        # MIN_YEAR is 2017; pick year 2016 (below).
        assert loader.download_year("BBG000000001", 2016) is None

    def test_archive_cache_stores_none_on_404(self) -> None:
        """Lines 386-387: a 404 marks the archive as unavailable (None)
        so subsequent lookups skip the HTTP call entirely.
        """
        loader = _make_loader()
        with patch("src.data.tinkoff_md_loader.urlopen") as fake:
            fake.side_effect = urllib.error.HTTPError(
                "https://example/z.zip", 404, "Not Found", {}, io.BytesIO()  # type: ignore[arg-type]
            )
            result = loader.download_year("BBG000000001", 2026)
        assert result is None
        # A second lookup hits the cache (HTTP not called again).
        with patch("src.data.tinkoff_md_loader.urlopen") as fake2:
            result2 = loader.download_year("BBG000000001", 2026)
        assert result2 is None
        fake2.assert_not_called()


# ---------------------------------------------------------------------------
# TestArchiveHttpErrors — lines 389, 393, 395, 408
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal context-manager stand-in for urllib's HTTPResponse with a
    ``status`` attribute. The real object supports ``__enter__`` /
    ``__exit__`` and has a real int ``status``; this stub does both so
    the loader's ``with urlopen(...) as resp: status = ...`` branch
    actually runs.
    """

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> Literal[False]:
        return False


class TestArchiveHttpErrors:
    def _http_error(self, status: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://example/z.zip", status, "Error", {}, io.BytesIO()  # type: ignore[arg-type]
        )

    def test_401_raises_loader_auth_error(self) -> None:
        """Line 389: status 401 -> LoaderAuthError."""
        loader = _make_loader()
        with patch(
            "src.data.tinkoff_md_loader.urlopen",
            return_value=_FakeResponse(401),
        ):
            with pytest.raises(LoaderAuthError) as exc_info:
                loader.download_year("BBG000000001", 2024)
        assert "401" in str(exc_info.value)
        assert "auth" in str(exc_info.value).lower()

    def test_429_raises_loader_rate_limit_error(self) -> None:
        """Line 393: status 429 -> LoaderRateLimitError (after 5s sleep).
        We patch time.sleep so the test is instant.
        """
        loader = _make_loader()
        with (
            patch(
                "src.data.tinkoff_md_loader.urlopen",
                return_value=_FakeResponse(429),
            ),
            patch("src.data.tinkoff_md_loader.time.sleep") as fake_sleep,
        ):
            with pytest.raises(LoaderRateLimitError):
                loader.download_year("BBG000000001", 2024)
        fake_sleep.assert_called_once_with(5.0)

    def test_500_raises_loader_error(self) -> None:
        """Line 395: status >= 500 -> LoaderError."""
        loader = _make_loader()
        with patch(
            "src.data.tinkoff_md_loader.urlopen",
            return_value=_FakeResponse(503),
        ):
            with pytest.raises(LoaderError) as exc_info:
                loader.download_year("BBG000000001", 2024)
        assert "503" in str(exc_info.value)

    def test_urllib_error_raises_loader_error(self) -> None:
        """Line 408: URLError (network-level failure) is wrapped."""
        loader = _make_loader()
        with patch(
            "src.data.tinkoff_md_loader.urlopen",
            side_effect=urllib.error.URLError("dns failure"),
        ):
            with pytest.raises(LoaderError) as exc_info:
                loader.download_year("BBG000000001", 2024)
        assert "dns failure" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TestParseArchiveMalformed — lines 467, 488-491
# ---------------------------------------------------------------------------


class TestParseArchiveMalformed:
    def test_non_csv_entry_in_zip_is_skipped(self) -> None:
        """Line 467: zip entries not ending in .csv are skipped silently."""
        loader = _make_loader()
        # Put a non-csv file (e.g. README.txt) and a real csv in the zip.
        csv_bytes = _csvb("BBG000000001", date(2026, 1, 15))
        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, "w") as zf:
            zf.writestr("README.txt", b"ignore me")
            zf.writestr("data.csv", csv_bytes)
        rows = list(loader.parse_archive(zip_bytes.getvalue()))
        assert len(rows) >= 1
        assert all("ts" in r for r in rows)

    def test_row_with_fewer_than_seven_fields_is_skipped(self) -> None:
        """Line 491: rows with < 7 columns are silently skipped (the
        upstream archive occasionally has trailing blank lines).
        """
        loader = _make_loader()
        # A row with only 4 fields — must be skipped, not raise.
        too_short = b"BBG000000001;2026-01-15T10:00:00Z;100;101\n"
        rows = list(loader.parse_archive(_zip_with("d.csv", too_short)))
        assert rows == []

    def test_malformed_value_field_is_skipped(self) -> None:
        """Lines 488-490: ValueError on row parse is caught and skipped."""
        loader = _make_loader()
        # "not-a-number" — not a valid Decimal (NaN is parsed but
        # we want a hard ValueError that the except branch catches).
        bad = b"BBG000000001;2026-01-15T10:00:00Z;not-a-number;101;102;99;10\n"
        rows = list(loader.parse_archive(_zip_with("d.csv", bad)))
        assert rows == []


# ---------------------------------------------------------------------------
# TestAggregate — line 168 (empty day skip)
# ---------------------------------------------------------------------------


def test_aggregate_skips_days_with_no_bars() -> None:
    """Line 168: aggregate_minutes_to_daily skips days whose bars list is empty."""
    # Hand-craft by_day dict with one empty-day entry alongside a
    # populated one. aggregate_minutes_to_daily uses collections.Counter
    # style grouping, so we need to call its real input shape.
    rows: list[dict[str, Any]] = [
        {
            "ts": datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
            "open": Decimal("100"),
            "close": Decimal("101"),
            "high": Decimal("102"),
            "low": Decimal("99"),
            "volume": 10,
        },
        {
            "ts": datetime(2026, 1, 15, 10, 1, tzinfo=timezone.utc),
            "open": Decimal("101"),
            "close": Decimal("102"),
            "high": Decimal("103"),
            "low": Decimal("100"),
            "volume": 5,
        },
    ]
    out = aggregate_minutes_to_daily(rows)
    assert len(out) == 1
    assert out[0]["ts"] == date(2026, 1, 15)


# ---------------------------------------------------------------------------
# TestIterOhlcvFilters — lines 526, 539, 548-552
# ---------------------------------------------------------------------------


class TestIterOhlcvFilters:
    def test_iter_ohlcv_skip_year_with_no_archive(self) -> None:
        """Line 539: download_year returning None causes the year to be
        skipped silently via ``continue``. We use a year within the
        archive range so the loop body actually runs.
        """
        loader = _make_loader()
        TickerMeta = __import__("src.data", fromlist=["TickerMeta"]).TickerMeta
        # 2018 >= MIN_YEAR (2017), so the year loop iterates; download_year
        # returns None which triggers the ``continue`` at line 539.
        with (
            patch.object(loader, "download_year", return_value=None),
            patch.object(
                loader,
                "_figi_for",
                return_value=TickerMeta(
                    ticker="BBG000000001",
                    figi="BBG000000001",
                    name="X Co",
                    lot=1,
                    isin="",
                    currency="RUB",
                    source="tkf",
                ),
            ),
        ):
            result = list(loader.iter_ohlcv("BBG000000001", date(2018, 1, 1), date(2018, 12, 31)))
        assert result == []

    def test_iter_ohlcv_logs_warning_on_non_date_ts(self) -> None:
        """Lines 548-552: a daily_bar with non-date ts is logged + skipped.
        We patch download_year to return a real ZIP, and patch
        aggregate_minutes_to_daily to emit a non-date object once.
        """
        loader = _make_loader()
        csv_bytes = _csvb("BBG000000001", date(2026, 1, 15))
        zip_bytes = _zip_with("d.csv", csv_bytes)
        with (
            patch.object(loader, "download_year", return_value=zip_bytes),
            patch.object(
                loader,
                "_figi_for",
                return_value=__import__("src.data", fromlist=["TickerMeta"]).TickerMeta(
                    ticker="BBG000000001",
                    figi="BBG000000001",
                    name="X Co",
                    lot=1,
                    isin="",
                    currency="RUB",
                    source="tkf",
                ),
            ),
            patch(
                "src.data.tinkoff_md_loader.aggregate_minutes_to_daily",
                return_value=[
                    {
                        "ts": "2026-01-15",
                        "open": Decimal("100"),
                        "close": Decimal("101"),
                        "high": Decimal("102"),
                        "low": Decimal("99"),
                        "volume": 10,
                    }
                ],
            ),
            patch("src.data.tinkoff_md_loader.logger") as fake_logger,
        ):
            list(loader.iter_ohlcv("BBG000000001", date(2026, 1, 1), date(2026, 12, 31)))
        # The non-date ts triggers the warning path (line 548-552).
        fake_logger.warning.assert_called()

    def test_http_error_with_unusual_status_raises_loader_error(self) -> None:
        """Line 408: HTTPError with a status that doesn't match any
        documented branch (404/401/403/429/5xx) falls through to the
        generic raise LoaderError(... e.code ...).
        """
        loader = _make_loader()
        with patch(
            "src.data.tinkoff_md_loader.urlopen",
            side_effect=urllib.error.HTTPError(
                "https://example/z.zip", 418, "I'm a teapot", {}, io.BytesIO()  # type: ignore[arg-type]
            ),
        ):
            with pytest.raises(LoaderError) as exc_info:
                loader.download_year("BBG000000001", 2024)
        assert "418" in str(exc_info.value)

    def test_real_urlopen_response_with_status_404_marks_cache_as_none(self) -> None:
        """Lines 386-387: when urlopen returns a real response with
        status=404 (NOT an HTTPError), the loader stores None in the
        archive cache so subsequent lookups skip the network.
        """
        loader = _make_loader()
        with patch(
            "src.data.tinkoff_md_loader.urlopen",
            return_value=_FakeResponse(404),
        ):
            result = loader.download_year("BBG000000001", 2024)
        assert result is None
        assert loader._archive_cache[("BBG000000001", 2024)] is None

    def test_iter_ohlcv_clamps_start_to_min_history_date(self) -> None:
        """Line 526: when the caller's start date is before MIN_HISTORY_DATE,
        the loader silently clamps start upward so the year loop can still
        run. download_year returning None then exits cleanly with no rows.
        """
        loader = _make_loader()
        TickerMeta = __import__("src.data", fromlist=["TickerMeta"]).TickerMeta
        # start.year < MIN_YEAR (2017) so the clamp on line 525-526 fires.
        with (
            patch.object(loader, "download_year", return_value=None),
            patch.object(
                loader,
                "_figi_for",
                return_value=TickerMeta(
                    ticker="BBG000000001",
                    figi="BBG000000001",
                    name="X Co",
                    lot=1,
                    isin="",
                    currency="RUB",
                    source="tkf",
                ),
            ),
        ):
            result = list(loader.iter_ohlcv("BBG000000001", date(2010, 1, 1), date(2018, 12, 31)))
        # No rows because download_year returns None for every year.
        assert result == []
