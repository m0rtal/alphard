"""Tests for the backfill circuit breaker (issue #430).

Backfill_history_md exits rc=1 every 30s on persistent broker network
outage. After 10 deaths/hour, the supervisor hits the
_BACKFILL_MAX_RESPAWNS_PER_HOUR fatal cap and exits the container, which
Docker restart-loops. The circuit breaker detects the network-outage
pattern from the log tail and backs off 5 minutes instead of 30s, so the
death rate stays under the cap until broker egress recovers.

These tests pin the helper functions. They do NOT spawn the supervisor
loop (see test_main_backfill_supervisor.py for why).
"""

from __future__ import annotations

from src.main import (
    _CB_BACKOFF_SECONDS,
    _CB_RECOVERY_SECONDS,
    _CB_UNAVAILABLE_THRESHOLD,
    _child_exit_was_network_outage,
    _read_unavail_streak,
    _write_unavail_streak,
)


class _StubLog:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


def test_child_exit_was_network_outage_true_on_unavailable_storm(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text(
        "[ERROR] tinkoff_grpc UNAVAILABLE 14 TimeoutError\n"
        "[ERROR] moex_iss UNAVAILABLE 14 connect() timed out\n"
        "[WARNING] all 9 broker calls failed (UNAVAILABLE)\n"
        "[ERROR] tinkoff_grpc UNAVAILABLE 14 connect() timed out\n"
        "[ERROR] moex_iss Max retries exceeded (UNAVAILABLE)\n"
    )
    assert _child_exit_was_network_outage(log_path=str(log)) is True


def test_child_exit_was_network_outage_false_on_clean_log(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text(
        "[INFO] backfill_history_md started\n"
        "[INFO] ticker=GAZP bars=1234 done\n"
        "[INFO] ticker=SBER bars=5678 done\n"
        "[INFO] backfill_history_md finished cleanly\n"
    )
    assert _child_exit_was_network_outage(log_path=str(log)) is False


def test_child_exit_was_network_outage_false_on_single_warning(tmp_path):
    """A single network warning is noise, not an outage pattern."""
    log = tmp_path / "log.txt"
    log.write_text(
        "[WARNING] tinkoff_grpc transient blip (UNAVAILABLE)\n"
        "[INFO] retry succeeded\n"
        "[INFO] ticker=SBER bars=999\n"
    )
    assert _child_exit_was_network_outage(log_path=str(log)) is False


def test_child_exit_was_network_outage_returns_false_when_log_missing(tmp_path):
    """Read failure is non-fatal — return False (don't trip breaker on FS error)."""
    missing = tmp_path / "does-not-exist.log"
    assert _child_exit_was_network_outage(log_path=str(missing)) is False


def test_read_unavail_streak_returns_zero_when_missing(tmp_path):
    assert _read_unavail_streak(str(tmp_path / "nope.json")) == 0


def test_read_unavail_streak_handles_corrupt_file(tmp_path):
    f = tmp_path / "broken.json"
    f.write_text("not-json{")
    assert _read_unavail_streak(str(f)) == 0


def test_read_unavail_streak_round_trips(tmp_path):
    f = tmp_path / "cb.json"
    _write_unavail_streak(str(f), 7, _StubLog())
    assert _read_unavail_streak(str(f)) == 7


def test_write_unavail_streak_swallows_fs_errors(tmp_path):
    """Persist failure is non-fatal — log warning and continue."""
    f = tmp_path / "ro_subdir" / "cb.json"
    f.parent.mkdir()
    f.write_text("placeholder")
    f.chmod(0o400)
    # parent dir is read-only — should not raise.
    _write_unavail_streak(str(f), 3, _StubLog())
    f.chmod(0o644)  # cleanup for tmp_path teardown


def test_circuit_breaker_constants_match_design():
    assert _CB_UNAVAILABLE_THRESHOLD == 3
    assert _CB_BACKOFF_SECONDS == 300  # 5 min
    assert _CB_RECOVERY_SECONDS == 900  # 15 min
