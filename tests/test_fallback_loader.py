"""Tests for FallbackDataLoader — the multi-source OHLCV loader.

The loader wraps three existing data sources behind a single iterator
and falls back when a source fails or returns no rows. These tests
pin the contract:

  1. Primary source (tinkoff_md) returns → its rows are yielded.
  2. Primary returns 0 rows → falls back to secondary.
  3. Primary raises → falls back to secondary.
  4. All three return 0 → no rows yielded, ticker logged as empty.
  5. Per-source stats are tracked.
  6. Corporate actions use the same fallback contract.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock


from src.data.fallback_loader import FALLBACK_ORDER, FallbackDataLoader


def _loader_with_rows(rows: list[Any]) -> MagicMock:
    m = MagicMock()
    m.iter_ohlcv.return_value = iter(rows)
    return m


def _loader_with_exc(exc: Exception) -> MagicMock:
    m = MagicMock()
    m.iter_ohlcv.side_effect = exc
    return m


def _loader_empty() -> MagicMock:
    return _loader_with_rows([])


# ---------------------------------------------------------------------------
# _resolve / order
# ---------------------------------------------------------------------------


def test_fallback_order_default_is_three_sources() -> None:
    assert FALLBACK_ORDER == ("tinkoff_md", "tinkoff_grpc", "moex_iss")


def test_custom_order_supported() -> None:
    md = _loader_empty()
    grpc = _loader_empty()
    moex = _loader_empty()
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex, order=("moex_iss",))
    assert fl.order == ("moex_iss",)
    assert fl._resolve("moex_iss") is moex


# ---------------------------------------------------------------------------
# iter_ohlcv happy path
# ---------------------------------------------------------------------------


def test_primary_source_returns_rows_immediately() -> None:
    """If source 1 returns a non-empty result, no fallback happens."""
    md_rows = ["row1", "row2", "row3"]
    md = _loader_with_rows(md_rows)
    fl = FallbackDataLoader(
        tinkoff_md=md,
        tinkoff_grpc=_loader_empty(),
        moex_iss=_loader_empty(),
    )

    out = list(fl.iter_ohlcv("SBER", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == md_rows
    md.iter_ohlcv.assert_called_once_with("SBER", date(2026, 1, 1), date(2026, 1, 31))
    # grpc / moex never called
    fl._resolve("tinkoff_grpc").iter_ohlcv.assert_not_called()  # type: ignore[attr-defined]
    fl._resolve("moex_iss").iter_ohlcv.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# fallback when source returns 0 rows
# ---------------------------------------------------------------------------


def test_zero_rows_triggers_fallback() -> None:
    """Source 1 returns 0 rows → fall through to source 2."""
    md = _loader_empty()
    grpc_rows = ["g1", "g2"]
    grpc = _loader_with_rows(grpc_rows)
    moex = _loader_empty()
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_ohlcv("GAZP", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == grpc_rows
    md.iter_ohlcv.assert_called_once()
    grpc.iter_ohlcv.assert_called_once_with("GAZP", date(2026, 1, 1), date(2026, 1, 31))
    moex.iter_ohlcv.assert_not_called()


def test_zero_rows_then_zero_rows_then_data() -> None:
    """All three sources tried in order; data comes from the third."""
    md = _loader_empty()
    grpc = _loader_empty()
    moex_rows = ["m1"]
    moex = _loader_with_rows(moex_rows)
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_ohlcv("YDEX", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == moex_rows
    md.iter_ohlcv.assert_called_once()
    grpc.iter_ohlcv.assert_called_once()
    moex.iter_ohlcv.assert_called_once()


# ---------------------------------------------------------------------------
# fallback when source raises
# ---------------------------------------------------------------------------


def test_exception_triggers_fallback() -> None:
    """A network error on source 1 must not abort — fall through."""
    md = _loader_with_exc(RuntimeError("429 too many requests"))
    grpc_rows = ["g1"]
    grpc = _loader_with_rows(grpc_rows)
    moex = _loader_empty()
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_ohlcv("OZON", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == grpc_rows
    md.iter_ohlcv.assert_called_once()
    grpc.iter_ohlcv.assert_called_once()


def test_all_sources_fail_yields_nothing() -> None:
    """Every source returns 0 — caller sees an empty iterator."""
    fl = FallbackDataLoader(
        tinkoff_md=_loader_empty(),
        tinkoff_grpc=_loader_empty(),
        moex_iss=_loader_empty(),
    )

    out = list(fl.iter_ohlcv("DELISTED", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == []


def test_all_sources_raise_yields_nothing() -> None:
    """Every source raises — caller sees an empty iterator."""
    fl = FallbackDataLoader(
        tinkoff_md=_loader_with_exc(RuntimeError("md fail")),
        tinkoff_grpc=_loader_with_exc(RuntimeError("grpc fail")),
        moex_iss=_loader_with_exc(RuntimeError("moex fail")),
    )

    out = list(fl.iter_ohlcv("BROKEN", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == []


# ---------------------------------------------------------------------------
# stats tracking
# ---------------------------------------------------------------------------


def test_stats_track_ok_fallback_error() -> None:
    md = _loader_with_exc(RuntimeError("boom"))  # error → fallback
    grpc = _loader_with_rows(["g1"])  # ok
    moex = _loader_empty()  # not reached
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)

    list(fl.iter_ohlcv("X", date(2026, 1, 1), date(2026, 1, 31)))

    stats = fl.stats
    assert stats["tinkoff_md"]["error"] == 1
    assert stats["tinkoff_md"]["fallback"] == 1
    assert stats["tinkoff_grpc"]["ok"] == 1
    assert stats["tinkoff_grpc"]["fallback"] == 0
    assert stats["tinkoff_grpc"]["error"] == 0
    assert stats["moex_iss"]["ok"] == 0
    assert stats["moex_iss"]["fallback"] == 0
    assert stats["moex_iss"]["error"] == 0


def test_stats_zero_rows_counts_as_fallback() -> None:
    """Source returning 0 rows counts as fallback (not error, not ok)."""
    fl = FallbackDataLoader(
        tinkoff_md=_loader_empty(),
        tinkoff_grpc=_loader_with_rows(["g1"]),
        moex_iss=_loader_empty(),
    )

    list(fl.iter_ohlcv("X", date(2026, 1, 1), date(2026, 1, 31)))

    assert fl.stats["tinkoff_md"]["fallback"] == 1
    assert fl.stats["tinkoff_md"]["error"] == 0
    assert fl.stats["tinkoff_grpc"]["ok"] == 1


# ---------------------------------------------------------------------------
# list_tickers + iter_corporate_actions
# ---------------------------------------------------------------------------


def test_list_tickers_uses_tinkoff_md() -> None:
    """Universe = source-of-truth from tinkoff_md (already includes
    bonds/ETFs from gRPC)."""
    md = MagicMock()
    md.list_tickers_with_figi.return_value = ["T1", "T2"]
    fl = FallbackDataLoader(
        tinkoff_md=md,
        tinkoff_grpc=_loader_empty(),
        moex_iss=_loader_empty(),
    )

    out = fl.list_tickers()

    assert out == ["T1", "T2"]
    md.list_tickers_with_figi.assert_called_once()


def test_iter_corporate_actions_falls_back() -> None:
    """Same fallback contract for corporate actions."""
    md = MagicMock()
    md.iter_corporate_actions.return_value = iter([])
    grpc = MagicMock()
    grpc.iter_corporate_actions.return_value = iter(["split"])
    moex = MagicMock()
    moex.iter_corporate_actions.return_value = iter([])
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_corporate_actions("SBER", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == ["split"]
    md.iter_corporate_actions.assert_called_once()
    grpc.iter_corporate_actions.assert_called_once()
    moex.iter_corporate_actions.assert_not_called()


def test_iter_corporate_actions_skips_sources_without_method() -> None:
    """If a source doesn't implement iter_corporate_actions, skip it."""
    md = MagicMock(spec=["iter_ohlcv"])  # no iter_corporate_actions
    grpc = MagicMock()
    grpc.iter_corporate_actions.return_value = iter(["div"])
    moex = MagicMock()
    moex.iter_corporate_actions.return_value = iter([])
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_corporate_actions("X", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == ["div"]
    grpc.iter_corporate_actions.assert_called_once()
    # moex was called (has method), but no rows from grpc -> moex
    moex.iter_corporate_actions.assert_not_called()
