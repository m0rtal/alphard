"""Tests for scripts/check_db_health.py and scripts/check_data_freshness.py.

These are standalone cron-driven runners. We don't hit a real Postgres —
we mock PostgresDataStore and verify control flow.

Phase 1.6 H-9 — silent-broken-postgres-on-redeploy defense.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_db_health  # noqa: E402
import check_data_freshness  # noqa: E402


# ---------------------------------------------------------------------------
# check_db_health.py
# ---------------------------------------------------------------------------


def test_check_db_health_returns_zero_on_ok() -> None:
    """auth_probe=True → exit 0, OK message."""
    fake = MagicMock()
    fake.auth_probe.return_value = True
    fake.return_value = fake  # in case anything is constructed
    with patch("check_db_health.PostgresDataStore", return_value=fake):
        rc = check_db_health.main()
    assert rc == 0
    fake.auth_probe.assert_called_once_with(source="check_db_health")


def test_check_db_health_returns_one_on_broken() -> None:
    """auth_probe=False → exit 1, BROKEN message."""
    fake = MagicMock()
    fake.auth_probe.return_value = False
    with patch("check_db_health.PostgresDataStore", return_value=fake):
        rc = check_db_health.main()
    assert rc == 1


def test_check_db_health_returns_one_on_dsn_missing() -> None:
    """If PostgresDataStore ctor raises (no DSN set), exit 1."""
    with patch(
        "check_db_health.PostgresDataStore",
        side_effect=RuntimeError("no DSN"),
    ):
        rc = check_db_health.main()
    assert rc == 1


def test_check_db_health_returns_one_on_auth_probe_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If PostgresDataStore.auth_probe raises (BUG case — not probe-fail),
    the runner exits non-zero so cron records it as a failure.

    auth_probe() itself catches errors and returns False in the auth-fail
    case. A RuntimeError means something else is wrong (e.g. connect
    failure before the try/except), and we WANT cron to alert.
    """
    fake = MagicMock()
    fake.auth_probe.side_effect = RuntimeError("connect refused")
    with patch("check_db_health.PostgresDataStore", return_value=fake):
        monkeypatch.setattr(sys, "argv", ["check_db_health.py"])
        # Must not raise — must return non-zero exit code.
        rc = check_db_health.main()
    assert rc != 0


# ---------------------------------------------------------------------------
# check_data_freshness.py
# ---------------------------------------------------------------------------


def test_freshness_returns_zero_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ohlcv_daily is a legitimate pre-launch state — not stale."""
    fake = MagicMock()
    fake.latest_ts_overall.return_value = None
    with patch("check_data_freshness.PostgresDataStore", return_value=fake):
        monkeypatch.setattr(sys, "argv", ["check_data_freshness.py"])
        rc = check_data_freshness.main()
    assert rc == 0


def test_freshness_returns_zero_when_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Latest bar within the threshold → exit 0."""
    fake = MagicMock()
    fake.latest_ts_overall.return_value = _dt.date.today()  # today
    with patch("check_data_freshness.PostgresDataStore", return_value=fake):
        monkeypatch.setattr(
            sys,
            "argv",
            ["check_data_freshness.py", "--stale-days", "1"],
        )
        rc = check_data_freshness.main()
    assert rc == 0


def test_freshness_returns_one_when_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Latest bar older than threshold → exit 1 (alert)."""
    fake = MagicMock()
    # 5 days ago — older than the default 1-day threshold
    fake.latest_ts_overall.return_value = _dt.date.today() - _dt.timedelta(days=5)
    with patch("check_data_freshness.PostgresDataStore", return_value=fake):
        monkeypatch.setattr(
            sys,
            "argv",
            ["check_data_freshness.py", "--stale-days", "1"],
        )
        rc = check_data_freshness.main()
    assert rc == 1


def test_freshness_returns_two_when_db_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgresDataStore ctor raises (no DSN / wrong password) → exit 2."""
    with patch(
        "check_data_freshness.PostgresDataStore",
        side_effect=RuntimeError("connect refused"),
    ):
        monkeypatch.setattr(sys, "argv", ["check_data_freshness.py"])
        rc = check_data_freshness.main()
    assert rc == 2


def test_freshness_default_threshold_is_one_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """argparse default for --stale-days is 1 (matches cron schedule)."""
    # Run with no args, mock to capture.
    fake = MagicMock()
    # 2 days ago → would be stale if default is 1
    fake.latest_ts_overall.return_value = _dt.date.today() - _dt.timedelta(days=2)
    with patch("check_data_freshness.PostgresDataStore", return_value=fake):
        monkeypatch.setattr(sys, "argv", ["check_data_freshness.py"])
        rc = check_data_freshness.main()
    assert rc == 1  # stale because default threshold = 1 day


def test_freshness_recent_within_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Latest bar exactly equal to threshold → not stale (>= check)."""
    fake = MagicMock()
    # 1 day ago — equal to threshold
    fake.latest_ts_overall.return_value = _dt.date.today() - _dt.timedelta(days=1)
    with patch("check_data_freshness.PostgresDataStore", return_value=fake):
        monkeypatch.setattr(
            sys,
            "argv",
            ["check_data_freshness.py", "--stale-days", "1"],
        )
        rc = check_data_freshness.main()
    # latest (today - 1) >= threshold (today - 1) → fresh
    assert rc == 0
