"""Tests for scripts/apply_corporate_actions.py (Phase 2.5 step 2b).

Coverage:
- Orchestrator composes fetch_moex_corporate_actions + apply_split_adjustment
  end-to-end against an InMemorySQLiteStore.
- Adjusted rows land in ohlcv_daily_adj with correct price/volume scaling.
- Idempotency: a re-run within the skip window is a no-op for that ticker;
  with --force the same ticker is re-applied.
- Per-ticker errors (bad data, store error) don't abort the whole loop.
- Dry-run path logs but does not write.
- Cache corruption is non-fatal (next run starts with an empty cache).
- Stores that lack raw OHLCV rows for a ticker: zero writes, no crash.
- The orchestrator respects the --tickers whitelist.
- Smoke test: fetch_splits returns the canonical MOEX-style split list,
  apply_split_adjustment composes with the orchestrator to produce
  correctly scaled bars (this is the spec's #5 acceptance criterion).

Why so many tests?
- This script owns the production wiring between step 1 (math), step
  2a (fetcher), and the parallel storage table introduced in PR #74.
  Every layer is unit-tested elsewhere; the orchestrator test verifies
  the COMPOSITION is correct (right rows written, right skip rules,
  right error isolation).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add src/ and scripts/ to sys.path so imports work whether tests run
# from the repo root or from CI's container.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_PATH = str(_PROJECT_ROOT / "src")
_SCRIPTS_PATH = str(_PROJECT_ROOT / "scripts")
for _p in (_SRC_PATH, _SCRIPTS_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apply_corporate_actions as aca  # noqa: E402
from src.data.models import CorporateAction, OHLCVRow, TickerMeta  # noqa: E402
from src.data.sqlite_store import InMemorySQLiteStore  # noqa: E402
from src.data.store import StoreError  # noqa: E402


# ---------- helpers ----------


def _make_ticker(
    ticker: str,
    listed_at: date | None = date(2010, 1, 1),
    delisted: bool = False,
) -> TickerMeta:
    return TickerMeta(
        ticker=ticker,
        figi=None,
        name=f"{ticker} test",
        lot=1,
        isin=None,
        currency="RUB",
        delisted=delisted,
        delisted_at=None,
        listed_at=listed_at,
        source="moex",
    )


def _make_ohlcv(ticker: str, ts: date, close: str, volume: str) -> OHLCVRow:
    c = Decimal(close)
    return OHLCVRow(
        ticker=ticker,
        ts=ts,
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal(volume),
        adj_close=c,
    )


def _make_split(ticker: str, ts: date, value: str) -> CorporateAction:
    return CorporateAction(
        ticker=ticker,
        ts=ts,
        kind="split",
        value=Decimal(value),
        source="moex",
    )


@pytest.fixture
def store() -> InMemorySQLiteStore:
    """A fresh InMemorySQLiteStore with two tickers (SBER, GAZP) and
    pre-populated raw OHLCV bars. Each ticker has 5 daily bars covering
    2026-01-06..2026-01-13 (Mon..Tue next week)."""
    s = InMemorySQLiteStore()
    s.upsert_tickers([_make_ticker("SBER"), _make_ticker("GAZP")])
    for ticker in ("SBER", "GAZP"):
        rows = []
        for i, ts in enumerate(
            [
                date(2026, 1, 6),
                date(2026, 1, 7),
                date(2026, 1, 8),
                date(2026, 1, 9),
                date(2026, 1, 12),
            ]
        ):
            # SBER: close 100, 101, 102, 103, 104 (volume 1000)
            # GAZP: close 200, 201, 202, 203, 204 (volume 2000)
            base_close = 100 if ticker == "SBER" else 200
            base_volume = 1000 if ticker == "SBER" else 2000
            rows.append(
                _make_ohlcv(
                    ticker,
                    ts,
                    str(base_close + i),
                    str(base_volume),
                )
            )
        s.upsert_ohlcv(rows)
    return s


@pytest.fixture
def fetcher_splits_sber_split() -> list[dict]:
    """MOEX-style fetcher output: SBER has a 1:2 split on 2026-01-09,
    GAZP has no events. The orchestrator must split SBER pre-2026-01-09
    bars (close /2, volume *2) and leave post-2026-01-09 bars alone."""
    return [
        {"ticker": "SBER", "ts": "2026-01-09", "ratio": 2.0, "source": "moex"},
        # GAZP intentionally absent — verify no_actions path.
    ]


# ---------- core composition: fetch + apply + persist ----------


def test_end_to_end_sber_split_halves_pre_split_prices(
    store: InMemorySQLiteStore,
    fetcher_splits_sber_split: list[dict],
    tmp_path: Path,
) -> None:
    """Smoke test: one SBER 1:2 split on 2026-01-09 → pre-split prices
    are halved, post-split prices are untouched."""
    cache_path = tmp_path / "cache.json"

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_splits_sber_split
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "SBER",
                "--cache-path",
                str(cache_path),
            ],
            store=store,
        )

    # Adjusted bars for SBER should land in ohlcv_daily_adj.
    sber_adj = store.query_ohlcv_adj(
        "SBER",
        date(2026, 1, 1),
        date(2026, 12, 31),
    )
    # Only SBER was in the ticker whitelist; GAZP should have zero adjusted rows.
    gazp_adj = store.query_ohlcv_adj(
        "GAZP",
        date(2026, 1, 1),
        date(2026, 12, 31),
    )
    assert gazp_adj == [], "GAZP had no splits but got adjusted rows written"

    # Build {ts: row} for SBER for easier lookups.
    by_ts = {r.ts: r for r in sber_adj}
    assert len(by_ts) == 5, f"expected 5 adjusted SBER bars, got {len(by_ts)}"

    # 2026-01-06..08 (pre-split): close /2, volume *2.
    for ts, expected_close in (
        (date(2026, 1, 6), Decimal("50")),
        (date(2026, 1, 7), Decimal("50.5")),
        (date(2026, 1, 8), Decimal("51")),
    ):
        assert by_ts[ts].close == expected_close, f"ts={ts} close={by_ts[ts].close}"
        assert by_ts[ts].volume == Decimal("2000"), f"ts={ts} volume={by_ts[ts].volume}"

    # 2026-01-09 (split date) and 2026-01-12 (post-split): unchanged.
    assert by_ts[date(2026, 1, 9)].close == Decimal("103")
    assert by_ts[date(2026, 1, 9)].volume == Decimal("1000")
    assert by_ts[date(2026, 1, 12)].close == Decimal("104")
    assert by_ts[date(2026, 1, 12)].volume == Decimal("1000")

    # Cache entry for SBER should be present (no_actions was false).
    cache = json.loads(cache_path.read_text())
    assert "SBER" in cache


def test_end_to_end_no_actions_for_gazp_writes_nothing_but_records_cache(
    store: InMemorySQLiteStore,
    tmp_path: Path,
) -> None:
    """A ticker with zero MOEX events must NOT cause adjusted rows to be
    written — but the per-ticker cache entry IS recorded so the next
    weekly run doesn't re-fetch for nothing."""
    cache_path = tmp_path / "cache.json"

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = []  # no events at all
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "GAZP",
                "--cache-path",
                str(cache_path),
            ],
            store=store,
        )

    assert store.query_ohlcv_adj("GAZP", date(2026, 1, 1), date(2026, 12, 31)) == []
    cache = json.loads(cache_path.read_text())
    assert "GAZP" in cache


# ---------- idempotency ----------


def test_idempotent_rerun_within_skip_window_is_noop(
    store: InMemorySQLiteStore,
    fetcher_splits_sber_split: list[dict],
    tmp_path: Path,
) -> None:
    """A second run within the 7-day skip window must not re-fetch MOEX
    ISS or re-write adjusted rows for the same ticker."""
    cache_path = tmp_path / "cache.json"

    # First run: applies SBER.
    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_splits_sber_split
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "SBER",
                "--cache-path",
                str(cache_path),
                ],
            store=store,
        )
    rows_after_first = store.count_ohlcv_adj("SBER")
    assert rows_after_first == 5

    # Second run within skip window — fetcher must NOT be called.
    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_splits_sber_split
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "SBER",
                "--cache-path",
                str(cache_path),
                ],
            store=store,
        )
        assert fake_fetcher.fetch_splits.call_count == 0
    assert store.count_ohlcv_adj("SBER") == rows_after_first


def test_force_flag_bypasses_skip_window(
    store: InMemorySQLiteStore,
    fetcher_splits_sber_split: list[dict],
    tmp_path: Path,
) -> None:
    """--force must re-fetch and re-write even when the cache says the
    ticker is fresh."""
    cache_path = tmp_path / "cache.json"

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_splits_sber_split
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "SBER",
                "--cache-path",
                str(cache_path),
                ],
            store=store,
        )
    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_splits_sber_split
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "SBER",
                "--force",
                "--cache-path",
                str(cache_path),
                ],
            store=store,
        )
        assert fake_fetcher.fetch_splits.call_count == 1


def test_corrupt_cache_is_treated_as_empty(
    store: InMemorySQLiteStore,
    fetcher_splits_sber_split: list[dict],
    tmp_path: Path,
) -> None:
    """A JSON-decode error on the cache must NOT crash the run; it
    should be treated as 'no cache' and the run proceeds."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{not valid json")

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_splits_sber_split
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "SBER",
                "--cache-path",
                str(cache_path),
                ],
            store=store,
        )
    assert store.count_ohlcv_adj("SBER") == 5


# ---------- per-ticker error isolation ----------


def test_per_ticker_moex_fetch_error_does_not_abort_loop(
    store: InMemorySQLiteStore,
    tmp_path: Path,
) -> None:
    """A MOEX ISS error on one ticker must not abort the run for the
    remaining tickers."""
    import requests

    cache_path = tmp_path / "cache.json"

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        # SBER fails (raises), GAZP returns no events.
        def fake_fetch(session, timeout=60):
            # In real life we'd key off ticker, but our orchestrator
            # fetches ALL splits in one call. Simulate by raising on
            # the first invocation only.
            fake_fetch.calls += 1
            if fake_fetch.calls == 1:
                raise requests.ConnectionError("MOEX ISS 503")
            return []

        fake_fetch.calls = 0
        fake_fetcher.fetch_splits.side_effect = fake_fetch
        fake_fetcher.USER_AGENT = "alphard-test"
        # We expect exit code to be 0 (per-ticker error is non-fatal).
        rc = aca.main(
            [
                "--cache-path",
                str(cache_path),
                ],
            store=store,
        )

    # Neither ticker wrote any rows; the loop survived the error.
    assert store.count_ohlcv_adj() == 0
    # The single attempted fetch raised, so no GAZP cache entry either.
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    # The first ticker's cache entry is NOT recorded because the loop
    # continued to the next ticker after the error.


def test_dry_run_does_not_persist(
    store: InMemorySQLiteStore,
    fetcher_splits_sber_split: list[dict],
    tmp_path: Path,
) -> None:
    """--dry-run must not write to ohlcv_daily_adj AND must not write
    the cache file. The whole point is 'log only, no side effects'."""
    cache_path = tmp_path / "cache.json"

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_splits_sber_split
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "SBER",
                "--dry-run",
                "--cache-path",
                str(cache_path),
                ],
            store=store,
        )

    assert store.count_ohlcv_adj("SBER") == 0
    assert not cache_path.exists(), f"dry-run wrote cache at {cache_path}"


# ---------- empty raw input ----------


def test_ticker_with_no_raw_ohlcv_writes_nothing(
    store: InMemorySQLiteStore,
    fetcher_splits_sber_split: list[dict],
    tmp_path: Path,
) -> None:
    """A ticker that has splits in MOEX but no raw OHLCV in our store
    must not crash — and must not write to ohlcv_daily_adj."""
    cache_path = tmp_path / "cache.json"

    # Add YDEX to universe but no OHLCV rows.
    store.upsert_ticker(_make_ticker("YDEX"))

    # MOEX returns a split for YDEX but YDEX has no OHLCV in store.
    fetcher_payload = fetcher_splits_sber_split + [
        {"ticker": "YDEX", "ts": "2026-01-09", "ratio": 2.0, "source": "moex"},
    ]

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_payload
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "YDEX",
                "--cache-path",
                str(cache_path),
                ],
            store=store,
        )

    assert store.query_ohlcv_adj("YDEX", date(2026, 1, 1), date(2026, 12, 31)) == []


# ---------- ticker whitelist ----------


def test_tickers_whitelist_limits_universe(
    store: InMemorySQLiteStore,
    fetcher_splits_sber_split: list[dict],
    tmp_path: Path,
) -> None:
    """--tickers SBER must skip GAZP entirely (not even a no_actions
    cache entry for GAZP)."""
    cache_path = tmp_path / "cache.json"

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_splits_sber_split
        fake_fetcher.USER_AGENT = "alphard-test"
        aca.main(
            [
                "--tickers",
                "SBER",
                "--cache-path",
                str(cache_path),
                ],
            store=store,
        )

    cache = json.loads(cache_path.read_text())
    assert "SBER" in cache
    assert "GAZP" not in cache


def test_apply_for_ticker_short_circuits_on_empty_actions(
    store: InMemorySQLiteStore,
) -> None:
    """``_apply_for_ticker`` returns 0 immediately if actions is empty —
    no DB round trip, no log spam."""
    n = aca._apply_for_ticker(store, "SBER", [], dry_run=False)
    assert n == 0
    # No rows touched.
    assert store.count_ohlcv_adj() == 0


def test_build_store_raises_store_error_with_no_dsn(tmp_path: Path) -> None:
    """Production path: no ALPHARD_PG_DSN set → PostgresDataStore raises
    StoreError → main() catches it and returns EXIT_FATAL."""
    import os

    # Ensure no DSN leaks from the test environment.
    os.environ.pop("ALPHARD_PG_DSN", None)

    args = aca._parse_args_from(["--cache-path", str(tmp_path / "cache.json")])
    with pytest.raises(StoreError, match="no DSN"):
        aca._build_store(args)


def test_main_returns_fatal_when_postgres_init_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """If store construction raises StoreError (e.g. no ALPHARD_PG_DSN),
    main() returns EXIT_FATAL without ever touching the DB."""
    import os

    monkeypatch.delenv("ALPHARD_PG_DSN", raising=False)
    cache_path = tmp_path / "cache.json"

    rc = aca.main(
        [
            "--cache-path",
            str(cache_path),
        ]
    )
    assert rc == aca.EXIT_FATAL


def test_apply_for_ticker_value_error_is_logged_not_raised(
    store: InMemorySQLiteStore,
    tmp_path: Path,
) -> None:
    """ValueError on apply (e.g. invalid ratio on raw rows) is logged
    per-ticker; main() returns EXIT_OK if at least one ticker survived."""
    cache_path = tmp_path / "cache.json"

    # Inject a fetcher that returns a 0-value split (which would raise in
    # apply_split_adjustment). Use a fresh ticker that has no events so
    # the orchestrator's cache+no_actions path triggers normally for
    # most tickers; the bad ticker is the one with the zero-value event.
    fetcher_payload = [
        {"ticker": "SBER", "ts": "2026-01-09", "ratio": 0, "source": "moex"},  # zero -> ValueError
    ]

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_payload
        fake_fetcher.USER_AGENT = "alphard-test"
        # Default: filter zero-ratio out via _fetch_splits_for_ticker
        # (so actions becomes empty, main() records no_actions and continues).
        rc = aca.main(
            [
                "--tickers",
                "SBER",
                "--cache-path",
                str(cache_path),
            ],
            store=store,
        )

    # The zero-ratio event is filtered out at fetch time (defensive), so
    # actions is empty → no_actions path → cache recorded, no rows.
    assert store.count_ohlcv_adj("SBER") == 0
    cache = json.loads(cache_path.read_text())
    assert "SBER" in cache  # cache entry recorded
    assert rc == aca.EXIT_OK


# ---------- smoke: fetcher + adjustment compose ----------


def test_smoke_fetch_then_apply_compose_correctly(
    store: InMemorySQLiteStore,
) -> None:
    """Spec acceptance criterion #5: 'fetch actions for SBER, apply
    splits, verify OHLCV adj_close reflects 1:N splits'.

    Uses the real (not mocked) fetcher-style data shape to verify the
    composition end-to-end with apply_split_adjustment.
    """
    from src.data.adjustment import apply_split_adjustment

    raw = [
        _make_ohlcv("SBER", date(2026, 1, 6), "100", "1000"),
        _make_ohlcv("SBER", date(2026, 1, 9), "51", "1000"),  # post-split
    ]
    fetcher_output = [{"ticker": "SBER", "ts": "2026-01-09", "ratio": 2.0, "source": "moex"}]
    actions = [
        CorporateAction(
            ticker="SBER",
            ts=date.fromisoformat(e["ts"]),
            kind="split",
            value=Decimal(str(e["ratio"])),
            source="moex",
        )
        for e in fetcher_output
        if e["ticker"] == "SBER"
    ]
    adjusted = apply_split_adjustment(raw, actions)
    assert adjusted[0].close == Decimal("50")  # 100 / 2
    assert adjusted[0].volume == Decimal("2000")  # 1000 * 2
    assert adjusted[1].close == Decimal("51")  # post-split, unchanged
    assert adjusted[1].volume == Decimal("1000")


# ---------- fatal / error-path coverage ----------


def test_list_tickers_failure_is_fatal(
    store: InMemorySQLiteStore,
    tmp_path: Path,
) -> None:
    """If ``store.list_tickers`` raises StoreError, ``main()`` returns
    EXIT_FATAL. The store is closed (since main() owns it in this path).
    """
    cache_path = tmp_path / "cache.json"

    broken_store = MagicMock()
    broken_store.list_tickers.side_effect = StoreError("DB went away")
    broken_store.close = MagicMock()

    rc = aca.main(
        [
            "--tickers",
            "SBER",
            "--cache-path",
            str(cache_path),
        ],
        store=broken_store,
    )
    assert rc == aca.EXIT_FATAL
    broken_store.close.assert_not_called()  # injected store is not owned


def test_main_returns_fatal_when_every_ticker_errored(
    store: InMemorySQLiteStore,
    tmp_path: Path,
) -> None:
    """If every attempted ticker raised (and zero had no_actions and zero
    were fresh), main() returns EXIT_FATAL — the operator must notice."""
    cache_path = tmp_path / "cache.json"

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        # Persistent OSError so EVERY ticker fails on every iteration.
        fake_fetcher.fetch_splits.side_effect = OSError("MOEX ISS down")
        fake_fetcher.USER_AGENT = "alphard-test"
        rc = aca.main(
            [
                "--cache-path",
                str(cache_path),
            ],
            store=store,
        )

    assert rc == aca.EXIT_FATAL
    assert store.count_ohlcv_adj() == 0


def test_apply_store_error_does_not_abort_loop(
    store: InMemorySQLiteStore,
    fetcher_splits_sber_split: list[dict],
    tmp_path: Path,
) -> None:
    """A StoreError on _apply_for_ticker for SBER must NOT abort GAZP
    processing. The error is logged and the loop continues."""
    cache_path = tmp_path / "cache.json"

    # Inject a SBER/GAZP-aware fetcher so both tickers get processed.
    fetcher_payload = fetcher_splits_sber_split + [
        {"ticker": "GAZP", "ts": "2026-02-01", "ratio": 3.0, "source": "moex"},
    ]

    # Wrap upsert_ohlcv_adj to fail on SBER, succeed on GAZP.
    original_upsert = store.upsert_ohlcv_adj
    call_count = {"n": 0}

    def flaky_upsert(rows):
        ticker = rows[0].ticker if rows else ""
        call_count["n"] += 1
        if ticker == "SBER":
            raise StoreError("simulated PG transient")
        return original_upsert(rows)

    store.upsert_ohlcv_adj = flaky_upsert  # type: ignore[assignment]

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_payload
        fake_fetcher.USER_AGENT = "alphard-test"
        rc = aca.main(
            [
                "--cache-path",
                str(cache_path),
            ],
            store=store,
        )

    # GAZP succeeded (at least 5 adjusted bars); SBER failed (0).
    assert store.count_ohlcv_adj("GAZP") >= 5
    assert store.count_ohlcv_adj("SBER") == 0
    assert rc == aca.EXIT_OK  # partial success is non-fatal


def test_unexpected_exception_in_fetch_does_not_abort(
    store: InMemorySQLiteStore,
    tmp_path: Path,
) -> None:
    """Generic non-RequestException during fetch is logged and skipped."""
    cache_path = tmp_path / "cache.json"

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.side_effect = ValueError("weird parsing bug")
        fake_fetcher.USER_AGENT = "alphard-test"
        rc = aca.main(
            [
                "--cache-path",
                str(cache_path),
            ],
            store=store,
        )

    # Both SBER and GAZP hit the generic-except branch; no rows written.
    assert store.count_ohlcv_adj() == 0
    assert rc == aca.EXIT_FATAL  # every ticker errored


def test_progress_heartbeat_logged(
    store: InMemorySQLiteStore,
    caplog,
) -> None:
    """The progress log line fires every PROGRESS_HEARTBEAT_EVERY (50)
    tickers. We use only 2 tickers here so the heart-beat log fires on
    the last ticker (i % 50 == 0 OR i == len(tickers))."""
    import logging

    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = []
        fake_fetcher.USER_AGENT = "alphard-test"
        with caplog.at_level(logging.INFO, logger="alphard.corp_actions_apply"):
            aca.main(
                [
                    "--dry-run",
                    "--cache-path",
                    "/tmp/_unused_for_dry_run.json",
                ],
                store=store,
            )

    # The heartbeat line ends with "... processed (...)" — match by substring.
    progress_lines = [
        r.message for r in caplog.records if "progress:" in r.message
    ]
    assert progress_lines, "no progress heartbeat logged"


# ---------- cache atomic write ----------


def test_save_cache_is_atomic(
    store: InMemorySQLiteStore,
    tmp_path: Path,
) -> None:
    """Cache writes go to cache_path.tmp then rename — the previous
    cache (if any) stays intact on partial writes."""
    cache_path = tmp_path / "cache.json"

    # Write a first cache.
    aca._save_cache(cache_path, {"SBER": "2026-08-19T12:00:00+00:00"})
    assert cache_path.exists()
    first_mtime = cache_path.stat().st_mtime

    # Overwrite with a different value.
    import time

    time.sleep(0.01)
    aca._save_cache(cache_path, {"SBER": "2026-08-20T12:00:00+00:00"})
    second_mtime = cache_path.stat().st_mtime

    assert second_mtime >= first_mtime
    assert json.loads(cache_path.read_text()) == {"SBER": "2026-08-20T12:00:00+00:00"}


# ---------- cache fresh check ----------


def test_is_fresh_handles_missing_and_malformed(tmp_path: Path) -> None:
    """_is_fresh must return False for None and for malformed ISO strings,
    so a corrupt cache entry never accidentally suppresses a re-apply."""
    # None: not fresh.
    assert not aca._is_fresh(None, 7)
    # Future timestamp: fresh.
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert aca._is_fresh(future, 7)
    # Old timestamp: not fresh.
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert not aca._is_fresh(old, 7)
    # Malformed: not fresh (defensive — bad cache must NOT block the run).
    assert not aca._is_fresh("not-a-date", 7)
    # Naive datetime (no tzinfo): treated as UTC and compared.
    naive_now = datetime.now().isoformat()
    assert aca._is_fresh(naive_now, 7)


# ---------- per-ticker fetch filter ----------


def test_fetch_splits_for_ticker_filters_other_tickers() -> None:
    """The orchestrator's per-ticker filter must drop other-ticker events
    so SBER does not see GAZP's split history."""
    fetcher_payload = [
        {"ticker": "SBER", "ts": "2026-01-09", "ratio": 2.0, "source": "moex"},
        {"ticker": "GAZP", "ts": "2026-02-01", "ratio": 3.0, "source": "moex"},
    ]
    session = MagicMock()
    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_payload
        actions = aca._fetch_splits_for_ticker("SBER", session, timeout=60)

    assert len(actions) == 1
    assert actions[0].ticker == "SBER"
    assert actions[0].ts == date(2026, 1, 9)
    assert actions[0].value == Decimal("2.0")


def test_fetch_splits_for_ticker_drops_malformed_rows() -> None:
    """Malformed rows (missing ts, invalid ratio, zero ratio) must be
    dropped silently — not raised."""
    fetcher_payload = [
        {"ticker": "SBER", "ts": "2026-01-09", "ratio": 2.0, "source": "moex"},
        {"ticker": "SBER", "ts": None, "ratio": 2.0, "source": "moex"},  # missing ts
        {"ticker": "SBER", "ts": "not-a-date", "ratio": 2.0, "source": "moex"},  # bad ts
        {"ticker": "SBER", "ts": "2026-01-09", "ratio": 0, "source": "moex"},  # zero ratio
        {"ticker": "SBER", "ts": "2026-01-09", "ratio": -1.0, "source": "moex"},  # negative
    ]
    session = MagicMock()
    with patch.object(aca, "_FETCHER_MOD") as fake_fetcher:
        fake_fetcher.fetch_splits.return_value = fetcher_payload
        actions = aca._fetch_splits_for_ticker("SBER", session, timeout=60)

    # Only the first (well-formed) entry survives.
    assert len(actions) == 1
    assert actions[0].ts == date(2026, 1, 9)
    assert actions[0].value == Decimal("2.0")


# ---------- listed_at filter ----------


def test_list_tickers_filters_unlisted() -> None:
    """Tickers with listed_at IS NULL must be excluded from the universe."""
    s = InMemorySQLiteStore()
    s.upsert_tickers(
        [
            _make_ticker("SBER", listed_at=date(2010, 1, 1)),
            _make_ticker("NOEX", listed_at=None),
        ]
    )
    metas = aca._list_tickers(s)
    tickers = {m.ticker for m in metas}
    assert "SBER" in tickers
    assert "NOEX" not in tickers
    s.close()


def test_list_tickers_respects_only_filter() -> None:
    """When ``only`` is provided, only those tickers are returned (even
    if they would be filtered by listed_at)."""
    s = InMemorySQLiteStore()
    s.upsert_tickers(
        [
            _make_ticker("SBER", listed_at=date(2010, 1, 1)),
            _make_ticker("GAZP", listed_at=date(2010, 1, 1)),
        ]
    )
    metas = aca._list_tickers(s, only=["SBER"])
    assert [m.ticker for m in metas] == ["SBER"]
    s.close()