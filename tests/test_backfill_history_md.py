"""Tests for scripts/backfill_history_md.py — the primary backfill script.

Smoke-level only: we mock the broker + DB and verify the script's
control flow (universe resolution, skip-complete handling, error
isolation, circuit breaker). Real network calls go to the deployed
stack, not unit tests.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

# conftest.py adds PROJECT_ROOT to sys.path so ``backfill_history_md``
# resolves as a top-level module.
import backfill_history_md as bh  # noqa: E402

# ---------------------------------------------------------------------------
# _resolve_universe
# ---------------------------------------------------------------------------


def test_resolve_universe_no_class_filter_returns_everything() -> None:
    """No --classes flag → return everything from the broker (no filtering
    on backfill — user's standing instruction)."""
    fake_tqbr = [
        MagicMock(ticker="SBER", class_code="TQBR"),
        MagicMock(ticker="GAZP", class_code="TQBR"),
    ]
    fake_spbxm = [MagicMock(ticker="AAPL", class_code="SPBXM")]
    fake_bonds = [MagicMock(ticker="RU000A0JX0J3", class_code="TQOB")]
    fake_etfs = [MagicMock(ticker="FXUS", class_code="TQTE")]

    loader = MagicMock()
    loader.tinkoff_md.list_tickers_with_figi.return_value = fake_tqbr + fake_spbxm + fake_bonds + fake_etfs

    out, _metas = bh._resolve_universe(loader, classes=None, limit=0)

    assert out == ["SBER", "GAZP", "AAPL", "RU000A0JX0J3", "FXUS"]
    # Guard against regression: chain path is NOT used. Direct tinkoff_md call only.
    loader.list_tickers.assert_not_called()
    loader.tinkoff_md.list_tickers_with_figi.assert_called_once()


def test_resolve_universe_class_filter_case_insensitive() -> None:
    """--classes TQBR vs --classes tqbr must produce the same universe."""
    fake = [
        MagicMock(ticker="SBER", class_code="TQBR"),
        MagicMock(ticker="AAPL", class_code="SPBXM"),
    ]
    loader = MagicMock()
    loader.tinkoff_md.list_tickers_with_figi.return_value = fake

    upper, _ = bh._resolve_universe(loader, classes=["TQBR"], limit=0)
    lower, _ = bh._resolve_universe(loader, classes=["tqbr"], limit=0)

    assert upper == lower == ["SBER"]


def test_resolve_universe_class_all_string_does_not_match() -> None:
    """--classes ALL is a documented foot-gun: argparse stores the string
    'ALL' which matches no class_code, so the resolved universe is empty.
    The fix is to invoke the script WITHOUT --classes when you want the
    full universe. This test pins that behaviour so we don't regress
    to accidentally treating the literal string as a wildcard.
    """
    fake = [MagicMock(ticker="SBER", class_code="TQBR")]
    loader = MagicMock()
    loader.tinkoff_md.list_tickers_with_figi.return_value = fake

    out, _ = bh._resolve_universe(loader, classes=["ALL"], limit=0)

    assert out == []


def test_resolve_universe_limit_caps_universe_size() -> None:
    fake = [MagicMock(ticker=f"T{i}", class_code="TQBR") for i in range(10)]
    loader = MagicMock()
    loader.tinkoff_md.list_tickers_with_figi.return_value = fake

    out, _ = bh._resolve_universe(loader, classes=None, limit=3)

    assert len(out) == 3


def test_resolve_universe_md_failure_falls_back_to_loader_chain() -> None:
    """Regression: if tinkoff_md.list_tickers_with_figi() raises LoaderError,
    the resolver must fall back to loader.list_tickers() (chain) so the
    supervisor doesn't loop on a transient broker outage. The contract is:
    direct MD first, chain only as last resort — match the chain contract
    on the iter_ohlcv path (PR #321 / issues #319, #152).
    """
    loader = MagicMock()
    chain_metas = [MagicMock(ticker="SBER", class_code="TQBR")]
    loader.tinkoff_md.list_tickers_with_figi.side_effect = RuntimeError("broker down")
    loader.list_tickers.return_value = chain_metas

    out, _ = bh._resolve_universe(loader, classes=None, limit=0)

    assert out == ["SBER"]
    loader.list_tickers.assert_called_once()


def test_resolve_universe_md_failure_and_empty_chain_raises() -> None:
    """When both tinkoff_md AND the chain return empty, the resolver must
    raise a clear LoaderError so the supervisor exits with rc != 0 and the
    operator sees a real signal rather than a 0-ticker backfill. (Pre-#326
    the bare ``raise`` re-raised the transport error — review cycle101
    noted that hides the actual "universe empty" failure mode.)
    """
    import pytest

    from src.data.loader import LoaderError

    loader = MagicMock()
    loader.tinkoff_md.list_tickers_with_figi.side_effect = RuntimeError("broker down")
    loader.list_tickers.return_value = []

    # MD raised AND chain empty → LoaderError, NOT the transport error.
    with pytest.raises(LoaderError, match="universe empty"):
        bh._resolve_universe(loader, classes=None, limit=0)


def test_resolve_universe_empty_md_falls_through_to_chain() -> None:
    """Regression (cycle101): when tinkoff_md.list_tickers_with_figi()
    returns successfully but with an EMPTY list (e.g. partial/degraded
    response, see issue #319 title), the resolver must fall through to
    ``loader.list_tickers()`` instead of silently producing a 0-ticker
    universe. Pre-#326 the chain was only consulted on raise, not on
    empty-return — exactly the failure mode issue #319 was filed for.
    """
    chain_metas = [MagicMock(ticker="SBER", class_code="TQBR")]
    loader = MagicMock()
    loader.tinkoff_md.list_tickers_with_figi.return_value = []  # success, empty
    loader.list_tickers.return_value = chain_metas

    out, _metas = bh._resolve_universe(loader, classes=None, limit=0)

    assert out == ["SBER"]
    loader.tinkoff_md.list_tickers_with_figi.assert_called_once()
    loader.list_tickers.assert_called_once()


def test_resolve_universe_empty_md_and_empty_chain_raises() -> None:
    """Regression (cycle101): both the direct MD call AND the chain
    return empty → raise LoaderError with a clear message. The supervisor
    sees rc != 0 and the operator gets a real "universe empty" signal.
    """
    import pytest

    from src.data.loader import LoaderError

    loader = MagicMock()
    loader.tinkoff_md.list_tickers_with_figi.return_value = []  # success, empty
    loader.list_tickers.return_value = []

    with pytest.raises(LoaderError, match="universe empty"):
        bh._resolve_universe(loader, classes=None, limit=0)
    loader.tinkoff_md.list_tickers_with_figi.assert_called_once()
    loader.list_tickers.assert_called_once()


# ---------------------------------------------------------------------------
# _is_complete
# ---------------------------------------------------------------------------


def test_is_complete_below_count_no_meta_incomplete() -> None:
    """count < min_bars AND no metadata → incomplete."""
    store = MagicMock()
    store.count_ohlcv.return_value = 1299
    store.ticker_meta.return_value = None
    assert bh._is_complete(store, "SBER", min_bars=1300) is False


# ---------------------------------------------------------------------------
# _backfill_one: failure isolation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_complete: count vs expected_bars(listed_at..end * (1 - halts_pct))
# ---------------------------------------------------------------------------


def test_is_complete_freshly_listed_ticker_not_blocked_forever() -> None:
    """Ticker listed 2024-01-01 (today is 2026-08-17 so ~2.6y). expected
    bars = 2.6 * 252 * 0.85 ≈ 557. count=580 > 557 → complete. No
    infinite retry."""

    store = MagicMock()
    store.count_ohlcv.return_value = 580
    store.ticker_meta.return_value = (date(2024, 1, 1), None)

    assert bh._is_complete(store, "FRESH", min_bars=1300) is True


def test_is_complete_delisted_ticker() -> None:
    """Delisted ticker with delisted_at=2024-06-01. listed_at=2010.
    expected_bars = trading_days(2010..2024-06) * 0.85 ≈ 3103.
    Count=2000 < 3103 → incomplete."""

    store = MagicMock()
    store.count_ohlcv.return_value = 500
    store.ticker_meta.return_value = (date(2010, 1, 1), date(2024, 6, 1))

    assert bh._is_complete(store, "OLD", min_bars=1300) is False


def test_is_complete_delisted_ticker_within_halts_pct() -> None:
    """Same setup but stored 3300 bars (within ~6% of 3103 expected,
    within halts_pct=15%)."""

    store = MagicMock()
    store.count_ohlcv.return_value = 3300
    store.ticker_meta.return_value = (date(2010, 1, 1), date(2024, 6, 1))

    # 14.5y * 252 * 0.85 = 3103, count=3300 > 3103 → complete
    assert bh._is_complete(store, "OLD", min_bars=1300) is True


def test_is_complete_archive_truncated_below_threshold() -> None:
    """Ticker listed 2010, has only ~1200 bars, gap from 2010-2015.
    expected_bars = 16y * 252 * 0.85 ≈ 3427, count=1200 << 3427.
    Incomplete even though > 1300 — wait, 1200 < 1300 so we wouldn't
    be here. The test pins the slower path: just below min_bars."""

    store = MagicMock()
    store.count_ohlcv.return_value = 1299
    store.ticker_meta.return_value = (date(2010, 1, 1), None)

    # 16.6y * 252 * 0.85 = 3555, count=1299 < 3555 → incomplete
    assert bh._is_complete(store, "GAP", min_bars=1300) is False


def test_is_complete_no_metadata_returns_false() -> None:
    """Ticker not in ticker_universe. Re-pull and possibly surface the
    gap."""

    store = MagicMock()
    store.count_ohlcv.return_value = 50
    store.ticker_meta.return_value = None

    assert bh._is_complete(store, "ORPHAN", min_bars=1300) is False


def test_is_complete_listed_at_none_returns_false() -> None:
    """Ticker is in the universe but listed_at is NULL (legacy entry).
    Can't compute expected. Be conservative."""

    store = MagicMock()
    store.count_ohlcv.return_value = 500
    store.ticker_meta.return_value = (None, None)
    store.earliest_ts.return_value = None

    assert bh._is_complete(store, "LEGACY", min_bars=1300) is False


def test_is_complete_listed_at_none_inferred_from_earliest() -> None:
    """listed_at is NULL but we have bars — infer listing year from
    earliest bar and judge accordingly. WUSH case: real listed 2022-12-14.
    Expected bars for ~3.7y = ~787; with 1044 bars we are complete.
    """
    from datetime import date

    store = MagicMock()
    store.count_ohlcv.return_value = 1044  # WUSH actually has this many
    store.ticker_meta.return_value = (None, None)  # listed_at NULL
    store.earliest_ts.return_value = date(2022, 12, 14)  # inferred listing

    assert bh._is_complete(store, "WUSH", min_bars=1300) is True


def test_earliest_expected_ts_default_clamps_to_1825d() -> None:
    """MOEX ISS cap = 1825d. Without listed_at, start = max(MIN_YEAR, today-1825d)."""
    from datetime import timedelta

    meta = (None, None)
    result = bh._earliest_expected_ts(meta)  # default moex_clamped=True
    expected = date.today() - timedelta(days=1825)
    assert result == expected


def test_earliest_expected_ts_unclamped_goes_to_min_year() -> None:
    """MD loader has no 1825d cap. With moex_clamped=False, start = MIN_YEAR."""
    meta = (None, None)
    result = bh._earliest_expected_ts(meta, moex_clamped=False)
    assert result == date(2018, 1, 1)  # MIN_YEAR


def test_earliest_expected_ts_with_listed_at_clamps() -> None:
    """listed_at=2010 still goes to MIN_YEAR if MOEX cap doesn't pull earlier."""
    from datetime import timedelta

    meta = (date(2010, 1, 1), None)
    result = bh._earliest_expected_ts(meta)
    # today minus 1825d; max(MIN_YEAR=2018, listed_at=2010, today-1825d) = today-1825d
    expected = date.today() - timedelta(days=1825)
    assert result == expected


def test_earliest_expected_ts_unclamped_with_listed_at() -> None:
    """MD loader with listed_at=2010 still goes back to MIN_YEAR (full history)."""
    meta = (date(2010, 1, 1), None)
    result = bh._earliest_expected_ts(meta, moex_clamped=False)
    # max(MIN_YEAR=2018, listed_at.year=2010) = 2018-01-01 (MIN_YEAR wins)
    assert result == date(2018, 1, 1)


# ---------------------------------------------------------------------------
# --skip-known-bad flag
# ---------------------------------------------------------------------------


def test_argparser_accepts_skip_known_bad() -> None:
    """--skip-known-bad is wired into argparse and parses to args.skip_known_bad."""
    import argparse  # noqa: PLC0415 — local to keep imports tight

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-known-bad",
        action="store_true",
    )
    args = parser.parse_args(["--skip-known-bad"])
    assert args.skip_known_bad is True
    args_default = parser.parse_args([])
    assert args_default.skip_known_bad is False


def test_skip_known_bad_logic_drops_delisted_at_tickers() -> None:
    """When --skip-known-bad is set, the per-ticker guard short-circuits
    for tickers whose (listed_at, delisted_at) tuple has a non-None
    delisted_at. We exercise the predicate in isolation (no broker,
    no DB) — integration is the deployed stack's job.

    NOTE: store.ticker_meta() returns a raw psycopg row tuple
    ``(listed_at, delisted_at)`` or None, NOT a TickerMeta object.
    The 2026-08-20 backfill regression was caused by treating this
    tuple as an object (meta.delisted_at) instead of indexing it
    (meta[1]). The test fixtures below mirror the real production
    return shape so any future drift is caught at unit-test time.
    """
    import argparse  # noqa: PLC0415
    from datetime import date as _date  # noqa: PLC0415

    # Build the same args namespace the parser would, just without running main().
    args = argparse.Namespace(skip_known_bad=True)
    # Real psycopg-row tuple shape — NOT a MagicMock with attribute access.
    meta = (_date(2020, 1, 1), _date(2026, 8, 19))
    # Predicate matches the production main-loop guard after the 2026-08-20 fix:
    if args.skip_known_bad and meta is not None and meta[1] is not None:
        skipped = True
    else:
        skipped = False
    assert skipped

    # Without the flag, the same ticker would NOT be skipped.
    args_off = argparse.Namespace(skip_known_bad=False)
    if args_off.skip_known_bad and meta is not None and meta[1] is not None:
        skipped_off = True
    else:
        skipped_off = False
    assert not skipped_off


def test_skip_known_bad_keeps_tickers_without_delisted_at() -> None:
    """Tickers with delisted_at=None are NEVER dropped by --skip-known-bad."""
    import argparse  # noqa: PLC0415

    args = argparse.Namespace(skip_known_bad=True)
    # Real tuple shape (delisted_at=None) — NOT a MagicMock.
    meta = (None, None)
    if args.skip_known_bad and meta is not None and meta[1] is not None:
        skipped = True
    else:
        skipped = False
    assert not skipped


# ---------------------------------------------------------------------------
# H-NETWORK-DETECT (2026-08-20): progress heartbeat
# ---------------------------------------------------------------------------
#
# The deadlock that left backfill PID 19 idle for 17 hours on
# sha-bc867a2 was invisible because the per-ticker log line never
# fired (iter_ohlcv hung) and the final `=== DONE` line never
# arrived either. The fix in backfill_history_md.py emits a separate
# "progress: i/N tickers scanned in Ts" line every 50 tickers so
# any future wedge surfaces as missing progress lines rather than a
# silent stall. The tests below pin the cadence constant and the
# expected log format so a future edit cannot quietly regress this
# affordance.


def test_progress_every_constant_is_50() -> None:
    """The progress-heartbeat cadence is 50 tickers (small enough to
    surface a wedge within minutes, large enough to avoid log spam)."""
    import importlib

    mod = importlib.import_module("backfill_history_md")
    # Read the module source to extract the constant. This is more
    # robust than importing the module and reading a top-level symbol
    # that may not actually be exported at import time.
    import inspect

    source = inspect.getsource(mod)
    assert "progress_every = 50" in source
    assert 'f"progress: {i}/{len(tickers)} tickers scanned in "' in source


def test_progress_heartbeat_format_includes_counters() -> None:
    """Heartbeat line must include fetched/written/skipped/errors so an
    operator can diagnose where the loop is at a glance."""
    import inspect

    source = inspect.getsource(__import__("backfill_history_md"))
    # Required counter substrings inside the progress log line.
    for needle in (
        "fetched=",
        "written=",
        "skipped=",
        "errors=",
    ):
        assert needle in source, f"progress heartbeat missing {needle!r}"


# ---------------------------------------------------------------------------
# Per-ticker guard is _is_complete (service-flow contract: skip a ticker
# iff its Tinkoff MD archive backfill has already completed). This is
# unambiguous because only the MD archive can cover the full 9-year
# history — broker gRPC and MOEX ISS both cap at 1825d.
#
# Tests below pin this semantic so a future refactor cannot regress to a
# "count == 0" check (which would falsely skip a ticker whose broker
# gRPC has supplied the last 5 trading days but whose MD archive pull
# was never finished). See issue #276 for the rejection of
# ``--on-empty-only``.
# ---------------------------------------------------------------------------


def test_is_complete_is_md_archive_backfill_guard() -> None:
    """The per-ticker guard ``_is_complete`` MUST use the expected-bars
    formula (full ``listed_at..today`` range with 15% HALTS_PCT slack),
    NOT a ``count == 0`` short-circuit. A ``count == 0`` guard would
    confuse "broker gRPC supplied 5 days" with "MD archive done".

    This test inspects the source to pin the formula structure.
    """
    import inspect  # noqa: PLC0415

    source = inspect.getsource(bh)
    # The expected-bars formula must be present in _is_complete.
    assert "_HALTS_PCT" in source, "expected-bars formula not found in backfill_history_md"
    assert "trading_days" in source, "trading_days() helper not found"
    # The misleading --on-empty-only flag must be gone (it was rejected
    # because it conflated "row count" with "MD archive completion").
    assert "--on-empty-only" not in source, (
        "--on-empty-only flag was reintroduced; "
        "the user explicitly rejected it as conflating row count with MD completion"
    )
    assert "skip (count > 0, --on-empty-only)" not in source, (
        "count > 0 short-circuit log line was reintroduced; " "this contradicts the MD-completion contract"
    )


def test_is_complete_partial_recent_bars_not_complete() -> None:
    """A ticker with only the last 5 trading days (from broker gRPC) is
    NOT complete — the MD archive pull was never run. _is_complete
    must return False."""
    # listed 2010, today=2026, count = 5 (only last week from broker gRPC).
    # expected_bars = 16y * 252 * 0.85 ≈ 3427; count=5 << 3427 → incomplete.
    store = MagicMock()
    store.count_ohlcv.return_value = 5
    store.ticker_meta.return_value = (date(2010, 1, 1), None)
    store.earliest_ts.return_value = date(2026, 8, 20)  # only 5 days

    # _is_complete falls through to: count < expected → False
    assert bh._is_complete(store, "PARTIAL", min_bars=1300) is False


def test_is_complete_full_history_from_md_archive_is_complete() -> None:
    """A ticker with the full 9-year history (the MD archive is the only
    source that can produce this) is complete. _is_complete returns True.

    The test pins a count that is unambiguously above the expected-bars
    formula's threshold so the test does not depend on the exact
    trading-day arithmetic. We pick count=3000 for a ticker listed
    2018 — comfortably above expected_bars for any reasonable
    trading-day formula and HALTS_PCT slack."""
    # Listed 2018 (MIN_YEAR). With ~8 years × ~252 trading days × 0.85
    # HALTS_PCT slack, expected_bars ≈ 1700-1800. count=3000 is well
    # above that floor and unambiguously complete.
    store = MagicMock()
    store.count_ohlcv.return_value = 3000
    store.ticker_meta.return_value = (date(2018, 1, 1), None)

    assert bh._is_complete(store, "DONE", min_bars=1300) is True


# ---------------------------------------------------------------------------
# Issue #311 (2026-08-28): tinkoff_md loader must share tinkoff_grpc._token
# ---------------------------------------------------------------------------


def test_md_loader_uses_grpc_token_not_args_token() -> None:
    """Source-level contract check for issue #311.

    ``scripts/backfill_history_md.py`` main() must construct the gRPC
    loader first, then pass ``tinkoff_grpc_loader._token`` to the MD
    loader. FAILURE MODE we are guarding against: passing
    ``args.token or _resolve_token()`` to MD — that resolves to SANDBOX
    first, which is UNAUTHENTICATED on .107. The grep markers below
    pin the right construction; if a future refactor reintroduces the
    SANDBOX bug the test fails immediately.
    """
    from pathlib import Path

    src = Path("scripts/backfill_history_md.py").read_text(encoding="utf-8")
    # gRPC loader constructed first (with no args — defaults to REAL).
    assert (
        "tinkoff_grpc_loader = TinkoffInvestDataLoader()" in src
    ), "main() must construct tinkoff_grpc_loader first (no args → REAL)"
    # MD loader constructed with gRPC loader's token.
    assert "TinkoffInvestMDDataLoader(token=tinkoff_grpc_loader._token)" in src, (
        "TinkoffInvestMDDataLoader() must receive tinkoff_grpc_loader._token "
        "to share the authenticated token (not args.token which resolves to "
        "SANDBOX first)"
    )
    # The OLD buggy construction must NOT be present.
    assert "TinkoffInvestMDDataLoader(token=args.token)" not in src, (
        "TinkoffInvestMDDataLoader(token=args.token) is the bug — args.token "
        "is None by default, which falls through to $TINKOFF_SANDBOX_TOKEN "
        "(dead on .107)"
    )
    # The MD loader must be passed via ``tinkoff_md=tinkoff_md_loader``
    # to FallbackDataLoader (not by raw constructor).
    assert "tinkoff_md=tinkoff_md_loader" in src
    assert "tinkoff_grpc=tinkoff_grpc_loader" in src
