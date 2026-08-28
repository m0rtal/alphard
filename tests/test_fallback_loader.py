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


def test_list_tickers_uses_tinkoff_md_first() -> None:
    """Issue #316 (2026-08-28): tinkoff_md is the PRIMARY universe source.

    Reason: tinkoff_md (history-data archive) covers ALL 4 MOEX classes
    (TQBR + SPBXM + TQCB + TQOB = ~3259 tickers including delisted). It
    uses the same REAL token shared from gRPC loader (post PR #313). Why
    not gRPC first? tinkoff_grpc.list_tickers() returns ONLY the TQBR
    class (~252 tickers) — losing ~3000 from the universe. Pre-fix
    (gRPC-first in PR #313) the universe had only 252 tickers when it
    should have had 3259. Post-fix: MD wins → 3259 → gRPC fallback
    (252) → MOEX ISS last-resort (~1927 TQBR).
    """
    md = MagicMock()
    md.list_tickers_with_figi.return_value = ["MD_T1", "MD_T2"]
    grpc = MagicMock()
    grpc.list_tickers.return_value = ["GRPC_T1", "GRPC_T2", "GRPC_T3"]
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=_loader_empty())

    out = fl.list_tickers()

    # MD wins (first non-empty in MD → gRPC → MOEX chain).
    assert out == ["MD_T1", "MD_T2"]
    md.list_tickers_with_figi.assert_called_once()
    # gRPC must NOT have been called when MD succeeded.
    grpc.list_tickers.assert_not_called()


def test_list_tickers_falls_back_to_grpc_when_md_empty() -> None:
    """When MD returns 0 tickers (degenerate case), fall back to gRPC."""
    md = MagicMock()
    md.list_tickers_with_figi.return_value = []
    grpc = MagicMock()
    grpc.list_tickers.return_value = ["GRPC_T1", "GRPC_T2"]
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=_loader_empty())

    out = fl.list_tickers()

    assert out == ["GRPC_T1", "GRPC_T2"]
    md.list_tickers_with_figi.assert_called_once()
    grpc.list_tickers.assert_called_once()
    assert fl.stats["tinkoff_md"]["fallback"] == 1
    assert fl.stats["tinkoff_grpc"]["ok"] == 1


def test_list_tickers_falls_back_to_grpc_when_md_raises() -> None:
    """When MD raises (UNAUTHENTICATED, network, etc.), fall back to gRPC."""
    md = MagicMock()
    md.list_tickers_with_figi.side_effect = RuntimeError("UNAUTHENTICATED")
    grpc = MagicMock()
    grpc.list_tickers.return_value = ["GRPC_T1"]
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=_loader_empty())

    out = fl.list_tickers()

    assert out == ["GRPC_T1"]
    grpc.list_tickers.assert_called_once()
    assert fl.stats["tinkoff_md"]["error"] == 1
    assert fl.stats["tinkoff_grpc"]["ok"] == 1


def test_list_tickers_falls_back_to_moex_when_md_and_grpc_fail() -> None:
    """When both MD and gRPC fail, try MOEX ISS (no auth, last resort)."""
    moex = MagicMock()
    moex.list_tickers.return_value = ["MOEX_T1", "MOEX_T2"]
    md = MagicMock()
    md.list_tickers_with_figi.side_effect = RuntimeError("UNAUTHENTICATED")
    grpc = MagicMock()
    grpc.list_tickers.side_effect = RuntimeError("UNAUTHENTICATED")
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)

    out = fl.list_tickers()

    assert out == ["MOEX_T1", "MOEX_T2"]
    assert fl.stats["tinkoff_md"]["error"] == 1
    assert fl.stats["tinkoff_grpc"]["error"] == 1
    assert fl.stats["moex_iss"]["ok"] == 1


def test_list_tickers_returns_empty_when_all_sources_fail() -> None:
    """When ALL THREE sources fail, return [] — supervisor treats this as
    rc=3 NO_UNIVERSE and applies exponential backoff (see test_main_backfill_supervisor.py).
    """
    moex = MagicMock()
    moex.list_tickers.side_effect = RuntimeError("network error")
    md = MagicMock()
    md.list_tickers_with_figi.side_effect = RuntimeError("UNAUTHENTICATED")
    grpc = MagicMock()
    grpc.list_tickers.side_effect = RuntimeError("UNAUTHENTICATED")
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)

    out = fl.list_tickers()

    assert out == []
    # Every source must be marked as errored.
    assert fl.stats["tinkoff_md"]["error"] == 1
    assert fl.stats["tinkoff_grpc"]["error"] == 1
    assert fl.stats["moex_iss"]["error"] == 1


def test_list_tickers_full_chain_md_empty_then_grpc_empty_then_moex_empty() -> None:
    """All three sources return [] (not raise, just empty). Returns [].

    This is the path that triggers rc=3 NO_UNIVERSE without any error
    counters being incremented — every source reports ``fallback`` (zero
    results) rather than ``error`` (exception). Order is MD → gRPC → MOEX.
    """
    moex = MagicMock()
    moex.list_tickers.return_value = []
    md = MagicMock()
    md.list_tickers_with_figi.return_value = []
    grpc = MagicMock()
    grpc.list_tickers.return_value = []
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)

    out = fl.list_tickers()

    assert out == []
    # Each source reports fallback (zero results), NOT error (exception).
    assert fl.stats["tinkoff_md"]["fallback"] == 1
    assert fl.stats["tinkoff_grpc"]["fallback"] == 1
    assert fl.stats["moex_iss"]["fallback"] == 1


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


# ---------------------------------------------------------------------------
# C5 coverage: defensive paths in FallbackDataLoader
# ---------------------------------------------------------------------------


def test_resolve_returns_none_for_unknown_source_name() -> None:
    """Line 150: ``_resolve`` returns ``None`` for unknown name.

    Exercising this path requires passing a custom ``order`` that contains
    a name not in {``tinkoff_md``, ``tinkoff_grpc``, ``moex_iss``}. The
    fallback contract should treat unknown sources as 'not configured'
    rather than raise.
    """
    md = _loader_with_rows(["row1"])
    fl = FallbackDataLoader(
        tinkoff_md=md,
        tinkoff_grpc=None,
        moex_iss=None,
        order=("nonexistent_source",),
    )
    out = list(fl.iter_ohlcv("X", date(2026, 1, 1), date(2026, 1, 31)))
    assert out == []
    # The unknown source never reaches the underlying loader.
    md.iter_ohlcv.assert_not_called()


def test_resolve_skips_missing_ohlcv_source_at_iter_time() -> None:
    """Line 124: ``_resolve`` returns ``None`` for a configured-but-unset source.

    If you instantiate FallbackDataLoader with only tinkoff_md and leave
    tinkoff_grpc/moex_iss at their defaults (``None``), ``_resolve("tinkoff_grpc")``
    returns ``None`` and the loop skips to the next source.
    """
    md = _loader_with_rows(["bar"])
    fl = FallbackDataLoader(
        tinkoff_md=md,
        tinkoff_grpc=None,  # type: ignore[arg-type]
        moex_iss=None,  # type: ignore[arg-type]
    )
    out = list(fl.iter_ohlcv("X", date(2026, 1, 1), date(2026, 1, 31)))
    assert out == ["bar"]


def test_iter_corporate_actions_skips_when_method_missing() -> None:
    """Lines 167-169: skip sources without ``iter_corporate_actions``.

    The ``hasattr(source, 'iter_corporate_actions')`` check should skip
    sources that don't implement the method.
    """
    md = MagicMock(spec=["iter_ohlcv"])  # no iter_corporate_actions
    grpc = MagicMock()
    grpc.iter_corporate_actions.return_value = iter(["div2"])
    # md is configured as first in the order but lacks iter_corporate_actions.
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=None)
    out = list(fl.iter_corporate_actions("X", date(2026, 1, 1), date(2026, 1, 31)))
    assert out == ["div2"]


def test_iter_corporate_actions_exception_falls_back() -> None:
    """Lines 170-172: source raises during corp-actions → fall back.

    Tinkoff gRPC raises; MOEX ISS returns rows. The exception branch
    logs + continues; MOEX supplies the answer.
    """
    grpc = MagicMock()
    grpc.iter_corporate_actions.side_effect = RuntimeError("grpc down")
    moex = MagicMock()
    moex.iter_corporate_actions.return_value = iter(["corp_event"])
    fl = FallbackDataLoader(tinkoff_md=None, tinkoff_grpc=grpc, moex_iss=moex)
    out = list(fl.iter_corporate_actions("X", date(2026, 1, 1), date(2026, 1, 31)))
    assert out == ["corp_event"]
