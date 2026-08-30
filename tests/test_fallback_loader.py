"""Tests for FallbackDataLoader — the multi-source OHLCV loader.

The loader wraps two production data sources behind a single iterator
and falls back when a source fails or returns no rows. These tests
pin the contract:

  1. Primary source (tinkoff_grpc / broker gRPC) returns → its rows are yielded.
  2. Primary returns 0 rows → falls back to MOEX ISS.
  3. Primary raises → falls back to MOEX ISS.
  4. Both sources return 0 / raise → no rows yielded, ticker logged as empty.
  5. Per-source stats are tracked.
  6. Corporate actions use the same fallback contract.
  7. ``tinkoff_md`` is no longer in the chain (issue #331, 2026-08-29);
     the parameter is preserved for backward compatibility but ignored.

Contract change 2026-08-29 (issue #331, m0rtal):
    chain = tinkoff_grpc → moex_iss (MD-archive dropped).
    tinkoff_md parameter kept as keyword-only for back-compat;
    it is stored on self.tinkoff_md but never tried in the chain.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock


from src.data.fallback_loader import FALLBACK_ORDER, FallbackDataLoader
from src.data.loader import LoaderError
from src.data.moex_loader import MAX_LOOKBACK as MOEX_MAX_LOOKBACK


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


def test_fallback_order_default_is_broker_first() -> None:
    # Contract change 2026-08-29 (issue #331): chain is now
    # broker-first (tinkoff_grpc → moex_iss). Tinkoff history-data HTTP
    # archive is no longer in the chain.
    assert FALLBACK_ORDER == ("tinkoff_grpc", "moex_iss")


def test_custom_order_supported() -> None:
    grpc = _loader_empty()
    moex = _loader_empty()
    fl = FallbackDataLoader(tinkoff_grpc=grpc, moex_iss=moex, order=("moex_iss",))
    assert fl.order == ("moex_iss",)
    assert fl._resolve("moex_iss") is moex
    assert fl._resolve("tinkoff_md") is None  # never in chain any more


def test_tinkoff_md_kwarg_kept_for_backcompat_but_not_in_chain() -> None:
    """Issue #331: tinkoff_md is preserved as a keyword-only arg so legacy
    callers don't raise, but it is never tried in the fallback chain."""
    md = _loader_with_rows(["should_never_be_yielded"])
    grpc = _loader_empty()
    moex = _loader_empty()
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=moex)
    # Stored on instance for back-compat
    assert fl.tinkoff_md is md
    # But never tried: _resolve("tinkoff_md") returns None
    assert fl._resolve("tinkoff_md") is None
    # And FALLBACK_ORDER doesn't contain it
    assert "tinkoff_md" not in FALLBACK_ORDER


# ---------------------------------------------------------------------------
# iter_ohlcv happy path
# ---------------------------------------------------------------------------


def test_primary_source_returns_rows_immediately() -> None:
    """If source 1 (broker gRPC) returns a non-empty result, no fallback happens.

    Contract change 2026-08-29 (issue #331): primary is now tinkoff_grpc
    (broker gRPC). MD archive is no longer in the chain.
    """
    grpc_rows = ["row1", "row2", "row3"]
    grpc = _loader_with_rows(grpc_rows)
    fl = FallbackDataLoader(
        tinkoff_md=_loader_empty(),
        tinkoff_grpc=grpc,
        moex_iss=_loader_empty(),
    )

    out = list(fl.iter_ohlcv("SBER", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == grpc_rows
    grpc.iter_ohlcv.assert_called_once_with("SBER", date(2026, 1, 1), date(2026, 1, 31))
    # md / moex never called
    fl._resolve("tinkoff_md")  # would be None in chain
    fl._resolve("moex_iss").iter_ohlcv.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# fallback when source returns 0 rows
# ---------------------------------------------------------------------------


def test_zero_rows_triggers_fallback() -> None:
    """Broker gRPC returns 0 rows → fall through to MOEX ISS."""
    grpc = _loader_empty()
    moex_rows = ["m1", "m2"]
    moex = _loader_with_rows(moex_rows)
    fl = FallbackDataLoader(tinkoff_md=_loader_empty(), tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_ohlcv("GAZP", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == moex_rows
    grpc.iter_ohlcv.assert_called_once()
    moex.iter_ohlcv.assert_called_once_with("GAZP", date(2026, 1, 1), date(2026, 1, 31))


def test_only_two_sources_in_chain() -> None:
    """Defensive: the chain has exactly 2 sources. tinkoff_md is not in it."""
    grpc = _loader_empty()
    moex = _loader_empty()
    fl = FallbackDataLoader(tinkoff_md=_loader_empty(), tinkoff_grpc=grpc, moex_iss=moex)
    assert len(fl.order) == 2
    assert "tinkoff_md" not in fl.order


# ---------------------------------------------------------------------------
# fallback when source raises
# ---------------------------------------------------------------------------


def test_exception_triggers_fallback() -> None:
    """A network error on broker gRPC must not abort — fall through to MOEX."""
    grpc = _loader_with_exc(RuntimeError("429 too many requests"))
    moex_rows = ["m1"]
    moex = _loader_with_rows(moex_rows)
    fl = FallbackDataLoader(tinkoff_md=_loader_empty(), tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_ohlcv("OZON", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == moex_rows
    grpc.iter_ohlcv.assert_called_once()
    moex.iter_ohlcv.assert_called_once()


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
    """When grpc raises, fallback to moex succeeds.

    Per issue #331: tinkoff_md is no longer in chain; its stats slot is gone.
    """
    grpc = _loader_with_exc(RuntimeError("boom"))  # error → fallback
    moex = _loader_with_rows(["m1"])  # ok
    fl = FallbackDataLoader(tinkoff_md=_loader_empty(), tinkoff_grpc=grpc, moex_iss=moex)

    list(fl.iter_ohlcv("X", date(2026, 1, 1), date(2026, 1, 31)))

    stats = fl.stats
    assert "tinkoff_md" not in stats  # dropped from stats dict (issue #331)
    assert stats["tinkoff_grpc"]["error"] == 1
    assert stats["tinkoff_grpc"]["fallback"] == 1
    assert stats["moex_iss"]["ok"] == 1
    assert stats["moex_iss"]["fallback"] == 0
    assert stats["moex_iss"]["error"] == 0


def test_stats_zero_rows_counts_as_fallback() -> None:
    """Source returning 0 rows counts as fallback (not error, not ok).

    grpc returns 0 → fallback to moex; moex returns 0 → fallback (recorded).
    """
    fl = FallbackDataLoader(
        tinkoff_md=_loader_empty(),
        tinkoff_grpc=_loader_empty(),
        moex_iss=_loader_empty(),
    )

    list(fl.iter_ohlcv("X", date(2026, 1, 1), date(2026, 1, 31)))

    assert fl.stats["tinkoff_grpc"]["fallback"] == 1
    assert fl.stats["tinkoff_grpc"]["error"] == 0
    assert fl.stats["moex_iss"]["fallback"] == 1
    assert fl.stats["moex_iss"]["error"] == 0
    # MD slot is gone (issue #331).
    assert "tinkoff_md" not in fl.stats


# ---------------------------------------------------------------------------
# list_tickers + iter_corporate_actions (chain = grpc → moex)
# ---------------------------------------------------------------------------


def test_list_tickers_uses_tinkoff_grpc_first() -> None:
    """Issue #331 (2026-08-29): tinkoff_grpc is the PRIMARY universe source.

    Reason: broker gRPC walks every tradable class (TQBR + SPBXM + TQCB + TQOB +
    ...) and returns ~3259 tickers with FIGI. The old chain (MD first) was
    indirect — ``tinkoff_md.list_tickers_with_figi()`` delegated to broker
    gRPC anyway (see ``src/data/tinkoff_md_loader.py:_fill_universe_cache``),
    so dropping MD from the chain removes a redundant round-trip while
    keeping the same ~3259-ticker universe.
    """
    grpc = MagicMock()
    grpc.list_tickers.return_value = ["GRPC_T1", "GRPC_T2", "GRPC_T3"]
    moex = MagicMock()
    fl = FallbackDataLoader(tinkoff_md=MagicMock(), tinkoff_grpc=grpc, moex_iss=moex)

    out = fl.list_tickers()

    # gRPC wins (first non-empty in chain).
    assert out == ["GRPC_T1", "GRPC_T2", "GRPC_T3"]
    grpc.list_tickers.assert_called_once()
    # moex must NOT have been called when gRPC succeeded.
    moex.list_tickers.assert_not_called()


def test_list_tickers_falls_back_to_moex_when_grpc_empty() -> None:
    """When gRPC returns 0 tickers (degenerate case), fall back to MOEX."""
    grpc = MagicMock()
    grpc.list_tickers.return_value = []
    moex = MagicMock()
    moex.list_tickers.return_value = ["MOEX_T1", "MOEX_T2"]
    fl = FallbackDataLoader(tinkoff_md=MagicMock(), tinkoff_grpc=grpc, moex_iss=moex)

    out = fl.list_tickers()

    assert out == ["MOEX_T1", "MOEX_T2"]
    grpc.list_tickers.assert_called_once()
    moex.list_tickers.assert_called_once()
    assert fl.stats["tinkoff_grpc"]["fallback"] == 1
    assert fl.stats["moex_iss"]["ok"] == 1


def test_list_tickers_falls_back_to_moex_when_grpc_raises() -> None:
    """When gRPC raises (UNAUTHENTICATED, network, etc.), fall back to MOEX."""
    grpc = MagicMock()
    grpc.list_tickers.side_effect = RuntimeError("UNAUTHENTICATED")
    moex = MagicMock()
    moex.list_tickers.return_value = ["MOEX_T1"]
    fl = FallbackDataLoader(tinkoff_md=MagicMock(), tinkoff_grpc=grpc, moex_iss=moex)

    out = fl.list_tickers()

    assert out == ["MOEX_T1"]
    moex.list_tickers.assert_called_once()
    assert fl.stats["tinkoff_grpc"]["error"] == 1
    assert fl.stats["moex_iss"]["ok"] == 1


def test_list_tickers_returns_empty_when_all_sources_fail() -> None:
    """When both sources fail, return [] — supervisor treats this as a clean
    exit (rc=0) and respawns after the fixed 30s _BACKFILL_RESPAWN_BACKOFF_SECONDS
    (src/main.py). Every source must be marked as errored.
    """
    moex = MagicMock()
    moex.list_tickers.side_effect = RuntimeError("network error")
    grpc = MagicMock()
    grpc.list_tickers.side_effect = RuntimeError("UNAUTHENTICATED")
    fl = FallbackDataLoader(tinkoff_md=MagicMock(), tinkoff_grpc=grpc, moex_iss=moex)

    out = fl.list_tickers()

    assert out == []
    # Every source must be marked as errored.
    assert fl.stats["tinkoff_grpc"]["error"] == 1
    assert fl.stats["moex_iss"]["error"] == 1


def test_list_tickers_full_chain_grpc_empty_then_moex_empty() -> None:
    """Both sources return [] (not raise, just empty). Returns [].

    This is the path that triggers a clean supervisor respawn (rc=0,
    fixed 30s backoff) without any error counters being incremented — every
    source reports fallback (zero results) rather than error (exception).
    Order is gRPC → MOEX.
    """
    moex = MagicMock()
    moex.list_tickers.return_value = []
    grpc = MagicMock()
    grpc.list_tickers.return_value = []
    fl = FallbackDataLoader(tinkoff_md=MagicMock(), tinkoff_grpc=grpc, moex_iss=moex)

    out = fl.list_tickers()

    assert out == []
    # Each source reports fallback (zero results), NOT error (exception).
    assert fl.stats["tinkoff_grpc"]["fallback"] == 1
    assert fl.stats["moex_iss"]["fallback"] == 1


def test_list_tickers_md_loader_is_never_consulted() -> None:
    """Defensive: tinkoff_md.list_tickers_with_figi() must NEVER be called,
    even if a tinkoff_md attribute is configured. This pins the chain order
    so a future refactor cannot silently re-introduce the MD archive.
    """
    md = MagicMock()
    md.list_tickers_with_figi.return_value = ["MD_T1"]
    grpc = MagicMock()
    grpc.list_tickers.return_value = ["GRPC_T1"]
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=grpc, moex_iss=MagicMock())

    fl.list_tickers()

    # The MD loader is stored on the instance but its universe API is never
    # invoked from the chain.
    md.list_tickers_with_figi.assert_not_called()
    md.list_tickers.assert_not_called()


def test_iter_corporate_actions_falls_back() -> None:
    """Same fallback contract for corporate actions: gRPC → MOEX."""
    grpc = MagicMock()
    grpc.iter_corporate_actions.return_value = iter([])
    moex = MagicMock()
    moex.iter_corporate_actions.return_value = iter(["split"])
    fl = FallbackDataLoader(tinkoff_md=MagicMock(), tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_corporate_actions("SBER", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == ["split"]
    grpc.iter_corporate_actions.assert_called_once()
    moex.iter_corporate_actions.assert_called_once()


def test_iter_corporate_actions_skips_sources_without_method() -> None:
    """If a source doesn't implement iter_corporate_actions, skip it."""
    # gRPC has iter_corporate_actions; moex has it too
    grpc = MagicMock()
    grpc.iter_corporate_actions.return_value = iter(["div"])
    moex = MagicMock()
    moex.iter_corporate_actions.return_value = iter([])
    fl = FallbackDataLoader(tinkoff_md=MagicMock(spec=["iter_ohlcv"]), tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_corporate_actions("X", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == ["div"]
    grpc.iter_corporate_actions.assert_called_once()
    # moex was NOT called (grpc returned data first).
    moex.iter_corporate_actions.assert_not_called()


# ---------------------------------------------------------------------------
# Defensive paths in FallbackDataLoader
# ---------------------------------------------------------------------------


def test_resolve_returns_none_for_unknown_source_name() -> None:
    """``_resolve`` returns ``None`` for unknown source name.

    The fallback contract should treat unknown sources as 'not configured'
    rather than raise.
    """
    grpc = _loader_with_rows(["row1"])
    fl = FallbackDataLoader(
        tinkoff_grpc=grpc,
        moex_iss=None,  # type: ignore[arg-type]
        order=("nonexistent_source",),
    )
    out = list(fl.iter_ohlcv("X", date(2026, 1, 1), date(2026, 1, 31)))
    assert out == []
    # The unknown source never reaches the underlying loader.
    grpc.iter_ohlcv.assert_not_called()


def test_resolve_skips_missing_ohlcv_source_at_iter_time() -> None:
    """``_resolve`` returns ``None`` for a configured-but-unset source.

    If you instantiate FallbackDataLoader without moex_iss (``None``), the
    loop's ``if source is None: continue`` branch must skip that source.
    """
    grpc = _loader_with_rows(["bar"])
    fl = FallbackDataLoader(
        tinkoff_grpc=grpc,
        moex_iss=None,  # type: ignore[arg-type]
    )
    out = list(fl.iter_ohlcv("X", date(2026, 1, 1), date(2026, 1, 31)))
    assert out == ["bar"]


def test_iter_corporate_actions_skips_when_method_missing() -> None:
    """Skip sources without ``iter_corporate_actions``.

    The ``hasattr(source, 'iter_corporate_actions')`` check should skip
    sources that don't implement the method.
    """
    grpc = MagicMock()
    grpc.iter_corporate_actions.return_value = iter(["div2"])
    fl = FallbackDataLoader(
        tinkoff_md=MagicMock(spec=["iter_ohlcv"]),  # no iter_corporate_actions
        tinkoff_grpc=grpc,
        moex_iss=None,  # type: ignore[arg-type]
    )
    out = list(fl.iter_corporate_actions("X", date(2026, 1, 1), date(2026, 1, 31)))
    assert out == ["div2"]


def test_iter_corporate_actions_exception_falls_back() -> None:
    """Source raises during corp-actions → fall back."""
    grpc = MagicMock()
    grpc.iter_corporate_actions.side_effect = RuntimeError("grpc down")
    moex = MagicMock()
    moex.iter_corporate_actions.return_value = iter(["corp_event"])
    fl = FallbackDataLoader(tinkoff_md=None, tinkoff_grpc=grpc, moex_iss=moex)
    out = list(fl.iter_corporate_actions("X", date(2026, 1, 1), date(2026, 1, 31)))
    assert out == ["corp_event"]


# ---------------------------------------------------------------------------
# Issue #346 — per-source max-lookback awareness (2026-08-30, m0rtal).
#
# Regression: ``FallbackDataLoader.iter_ohlcv`` previously passed the
# script's outer start/end window straight to every source in the chain.
# ``MOEXDataLoader`` enforces a hard 1825-day cap and rejects any
# longer request with ``LoaderError: range ... exceeds upstream max
# lookback``. The supervisor invokes backfill with ``--start-year
# 2018 --end-year <current>`` (a 9-year window), so any ticker whose
# broker-gRPC fetch returned 0 bars fell through to moex_iss, which
# raised the cap error and was logged as ``ALL sources returned 0
# bars``. ~87% of those failures on .107 production were this bug.
#
# Fix contract: when a source has a known ``MAX_LOOKBACK``, the fallback
# loader must split the request into chunks no longer than that cap and
# concatenate the results. Sources without a known cap receive the full
# window as before.
# ---------------------------------------------------------------------------


def test_iter_ohlcv_splits_window_for_moex_lookback_cap() -> None:
    """9-year window that would trip MOEX's 1825d cap gets chunked.

    Regression test for issue #346. ``tinkoff_grpc`` returns 0 bars
    (simulating a broker-no-data outcome); without chunking, moex_iss
    would receive ``2018-01-01..2026-12-31`` and raise ``LoaderError``.
    With chunking, moex_iss receives two sub-ranges that each fit under
    the 1825d cap and yields their concatenated rows.
    """
    grpc = _loader_empty()
    moex = MagicMock()
    cap = MOEX_MAX_LOOKBACK.days  # 1825
    # Pre-cap chunk: 2018-01-01..start + cap_days (chunk has cap_days span).
    chunk_a_end = date(2018, 1, 1) + timedelta(days=cap)
    # Post-cap chunk: chunk_a_end+1 .. 2026-12-31
    chunk_b_start = chunk_a_end + timedelta(days=1)

    moex_rows_a = [f"a{i}" for i in range(3)]
    moex_rows_b = [f"b{i}" for i in range(2)]

    # side_effect keyed on (ticker, start, end): return appropriate rows.
    def moex_side_effect(ticker: str, start: date, end: date) -> Any:
        if end == chunk_a_end:
            return iter(moex_rows_a)
        if start == chunk_b_start:
            return iter(moex_rows_b)
        # A mid-cap chunk shouldn't be requested.
        raise AssertionError(f"Unexpected moex window: {start}..{end}")

    moex.iter_ohlcv.side_effect = moex_side_effect

    fl = FallbackDataLoader(tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_ohlcv("X", date(2018, 1, 1), date(2026, 12, 31)))

    assert out == moex_rows_a + moex_rows_b
    # grpc got the full window first (broker-first contract).
    grpc.iter_ohlcv.assert_called_once_with("X", date(2018, 1, 1), date(2026, 12, 31))
    # moex was called with two sub-windows, each within the cap.
    assert moex.iter_ohlcv.call_count == 2
    call_args = [c.args for c in moex.iter_ohlcv.call_args_list]
    assert ("X", date(2018, 1, 1), chunk_a_end) in call_args
    assert ("X", chunk_b_start, date(2026, 12, 31)) in call_args
    # Per-source stats reflect success.
    assert fl.stats["moex_iss"]["ok"] == 1


def test_iter_ohlcv_no_chunking_when_window_fits_moex_cap() -> None:
    """Window ≤ MOEX_MAX_LOOKBACK passes through unmodified.

    Regression guard for issue #346: short windows must not be split
    (would add unnecessary HTTP calls and break the call-args equality
    tests in earlier blocks).
    """
    grpc = _loader_empty()
    moex = MagicMock()
    moex.iter_ohlcv.return_value = iter(["m1", "m2"])

    fl = FallbackDataLoader(tinkoff_grpc=grpc, moex_iss=moex)

    out = list(fl.iter_ohlcv("X", date(2026, 1, 1), date(2026, 1, 31)))

    assert out == ["m1", "m2"]
    # moex called exactly once with the original window.
    moex.iter_ohlcv.assert_called_once_with("X", date(2026, 1, 1), date(2026, 1, 31))


def test_iter_ohlcv_chunked_moex_partial_failure_marks_source_failed() -> None:
    """If any chunk raises, moex is marked failed for the whole range.

    Regression test for issue #346: partial chunk success followed by
    a mid-window failure cannot be trusted — we may have a partial
    series whose coverage is not what the caller expected. The chain
    marks the source as failed, falls through, and yields no rows
    from that source. (The caller can choose to retry with a shorter
    window.)
    """
    grpc = _loader_empty()
    moex = MagicMock()

    def moex_side_effect(ticker: str, start: date, end: date) -> Any:
        # First chunk succeeds, second raises.
        if start == date(2018, 1, 1):
            return iter(["first"])
        raise LoaderError("range ... exceeds upstream max lookback 1825d")

    moex.iter_ohlcv.side_effect = moex_side_effect
    fl = FallbackDataLoader(tinkoff_grpc=grpc, moex_iss=moex)

    # No rows yielded — the partial chunk success is dropped so the
    # caller doesn't silently persist a partial series.
    out = list(fl.iter_ohlcv("X", date(2018, 1, 1), date(2026, 12, 31)))

    assert out == []
    # moex was tried for every chunk; the second attempt errored.
    assert moex.iter_ohlcv.call_count == 2
    assert fl.stats["moex_iss"]["error"] == 1
    assert fl.stats["moex_iss"]["ok"] == 0


def test_tinkoff_md_attribute_is_preserved() -> None:
    """Back-compat: ``self.tinkoff_md`` is still set on the instance for any
    caller that introspects it. The chain just doesn't consult it.
    """
    md = MagicMock()
    fl = FallbackDataLoader(tinkoff_md=md, tinkoff_grpc=MagicMock(), moex_iss=MagicMock())
    assert fl.tinkoff_md is md
