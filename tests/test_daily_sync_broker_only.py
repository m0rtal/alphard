"""Tests for the daily_sync.py broker-only contract (issue #276).

Background
----------
Service-flow contract:
- "При запуске, если данных по тикерам нет — запускается бэкфил"
- "Бэкфил имеет фоллбэк: данные из zip - данные от брокера - данные от moex"
- "После бэкфила перестаём использовать zip, переходим на данные от брокера"

The legacy ``--prefer-md-backfill`` flag on ``daily_sync.py`` re-enabled
the MD (zip) archive on the hot path, contradicting the last rule
("после бэкфила переходим на broker"). As of 2026-08-27 the flag is
hidden via ``argparse.SUPPRESS`` so daily_sync is unambiguously
broker-only after the initial backfill completes.

These tests pin the contract so a future refactor cannot silently
re-enable the MD path.
"""

from __future__ import annotations

import inspect


def test_prefer_md_backfill_argument_is_hidden() -> None:
    """``--prefer-md-backfill`` is registered but hidden in argparse help
    so operators cannot accidentally re-enable the MD archive on the
    hot path."""
    import daily_sync  # type: ignore[import-not-found]  # noqa: PLC0415

    src = inspect.getsource(daily_sync)
    # The argparse registration is still present (so legacy callers that
    # pass the flag don't error out), but its help is suppressed.
    assert '"--prefer-md-backfill"' in src or "'--prefer-md-backfill'" in src
    assert "argparse.SUPPRESS" in src


def test_prefer_md_backfill_does_not_instantiate_md_loader() -> None:
    """Even when ``--prefer-md-backfill`` is parsed, the main loop must
    NOT instantiate ``TinkoffInvestMDDataLoader`` — that import would
    re-introduce the MD archive into the hot path and break the
    post-backfill broker-only contract."""
    import daily_sync  # type: ignore[import-not-found]  # noqa: PLC0415

    src = inspect.getsource(daily_sync)
    # The lazy loader block (md_loader = TinkoffInvestMDDataLoader())
    # and the per-ticker MD pull (md_loader.iter_ohlcv) must be gone.
    assert "md_loader = TinkoffInvestMDDataLoader" not in src
    assert "md_loader.iter_ohlcv" not in src


def test_daily_sync_done_log_mentions_broker_only() -> None:
    """The final log line should announce "broker-only" so an operator
    can verify the MD archive is out of the hot path at a glance."""
    import daily_sync  # type: ignore[import-not-found]  # noqa: PLC0415

    src = inspect.getsource(daily_sync)
    assert "broker-only" in src
    # The legacy md_archive_used counter must be gone too.
    assert "md_archive_used=" not in src


def test_daily_sync_default_source_is_tkf() -> None:
    """The default source remains "tkf" (broker gRPC). MOEX ISS stays
    available via ``--source moex`` for legacy operators but is not the
    default — the broker path is the canonical post-backfill contract."""
    import daily_sync  # type: ignore[import-not-found]  # noqa: PLC0415

    src = inspect.getsource(daily_sync)
    assert '"tkf"' in src
    # The default choice is "tkf" (Tinkoff gRPC).
    assert 'default="tkf"' in src


def test_daily_sync_source_branch_uses_broker_or_moex_only() -> None:
    """The per-ticker dispatch is a 2-branch dispatch on
    ``args.source``: broker gRPC (Tinkoff) or MOEX ISS. There must be no
    third "MD archive" branch."""
    import daily_sync  # type: ignore[import-not-found]  # noqa: PLC0415

    src = inspect.getsource(daily_sync)
    # The two branches: tkf uses loader.fetch_ohlcv (broker gRPC stream),
    # moex uses loader.iter_ohlcv (MOEX ISS REST). No third path.
    assert 'args.source == "tkf"' in src
    assert "loader.fetch_ohlcv" in src
    assert "loader.iter_ohlcv" in src


def test_daily_sync_main_loop_skips_md_path_in_incremental_run() -> None:
    """The legacy ``--prefer-md-backfill`` block (lazy md_loader init +
    per-ticker md pull + md_used_count log) must be fully absent from
    the main loop. This is the structural test: a future refactor that
    re-adds the MD path will trip the assertion below."""
    import daily_sync  # type: ignore[import-not-found]  # noqa: PLC0415

    src = inspect.getsource(daily_sync)
    # All three legacy artefacts must be gone:
    # 1. Lazy MD loader init
    assert "md_loader = TinkoffInvestMDDataLoader" not in src
    # 2. Per-ticker MD pull
    assert "md_loader.iter_ohlcv" not in src
    # 3. md_used_count tracking
    assert "md_used_count" not in src
    # 4. The legacy guard that wrapped everything
    assert "if md_loader is not None" not in src
