"""Coverage tests for the bonds-history branch of ``src/data/moex_loader.py``.

Issue #361: 1519 tickers remain incomplete on .107 production because the
fallback chain (``tinkoff_grpc`` → ``moex_iss``) only knows about MOEX ISS
**shares** endpoint. ISIN-prefixed tickers (`SU...` / `RU...` OFZ and
corporate bonds) are not reachable through the shares endpoint, so the
chain reports ``ALL sources returned 0 bars`` for them on every cycle.

Fix is to teach ``MOEXDataLoader.iter_ohlcv`` to dispatch
ISIN-prefixed tickers to the MOEX ISS bonds history endpoint::

    GET /iss/history/engines/stock/markets/bonds/securities/{secid}.json

with paginated ``?start=N`` cursor, and ``tinkoff_grpc`` short-circuits
the chain when it returns the same window successfully.

These tests pin the contract:

  * ISIN-prefixed tickers route to the bonds endpoint.
  * Non-ISIN tickers still route to the shares endpoint (no regression).
  * The MOEX bonds history endpoint returns OHLCV with UPPERCASE column
    names (``TRADEDATE``, ``OPEN``, ``HIGH``, ``LOW``, ``CLOSE``,
    ``VOLUME``) — already case-mixed in ``_row_to_ohlcv`` via
    ``begin``/``tradedate`` fallback.
  * Pagination uses ``?start=N`` and stops when a page returns < page_size.
  * A `LoaderNotFoundError` from the bonds endpoint means the ticker is
    not a bond and we DO NOT silently swallow it (the chain will then
    fall through to whatever the next source is, with the error recorded).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from src.data import (
    LoaderNotFoundError,
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
    def __init__(self, handlers: list[Any] | None = None) -> None:
        self._handlers = handlers or []
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: Any = None):
        self.calls.append({"url": url, "params": params or {}, "timeout": timeout})
        if not self._handlers:
            raise AssertionError(f"no handler for {url}")
        return self._handlers.pop(0)


def _make_loader(handlers: list[Any]) -> MOEXDataLoader:
    return MOEXDataLoader(
        session=_FakeSession(handlers),  # type: ignore[arg-type]
        rate_per_min=600.0,
    )


def _bonds_history_block(rows: list[list[Any]], *, total: int | None = None) -> dict[str, Any]:
    """A page from ``/iss/history/engines/stock/markets/bonds/securities/SECID.json``.

    Column order is the ISS bonds-history canonical order (snippet taken
    from a real 2026-08 response).
    """
    cols = [
        "BOARDID",
        "TRADEDATE",
        "SHORTNAME",
        "SECID",
        "NUMTRADES",
        "VALUE",
        "LOW",
        "HIGH",
        "CLOSE",
        "LEGALCLOSEPRICE",
        "ACCINT",
        "WAPRICE",
        "OPEN",
        "VOLUME",
    ]
    return {
        "history": {"columns": cols, "data": rows},
        "history.cursor": {
            "columns": ["INDEX", "TOTAL", "PAGESIZE"],
            "data": [[0, total if total is not None else len(rows), 100]],
        },
    }


def _bonds_bar(d: date, *, ticker: str, close: str = "100") -> list[Any]:
    return [
        "TQOB",  # BOARDID
        d.isoformat(),  # TRADEDATE
        ticker,  # SHORTNAME
        ticker,  # SECID
        10,  # NUMTRADES
        Decimal("100000"),  # VALUE
        Decimal("95"),  # LOW
        Decimal("105"),  # HIGH
        Decimal(close),  # CLOSE
        Decimal(close),  # LEGALCLOSEPRICE
        Decimal("0"),  # ACCINT
        Decimal(close),  # WAPRICE
        Decimal("98"),  # OPEN
        100,  # VOLUME
    ]


def _safe_bonds_bar(d: date, *, ticker: str, base: float = 100.0) -> list[Any]:
    """Build a bonds row with valid OHLCV invariants (``low <= open/close <= high``).

    The default ``_bonds_bar`` uses ``low=95, high=105, close=100`` style
    values which violate the OHLCV pydantic invariants when ``close != 100``.
    Tests that don't care about precise price levels should use this helper.
    """
    lo = Decimal(str(base)) - Decimal("1")
    o = Decimal(str(base)) + Decimal("0.5")
    c = Decimal(str(base))
    h = Decimal(str(base)) + Decimal("2")
    return [
        "TQOB",  # BOARDID
        d.isoformat(),  # TRADEDATE
        ticker,  # SHORTNAME
        ticker,  # SECID
        10,  # NUMTRADES
        Decimal("100000"),  # VALUE
        Decimal(str(lo)),  # LOW
        Decimal(str(h)),  # HIGH
        Decimal(str(c)),  # CLOSE
        Decimal(str(c)),  # LEGALCLOSEPRICE
        Decimal("0"),  # ACCINT
        Decimal(str(c)),  # WAPRICE
        Decimal(str(o)),  # OPEN
        100,  # VOLUME
    ]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestIsinRoutingToBonds:
    def test_su_prefix_routes_to_bonds_history_endpoint(self) -> None:
        """A `SU`-prefixed bond SHOULD hit `/iss/history/engines/stock/markets/bonds/...`,
        NOT the `/markets/shares/...candles.json` endpoint.
        """
        handlers = [
            _FakeResponse(
                _bonds_history_block(
                    [_bonds_bar(date(2025, 8, 1), ticker="SU46020RMFS2")],
                    total=1,
                )
            ),
        ]
        loader = _make_loader(handlers)
        out = list(loader.iter_ohlcv("SU46020RMFS2", date(2025, 8, 1), date(2025, 8, 2)))
        assert len(out) == 1
        # Verify the URL hit
        assert len(loader._session.calls) == 1  # type: ignore[attr-defined]
        call = loader._session.calls[0]  # type: ignore[attr-defined]
        assert "/iss/history/engines/stock/markets/bonds/securities/SU46020RMFS2" in call["url"]
        assert "shares" not in call["url"]

    def test_ru_prefix_routes_to_bonds_history_endpoint(self) -> None:
        handlers = [
            _FakeResponse(
                _bonds_history_block(
                    [_bonds_bar(date(2024, 8, 1), ticker="RU000A100FE5")],
                    total=1,
                )
            ),
        ]
        loader = _make_loader(handlers)
        out = list(loader.iter_ohlcv("RU000A100FE5", date(2024, 8, 1), date(2024, 8, 2)))
        assert len(out) == 1
        call = loader._session.calls[0]  # type: ignore[attr-defined]
        assert "/bonds/securities/RU000A100FE5" in call["url"]

    def test_non_isin_still_uses_shares_endpoint(self) -> None:
        """SBER (no ISIN prefix) MUST still hit the shares endpoint — no regression."""
        # Universe cache primed for ticker lookup
        from src.data import TickerMeta

        handlers = [
            _FakeResponse(
                {
                    "candles": {
                        "columns": [
                            "open",
                            "close",
                            "high",
                            "low",
                            "value",
                            "volume",
                            "begin",
                            "end",
                        ],
                        "data": [
                            [
                                Decimal("100"),
                                Decimal("105"),
                                Decimal("110"),
                                Decimal("95"),
                                Decimal("0"),
                                Decimal("100"),
                                "2025-08-01",
                                "2025-08-01",
                            ],
                        ],
                    }
                }
            ),
        ]
        loader = _make_loader(handlers)
        loader._universe_cache = [
            TickerMeta(
                ticker="SBER",
                figi=None,
                name="SBER",
                lot=1,
                isin="RU0",
                currency="RUB",
                delisted=False,
                delisted_at=None,
                listed_at=None,
                source="moex",
            )
        ]
        loader._board_filter = "TQBR"
        out = list(loader.iter_ohlcv("SBER", date(2025, 8, 1), date(2025, 8, 2)))
        assert len(out) == 1
        call = loader._session.calls[0]  # type: ignore[attr-defined]
        assert "/markets/shares/securities/SBER/candles.json" in call["url"]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestBondsPagination:
    def test_pagination_uses_start_cursor(self) -> None:
        """A page smaller than total should request ``?start=N`` for the next page."""
        # Page 1: full, page 2: 5 rows < page_size -> end
        rows_p1 = [
            _safe_bonds_bar(
                date(2024, 1, 1).replace(day=1 + (i % 28) or 1, month=1 + (i // 28)),
                ticker="SU1",
                base=100 + i,
            )
            for i in range(100)
        ]
        rows_p2 = [_safe_bonds_bar(date(2024, 4, 9 + i), ticker="SU1", base=110 + i) for i in range(5)]  # noqa: E501
        handlers = [
            _FakeResponse(_bonds_history_block(rows_p1, total=105)),
            _FakeResponse(_bonds_history_block(rows_p2, total=105)),
        ]
        loader = _make_loader(handlers)
        loader._page_size = 100
        out = list(loader.iter_ohlcv("SU1", date(2024, 1, 1), date(2024, 12, 31)))
        assert len(out) == 105
        # Second call MUST include start=100
        assert len(loader._session.calls) == 2  # type: ignore[attr-defined]
        assert loader._session.calls[1]["params"].get("start") == 100  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Mapping MOEX bonds rows → OHLCVRow
# ---------------------------------------------------------------------------


class TestBondsRowMapping:
    def test_uppercase_columns_are_mapped_to_ohlcv_row(self) -> None:
        handlers = [
            _FakeResponse(
                _bonds_history_block(
                    [_safe_bonds_bar(date(2025, 8, 1), ticker="SU46020RMFS2", base=64.457)],
                    total=1,
                )
            ),
        ]
        loader = _make_loader(handlers)
        out = list(loader.iter_ohlcv("SU46020RMFS2", date(2025, 8, 1), date(2025, 8, 2)))
        assert len(out) == 1
        bar = out[0]
        assert bar.ticker == "SU46020RMFS2"
        assert bar.ts == date(2025, 8, 1)
        assert bar.close == Decimal("64.457")
        assert bar.high == Decimal("66.457")
        assert bar.low == Decimal("63.457")
        assert bar.open == Decimal("64.957")
        assert bar.volume  # shares, derived from VOLUME * lot

    def test_window_filter_drops_out_of_range_rows(self) -> None:
        rows = [
            _safe_bonds_bar(date(2025, 7, 31), ticker="SU1"),  # before window
            _safe_bonds_bar(date(2025, 8, 1), ticker="SU1"),
            _safe_bonds_bar(date(2025, 8, 2), ticker="SU1"),
            _safe_bonds_bar(date(2025, 8, 3), ticker="SU1"),  # after window
        ]
        handlers = [_FakeResponse(_bonds_history_block(rows, total=4))]
        loader = _make_loader(handlers)
        out = list(loader.iter_ohlcv("SU1", date(2025, 8, 1), date(2025, 8, 2)))
        assert [b.ts for b in out] == [date(2025, 8, 1), date(2025, 8, 2)]

    def test_bonds_404_raises_loader_not_found(self) -> None:
        """A 404 from the bonds endpoint is a clear "not a bond" signal — surface it."""
        handlers = [_FakeResponse({}, status_code=404)]
        loader = _make_loader(handlers)
        with pytest.raises(LoaderNotFoundError):
            list(loader.iter_ohlcv("SU-DOES-NOT-EXIST", date(2025, 8, 1), date(2025, 8, 2)))


# ---------------------------------------------------------------------------
# Issue #364 — vol_raw fallback chain must NOT include NUMTRADES.
#
# NUMTRADES on MOEX ISS is the *count of executed trades*, not traded volume.
# When VOLUME is absent/zero on a real ISS row (illiquid session,
# last-trade-day entry, delisted trailing bar), PR #362's fallback chain
# silently substitutes NUMTRADES (typically 1..150) and we lot-multiply that
# as if it were a share count. That's data corruption, not graceful
# degradation. Original pre-#362 behaviour was `volume or VOLUME` → 0;
# accurate silence is better than a fabricated value.
# ---------------------------------------------------------------------------


def _bonds_bar_with_volume(
    d: date,
    *,
    ticker: str,
    volume: Any = 100,
    numtrades: int = 10,
) -> list[Any]:
    """Build a bonds row with explicit (possibly zero/absent) volume values.

    Shares branch equivalent: see _shares_bar_zero_volume below.
    """
    lo = Decimal("99")
    o = Decimal("100.5")
    c = Decimal("100")
    h = Decimal("102")
    return [
        "TQOB",
        d.isoformat(),
        ticker,
        ticker,
        numtrades,  # NUMTRADES — count of executed trades, NOT volume
        Decimal("100000"),  # VALUE
        Decimal(str(lo)),
        Decimal(str(h)),
        Decimal(str(c)),
        Decimal(str(c)),
        Decimal("0"),
        Decimal(str(c)),
        Decimal(str(o)),
        volume,  # VOLUME
    ]


class TestVolumeFallbackExcludesNumtrades:
    """Regression guard for issue #364."""

    def test_bonds_volume_zero_does_not_silently_become_numtrades(self) -> None:
        """A bonds row with VOLUME=0 must produce volume=0, NOT NUMTRADES (10).

        This is the exact failure mode that PR #362 introduced: when ISS
        returns a row with volume=0 for an illiquid OFZ session, the
        fallback chain `VOLUME or NUMTRADES` would silently substitute 10
        (the trade count) and persist that as the day's volume. That's
        silent data corruption.
        """
        handlers = [
            _FakeResponse(
                _bonds_history_block(
                    [
                        _bonds_bar_with_volume(
                            date(2025, 8, 1),
                            ticker="SU46020RMFS2",
                            volume=0,
                            numtrades=10,
                        ),
                    ],
                    total=1,
                )
            ),
        ]
        loader = _make_loader(handlers)
        out = list(loader.iter_ohlcv("SU46020RMFS2", date(2025, 8, 1), date(2025, 8, 2)))
        assert len(out) == 1
        assert out[0].volume == 0, f"bonds VOLUME=0 must stay 0, not NUMTRADES; got volume={out[0].volume}"

    def test_bonds_volume_missing_does_not_silently_become_numtrades(self) -> None:
        """A bonds row with VOLUME absent (None) must produce volume=0, NOT NUMTRADES."""
        handlers = [
            _FakeResponse(
                _bonds_history_block(
                    [
                        _bonds_bar_with_volume(
                            date(2025, 8, 1),
                            ticker="SU46020RMFS2",
                            volume=None,
                            numtrades=42,
                        ),
                    ],
                    total=1,
                )
            ),
        ]
        loader = _make_loader(handlers)
        out = list(loader.iter_ohlcv("SU46020RMFS2", date(2025, 8, 1), date(2025, 8, 2)))
        assert len(out) == 1
        assert out[0].volume == 0, f"bonds VOLUME=None must stay 0, not NUMTRADES; got volume={out[0].volume}"

    def test_bonds_volume_present_is_used_directly(self) -> None:
        """Sanity: when VOLUME is present, that exact value is used."""
        handlers = [
            _FakeResponse(
                _bonds_history_block(
                    [
                        _bonds_bar_with_volume(
                            date(2025, 8, 1),
                            ticker="SU46020RMFS2",
                            volume=500,
                            numtrades=10,
                        ),
                    ],
                    total=1,
                )
            ),
        ]
        loader = _make_loader(handlers)
        out = list(loader.iter_ohlcv("SU46020RMFS2", date(2025, 8, 1), date(2025, 8, 2)))
        assert len(out) == 1
        # Bonds branch does NOT lot-multiply; volume == raw VOLUME column.
        assert out[0].volume == 500
