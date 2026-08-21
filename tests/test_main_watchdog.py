"""Tests for the daily_sync watchdog (`_run_daily_sync_watchdog`).

The watchdog reads `_daily_sync_health.last_successful_run_at` from Postgres and
forces a container restart (sys.exit(1)) when the daily_sync daemon thread
has either crashed silently or is wedged. This is the only reliable recovery
mechanism: a thread crash inside a live process leaves no signal to Docker
otherwise.

Coverage targets:
- last_run is fresh (< 26h) → log "OK" and return
- last_run is stale (> 26h) → log CRITICAL + sys.exit(1)
- last_run is None and container age < 24h → log "skipping (pre-first-run)"
- last_run is None and container age >= 24h → log CRITICAL + sys.exit(1)
- store init fails → log warning, skip (don't crash heartbeat)
- store.close() fails during shutdown → log warning (issue #14 D.1 fix)

We mock PostgresDataStore to avoid a real DB connection.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add src/ to sys.path so `import main` works whether tests run from /root
# (local) or /__w/alphard/alphard (CI). Resolve relative to this file.
_SRC_PATH = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)


def _import_main():
    if "main" not in sys.modules:
        import main as _alphard_main  # type: ignore  # noqa: F401
    return sys.modules["main"]


@pytest.fixture(autouse=True)
def _setup():
    main = _import_main()
    main._shutdown_event.clear()
    return main


def _make_store(last_run, store_close_raises=False):
    """Build a mock PostgresDataStore with the given last_daily_sync_run_at."""
    store = MagicMock()
    store.last_daily_sync_run_at.return_value = last_run
    if store_close_raises:
        store.close.side_effect = OSError("connection lost during shutdown")
    else:
        store.close.return_value = None
    return store


def _patch_store(monkeypatch, store):
    """Patch PostgresDataStore construction to return our mock."""
    monkeypatch.setattr("src.data.pg_store.PostgresDataStore", lambda *a, **kw: store)


def test_watchdog_ok_when_recent_run(_setup, monkeypatch, caplog):
    """If last_run is < WATCHDOG_STALE_SECONDS, watchdog logs OK and returns."""
    main = _setup
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    store = _make_store(recent)
    _patch_store(monkeypatch, store)

    with caplog.at_level(logging.INFO, logger="alphard.watchdog"):
        main._run_daily_sync_watchdog()

    assert any("daily_sync OK" in r.message for r in caplog.records)
    # No exit called.
    store.close.assert_called_once()


def test_watchdog_exits_on_stale_run(_setup, monkeypatch, caplog):
    """If last_run > WATCHDOG_STALE_SECONDS, sys.exit(1) is called."""
    main = _setup
    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    store = _make_store(stale)
    _patch_store(monkeypatch, store)

    with caplog.at_level(logging.CRITICAL, logger="alphard.watchdog"):
        with pytest.raises(SystemExit) as exc_info:
            main._run_daily_sync_watchdog()
        assert exc_info.value.code == 1

    assert any("broken or wedged" in r.message for r in caplog.records)
    assert store.close.call_count >= 1


def test_watchdog_skips_when_no_run_but_container_young(_setup, monkeypatch, caplog):
    """last_run None + container age < 24h → pre-first-run, skip."""
    main = _setup
    store = _make_store(None)
    _patch_store(monkeypatch, store)

    # Patch the watchdog's /proc/1/stat + /proc/stat readers. The watchdog
    # opens "/proc/1/stat" for start_jiffies and "/proc/stat" for btime.
    # We mock them to fake a 1-hour-old container.
    fake_btime = int(datetime.now(timezone.utc).timestamp()) - 3600  # 1h ago

    proc_1_stat_content = "1 (init) S 0 1 1 0 -1 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    proc_stat_content = f"btime {fake_btime}\n"

    real_open = open

    def fake_open(file, *args, **kwargs):
        if "/proc/1/stat" in str(file):
            from unittest.mock import mock_open

            return mock_open(read_data=proc_1_stat_content)()
        if "/proc/stat" in str(file):
            from unittest.mock import mock_open

            return mock_open(read_data=proc_stat_content)()
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr("os.sysconf", lambda name: 100 if "CLK_TCK" in name else 1)

    with caplog.at_level(logging.INFO, logger="alphard.watchdog"):
        main._run_daily_sync_watchdog()

    # Skip means no exit, no CRITICAL.
    assert not any(r.levelname == "CRITICAL" for r in caplog.records)
    assert store.close.call_count >= 1


def test_watchdog_exits_when_no_run_and_container_old(_setup, monkeypatch, caplog):
    """last_run None + container age >= 24h → CRITICAL + sys.exit(1)."""
    main = _setup
    store = _make_store(None)
    _patch_store(monkeypatch, store)

    # 25 hours ago.
    fake_btime = int(datetime.now(timezone.utc).timestamp()) - 25 * 3600

    proc_1_stat_content = "1 (init) S 0 1 1 0 -1 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    proc_stat_content = f"btime {fake_btime}\n"

    real_open = open

    def fake_open(file, *args, **kwargs):
        if "/proc/1/stat" in str(file):
            from unittest.mock import mock_open

            return mock_open(read_data=proc_1_stat_content)()
        if "/proc/stat" in str(file):
            from unittest.mock import mock_open

            return mock_open(read_data=proc_stat_content)()
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr("os.sysconf", lambda name: 100 if "CLK_TCK" in name else 1)

    with caplog.at_level(logging.CRITICAL, logger="alphard.watchdog"):
        with pytest.raises(SystemExit) as exc_info:
            main._run_daily_sync_watchdog()
        assert exc_info.value.code == 1

    assert any("never stamped after 24h" in r.message for r in caplog.records)
    assert store.close.call_count >= 1


def test_watchdog_skips_when_store_init_fails(_setup, monkeypatch, caplog):
    """If PostgresDataStore() raises, watchdog logs warning and returns."""
    main = _setup

    def boom(*a, **kw):
        raise OSError("postgres not reachable")

    monkeypatch.setattr("src.data.pg_store.PostgresDataStore", boom)

    with caplog.at_level(logging.WARNING, logger="alphard.watchdog"):
        main._run_daily_sync_watchdog()

    assert any("cannot init store" in r.message for r in caplog.records)
    # No exit, no CRITICAL.
    assert not any(r.levelname == "CRITICAL" for r in caplog.records)


def test_watchdog_logs_warning_on_store_close_failure(_setup, monkeypatch, caplog):
    """If store.close() fails during shutdown, watchdog logs warning (issue #14 D.1 fix)."""
    main = _setup
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    store = _make_store(recent, store_close_raises=True)
    _patch_store(monkeypatch, store)

    with caplog.at_level(logging.INFO, logger="alphard.watchdog"):
        main._run_daily_sync_watchdog()

    # Even though close failed, the watchdog did not propagate the exception.
    assert any("store.close() failed" in r.message for r in caplog.records)
    # Successful OK log still printed.
    assert any("daily_sync OK" in r.message for r in caplog.records)


def test_sleep_interruptible_returns_on_keyboard_interrupt(_setup, monkeypatch):
    """_sleep_interruptible must catch KeyboardInterrupt and return cleanly."""
    main = _setup

    # Force time.sleep to raise KeyboardInterrupt immediately.
    def boom(_seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr("time.sleep", boom)

    # Should return without propagating.
    main._sleep_interruptible(10)
    # No exception raised → test passes.


def test_watchdog_skips_when_btime_missing(_setup, monkeypatch, caplog):
    """If /proc/stat doesn't contain btime=0, watchdog logs and skips."""
    main = _setup
    store = _make_store(None)
    _patch_store(monkeypatch, store)

    proc_1_stat_content = "1 (init) S 0 1 1 0 -1 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    proc_stat_content = "cpu 0 0 0 0 0 0 0 0 0 0\n"  # no btime line → triggers fallback

    real_open = open

    def fake_open(file, *args, **kwargs):
        if "/proc/1/stat" in str(file):
            from unittest.mock import mock_open

            return mock_open(read_data=proc_1_stat_content)()
        if "/proc/stat" in str(file):
            from unittest.mock import mock_open

            return mock_open(read_data=proc_stat_content)()
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr("os.sysconf", lambda name: 100 if "CLK_TCK" in name else 1)

    with caplog.at_level(logging.INFO, logger="alphard.watchdog"):
        main._run_daily_sync_watchdog()

    # btime missing → "cannot determine container uptime" → skip
    assert any("cannot determine container uptime" in r.message for r in caplog.records)
    assert not any(r.levelname == "CRITICAL" for r in caplog.records)


def test_watchdog_handles_probe_exception(_setup, monkeypatch, caplog):
    """If last_daily_sync_run_at() raises, watchdog swallows and logs."""
    main = _setup

    store = MagicMock()
    store.last_daily_sync_run_at.side_effect = RuntimeError("db locked")
    store.close.return_value = None
    _patch_store(monkeypatch, store)

    with caplog.at_level(logging.WARNING, logger="alphard.watchdog"):
        main._run_daily_sync_watchdog()

    assert any("probe failed" in r.message for r in caplog.records)
    store.close.assert_called_once()


# ---------------------------------------------------------------------------
# Issue #106: /proc/<pid>/stat parser must handle whitespace in ``comm``.
#
# proc(5) explicitly warns: the comm field is wrapped in parens and may
# contain ANY character except ')' and NUL, including spaces. A naive
# ``read().split()`` lands on the wrong offsets whenever comm has a
# space. The old implementation used ``fields[21]`` which was wrong for
# entrypoint.sh-with-args, kernel threads whose comm is empty, and any
# Python daemon launched via a wrapper script.
# ---------------------------------------------------------------------------


def test_read_proc_starttime_handles_whitespace_in_comm(_setup):
    """Parser must ignore the comm field entirely (rfind(')') idiom)."""
    main = _setup
    # Realistic /proc/1/stat with comm = "entrypoint.sh bash" — a
    # wrapper that itself has a space. The trailing fields are the
    # standard proc(5) layout; starttime is the 22nd field overall,
    # which sits at index 19 of the post-comm slice.
    fixture = (
        "1 (entrypoint.sh bash) S 0 1 1 0 -1 4194304 100 0 0 0 "
        "100 50 0 0 20 0 1 0 9876543 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0"
    )
    from unittest.mock import mock_open, patch

    with patch("builtins.open", mock_open(read_data=fixture)):
        jiffies = main._read_proc_starttime_jiffies(1)
    # field 22 (starttime) in the fixture above is 9876543.
    assert jiffies == 9876543, f"parser landed on wrong field; got {jiffies}, expected 9876543"


def test_read_proc_starttime_handles_nested_parens_in_comm(_setup):
    """comm may contain '(' as long as no ')' — parser still works."""
    main = _setup
    # comm = "weird(proc)name" is NOT legal (it has ')'); use "(" only.
    fixture = (
        "42 (weird(proc name) R 0 1 1 0 -1 4194304 100 0 0 0 " "0 0 0 0 20 0 1 0 12345 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0"
    )
    # Note: this fixture is intentionally malformed because ')' closes
    # comm. rfind(')') correctly stops at the comm-closer. starttime=12345.
    from unittest.mock import mock_open, patch

    with patch("builtins.open", mock_open(read_data=fixture)):
        jiffies = main._read_proc_starttime_jiffies(42)
    assert jiffies == 12345


def test_read_proc_starttime_raises_on_malformed(_setup):
    """Malformed /proc/<pid>/stat (no closing paren) must raise clearly."""
    main = _setup
    from unittest.mock import mock_open, patch

    with patch("builtins.open", mock_open(read_data="garbage no parens here")):
        with pytest.raises(RuntimeError, match="malformed"):
            main._read_proc_starttime_jiffies(1)


def test_read_proc_starttime_raises_on_too_short(_setup):
    """If post-comm slice is too short, raise rather than IndexError."""
    main = _setup
    from unittest.mock import mock_open, patch

    # comm = "x", only 5 fields after → len(tail) < 20
    with patch("builtins.open", mock_open(read_data="1 (x) R 0 0")):
        with pytest.raises(RuntimeError, match="expected >=20 fields"):
            main._read_proc_starttime_jiffies(1)


def test_container_uptime_falls_back_to_self_when_pid1_missing(_setup, monkeypatch, caplog):
    """If /proc/1/stat raises OSError, fall back to /proc/self/stat.

    This is the common shape on .107 when Docker is run with the default
    PID namespace (PID 1 inside the container IS entrypoint.sh) but a
    future ``docker run --pid=host`` would expose the host init — we want
    the watchdog to keep working in either case.
    """
    main = _setup
    store = _make_store(None)
    _patch_store(monkeypatch, store)

    fake_btime = int(datetime.now(timezone.utc).timestamp()) - 2 * 3600  # 2h ago

    # /proc/self/stat fixture: comm = "python3", starttime close to btime.
    # Set starttime so that container_uptime_seconds() returns ~2h.
    # start_sec = (now - btime) - uptime → for uptime=2h,
    # start_jiffies = (2h * clk_tck) = 2*3600*100 = 720000.
    self_stat = (
        f"{os.getpid()} (python3) S 0 1 1 0 -1 4194304 100 0 0 0 "
        f"0 0 0 0 20 0 1 0 720000 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0"
    )
    proc_stat_content = f"btime {fake_btime}\n"

    real_open = open

    def fake_open(file, *args, **kwargs):
        path = str(file)
        from unittest.mock import mock_open

        if "/proc/1/stat" in path:
            raise FileNotFoundError("[Errno 2] No such file or directory")
        if "/proc/self/stat" in path:
            return mock_open(read_data=self_stat)()
        if "/proc/stat" in path:
            return mock_open(read_data=proc_stat_content)()
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr("os.sysconf", lambda name: 100 if "CLK_TCK" in name else 1)

    with caplog.at_level(logging.INFO, logger="alphard.watchdog"):
        main._run_daily_sync_watchdog()

    # 2h container age < 24h → skip (no CRITICAL, no exit).
    assert not any(r.levelname == "CRITICAL" for r in caplog.records)


def test_container_uptime_raises_when_all_proc_unreadable(_setup, monkeypatch):
    """When both /proc/1 and /proc/self are unreadable, raise RuntimeError.

    Caller (watchdog) catches this and logs + skips. We test the helper
    in isolation so future refactors cannot accidentally swallow the
    error and silently return 0 (which would make the watchdog trigger
    a restart on a fresh container).
    """
    main = _setup

    def fake_open(file, *args, **kwargs):
        raise FileNotFoundError(f"nope: {file}")

    monkeypatch.setattr("builtins.open", fake_open)

    with pytest.raises(RuntimeError, match="neither"):
        main._container_uptime_seconds()
