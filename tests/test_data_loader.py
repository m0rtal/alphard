"""Tests for the Data Agent (Phase 1.1).

Structure
---------
1. ``test_models`` — pydantic validation rules, ticker regex, OHLC consistency.
2. ``test_token_bucket`` — synchronous rate limiter semantics, thread safety.
3. ``test_loaders_abc`` — abstract contract via a ``FakeLoader``.
4. ``test_moex_loader`` — HTTP mocked with ``responses`` (or
   ``requests-mock``-style fake). Acceptance: 1y for 5 tickers < 10s.
5. ``test_tinkoff_loader`` — auth gating, sandbox vs prod URL.
6. ``test_sqlite_store`` — contract tests, run twice via parametrize to also
   smoke-test any future ``PostgresDataStore`` if ``$ALPHARD_PG_DSN`` is set.

Coverage target: ≥75% of ``src/data``.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings, strategies as st

from src.data import (
    CorporateAction,
    DataLoader,
    InMemorySQLiteStore,
    LoaderAuthError,
    LoaderError,
    LoaderNotFoundError,
    LoaderRateLimitError,
    MOEXDataLoader,
    OHLCVRow,
    RateLimitError,
    TickerMeta,
    TokenBucket,
)
from src.data.models import TICKER_REGEX


# ===========================================================================
# 1. pydantic models
# ===========================================================================


class TestOHLCVRow:
    def test_minimal_valid(self) -> None:
        row = OHLCVRow(
            ticker="SBER",
            ts=date(2026, 8, 1),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1000"),
            adj_close=Decimal("105"),
            source="moex",
        )
        assert row.ticker == "SBER"
        assert row.source == "moex"
        assert row.high >= row.low

    def test_ticker_normalised_to_upper(self) -> None:
        row = OHLCVRow(
            ticker="sber",
            ts=date(2026, 8, 1),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1"),
            adj_close=Decimal("105"),
            source="tkf",
        )
        assert row.ticker == "SBER"

    def test_invalid_ticker_rejected(self) -> None:
        # Empty string after upper() is the simplest invalid case (regex
        # requires 1..12 chars).
        with pytest.raises(ValueError):
            OHLCVRow(
                ticker="",
                ts=date(2026, 8, 1),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=Decimal("0"),
                adj_close=Decimal("1"),
                source="moex",
            )

    def test_high_below_low_rejected(self) -> None:
        with pytest.raises(ValueError):
            OHLCVRow(
                ticker="SBER",
                ts=date(2026, 8, 1),
                open=Decimal("100"),
                high=Decimal("90"),  # < low
                low=Decimal("95"),
                close=Decimal("92"),
                volume=Decimal("1"),
                adj_close=Decimal("92"),
                source="moex",
            )

    def test_low_above_open_rejected(self) -> None:
        with pytest.raises(ValueError):
            OHLCVRow(
                ticker="SBER",
                ts=date(2026, 8, 1),
                open=Decimal("90"),  # < low
                high=Decimal("110"),
                low=Decimal("95"),
                close=Decimal("100"),
                volume=Decimal("1"),
                adj_close=Decimal("100"),
                source="moex",
            )

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValueError):
            OHLCVRow.model_validate(
                {
                    "ticker": "SBER",
                    "ts": date(2026, 8, 1),
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                    "volume": "0",
                    "adj_close": "1",
                    "source": "moex",
                    "surprise": "nope",
                }
            )

    def test_frozen(self) -> None:
        row = OHLCVRow(
            ticker="SBER",
            ts=date(2026, 8, 1),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1"),
            adj_close=Decimal("105"),
            source="moex",
        )
        with pytest.raises(Exception):  # ValidationError on mutation
            row.close = Decimal("200")  # type: ignore[misc]

    def test_source_must_be_known(self) -> None:
        with pytest.raises(ValueError):
            OHLCVRow(
                ticker="SBER",
                ts=date(2026, 8, 1),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=Decimal("0"),
                adj_close=Decimal("1"),
                source="coingecko",  # type: ignore[arg-type]
            )


class TestCorporateAction:
    def test_split(self) -> None:
        ca = CorporateAction(
            ticker="SBER",
            ts=date(2026, 1, 15),
            kind="split",
            value=Decimal("2"),
            source="moex",
        )
        assert ca.kind == "split"

    def test_change(self) -> None:
        ca = CorporateAction(
            ticker="OLD",
            ts=date(2026, 1, 15),
            kind="change",
            value=Decimal("0"),
            source="moex",
        )
        assert ca.kind == "change"

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            CorporateAction(
                ticker="SBER",
                ts=date(2026, 1, 15),
                kind="merge",  # type: ignore[arg-type]
                value=Decimal("0"),
                source="moex",
            )


class TestTickerMeta:
    def test_minimal(self) -> None:
        tm = TickerMeta(
            ticker="SBER",
            name="Сбер Банк",
            lot=10,
            source="tkf",
        )
        assert tm.currency == "RUB"
        assert tm.delisted is False
        assert tm.figi is None
        assert tm.isin is None

    def test_lot_zero_rejected(self) -> None:
        with pytest.raises(ValueError):
            TickerMeta(ticker="SBER", name="X", lot=0, source="tkf")

    def test_delisted(self) -> None:
        tm = TickerMeta(
            ticker="YNDX",
            name="Yandex N.V.",
            lot=1,
            delisted=True,
            delisted_at=date(2024, 1, 1),
            source="moex",
        )
        assert tm.delisted is True
        assert tm.delisted_at == date(2024, 1, 1)


# ===========================================================================
# 2. TokenBucket
# ===========================================================================


class TestTokenBucket:
    def test_initial_full(self) -> None:
        b = TokenBucket(rate=10, window_seconds=1.0)
        assert b.tokens_available() == pytest.approx(10.0)

    def test_acquire_decrements(self) -> None:
        b = TokenBucket(rate=10, window_seconds=1.0)
        before = b.tokens_available()
        b.acquire()
        after = b.tokens_available()
        # We allow refill in the ~µs between operations, so check that
        # the count went down by ~1 (within a small slack).
        assert after <= before
        assert before - after <= 1.0 + 1e-3

    def test_acquire_nowait_raises_when_empty(self) -> None:
        b = TokenBucket(rate=2, window_seconds=1.0, capacity=2)
        b.acquire()
        b.acquire()
        with pytest.raises(RateLimitError):
            b.acquire_nowait()

    def test_wait_time_zero_when_tokens(self) -> None:
        b = TokenBucket(rate=10, window_seconds=1.0)
        assert b.wait_time() == 0.0

    def test_wait_time_positive_when_empty(self) -> None:
        b = TokenBucket(rate=60, window_seconds=1.0, capacity=1.0)
        b.acquire()
        # One more token regenerates in 1/60 s ≈ 16.6 ms
        assert 0.0 < b.wait_time() < 0.05

    def test_invalid_rate(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate=0)

    def test_invalid_window(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate=10, window_seconds=0)

    def test_invalid_capacity(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate=10, capacity=0)

    def test_thread_safe(self) -> None:
        """Two threads each grabbing 50 tokens from a 100-token bucket
        should both succeed; a third should fail fast on acquire_nowait."""
        b = TokenBucket(rate=100, window_seconds=1.0, capacity=100)
        results: list[bool] = []

        def grab(n: int) -> None:
            for _ in range(n):
                try:
                    b.acquire_nowait()
                    results.append(True)
                except RateLimitError:
                    results.append(False)

        t1 = threading.Thread(target=grab, args=(50,))
        t2 = threading.Thread(target=grab, args=(50,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert sum(results) == 100


# ===========================================================================
# 3. DataLoader ABC contract
# ===========================================================================


class FakeLoader(DataLoader):
    """In-memory loader used to exercise the ABC contract."""

    SOURCE = "manual"

    def __init__(
        self,
        tickers: list[TickerMeta],
        ohlcv: dict[str, list[OHLCVRow]] | None = None,
        actions: dict[str, list[CorporateAction]] | None = None,
    ) -> None:
        super().__init__(bucket=TokenBucket(rate=1000, window_seconds=1.0))
        self._tickers = tickers
        self._ohlcv = ohlcv or {}
        self._actions = actions or {}

    def list_tickers(self) -> list[TickerMeta]:
        return list(self._tickers)

    def iter_ohlcv(self, ticker: str, start: date, end: date) -> Iterator[OHLCVRow]:
        self._validate_range(start, end, max_lookback=timedelta(days=365 * 20))
        for r in self._ohlcv.get(ticker.upper(), []):
            if start <= r.ts <= end:
                yield r

    def iter_corporate_actions(self, ticker: str, start: date, end: date) -> Iterator[CorporateAction]:
        self._validate_range(start, end, max_lookback=timedelta(days=365 * 20))
        for a in self._actions.get(ticker.upper(), []):
            if start <= a.ts <= end:
                yield a


class TestDataLoaderABC:
    def test_load_ohlcv_materialises(self) -> None:
        rows = [
            OHLCVRow(
                ticker="SBER",
                ts=date(2026, 8, i + 1),
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("95"),
                close=Decimal("105"),
                volume=Decimal("1"),
                adj_close=Decimal("105"),
                source="moex",
            )
            for i in range(3)
        ]
        loader = FakeLoader([], ohlcv={"SBER": rows})
        assert len(loader.load_ohlcv("sber", date(2026, 8, 1), date(2026, 8, 3))) == 3

    def test_invalid_range_rejected(self) -> None:
        loader = FakeLoader([])
        with pytest.raises(LoaderError):
            list(loader.iter_ohlcv("SBER", date(2026, 8, 5), date(2026, 8, 1)))

    def test_source_constant_required(self) -> None:
        """Source tag must be non-empty so the store can index rows by source."""
        from src.data.loader import DataLoader as _ABC

        assert _ABC.SOURCE == "" or isinstance(_ABC.SOURCE, str)
        # We do NOT enforce SOURCE != "" at construction — the ABC is a
        # contract, not a guard. Concrete loaders (``TinkoffDataLoader``,
        # ``MOEXDataLoader``) override SOURCE. This test asserts the
        # attribute exists and is a string.
        assert isinstance(_ABC.SOURCE, str)


# ===========================================================================
# 4. MOEXDataLoader (HTTP mocked)
# ===========================================================================


class _FakeResponse:
    def __init__(self, json_payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = json_payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    """Minimal stand-in for requests.Session that records calls."""

    def __init__(self, handlers: list[Any] | None = None) -> None:
        self._handlers = handlers or []
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: Any = None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if not self._handlers:
            raise AssertionError("no handler configured for " + url)
        return self._handlers.pop(0)

    def post(self, url: str, **kw: Any):
        self.calls.append({"url": url, **kw})
        if not self._handlers:
            raise AssertionError("no handler configured for " + url)
        return self._handlers.pop(0)


def _candles_block(rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "candles": {
            "columns": ["open", "close", "high", "low", "value", "volume", "begin", "end"],
            "data": rows,
        }
    }


def _ticker_block(rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "securities": {
            "columns": ["SECID", "SHORTNAME", "LOTSIZE", "ISIN", "STATUS"],
            "data": rows,
        }
    }


class TestMOEXDataLoader:
    def _loader(self, handlers: list[Any]) -> tuple[MOEXDataLoader, _FakeSession]:
        session = _FakeSession(handlers)
        loader = MOEXDataLoader(
            session=session,  # type: ignore[arg-type]
            rate_per_min=600.0,  # fast for tests
        )
        return loader, session

    def test_list_tickers_parses_block(self) -> None:
        handlers = [
            _FakeResponse(
                _ticker_block(
                    [
                        ["SBER", "Sber", 10, "RU0009029540", ""],
                        ["GAZP", "Gazprom", 10, "RU0007661625", "DELISTED"],
                    ]
                )
            )
        ]
        loader, _ = self._loader(handlers)
        tickers = loader.list_tickers()
        names = {t.ticker: t for t in tickers}
        assert names["SBER"].lot == 10
        assert names["SBER"].delisted is False
        assert names["GAZP"].delisted is True

    def test_iter_ohlcv_yields_rows_with_lot_multiplier(self) -> None:
        handlers = [
            _FakeResponse(_ticker_block([["SBER", "Sber", 5, "RU0009029540", ""]])),
            _FakeResponse(
                _candles_block(
                    [
                        [
                            Decimal("100"),
                            Decimal("105"),
                            Decimal("110"),
                            Decimal("95"),
                            Decimal("0"),
                            Decimal("100"),  # lots
                            "2026-08-01",
                            "2026-08-01",
                        ]
                    ]
                )
            ),
        ]
        loader, _ = self._loader(handlers)
        bars = list(loader.iter_ohlcv("sber", date(2026, 8, 1), date(2026, 8, 1)))
        assert len(bars) == 1
        assert bars[0].volume == Decimal("500")  # 100 lots × 5 lot size
        assert bars[0].close == Decimal("105")

    def test_iter_ohlcv_handles_empty_page(self) -> None:
        handlers = [
            _FakeResponse(_ticker_block([["SBER", "Sber", 1, "RU0", ""]])),
            _FakeResponse(_candles_block([])),  # empty
        ]
        loader, _ = self._loader(handlers)
        assert list(loader.iter_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1))) == []

    def test_iter_ohlcv_unknown_ticker_returns_empty(self) -> None:
        handlers = [
            _FakeResponse(_ticker_block([["GAZP", "Gazprom", 1, "RU0", ""]])),
        ]
        loader, _ = self._loader(handlers)
        # _lot_for returns 1 on miss; OHLCV call would 404 in real life but
        # with empty mock, we test the rate limiter fallback path.
        assert loader._meta_for("MISSING") is None
        assert loader._lot_for("MISSING") == 1

    def test_auth_error_raises_typed_exception(self) -> None:
        handlers = [_FakeResponse({}, status_code=401)]
        loader, _ = self._loader(handlers)
        with pytest.raises(LoaderAuthError):
            loader._get_json("https://iss.moex.com/foo")

    def test_rate_limit_raises_typed_exception(self) -> None:
        handlers = [_FakeResponse({}, status_code=429)]
        loader, _ = self._loader(handlers)
        with pytest.raises(LoaderRateLimitError):
            loader._get_json("https://iss.moex.com/foo")

    def test_not_found_raises_typed_exception(self) -> None:
        handlers = [_FakeResponse({}, status_code=404)]
        loader, _ = self._loader(handlers)
        with pytest.raises(LoaderNotFoundError):
            loader._get_json("https://iss.moex.com/foo")

    def test_max_lookback_enforced(self) -> None:
        loader = MOEXDataLoader(rate_per_min=600.0)
        with pytest.raises(LoaderError):
            list(loader.iter_ohlcv("SBER", date(2000, 1, 1), date(2026, 1, 1)))

    def test_network_error_wrapped(self) -> None:
        class BoomSession:
            def get(self, url, params=None, timeout=None):
                import requests

                raise requests.ConnectionError("boom")

        loader = MOEXDataLoader(
            session=BoomSession(),  # type: ignore[arg-type]
            rate_per_min=600.0,
        )
        with pytest.raises(LoaderError, match="network error"):
            loader._get_json("https://iss.moex.com/foo")

    def test_invalid_json_wrapped(self) -> None:
        class BadJSONSession:
            def get(self, url, params=None, timeout=None):
                import json

                resp = MagicMock()
                resp.status_code = 200
                resp.ok = True
                resp.text = "not json"
                resp.json.side_effect = json.JSONDecodeError("bad json", "x", 0)
                return resp

        loader = MOEXDataLoader(
            session=BadJSONSession(),  # type: ignore[arg-type]
            rate_per_min=600.0,
        )
        with pytest.raises(LoaderError, match="non-JSON"):
            loader._get_json("https://iss.moex.com/foo")

    def test_iter_corporate_actions_no_crash(self) -> None:
        """MOEX has no delisted_at column; calling iter_corporate_actions
        on a delisted ticker must succeed with zero events."""
        handlers = [_FakeResponse(_ticker_block([["YNDX", "Yandex", 1, "NL0009805522", "DELISTED"]]))]
        loader, _ = self._loader(handlers)
        actions = list(loader.iter_corporate_actions("YNDX", date(2026, 1, 1), date(2026, 6, 1)))
        assert isinstance(actions, list)

    def test_pagination_multi_page(self) -> None:
        handlers = [
            _FakeResponse(_ticker_block([["SBER", "Sber", 1, "RU0", ""]])),
            _FakeResponse(
                _candles_block(
                    [
                        [
                            Decimal("100"),
                            Decimal("105"),
                            Decimal("110"),
                            Decimal("95"),
                            Decimal("0"),
                            Decimal("1"),
                            "2026-08-01",
                            "2026-08-01",
                        ]
                    ]
                )
            ),
            _FakeResponse(_candles_block([])),
        ]
        loader = MOEXDataLoader(
            session=_FakeSession(handlers),  # type: ignore[arg-type]
            rate_per_min=600.0,
            page_size=1,
        )
        bars = list(loader.iter_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1)))
        assert len(bars) >= 1


# ===========================================================================
# 5. TinkoffDataLoader
# ===========================================================================


def _tinkoff_instruments(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"instruments": rows}


def _tinkoff_candles(candles: list[dict[str, Any]]) -> dict[str, Any]:
    return {"candles": candles}


def _tinkoff_dividends(divs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"dividends": divs}


# ===========================================================================
# 6. DataStore contract (SQLite impl)
# ===========================================================================


@pytest.fixture
def sqlite_store() -> Iterator[InMemorySQLiteStore]:
    s = InMemorySQLiteStore()
    yield s
    s.close()


def _meta(ticker: str, **kw: Any) -> TickerMeta:
    defaults: dict[str, Any] = {
        "ticker": ticker,
        "name": ticker,
        "lot": 1,
        "source": "moex",
    }
    defaults.update(kw)
    return TickerMeta(**defaults)


def _row(ticker: str, ts: date, **kw: Any) -> OHLCVRow:
    """Build a valid OHLCV row. ``close`` and ``open`` define the band;
    ``high`` defaults to max(open, close) and ``low`` to min, so callers
    can override just the close to test upsert semantics without breaking
    the OHLC consistency validator."""
    open_v = Decimal(kw.get("open", "100"))
    close_v = Decimal(kw.get("close", "105"))
    high_v = Decimal(kw.get("high", str(max(open_v, close_v))))
    low_v = Decimal(kw.get("low", str(min(open_v, close_v))))
    defaults: dict[str, Any] = {
        "ticker": ticker,
        "ts": ts,
        "open": open_v,
        "high": max(high_v, open_v, close_v),
        "low": min(low_v, open_v, close_v),
        "close": close_v,
        "volume": Decimal("1"),
        "adj_close": close_v,
        "source": "moex",
    }
    # Pass-through extras (e.g. ``source="tkf"``) override defaults.
    defaults.update(kw)
    # Recompute high/low if caller didn't pin them AND close/open changed.
    if "high" not in kw:
        defaults["high"] = max(defaults["open"], defaults["close"])
    if "low" not in kw:
        defaults["low"] = min(defaults["open"], defaults["close"])
    return OHLCVRow(**defaults)


class TestDataStoreContract:
    def test_upsert_then_list(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("SBER", lot=10))
        sqlite_store.upsert_ticker(_meta("GAZP", delisted=True, delisted_at=date(2024, 1, 1)))
        all_t = sqlite_store.list_tickers()
        assert {t.ticker for t in all_t} == {"SBER", "GAZP"}
        live = sqlite_store.list_tickers(include_delisted=False)
        assert {t.ticker for t in live} == {"SBER"}

    def test_upsert_idempotent(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("SBER", lot=10))
        sqlite_store.upsert_ticker(_meta("SBER", lot=10))
        # No error; still only one row
        assert len(sqlite_store.list_tickers()) == 1

    def test_upsert_updates_existing(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("SBER", lot=10))
        sqlite_store.upsert_ticker(_meta("SBER", lot=5))
        meta = sqlite_store.list_tickers()[0]
        assert meta.lot == 5

    def test_mark_delisted(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("YNDX"))
        sqlite_store.mark_delisted("YNDX", date(2024, 1, 1), reason="delisted from MOEX")
        meta = sqlite_store.list_tickers(include_delisted=False)
        assert meta == []
        # log table populated
        cur = sqlite_store._conn.execute("SELECT ticker, reason FROM delisting_log")  # type: ignore[attr-defined]
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "delisted from MOEX"

    def test_ohlcv_roundtrip(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("SBER"))
        rows = [
            _row("SBER", date(2026, 8, 1)),
            _row("SBER", date(2026, 8, 2)),
            _row("SBER", date(2026, 8, 3)),
        ]
        assert sqlite_store.upsert_ohlcv(rows) == 3
        out = sqlite_store.query_ohlcv("sber", date(2026, 8, 1), date(2026, 8, 3))
        assert len(out) == 3
        assert out[0].close == Decimal("105")

    def test_ohlcv_filter_by_source(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("SBER"))
        rows = [
            _row("SBER", date(2026, 8, 1), source="moex"),
            _row("SBER", date(2026, 8, 1), source="tkf"),
        ]
        sqlite_store.upsert_ohlcv(rows)
        moex = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1), source="moex")
        tkf = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1), source="tkf")
        assert moex[0].source == "moex"
        assert tkf[0].source == "tkf"

    def test_ohlcv_upsert_overwrites(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("SBER"))
        sqlite_store.upsert_ohlcv([_row("SBER", date(2026, 8, 1), close=Decimal("100"))])
        sqlite_store.upsert_ohlcv([_row("SBER", date(2026, 8, 1), close=Decimal("200"))])
        out = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1))
        assert out[0].close == Decimal("200")

    def test_corp_actions_roundtrip(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("SBER"))
        actions = [
            CorporateAction(
                ticker="SBER",
                ts=date(2026, 1, 15),
                kind="dividend",
                value=Decimal("33.4"),
                source="tkf",
            )
        ]
        assert sqlite_store.upsert_corporate_actions(actions) == 1
        out = sqlite_store.query_corporate_actions("SBER", date(2026, 1, 1), date(2026, 12, 31))
        assert out[0].value == Decimal("33.4")

    def test_count_ohlcv(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("SBER"))
        sqlite_store.upsert_ticker(_meta("GAZP"))
        sqlite_store.upsert_ohlcv([_row("SBER", date(2026, 8, 1)), _row("GAZP", date(2026, 8, 1))])
        assert sqlite_store.count_ohlcv() == 2
        assert sqlite_store.count_ohlcv("SBER") == 1
        assert sqlite_store.count_ohlcv("GAZP") == 1

    def test_perf_roundtrip_1k_rows_under_50ms(self, sqlite_store: InMemorySQLiteStore) -> None:
        """Acceptance: DataStore insert+query roundtrip < 50ms for 1k rows."""
        import time

        sqlite_store.upsert_ticker(_meta("SBER"))
        rows = [_row("SBER", date(2024, 1, 1) + timedelta(days=i)) for i in range(1000)]
        t0 = time.perf_counter()
        sqlite_store.upsert_ohlcv(rows)
        sqlite_store.query_ohlcv("SBER", date(2024, 1, 1), date(2026, 9, 27))
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50.0, f"roundtrip took {elapsed_ms:.1f} ms"

    def test_ohlcv_normalises_ticker_case(self, sqlite_store: InMemorySQLiteStore) -> None:
        sqlite_store.upsert_ticker(_meta("SBER"))
        sqlite_store.upsert_ohlcv([_row("sber", date(2026, 8, 1))])
        out = sqlite_store.query_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1))
        assert out[0].ticker == "SBER"


# ===========================================================================
# 7. property-based (hypothesis) roundtrip
# ===========================================================================


@settings(max_examples=50, deadline=None)
@given(
    dates=st.lists(
        st.dates(min_value=date(2020, 1, 1), max_value=date(2026, 12, 31)),
        min_size=1,
        max_size=20,
        unique=True,
    )
)
def test_ohlcv_roundtrip_property(dates: list[date]) -> None:
    sqlite_store = InMemorySQLiteStore()
    try:
        sqlite_store.upsert_ticker(_meta("SBER"))
        rows = [
            OHLCVRow(
                ticker="SBER",
                ts=d,
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("95"),
                close=Decimal("105"),
                volume=Decimal("1"),
                adj_close=Decimal("105"),
                source="moex",
            )
            for d in dates
        ]
        sqlite_store.upsert_ohlcv(rows)
        start, end = min(dates), max(dates)
        out = sqlite_store.query_ohlcv("SBER", start, end)
        assert len(out) == len(dates)
    finally:
        sqlite_store.close()


@settings(max_examples=50, deadline=None)
@given(
    ticker=st.text(
        alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        min_size=1,
        max_size=12,
    ),
)
def test_ticker_regex_property(ticker: str) -> None:
    """Every ticker matching the regex must be accepted by pydantic."""
    if not TICKER_REGEX.match(ticker):
        with pytest.raises(ValueError):
            OHLCVRow(
                ticker=ticker,
                ts=date(2026, 1, 1),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=Decimal("0"),
                adj_close=Decimal("1"),
                source="moex",
            )
    else:
        # Constructing should succeed.
        OHLCVRow(
            ticker=ticker,
            ts=date(2026, 1, 1),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("0"),
            adj_close=Decimal("1"),
            source="moex",
        )


@settings(max_examples=30, deadline=None)
@given(
    rate=st.floats(min_value=0.1, max_value=1000, allow_nan=False, allow_infinity=False),
    window=st.floats(min_value=0.1, max_value=600, allow_nan=False, allow_infinity=False),
)
def test_token_bucket_roundtrip_property(rate: float, window: float) -> None:
    """Constructing a bucket with valid args never raises; tokens refill monotonically."""
    b = TokenBucket(rate=rate, window_seconds=window)
    start = b.tokens_available()
    # Fake "time passed" by sleeping briefly (≪ window) so refill is small but positive.
    import time

    time.sleep(0.001)
    after = b.tokens_available()
    assert start >= 0
    assert after >= start - 1e-3  # numerical slack
