"""Coverage tests for ``src/data/moex_loader.py``.

Goal: drive ``MOEXDataLoader`` coverage from the 88% baseline to >=95%.

Strategy
--------
- Reuse the ``_FakeSession`` / ``_FakeResponse`` / ``_ticker_block`` /
  ``_candles_block`` helpers style from ``test_data_loader.py`` so this
  module is self-contained and easy to read.
- Cover every missing line in the coverage report:

  141    iter_ohlcv: empty candles.columns -> return
  168-9  iter_corporate_actions: meta is None -> return
  208    _get_json: non-OK HTTP -> LoaderError
  228    _fetch_all_rows: no block -> []
  232-5  _first_block_with_columns: non-dict entries skipped, dict w/o
         columns skipped, return None
  240    _extract_block: missing block -> LoaderError
  250    _rows_from_block: non-list row skipped
  261    _row_to_ticker_meta: missing SECID -> None
  283-5  _row_to_ticker_meta: malformed row -> None (warned)
  298    _row_to_ohlcv: missing ts -> None
  321-3  _row_to_ohlcv: malformed row -> None (warned)
  329    _d: None -> Decimal("0")
  332    _d: str -> Decimal
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from src.data import (
    LoaderAuthError,
    LoaderError,
    LoaderNotFoundError,
    LoaderRateLimitError,
    MOEXDataLoader,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, json_payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = json_payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = "" if self.ok else "server error"

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    """Minimal stand-in for ``requests.Session``."""

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


def _ticker_block(rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "securities": {
            "columns": ["SECID", "SHORTNAME", "LOTSIZE", "ISIN", "STATUS"],
            "data": rows,
        }
    }


def _candles_block(rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "candles": {
            "columns": ["open", "close", "high", "low", "value", "volume", "begin", "end"],
            "data": rows,
        }
    }


def _make_loader(handlers: list[Any]) -> MOEXDataLoader:
    return MOEXDataLoader(
        session=_FakeSession(handlers),  # type: ignore[arg-type]
        rate_per_min=600.0,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar_row(d: date, lot: int = 1) -> list[Any]:
    return [
        Decimal("100"),
        Decimal("105"),
        Decimal("110"),
        Decimal("95"),
        Decimal("0"),
        Decimal("100"),
        d.isoformat(),
        d.isoformat(),
    ]


def _prime_universe(
    loader: MOEXDataLoader,
    ticker: str = "SBER",
    lot: int = 1,
    *,
    board_filter: str | None = "TQBR",
) -> None:
    """Set the universe cache directly so tests don't have to spin an HTTP handler.

    ``board_filter`` is the value stored in ``loader._board_filter`` —
    the cache key. Default ``"TQBR"`` matches the default
    ``list_tickers(board_id="TQBR")`` that ``iter_ohlcv`` ->
    ``_meta_for`` -> ``list_tickers`` performs internally, so the cache
    is hit without an HTTP call.
    """
    from src.data import TickerMeta

    loader._universe_cache = [
        TickerMeta(
            ticker=ticker,
            figi=None,
            name=ticker,
            lot=lot,
            isin="RU0",
            currency="RUB",
            delisted=False,
            delisted_at=None,
            listed_at=None,
            source="moex",
        )
    ]
    loader._board_filter = board_filter


# ---------------------------------------------------------------------------
# iter_ohlcv — missing-line 141
# ---------------------------------------------------------------------------


class TestIterOhlcv:
    def test_empty_candles_columns_returns_early(self) -> None:
        """When MOEX returns a candles block with no columns, the iterator
        yields nothing and returns immediately instead of looping."""
        handlers = [
            _FakeResponse(
                {
                    "candles": {
                        "columns": [],
                        "data": [],
                    }
                }
            ),
        ]
        loader = _make_loader(handlers)
        _prime_universe(loader)  # cache primed with default board_filter="TQBR"
        out = list(loader.iter_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 1)))
        assert out == []

    def test_empty_page_terminates_loop(self) -> None:
        """If a subsequent page returns rows < page_size, pagination stops."""
        handlers = [
            _FakeResponse(
                _candles_block(
                    [
                        _bar_row(date(2026, 8, 1)),
                        _bar_row(date(2026, 8, 2)),
                    ]
                )
            ),
        ]
        loader = _make_loader(handlers)
        loader._page_size = 5  # ensure 2 rows < page_size -> return
        _prime_universe(loader)
        out = list(loader.iter_ohlcv("SBER", date(2026, 8, 1), date(2026, 8, 2)))
        assert len(out) == 2


# ---------------------------------------------------------------------------
# iter_corporate_actions — missing lines 168-169
# ---------------------------------------------------------------------------


class TestIterCorporateActions:
    def test_unknown_ticker_returns_empty(self) -> None:
        """When meta is None for the ticker, the iterator is empty."""
        loader = _make_loader([])
        _prime_universe(loader, ticker="SBER")
        out = list(loader.iter_corporate_actions("GAZP", date(2026, 1, 1), date(2026, 12, 31)))
        assert out == []

    def test_meta_returns_none_when_universe_empty(self) -> None:
        """Empty universe -> meta None -> no events."""
        loader = _make_loader([])
        loader._universe_cache = []  # nothing in universe
        loader._board_filter = "TQBR"
        out = list(loader.iter_corporate_actions("SBER", date(2026, 1, 1), date(2026, 12, 31)))
        assert out == []

    def test_delisted_in_range_yields_change_event(self) -> None:
        """Delisted ticker within [start, end] yields a 'change' event."""
        loader = _make_loader([])
        from src.data import TickerMeta

        loader._universe_cache = [
            TickerMeta(
                ticker="YNDX",
                figi=None,
                name="Yandex",
                lot=1,
                isin="NL0",
                currency="RUB",
                delisted=True,
                delisted_at=date(2026, 6, 15),
                listed_at=None,
                source="moex",
            )
        ]
        loader._board_filter = "TQBR"
        out = list(loader.iter_corporate_actions("YNDX", date(2026, 1, 1), date(2026, 12, 31)))
        assert len(out) == 1
        assert out[0].kind == "change"
        assert out[0].ts == date(2026, 6, 15)

    def test_delisted_out_of_range_yields_none(self) -> None:
        """Delisted ticker outside [start, end] yields no events."""
        loader = _make_loader([])
        from src.data import TickerMeta

        loader._universe_cache = [
            TickerMeta(
                ticker="YNDX",
                figi=None,
                name="Yandex",
                lot=1,
                isin="NL0",
                currency="RUB",
                delisted=True,
                delisted_at=date(2020, 1, 1),
                listed_at=None,
                source="moex",
            )
        ]
        loader._board_filter = "TQBR"
        out = list(loader.iter_corporate_actions("YNDX", date(2026, 1, 1), date(2026, 12, 31)))
        assert out == []


# ---------------------------------------------------------------------------
# _get_json — missing line 208
# ---------------------------------------------------------------------------


class TestGetJson:
    def test_non_ok_http_raises_loader_error(self) -> None:
        """HTTP 500 falls through checks and raises LoaderError."""
        handlers = [_FakeResponse({}, status_code=500)]
        loader = _make_loader(handlers)
        with pytest.raises(LoaderError, match="HTTP 500"):
            loader._get_json("https://iss.moex.com/foo")

    def test_500_response_text_is_in_message(self) -> None:
        """Server error text is included in the exception, sliced to 200."""
        resp = _FakeResponse({}, status_code=503)
        resp.text = "boom" * 200  # 800 chars
        loader = _make_loader([resp])
        with pytest.raises(LoaderError, match="503"):
            loader._get_json("https://iss.moex.com/foo")

    def test_403_raises_auth_error(self) -> None:
        handlers = [_FakeResponse({}, status_code=403)]
        loader = _make_loader(handlers)
        with pytest.raises(LoaderAuthError):
            loader._get_json("https://iss.moex.com/foo")

    def test_404_raises_not_found_error(self) -> None:
        handlers = [_FakeResponse({}, status_code=404)]
        loader = _make_loader(handlers)
        with pytest.raises(LoaderNotFoundError):
            loader._get_json("https://iss.moex.com/foo")

    def test_429_raises_rate_limit_error(self) -> None:
        handlers = [_FakeResponse({}, status_code=429)]
        loader = _make_loader(handlers)
        with pytest.raises(LoaderRateLimitError):
            loader._get_json("https://iss.moex.com/foo")

    def test_request_exception_wrapped(self) -> None:
        """requests.RequestException (e.g. ConnectionError) -> LoaderError."""

        class BoomSession:
            def get(self, url, params=None, timeout=None):
                raise requests.ConnectionError("kaboom")

        loader = MOEXDataLoader(
            session=BoomSession(),  # type: ignore[arg-type]
            rate_per_min=600.0,
        )
        with pytest.raises(LoaderError, match="network error"):
            loader._get_json("https://iss.moex.com/foo")

    def test_invalid_json_wrapped(self) -> None:
        """json.JSONDecodeError on .json() is wrapped as LoaderError."""

        class BadJSONSession:
            def get(self, url, params=None, timeout=None):
                resp = MagicMock()
                resp.status_code = 200
                resp.ok = True
                resp.text = "not json"
                resp.json.side_effect = json.JSONDecodeError("bad", "x", 0)
                return resp

        loader = MOEXDataLoader(
            session=BadJSONSession(),  # type: ignore[arg-type]
            rate_per_min=600.0,
        )
        with pytest.raises(LoaderError, match="non-JSON"):
            loader._get_json("https://iss.moex.com/foo")


# ---------------------------------------------------------------------------
# _fetch_all_rows — missing lines 228, 232-235
# ---------------------------------------------------------------------------


class TestFetchAllRows:
    def test_returns_empty_when_no_block_found(self) -> None:
        """When the payload has no block with 'columns', return []."""
        handlers = [_FakeResponse({"unrelated": {"foo": "bar"}})]
        loader = _make_loader(handlers)
        rows = loader._fetch_all_rows("https://iss.moex.com/foo", columns_metadata_key="securities")
        assert rows == []

    def test_first_block_with_columns_finds_block_without_key(self) -> None:
        """If the expected key is missing, _first_block_with_columns falls
        through other dicts and returns the one with 'columns'."""
        loader = _make_loader([])
        payload = {
            "metadata": {"foo": "bar"},  # not a dict-with-columns
            "securities": {"columns": ["X"], "data": [[1]]},
        }
        block = loader._first_block_with_columns(payload)
        assert block is not None
        assert block["columns"] == ["X"]

    def test_first_block_with_columns_skips_non_dicts(self) -> None:
        """Non-dict values are skipped."""
        loader = _make_loader([])
        payload = {
            "wat": "string",
            "42": 99,
            "securities": {"columns": ["X"], "data": []},
        }
        block = loader._first_block_with_columns(payload)
        assert block is not None
        assert block["columns"] == ["X"]

    def test_first_block_with_columns_returns_none(self) -> None:
        """All entries lack 'columns' -> return None."""
        loader = _make_loader([])
        payload = {"a": {"x": 1}, "b": "string", "c": 42}
        assert loader._first_block_with_columns(payload) is None

    def test_fetch_all_rows_uses_first_block_fallback(self) -> None:
        """When the named block is missing, fall back to the first block with columns."""
        handlers = [
            _FakeResponse(
                {
                    "securities": {
                        "columns": ["SECID", "SHORTNAME"],
                        "data": [["SBER", "Sber"]],
                    }
                }
            )
        ]
        loader = _make_loader(handlers)
        # Requesting a different key forces the fallback path.
        rows = loader._fetch_all_rows("https://iss.moex.com/foo", columns_metadata_key="missing")
        assert rows == [{"SECID": "SBER", "SHORTNAME": "Sber"}]


# ---------------------------------------------------------------------------
# _extract_block — missing line 240
# ---------------------------------------------------------------------------


class TestExtractBlock:
    def test_missing_block_raises_loader_error(self) -> None:
        loader = _make_loader([])
        with pytest.raises(LoaderError, match="missing block 'candles'"):
            loader._extract_block({}, "candles")

    def test_non_dict_block_raises_loader_error(self) -> None:
        loader = _make_loader([])
        with pytest.raises(LoaderError, match="missing block 'candles'"):
            loader._extract_block({"candles": "not a dict"}, "candles")

    def test_present_block_returned(self) -> None:
        loader = _make_loader([])
        block = {"columns": ["X"], "data": []}
        assert loader._extract_block({"candles": block}, "candles") is block


# ---------------------------------------------------------------------------
# _rows_from_block — missing line 250
# ---------------------------------------------------------------------------


class TestRowsFromBlock:
    def test_skips_non_list_rows(self) -> None:
        """A row that isn't a list is skipped (defensive)."""
        loader = _make_loader([])
        block = {
            "columns": ["A", "B"],
            "data": [
                [1, 2],
                "not a list",  # skipped
                {"nested": "dict"},  # skipped
                [3, 4],
            ],
        }
        assert loader._rows_from_block(block) == [{"A": 1, "B": 2}, {"A": 3, "B": 4}]

    def test_handles_short_row(self) -> None:
        """A row shorter than columns pads with None."""
        loader = _make_loader([])
        block = {"columns": ["A", "B", "C"], "data": [[1, 2]]}
        assert loader._rows_from_block(block) == [{"A": 1, "B": 2, "C": None}]

    def test_empty_columns_returns_empty(self) -> None:
        loader = _make_loader([])
        assert loader._rows_from_block({"data": [[1, 2]]}) == []

    def test_empty_data_returns_empty(self) -> None:
        loader = _make_loader([])
        assert loader._rows_from_block({"columns": ["A"], "data": []}) == []


# ---------------------------------------------------------------------------
# _row_to_ticker_meta — missing lines 261, 283-285
# ---------------------------------------------------------------------------


class TestRowToTickerMeta:
    def test_missing_secid_returns_none(self) -> None:
        """Without a SECID string, the row is skipped."""
        loader = _make_loader([])
        assert loader._row_to_ticker_meta({"SECID": None}) is None
        assert loader._row_to_ticker_meta({"SECID": ""}) is None
        # Non-string SECID is also rejected.
        assert loader._row_to_ticker_meta({"SECID": 123}) is None

    def test_malformed_row_returns_none(self) -> None:
        """An exception during construction -> None + warning."""
        loader = _make_loader([])
        # Pass ``LOTSIZE`` as a list that int() rejects loudly. We rely
        # on the except clause rather than ``int(list)`` directly triggering
        # a TypeError; the wrapped code calls int(), which raises TypeError
        # for a list argument.
        bad = {"SECID": "SBER", "LOTSIZE": [1, 2]}
        assert loader._row_to_ticker_meta(bad) is None

    def test_status_mapping(self) -> None:
        """DELISTED / EXCLUDED / HALTED -> ``delisted=True``."""
        loader = _make_loader([])
        for status in ("DELISTED", "EXCLUDED", "HALTED", "delisted"):
            row = {"SECID": "X", "SHORTNAME": "X", "LOTSIZE": 1, "STATUS": status}
            meta = loader._row_to_ticker_meta(row)
            assert meta is not None
            assert meta.delisted is True, status

    def test_falls_back_to_secname_when_no_shortname(self) -> None:
        loader = _make_loader([])
        meta = loader._row_to_ticker_meta({"SECID": "X", "SECNAME": "Long name", "LOTSIZE": 1})
        assert meta is not None
        assert meta.name == "Long name"

    def test_isin_stringification(self) -> None:
        loader = _make_loader([])
        meta = loader._row_to_ticker_meta({"SECID": "X", "SHORTNAME": "X", "LOTSIZE": 1, "ISIN": 12345})
        assert meta is not None
        assert meta.isin == "12345"

    def test_lotsize_default_when_missing(self) -> None:
        loader = _make_loader([])
        meta = loader._row_to_ticker_meta({"SECID": "X", "SHORTNAME": "X"})
        assert meta is not None
        assert meta.lot == 1


# ---------------------------------------------------------------------------
# _row_to_ohlcv — missing lines 298, 321-323
# ---------------------------------------------------------------------------


class TestRowToOhlcv:
    def test_missing_ts_returns_none(self) -> None:
        loader = _make_loader([])
        # No 'begin' nor 'tradedate' -> None
        assert loader._row_to_ohlcv("SBER", 1, {"open": 1}) is None

    def test_malformed_row_returns_none(self) -> None:
        """open == 'not a number' => Decimal(str(v)) raises InvalidOperation."""
        loader = _make_loader([])
        row = {
            "open": "not a number",
            "high": "1",
            "low": "1",
            "close": "1",
            "volume": "1",
            "begin": "2026-08-01",
        }
        assert loader._row_to_ohlcv("SBER", 1, row) is None

    def test_volume_multiplied_by_lot(self) -> None:
        loader = _make_loader([])
        row = {
            "open": "100",
            "high": "110",
            "low": "95",
            "close": "105",
            "volume": "10",
            "begin": "2026-08-01",
        }
        bar = loader._row_to_ohlcv("SBER", 7, row)
        assert bar is not None
        assert bar.volume == Decimal("70")
        assert bar.ticker == "SBER"
        assert bar.ts == date(2026, 8, 1)


# ---------------------------------------------------------------------------
# _d — missing lines 329, 332
# ---------------------------------------------------------------------------


class TestDecimalCoerce:
    def test_none_returns_zero(self) -> None:
        # _d is module-level; import via module attributes.
        from src.data.moex_loader import _d

        assert _d(None) == Decimal("0")

    def test_empty_string_returns_zero(self) -> None:
        from src.data.moex_loader import _d

        assert _d("") == Decimal("0")

    def test_decimal_passthrough(self) -> None:
        from src.data.moex_loader import _d

        v = Decimal("1.5")
        assert _d(v) is v

    def test_string_coerced_to_decimal(self) -> None:
        from src.data.moex_loader import _d

        assert _d("123.45") == Decimal("123.45")

    def test_numeric_coerced_to_decimal(self) -> None:
        """Non-string, non-Decimal values are coerced via str()."""
        from src.data.moex_loader import _d

        # int goes through str(int) -> "100" -> Decimal("100")
        assert _d(100) == Decimal("100")
        # float -> uses repr which is fine for whole numbers
        assert _d(2.0) == Decimal("2.0")


# ---------------------------------------------------------------------------
# list_tickers — board filter caching
# ---------------------------------------------------------------------------


class TestListTickersCaching:
    def test_board_filter_cache_reused(self) -> None:
        """Calling list_tickers twice with the same board_id uses the cache."""
        handlers = [_FakeResponse(_ticker_block([["SBER", "Sber", 1, "RU0", ""]]))]
        loader = _make_loader(handlers)
        first = loader.list_tickers(board_id=None)
        second = loader.list_tickers(board_id=None)
        assert first is second
        # Only one HTTP call was made.
        assert len(loader._session.calls) == 1  # type: ignore[attr-defined]

    def test_explicit_board_filter(self) -> None:
        """Passing board_id keeps only matching rows."""
        # Use the BOARDID column too.
        payload = {
            "securities": {
                "columns": ["SECID", "BOARDID", "SHORTNAME", "LOTSIZE"],
                "data": [
                    ["SBER", "TQBR", "Sber", 10],
                    ["GAZP", "TQOB", "Gazprom", 1],
                ],
            }
        }
        loader = _make_loader([_FakeResponse(payload)])
        out = loader.list_tickers(board_id="TQBR")
        names = {t.ticker for t in out}
        assert names == {"SBER"}

    def test_malformed_row_in_universe_is_skipped(self) -> None:
        """A row that raises during _row_to_ticker_meta is dropped."""
        # LOTSIZE as a list -> raises in _row_to_ticker_meta -> row skipped.
        payload = {
            "securities": {
                "columns": ["SECID", "SHORTNAME", "LOTSIZE"],
                "data": [
                    ["SBER", "Sber", 1],
                    ["BAD", "Bad", [1, 2]],
                ],
            }
        }
        loader = _make_loader([_FakeResponse(payload)])
        out = loader.list_tickers(board_id=None)
        tickers = {t.ticker for t in out}
        assert "SBER" in tickers
        assert "BAD" not in tickers

    def test_cache_refetches_when_board_id_differs_after_tqbr(self) -> None:
        """Issue #162: a None call after a TQBR fill must NOT return the TQBR cache.

        Before the fix, ``list_tickers(board_id=None)`` after a cached
        ``board_id="TQBR"`` call returned the TQBR-filtered list because
        the cache-hit guard short-circuited on ``board_id is None``.
        Correct behaviour: refetch and return the full universe.
        """
        # First call: TQBR-only payload (1 row).
        tqbr_payload = {
            "securities": {
                "columns": ["SECID", "BOARDID", "SHORTNAME", "LOTSIZE"],
                "data": [["SBER", "TQBR", "Sber", 10]],
            }
        }
        # Second call (after cache invalidation): full payload (2 rows).
        full_payload = {
            "securities": {
                "columns": ["SECID", "BOARDID", "SHORTNAME", "LOTSIZE"],
                "data": [
                    ["SBER", "TQBR", "Sber", 10],
                    ["GAZP", "TQOB", "Gazprom", 1],
                ],
            }
        }
        loader = _make_loader([_FakeResponse(tqbr_payload), _FakeResponse(full_payload)])
        first = loader.list_tickers(board_id="TQBR")
        assert {t.ticker for t in first} == {"SBER"}
        # Second call asks for the full universe — must refetch and
        # return BOTH rows (pre-fix returned only SBER).
        second = loader.list_tickers(board_id=None)
        assert {t.ticker for t in second} == {"SBER", "GAZP"}
        # Two HTTP round-trips, not one.
        assert len(loader._session.calls) == 2  # type: ignore[attr-defined]

    def test_cache_refetches_when_board_id_differs_after_none(self) -> None:
        """Issue #162 (reverse direction): TQBR call after a None fill must NOT
        return the unfiltered cache.

        Symmetric counterpart to the test above. A cached full-universe
        call followed by a ``board_id="TQBR"`` request must refetch and
        filter — pre-fix it returned the full list.
        """
        full_payload = {
            "securities": {
                "columns": ["SECID", "BOARDID", "SHORTNAME", "LOTSIZE"],
                "data": [
                    ["SBER", "TQBR", "Sber", 10],
                    ["GAZP", "TQOB", "Gazprom", 1],
                ],
            }
        }
        tqbr_payload = {
            "securities": {
                "columns": ["SECID", "BOARDID", "SHORTNAME", "LOTSIZE"],
                "data": [["SBER", "TQBR", "Sber", 10]],
            }
        }
        loader = _make_loader([_FakeResponse(full_payload), _FakeResponse(tqbr_payload)])
        first = loader.list_tickers(board_id=None)
        assert {t.ticker for t in first} == {"SBER", "GAZP"}
        second = loader.list_tickers(board_id="TQBR")
        assert {t.ticker for t in second} == {"SBER"}
        assert len(loader._session.calls) == 2  # type: ignore[attr-defined]

    def test_cache_reused_when_board_id_matches(self) -> None:
        """Issue #162 (regression guard): the existing cache-hit path still
        works — calling ``list_tickers(board_id="TQBR")`` twice in a row
        issues one HTTP call and returns the same list object.
        """
        payload = {
            "securities": {
                "columns": ["SECID", "BOARDID", "SHORTNAME", "LOTSIZE"],
                "data": [["SBER", "TQBR", "Sber", 10]],
            }
        }
        loader = _make_loader([_FakeResponse(payload)])
        first = loader.list_tickers(board_id="TQBR")
        second = loader.list_tickers(board_id="TQBR")
        assert first is second
        assert len(loader._session.calls) == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Concurrency — issue #193 (race-prone list_tickers cache fill)
# ---------------------------------------------------------------------------


class _ThreadSafeFakeSession:
    """Thread-safe stand-in for ``requests.Session``.

    Mirrors ``_FakeSession`` (above) but stores handlers in a
    ``queue.Queue`` so concurrent ``get()`` callers can dequeue without
    the ``list.pop(0)`` race that would corrupt ``_handlers`` and
    surface as ``IndexError`` on the second thread.
    """

    def __init__(self, handlers: list[Any] | None = None) -> None:
        self._handlers: queue.Queue[Any] = queue.Queue()
        for h in handlers or []:
            self._handlers.put(h)
        self.calls: list[dict[str, Any]] = []
        self._calls_lock = threading.Lock()

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: Any = None):
        with self._calls_lock:
            self.calls.append({"url": url, "params": params, "timeout": timeout})
        try:
            return self._handlers.get_nowait()
        except queue.Empty as exc:
            raise AssertionError(
                "no handler configured for " + url + " (HTTP called more times than handlers)"
            ) from exc

    def post(self, url: str, **kw: Any):
        with self._calls_lock:
            self.calls.append({"url": url, **kw})
        try:
            return self._handlers.get_nowait()
        except queue.Empty as exc:
            raise AssertionError(
                "no handler configured for " + url + " (HTTP called more times than handlers)"
            ) from exc


def _ts_loader(handlers: list[Any]) -> MOEXDataLoader:
    """Build a ``MOEXDataLoader`` with a thread-safe ``_FakeSession``."""
    return MOEXDataLoader(
        session=_ThreadSafeFakeSession(handlers),  # type: ignore[arg-type]
        rate_per_min=10000.0,
    )


class TestListTickersConcurrency:
    """Issue #193 — ``MOEXDataLoader.list_tickers`` cache fill must be safe
    under concurrent first-time callers.

    Without the lock, two threads entering ``list_tickers(board_id)``
    simultaneously would both observe ``self._universe_cache is None``
    and both walk through ``_fetch_all_rows(...)`` — duplicating the
    HTTP call, consuming two rate-limit slots, and silently discarding
    the slow builder's results. With double-checked locking, only one
    thread performs the fill; the rest observe the populated cache and
    return immediately. Mirrors the contract already enforced for
    ``TinkoffInvestDataLoader.list_tickers`` (issue #175) and
    ``TinkoffInvestMDDataLoader.list_tickers`` (issue #152).
    """

    def test_concurrent_first_call_fills_cache_exactly_once(self) -> None:
        """8 threads call ``list_tickers("TQBR")`` for the first time —
        ``_fetch_all_rows`` (and therefore the underlying HTTP session)
        must be invoked exactly once, and every thread must receive
        the same list object (the canonical fill-then-return contract).
        """
        import time as _time

        payload = {
            "securities": {
                "columns": ["SECID", "BOARDID", "SHORTNAME", "LOTSIZE"],
                "data": [
                    ["SBER", "TQBR", "Sber", 10],
                    ["GAZP", "TQBR", "Gazprom", 1],
                ],
            }
        }
        loader = _ts_loader([_FakeResponse(payload)])

        # Patch _fetch_all_rows to count calls and add a sleep so the
        # race window is wide enough that 8 threads actually overlap
        # inside the lock-free section on the buggy code. Without the
        # lock, every thread that arrives during the sleep would
        # increment call_count.
        call_count = 0
        original = loader._fetch_all_rows

        def counting_fetch(url: str, *, columns_metadata_key: str) -> list[dict[str, Any]]:
            nonlocal call_count
            call_count += 1
            _time.sleep(0.05)  # 50ms window — long enough to overlap 8 threads
            return original(url, columns_metadata_key=columns_metadata_key)

        loader._fetch_all_rows = counting_fetch  # type: ignore[method-assign]

        results: list[list[Any]] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(loader.list_tickers(board_id="TQBR"))
            except BaseException as exc:  # pragma: no cover — fail loud
                errors.append(exc)

        barrier = threading.Barrier(parties=8)

        def worker_synced() -> None:
            try:
                barrier.wait(timeout=5.0)
                results.append(loader.list_tickers(board_id="TQBR"))
            except BaseException as exc:  # pragma: no cover — fail loud
                errors.append(exc)

        threads = [threading.Thread(target=worker_synced) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive(), "thread hung — lock deadlock"

        assert errors == [], f"unexpected exceptions: {errors}"
        # The single-fill contract.
        assert call_count == 1, (
            f"_fetch_all_rows called {call_count} times, expected 1 — " "the cache fill is not protected by the lock"
        )
        # Every thread must observe the same list object.
        first = results[0]
        assert {t.ticker for t in first} == {"SBER", "GAZP"}
        for r in results[1:]:
            assert r is first, (
                "each thread got a distinct list — the lock-less code "
                "let concurrent builders race on the cache assignment"
            )
        # The session saw exactly one HTTP call.
        assert len(loader._session.calls) == 1  # type: ignore[attr-defined]

    def test_lock_is_present(self) -> None:
        """Regression guard: ``__init__`` must install ``_universe_lock``.

        This is the structural assertion — the lock object is the
        mechanism that makes the concurrency test above meaningful. If
        a future refactor removes the lock field (or renames it), this
        test fails fast and the concurrency test stops being a
        regression guard.
        """
        loader = _ts_loader([])
        assert hasattr(loader, "_universe_lock"), "MOEXDataLoader.__init__ must install _universe_lock " "(issue #193)"
        # Must be a real lock-like object with acquire/release.
        lock = loader._universe_lock
        assert hasattr(lock, "acquire") and hasattr(
            lock, "release"
        ), "_universe_lock must be a threading.Lock-like object"

    def test_board_id_mismatch_still_refetches_under_lock(self) -> None:
        """The issue #162 cache-key invariant survives the new lock.

        A cached ``TQBR`` call followed by a ``None`` call must STILL
        refetch (and vice versa). The lock added in issue #193 must
        not collapse the equality check — both guards (cache-set AND
        ``_board_filter == board_id``) are required.
        """
        tqbr_payload = {
            "securities": {
                "columns": ["SECID", "BOARDID", "SHORTNAME", "LOTSIZE"],
                "data": [["SBER", "TQBR", "Sber", 10]],
            }
        }
        full_payload = {
            "securities": {
                "columns": ["SECID", "BOARDID", "SHORTNAME", "LOTSIZE"],
                "data": [
                    ["SBER", "TQBR", "Sber", 10],
                    ["GAZP", "TQOB", "Gazprom", 1],
                ],
            }
        }
        loader = _ts_loader([_FakeResponse(tqbr_payload), _FakeResponse(full_payload)])
        first = loader.list_tickers(board_id="TQBR")
        assert {t.ticker for t in first} == {"SBER"}
        second = loader.list_tickers(board_id=None)
        # Refetch must include the TQOB row — proving the new lock
        # did not collapse the cache-key equality check.
        assert {t.ticker for t in second} == {"SBER", "GAZP"}
        assert len(loader._session.calls) == 2  # type: ignore[attr-defined]
