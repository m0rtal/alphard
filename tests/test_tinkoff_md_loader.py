"""Tests for TinkoffInvestMDDataLoader — MD archive backfill path.

Covers:
- ``aggregate_minutes_to_daily`` (pure function, no I/O).
- Token resolution / auth.
- Yearly archive download via mocked urllib.
- 404 / 429 / 500 error mapping.
- ZIP parsing (synthetic bytes, no real network).
- ``iter_ohlcv`` end-to-end with mocked upstream.
- Idempotency semantics (cached archive bytes).
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from src.data import (
    LoaderAuthError,
    LoaderError,
    LoaderNotFoundError,
    LoaderRateLimitError,
    TickerMeta,
)
from src.data.tinkoff_md_loader import (
    TinkoffInvestMDDataLoader,
    aggregate_minutes_to_daily,
)


# ---------------------------------------------------------------------------
# Pure aggregator
# ---------------------------------------------------------------------------


def _minute(ts_iso: str, o: str, c: str, h: str, lo: str, v: int) -> dict:
    return {
        "ts": datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone(timezone.utc),
        "open": Decimal(o),
        "close": Decimal(c),
        "high": Decimal(h),
        "low": Decimal(lo),
        "volume": v,
    }


class TestAggregator:
    def test_empty(self) -> None:
        assert aggregate_minutes_to_daily([]) == []

    def test_single_minute(self) -> None:
        rows = [_minute("2024-03-15T07:00:00Z", "100", "101", "102", "99", "1000")]
        out = aggregate_minutes_to_daily(rows)
        assert len(out) == 1
        d = out[0]
        assert d["ts"] == date(2024, 3, 15)
        assert d["open"] == Decimal("100")
        assert d["close"] == Decimal("101")
        assert d["high"] == Decimal("102")
        assert d["low"] == Decimal("99")
        assert d["volume"] == 1000

    def test_ohlc_invariants(self) -> None:
        """open = first, close = last, high = max, low = min, vol = sum."""
        rows = [
            _minute("2024-03-15T07:00:00Z", "100", "101", "101", "100", "100"),
            _minute("2024-03-15T07:01:00Z", "101", "103", "104", "100", "200"),
            _minute("2024-03-15T07:02:00Z", "103", "102", "103", "98", "300"),
        ]
        out = aggregate_minutes_to_daily(rows)
        assert len(out) == 1
        d = out[0]
        assert d["open"] == Decimal("100")  # first.open
        assert d["close"] == Decimal("102")  # last.close
        assert d["high"] == Decimal("104")  # max of all highs
        assert d["low"] == Decimal("98")  # min of all lows
        assert d["volume"] == 600  # sum

    def test_splits_by_date(self) -> None:
        rows = [
            _minute("2024-03-15T07:00:00Z", "100", "101", "101", "100", "100"),
            _minute("2024-03-15T07:01:00Z", "101", "102", "102", "101", "200"),
            _minute("2024-03-16T07:00:00Z", "200", "202", "203", "199", "500"),
        ]
        out = aggregate_minutes_to_daily(rows)
        assert len(out) == 2
        assert out[0]["ts"] == date(2024, 3, 15)
        assert out[0]["volume"] == 300
        assert out[1]["ts"] == date(2024, 3, 16)
        assert out[1]["volume"] == 500

    def test_unsorted_input_is_normalised(self) -> None:
        rows = [
            _minute("2024-03-15T07:02:00Z", "103", "102", "103", "98", "300"),
            _minute("2024-03-15T07:00:00Z", "100", "101", "101", "100", "100"),
        ]
        out = aggregate_minutes_to_daily(rows)
        assert out[0]["open"] == Decimal("100")  # first after sort
        assert out[0]["close"] == Decimal("102")  # last after sort


# ---------------------------------------------------------------------------
# Token / auth
# ---------------------------------------------------------------------------


class TestTokenResolution:
    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TINKOFF_SANDBOX_TOKEN", raising=False)
        monkeypatch.delenv("TINKOFF_REAL_TOKEN", raising=False)
        with pytest.raises(LoaderAuthError):
            TinkoffInvestMDDataLoader()

    def test_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")
        loader = TinkoffInvestMDDataLoader()
        assert loader._token == "fake-token-1234567890"

    def test_explicit_token_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "env-token-1234567890")
        loader = TinkoffInvestMDDataLoader(token="explicit-token-1234567890")
        assert loader._token == "explicit-token-1234567890"

    def test_short_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(LoaderAuthError):
            TinkoffInvestMDDataLoader(token="short")

    def test_real_token_used_when_sandbox_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TINKOFF_SANDBOX_TOKEN", raising=False)
        monkeypatch.setenv("TINKOFF_REAL_TOKEN", "real-token-1234567890")
        loader = TinkoffInvestMDDataLoader()
        assert loader._token == "real-token-1234567890"

    def test_min_year_below_2017_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")
        with pytest.raises(LoaderError):
            TinkoffInvestMDDataLoader(min_year=2010)


# ---------------------------------------------------------------------------
# Archive parsing
# ---------------------------------------------------------------------------


def _make_zip(minute_rows: list[tuple[str, str, str, str, str, int]]) -> bytes:
    """Build an in-memory ZIP of a single day's CSV.

    ``minute_rows`` is a list of (ts, open, close, high, low, volume).
    Format matches Tinkoff: ``<figi>;<ts>;<o>;<c>;<h>;<l>;<v>``.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        with zf.open("20240101.csv", mode="w") as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
            writer = csv.writer(text, delimiter=";")
            for r in minute_rows:
                writer.writerow(["BBG004730N88", *r])
            text.flush()
            text.detach()
    return buf.getvalue()


class TestArchiveParsing:
    def test_parses_valid_archive(self) -> None:
        z = _make_zip(
            [
                ("2024-01-01T07:00:00Z", "100", "101", "102", "99", "1000"),
                ("2024-01-01T07:01:00Z", "101", "102", "103", "100", "500"),
            ]
        )
        loader = TinkoffInvestMDDataLoader.__new__(TinkoffInvestMDDataLoader)
        out = loader.parse_archive(z)
        assert len(out) == 2
        assert out[0]["ts"].year == 2024
        assert out[1]["close"] == Decimal("102")

    def test_skips_malformed_rows(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w") as zf:
            with zf.open("m.csv", mode="w") as fh:
                fh.write(b"BBG;2024-01-01T07:00:00Z;100;101;102;99;1000\n")
                fh.write(b"TOO_SHORT\n")  # malformed
                fh.write(b"\n")  # empty
        loader = TinkoffInvestMDDataLoader.__new__(TinkoffInvestMDDataLoader)
        out = loader.parse_archive(buf.getvalue())
        assert len(out) == 1

    def test_bad_zip_raises(self) -> None:
        loader = TinkoffInvestMDDataLoader.__new__(TinkoffInvestMDDataLoader)
        with pytest.raises(LoaderError):
            loader.parse_archive(b"not a zip file")

    def test_rejects_zip_bomb_total_size(self) -> None:
        """BUGFIX (H-2): total uncompressed size over the cap must raise.

        We can't actually allocate 500 MB in CI, but we can override the
        class attribute to a tiny limit and verify the guard fires.
        """
        loader = TinkoffInvestMDDataLoader.__new__(TinkoffInvestMDDataLoader)
        loader._MAX_UNCOMPRESSED_BYTES = 10  # tiny cap
        loader._MAX_INFLATE_RATIO = 100
        # _make_zip produces a multi-byte archive; 10 bytes is well below.
        z = _make_zip([("2024-01-01T07:00:00Z", "100", "101", "102", "99", "1000")])
        with pytest.raises(LoaderError, match="exceeds max uncompressed size"):
            loader.parse_archive(z)

    def test_rejects_zip_bomb_inflate_ratio(self) -> None:
        """BUGFIX (H-2): per-file inflate ratio over the cap must raise."""
        loader = TinkoffInvestMDDataLoader.__new__(TinkoffInvestMDDataLoader)
        loader._MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB
        loader._MAX_INFLATE_RATIO = 5  # tiny ratio cap
        # Hand-build a ZIP whose single CSV compresses better than 5x —
        # uses 1000 highly-compressible zero-rows to push inflate ratio up.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            with zf.open("20240101.csv", mode="w") as fh:
                text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
                w = csv.writer(text, delimiter=";")
                for _ in range(1000):
                    w.writerow(["BBG004730N88"] + ["0"] * 6)
                text.flush()
                text.detach()
        z = buf.getvalue()
        with pytest.raises(LoaderError, match="ZIP bomb suspected"):
            loader.parse_archive(z)


# ---------------------------------------------------------------------------
# Download (HTTP mocked)
# ---------------------------------------------------------------------------


def _urlopen_side_effect(status: int, body: bytes = b""):
    """Build a side-effect callable for urllib.request.urlopen patching."""

    def side_effect(req: Any, timeout: int = 0) -> Any:
        if status == 200:
            resp = MagicMock()
            resp.status = 200
            resp.read = lambda: body
            resp.__enter__ = lambda self: self
            resp.__exit__ = lambda self, *a: None
            return resp
        if status == 404:
            raise HTTPError(req.full_url, 404, "Not Found", {}, None)
        if status == 401:
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        if status == 429:
            raise HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        if status >= 500:
            raise HTTPError(req.full_url, status, "Server Error", {}, None)
        raise HTTPError(req.full_url, status, "?", {}, None)

    return side_effect


class TestDownload:
    def _loader(self, monkeypatch: pytest.MonkeyPatch) -> TinkoffInvestMDDataLoader:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")
        # Use a permissive bucket so tests don't block.
        from src.data.token_bucket import TokenBucket

        return TinkoffInvestMDDataLoader(bucket=TokenBucket(rate=1000.0, window_seconds=1.0))

    def test_404_caches_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        with patch("src.data.tinkoff_md_loader.urlopen", side_effect=_urlopen_side_effect(404)):
            result = loader.download_year("BBG004730N88", 2024)
            assert result is None
            assert loader._archive_cache[("BBG004730N88", 2024)] is None
            # Second call: still no upstream hit (cached).
            with patch("src.data.tinkoff_md_loader.urlopen") as urlopen:
                loader.download_year("BBG004730N88", 2024)
                urlopen.assert_not_called()

    def test_200_caches_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        body = b"fake-zip-bytes"
        with patch("src.data.tinkoff_md_loader.urlopen", side_effect=_urlopen_side_effect(200, body)):
            r = loader.download_year("BBG004730N88", 2024)
            assert r == body
            r2 = loader.download_year("BBG004730N88", 2024)
            assert r2 == body  # cached, no second HTTP call

    def test_401_maps_to_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        with patch("src.data.tinkoff_md_loader.urlopen", side_effect=_urlopen_side_effect(401)):
            with pytest.raises(LoaderAuthError):
                loader.download_year("BBG004730N88", 2024)

    def test_429_maps_to_rate_limit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        with patch("src.data.tinkoff_md_loader.urlopen", side_effect=_urlopen_side_effect(429)):
            with pytest.raises(LoaderRateLimitError):
                loader.download_year("BBG004730N88", 2024)

    def test_500_maps_to_loader_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        with patch("src.data.tinkoff_md_loader.urlopen", side_effect=_urlopen_side_effect(503)):
            with pytest.raises(LoaderError):
                loader.download_year("BBG004730N88", 2024)

    def test_url_error_maps_to_loader_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)

        def boom(req: Any, timeout: int = 0) -> Any:
            raise URLError("connection refused")

        with patch("src.data.tinkoff_md_loader.urlopen", side_effect=boom):
            with pytest.raises(LoaderError):
                loader.download_year("BBG004730N88", 2024)


# ---------------------------------------------------------------------------
# iter_ohlcv end-to-end
# ---------------------------------------------------------------------------


class TestIterOHLCV:
    def _loader(self, monkeypatch: pytest.MonkeyPatch) -> TinkoffInvestMDDataLoader:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")
        from src.data.token_bucket import TokenBucket

        return TinkoffInvestMDDataLoader(bucket=TokenBucket(rate=1000.0, window_seconds=1.0))

    def _stub_universe(self, loader: TinkoffInvestMDDataLoader, ticker: str = "SBER") -> None:
        """Inject a cached universe entry for SBER -> BBG004730N88."""
        meta = TickerMeta(
            ticker=ticker,
            figi="BBG004730N88",
            name="Sber",
            lot=1,
            source="tkf",
        )
        # Patch the universe helper to return this single entry.
        loader._figi_for = lambda t: meta if t.upper() == ticker else None  # type: ignore[assignment]

    def test_aggregates_two_days(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        self._stub_universe(loader, "SBER")

        # Two yearly archives, each a one-day ZIP.
        z_2024 = _make_zip(
            [
                ("2024-01-01T07:00:00Z", "100", "101", "102", "99", "100"),
                ("2024-01-01T07:01:00Z", "101", "102", "103", "100", "200"),
            ]
        )
        z_2025 = _make_zip(
            [
                ("2025-06-15T07:00:00Z", "300", "302", "305", "298", "700"),
                ("2025-06-15T07:01:00Z", "302", "303", "304", "300", "300"),
            ]
        )

        def fake_download(figi: str, year: int) -> bytes | None:
            if year == 2024:
                return z_2024
            if year == 2025:
                return z_2025
            return None

        with patch.object(loader, "download_year", side_effect=fake_download):
            bars = list(loader.iter_ohlcv("SBER", date(2024, 1, 1), date(2025, 12, 31)))
        assert len(bars) == 2
        b0 = bars[0]
        assert b0.ticker == "SBER"
        assert b0.ts == date(2024, 1, 1)
        assert b0.open == Decimal("100")
        assert b0.close == Decimal("102")
        assert b0.high == Decimal("103")
        assert b0.low == Decimal("99")
        assert b0.volume == Decimal(300)
        assert b0.adj_close == Decimal("102")
        # Second bar — 2025
        b1 = bars[1]
        assert b1.ts == date(2025, 6, 15)
        assert b1.volume == Decimal(1000)

    def test_missing_figi_raises_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        # Empty universe — no SBER -> NotFound.
        with patch.object(loader, "list_tickers", return_value=[]):
            with pytest.raises(LoaderNotFoundError):
                list(loader.iter_ohlcv("DOES_NOT_EXIST", date(2024, 1, 1), date(2024, 1, 2)))

    def test_start_after_end_yields_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        self._stub_universe(loader, "SBER")
        with patch.object(loader, "download_year", return_value=None):
            bars = list(loader.iter_ohlcv("SBER", date(2024, 5, 1), date(2024, 1, 1)))
        assert bars == []

    def test_window_filters_inside_year(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        self._stub_universe(loader, "SBER")
        z = _make_zip(
            [
                ("2024-01-01T07:00:00Z", "100", "101", "102", "99", "100"),
                ("2024-12-31T07:00:00Z", "500", "510", "520", "490", "5000"),
            ]
        )
        with patch.object(loader, "download_year", return_value=z):
            bars = list(loader.iter_ohlcv("SBER", date(2024, 6, 1), date(2024, 6, 30)))
        # 2024-01-01 and 2024-12-31 are both outside the window -> 0 bars.
        assert bars == []

    def test_corp_actions_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loader = self._loader(monkeypatch)
        with pytest.raises(LoaderError):
            list(loader.iter_corporate_actions("SBER", date(2024, 1, 1), date(2024, 2, 1)))


# ---------------------------------------------------------------------------
# Universe delegation
# ---------------------------------------------------------------------------


class TestUniverse:
    def test_list_tickers_harvests_all_class_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")
        from src.data.token_bucket import TokenBucket

        loader = TinkoffInvestMDDataLoader(bucket=TokenBucket(rate=1000.0, window_seconds=1.0))

        fake_tqbr = [
            TickerMeta(ticker="SBER", figi="BBG004730N88", class_code="TQBR", name="Sber", lot=1, source="tkf"),
            TickerMeta(ticker="NODELIST", figi=None, class_code="TQBR", name="X", lot=1, source="tkf"),
        ]
        fake_spbxm = [
            TickerMeta(ticker="AAPL", figi="BBG000B9XRY4", class_code="SPBXM", name="Apple", lot=1, source="tkf"),
        ]
        with patch("src.data.tinkoff_loader.TinkoffInvestDataLoader") as mock_grpc:
            mock_grpc.return_value.list_shares_all.side_effect = [fake_tqbr, fake_spbxm, [], [], [], [], []]
            mock_grpc.return_value.list_bonds.return_value = []
            mock_grpc.return_value.list_etfs.return_value = []
            metas = loader.list_tickers_with_figi()
        assert len(metas) == 2
        tickers = {m.ticker for m in metas}
        assert tickers == {"SBER", "AAPL"}

    def test_list_tickers_continues_on_class_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")
        from src.data.token_bucket import TokenBucket

        loader = TinkoffInvestMDDataLoader(bucket=TokenBucket(rate=1000.0, window_seconds=1.0))

        good = [TickerMeta(ticker="SBER", figi="BBG004730N88", class_code="TQBR", name="Sber", lot=1, source="tkf")]
        with patch("src.data.tinkoff_loader.TinkoffInvestDataLoader") as mock_grpc:
            # TQBR fails, SPBXM succeeds.
            mock_grpc.return_value.list_shares_all.side_effect = [RuntimeError("rate"), good, [], [], [], [], []]
            mock_grpc.return_value.list_bonds.return_value = []
            mock_grpc.return_value.list_etfs.return_value = []
            metas = loader.list_tickers_with_figi()
        assert len(metas) == 1
        assert metas[0].ticker == "SBER"

    def test_list_tickers_includes_bonds_and_etfs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bonds (TQOB/TQCB) and ETFs (TQTE) must be merged into the universe
        on the backfill path — no client-side filter on asset type."""
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")
        from src.data.token_bucket import TokenBucket

        loader = TinkoffInvestMDDataLoader(bucket=TokenBucket(rate=1000.0, window_seconds=1.0))

        fake_share = [
            TickerMeta(ticker="SBER", figi="BBG004730N88", class_code="TQBR", name="Sber", lot=1, source="tkf")
        ]
        fake_bond = [
            TickerMeta(ticker="RU000A0ZZZ", figi="BBG00BOND001", class_code="TQOB", name="OFZ", lot=1, source="tkf")
        ]
        fake_etf = [
            TickerMeta(ticker="FXUS", figi="BBG00ETF001", class_code="TQTE", name="FinEx US", lot=1, source="tkf")
        ]
        with patch("src.data.tinkoff_loader.TinkoffInvestDataLoader") as mock_grpc:
            mock_grpc.return_value.list_shares_all.side_effect = [fake_share, [], [], [], [], [], []]
            mock_grpc.return_value.list_bonds.return_value = fake_bond
            mock_grpc.return_value.list_etfs.return_value = fake_etf
            metas = loader.list_tickers_with_figi()
        tickers = {m.ticker for m in metas}
        assert tickers == {"SBER", "RU000A0ZZZ", "FXUS"}

    def test_list_tickers_continues_when_bonds_or_etfs_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If list_bonds() / list_etfs() raise, shares must still come through."""
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")
        from src.data.token_bucket import TokenBucket

        loader = TinkoffInvestMDDataLoader(bucket=TokenBucket(rate=1000.0, window_seconds=1.0))

        fake_share = [
            TickerMeta(ticker="SBER", figi="BBG004730N88", class_code="TQBR", name="Sber", lot=1, source="tkf")
        ]
        with patch("src.data.tinkoff_loader.TinkoffInvestDataLoader") as mock_grpc:
            mock_grpc.return_value.list_shares_all.side_effect = [fake_share, [], [], [], [], [], []]
            mock_grpc.return_value.list_bonds.side_effect = RuntimeError("rate limit")
            mock_grpc.return_value.list_etfs.side_effect = RuntimeError("auth")
            metas = loader.list_tickers_with_figi()
        tickers = {m.ticker for m in metas}
        assert tickers == {"SBER"}

    def test_figi_for_returns_none_for_unknown_ticker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_figi_for returns None when ticker is not in cached universe.

        This covers the runtime check from issue #14 D.2 (assert -> runtime)
        in src/data/tinkoff_md_loader.py:540-549.
        """
        monkeypatch.setenv("TINKOFF_SANDBOX_TOKEN", "fake-token-1234567890")
        from src.data.token_bucket import TokenBucket
        from src.data.models import TickerMeta

        loader = TinkoffInvestMDDataLoader(bucket=TokenBucket(rate=1000.0, window_seconds=1.0))

        # Mock list_tickers so _figi_for has a deterministic universe
        # without needing a real gRPC connection.
        cached = [
            TickerMeta(
                ticker="SBER",
                figi="BBG004730N88",
                class_code="TQBR",
                name="Sber",
                lot=1,
                source="tkf",
            ),
        ]
        monkeypatch.setattr(loader, "list_tickers", lambda: cached)

        # SBER is cached -> TickerMeta returned
        assert loader._figi_for("SBER") is not None
        # Unknown ticker -> None (covers line 549)
        assert loader._figi_for("UNKNOWN") is None
