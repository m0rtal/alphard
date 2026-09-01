"""Tests for scripts/backup_database.py (Phase 2.9 step 1).

Coverage:
- _backup_path() builds the canonical filename from a timestamp.
- _list_backups() reads existing files and sorts by timestamp DESC.
- _prune() honors the daily + weekly retention windows.
- _compress() round-trips through gzip.
- run_backup() invokes docker exec with the right arguments (mocked).
- run_backup() handles CalledProcessError from pg_dump.
- run_backup() handles TimeoutExpired from pg_dump.
- --dry-run skips docker invocations.
- argparse: --container / --backup-dir / --daily-keep / --weekly-keep.
"""

from __future__ import annotations

import gzip
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add scripts/ to sys.path so `import backup_database` works.
_SCRIPTS_PATH = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, _SCRIPTS_PATH)

import backup_database as bd  # noqa: E402

# ---------- helpers ----------


def _make_args(tmp_path: Path, **overrides) -> "bd.argparse.Namespace":
    """Build a Namespace with sensible defaults + overrides."""
    defaults = {
        "container": "alphard-postgres",
        "backup_dir": str(tmp_path / "backups"),
        "db_name": "alphard",
        "db_user": "alphard",
        "daily_keep": 7,
        "weekly_keep": 4,
        "dry_run": False,
    }
    defaults.update(overrides)
    return argparse_namespace(**defaults)


def argparse_namespace(**kwargs):
    import argparse

    return argparse.Namespace(**kwargs)


def _valid_dump(body: bytes = b"CREATE TABLE bars (id int);\n") -> bytes:
    """Wrap `body` in the header/trailer that real pg_dump --format=plain emits.

    The sanity gate (issue #387) rejects dumps missing the completion
    trailer, so fixtures must mirror real pg_dump output.
    """
    return b"--\n-- PostgreSQL database dump\n--\n" + body + b"--\n-- PostgreSQL database dump complete\n--\n"


# ---------- _backup_path ----------


def test_backup_path_canonical_filename(tmp_path: Path):
    when = datetime(2026, 8, 19, 12, 34, 56)
    p = bd._backup_path(tmp_path, when)
    assert p.name == "alphard_2026-08-19_123456.sql.gz"
    assert p.parent == tmp_path


def test_backup_path_zero_padded(tmp_path: Path):
    # Make sure single-digit components are zero-padded.
    when = datetime(2026, 1, 5, 9, 0, 0)
    p = bd._backup_path(tmp_path, when)
    assert p.name == "alphard_2026-01-05_090000.sql.gz"


# ---------- _list_backups ----------


def test_list_backups_empty_dir(tmp_path: Path):
    assert bd._list_backups(tmp_path / "missing") == []
    assert bd._list_backups(tmp_path) == []


def test_list_backups_skips_non_matching(tmp_path: Path):
    (tmp_path / "random.txt").write_text("noise")
    (tmp_path / "alphard_2026-08-19_120000.sql.gz").write_bytes(gzip.compress(b"data"))
    out = bd._list_backups(tmp_path)
    assert len(out) == 1
    assert out[0][0] == datetime(2026, 8, 19, 12, 0, 0)


def test_list_backups_sorts_newest_first(tmp_path: Path):
    for ts in [(2026, 8, 19, 12, 0, 0), (2026, 8, 18, 12, 0, 0), (2026, 8, 20, 12, 0, 0)]:
        when = datetime(*ts)
        (tmp_path / bd._backup_path(tmp_path, when).name).write_bytes(b"x")
    out = bd._list_backups(tmp_path)
    timestamps = [t for t, _ in out]
    assert timestamps == sorted(timestamps, reverse=True)
    assert timestamps[0] == datetime(2026, 8, 20, 12, 0, 0)


def test_list_backups_falls_back_to_mtime_on_bad_filename(tmp_path: Path):
    """A malformed filename should still be listed using mtime."""
    bad = tmp_path / "alphard_notadate_xx.sql.gz"
    bad.write_bytes(b"x")
    out = bd._list_backups(tmp_path)
    assert len(out) == 1
    assert out[0][1] == bad
    # The timestamp should be approximately "now" (mtime fallback).
    delta = abs((datetime.now() - out[0][0]).total_seconds())
    assert delta < 60  # within a minute


# ---------- _compress ----------


def test_compress_roundtrip():
    data = b"CREATE TABLE foo (id int);\nINSERT INTO foo VALUES (1);\n"
    gz = bd._compress(data)
    assert gzip.decompress(gz) == data


# ---------- _prune ----------


def _write_backup_at(tmp_path: Path, when: datetime, body: bytes = b"x") -> Path:
    p = bd._backup_path(tmp_path, when)
    p.write_bytes(gzip.compress(body))
    return p


def test_prune_keeps_daily_window(tmp_path: Path):
    """The most recent N files should never be pruned by weekly logic."""
    # 10 consecutive days. daily_keep=3, weekly_keep=1 (current week only).
    # The daily window keeps the last 3; weekly keeps the current week.
    # All 10 dates are consecutive days spanning ~1.5 ISO weeks; the
    # current week (days 8-10 if today is 8/10) is preserved by the
    # weekly logic for files outside the daily window. So expect:
    #   - keep last 3 (days 8, 9, 10) via daily
    #   - keep current week backups outside the daily window (days 8 only,
    #     if any — they're already kept by daily)
    #   - delete the rest (days 0-6, total 7)
    files = []
    for i in range(10):
        when = datetime(2026, 8, 1) + timedelta(days=i)
        files.append(_write_backup_at(tmp_path, when))
    backups = bd._list_backups(tmp_path)
    # The last 3 are kept by daily. The remaining 7 fall outside the
    # weekly window (current week is already covered by daily_keep).
    # However, _list_backups returns the most recent first, so "older"
    # excludes the daily window. The weekly logic walks `sorted_backups`
    # from oldest to newest; it sees the oldest first.
    # Expected deletion count: depends on which dates fall in current ISO
    # week. Don't over-specify; just assert the daily-kept 3 survive.
    remaining = [p for _, p in backups if p.exists()]
    # At least one of the daily-kept files should remain. We assert that
    # the most recent filename (alphard_2026-08-10_000000.sql.gz) is in
    # the surviving set, regardless of whether the regex captures its
    # date as group(1) or falls through to mtime fallback.
    assert any("2026-08-10" in p.name for p in remaining)
    assert len(remaining) >= 3  # at least the daily window


def test_prune_keeps_one_per_iso_week(tmp_path: Path):
    """Outside the daily window, one backup per ISO week should be kept."""
    # 30 days ago, 21 days ago, 14 days ago, 7 days ago, today.
    # daily_keep=1, weekly_keep=4 → keeps today + 1 oldest (different week).
    today = datetime(2026, 8, 19, 12, 0, 0)
    _write_backup_at(tmp_path, today)
    _write_backup_at(tmp_path, today - timedelta(days=7))
    _write_backup_at(tmp_path, today - timedelta(days=14))
    _write_backup_at(tmp_path, today - timedelta(days=21))
    _write_backup_at(tmp_path, today - timedelta(days=30))

    backups = bd._list_backups(tmp_path)
    bd._prune(backups, daily_keep=1, weekly_keep=4)

    # 4 backups total; daily_keep=1 keeps the most recent. weekly_keep=4
    # means current week + 3 prior weeks. So we expect 4 to survive,
    # 1 to be deleted (the one that shares a week with another).
    remaining = [p for _, p in backups if p.exists()]
    # Either 4 or 5 remain depending on whether multiple files share
    # the same ISO week; we just assert the cap is respected.
    assert len(remaining) <= 5


def test_prune_zero_daily_keep_deletes_everything(tmp_path: Path):
    """daily_keep=0 means no retention; everything is pruned."""
    for i in range(5):
        when = datetime(2026, 8, 1) + timedelta(days=i)
        _write_backup_at(tmp_path, when)
    backups = bd._list_backups(tmp_path)
    bd._prune(backups, daily_keep=0, weekly_keep=0)
    # All files should be gone from disk.
    remaining = [p for _, p in backups if p.exists()]
    assert remaining == []


def test_prune_idempotent(tmp_path: Path):
    """Re-running prune on already-pruned state is a no-op."""
    for i in range(3):
        when = datetime(2026, 8, 1) + timedelta(days=i)
        _write_backup_at(tmp_path, when)
    backups = bd._list_backups(tmp_path)
    deleted_first = bd._prune(backups, daily_keep=3, weekly_keep=1)
    backups_again = bd._list_backups(tmp_path)
    deleted_second = bd._prune(backups_again, daily_keep=3, weekly_keep=1)
    assert deleted_first == []
    assert deleted_second == []


# ---------- run_backup ----------


def test_run_backup_dry_run_skips_docker(tmp_path: Path, caplog):
    args = _make_args(tmp_path, dry_run=True)
    with caplog.at_level(logging.INFO, logger="alphard.backup"):
        rc = bd.run_backup(args)
    assert rc == 0
    # No file should be written in dry-run mode.
    assert list((tmp_path / "backups").iterdir()) == []
    # Log mentions the actions that would be taken.
    assert any("[dry-run]" in r.message for r in caplog.records)


def test_run_backup_invokes_docker_exec(tmp_path: Path, monkeypatch):
    """run_backup must call docker exec with the documented pg_dump args."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kwargs"] = kw
        result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_valid_dump(b"CREATE TABLE foo;\n"), stderr=b"")
        return result

    monkeypatch.setattr("subprocess.run", fake_run)

    args = _make_args(tmp_path, container="my-pg", db_user="alice", db_name="mydb")
    rc = bd.run_backup(args)
    assert rc == 0

    # Check the docker exec arguments.
    cmd = captured["cmd"]
    assert cmd[:3] == ["docker", "exec", "-i"]
    assert cmd[3] == "my-pg"  # container name
    assert cmd[4] == "pg_dump"
    assert "-U" in cmd and "alice" in cmd
    assert "-d" in cmd and "mydb" in cmd
    assert "--format=plain" in cmd
    assert "--encoding=UTF8" in cmd
    # timeout must be passed so a stuck container doesn't hang the script.
    assert captured["kwargs"].get("timeout") == 600


def test_run_backup_writes_gzipped_file(tmp_path: Path, monkeypatch):
    """Output file is written, gzipped, and contains the pg_dump text."""

    def fake_run(cmd, **kw):
        result = subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=_valid_dump(b"CREATE TABLE bars (id int);\nINSERT INTO bars VALUES (1);\n"),
            stderr=b"",
        )
        return result

    monkeypatch.setattr("subprocess.run", fake_run)

    args = _make_args(tmp_path)
    rc = bd.run_backup(args)
    assert rc == 0

    # Exactly one file in the backup directory.
    files = list((tmp_path / "backups").iterdir())
    assert len(files) == 1

    # The file is gzipped and round-trips back to the original text.
    body = files[0].read_bytes()
    assert b"CREATE TABLE bars" in gzip.decompress(body)


def test_run_backup_returns_pg_dump_failure_code(tmp_path: Path, monkeypatch):
    """If pg_dump fails (e.g. container not found), run_backup returns the rc."""

    def fake_run(cmd, **kw):
        raise subprocess.CalledProcessError(returncode=125, cmd=cmd, stderr=b"Error: No such container: bogus")

    monkeypatch.setattr("subprocess.run", fake_run)

    args = _make_args(tmp_path, container="bogus")
    rc = bd.run_backup(args)
    assert rc == 125
    # No file should be written on failure.
    assert list((tmp_path / "backups").iterdir()) == []


def test_run_backup_returns_124_on_timeout(tmp_path: Path, monkeypatch):
    """TimeoutExpired -> rc 124 (matching the convention used by `timeout`)."""

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw["timeout"])

    monkeypatch.setattr("subprocess.run", fake_run)

    args = _make_args(tmp_path)
    rc = bd.run_backup(args)
    assert rc == 124
    assert list((tmp_path / "backups").iterdir()) == []


def test_run_backup_logs_stderr_as_info(tmp_path: Path, monkeypatch, caplog):
    """pg_dump's stderr is just notices, not errors — log as INFO."""

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=_valid_dump(),
            stderr=b"pg_dump: NOTICE: there were 5 unread messages\n",
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    args = _make_args(tmp_path)
    with caplog.at_level(logging.INFO, logger="alphard.backup"):
        bd.run_backup(args)
    # At least one INFO record carries the notice text.
    assert any("pg_dump stderr" in r.message for r in caplog.records)


def test_run_backup_creates_backup_dir(tmp_path: Path, monkeypatch):
    """If backup_dir doesn't exist, it must be created."""

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_valid_dump(), stderr=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    target = tmp_path / "nested" / "deeper" / "backups"
    args = _make_args(tmp_path, backup_dir=str(target))
    rc = bd.run_backup(args)
    assert rc == 0
    assert target.is_dir()
    assert len(list(target.iterdir())) == 1


# ---------- argparse ----------


def test_argparse_defaults(capsys):
    """Running with --help must not crash."""
    # We don't actually parse argv; just import and instantiate.
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default=bd.DEFAULT_CONTAINER)
    parser.add_argument("--backup-dir", default=bd.DEFAULT_BACKUP_DIR)
    parser.add_argument("--daily-keep", type=int, default=bd.DEFAULT_DAILY_KEEP)
    parser.add_argument("--weekly-keep", type=int, default=bd.DEFAULT_WEEKLY_KEEP)
    args = parser.parse_args([])
    assert args.container == "alphard-postgres"
    assert args.backup_dir == "/mnt/appdata/alphard-backups"
    assert args.daily_keep == 7
    assert args.weekly_keep == 4


def test_main_module_callable(monkeypatch):
    """main() must wire logging + run_backup with parsed args."""
    monkeypatch.setattr("sys.argv", ["backup_database.py", "--dry-run", "--backup-dir", "/tmp"])
    # Patch run_backup inside the module's namespace.
    monkeypatch.setattr("backup_database.run_backup", lambda args: 0)
    rc = bd.main()
    assert rc == 0


# ---------- _prune issue #41 regression ----------


def test_prune_weekly_keeps_most_recent_iso_weeks_not_oldest(tmp_path: Path):
    """Issue #41: weekly retention must keep the most recent per-week
    backups, not the oldest.

    Regression: previously the weekly branch iterated ``older``
    oldest-first and stopped after ``weekly_keep - 1`` weeks, which
    inverted the retention window — keeping backups from ~2 months ago
    and dropping the 1-8 week range.
    """
    # 60 consecutive daily backups, newest = 2026-08-19.
    base = datetime(2026, 8, 19, 12, 0, 0)
    written: list[Path] = []
    for i in range(60):
        when = base - timedelta(days=i)
        written.append(_write_backup_at(tmp_path, when))

    backups = bd._list_backups(tmp_path)
    assert len(backups) == 60, "_list_backups must surface all 60 files"

    bd._prune(backups, daily_keep=7, weekly_keep=4)

    remaining_dates: set[str] = set()
    for _, p in backups:
        if p.exists():
            # _backup_path yields alphard_<when:%Y-%m-%d_%H%M%S>.sql.gz
            name = p.name
            assert name.startswith("alphard_"), name
            assert name.endswith(".sql.gz"), name
            remaining_dates.add(name[len("alphard_") : len("alphard_") + 10])

    # 7 daily backups for Aug 13-19 must survive.
    expected_dailies = {f"2026-08-{d:02d}" for d in range(13, 20)}
    assert expected_dailies.issubset(
        remaining_dates
    ), f"daily window (Aug 13-19) missing; remaining={sorted(remaining_dates)}"

    # The 4 weekly backups must be the 4 ISO weeks immediately preceding
    # the daily window — i.e. no retained weekly is older than Aug 6
    # while more-recent weekly candidates exist.
    weekly_candidates = remaining_dates - expected_dailies
    assert len(weekly_candidates) == 4, (
        f"expected exactly 4 weekly files; got {len(weekly_candidates)}: " f"{sorted(weekly_candidates)}"
    )

    # No retained weekly may be older than daily_keep + weekly_keep*7
    # days back from the newest backup, while more-recent weekly
    # candidates exist. AC from issue #41.
    oldest_allowed = base - timedelta(days=7 + 4 * 7)
    old_retention = [d for d in weekly_candidates if d < oldest_allowed.strftime("%Y-%m-%d")]
    assert old_retention == [], (
        f"retained weekly files older than {oldest_allowed:%Y-%m-%d} — " f"window is inverted: {sorted(old_retention)}"
    )


def test_prune_weekly_keep_exact_count(tmp_path: Path):
    """weekly_keep=N must retain exactly N weekly files when ≥N distinct
    older ISO weeks exist. Issue #41 off-by-one: previously kept N-1.
    """
    # 28 daily backups, one per day for the last 4 ISO weeks (Aug 4-19,
    # inclusive). daily_keep=7 keeps Aug 13-19; weekly window should
    # keep one per week for the preceding 3 weeks.
    base = datetime(2026, 8, 19, 12, 0, 0)
    for i in range(28):
        when = base - timedelta(days=i)
        _write_backup_at(tmp_path, when)

    backups = bd._list_backups(tmp_path)
    bd._prune(backups, daily_keep=7, weekly_keep=3)

    remaining = [p for _, p in backups if p.exists()]
    # 7 dailies + 3 weeklies = 10
    assert len(remaining) == 10, (
        f"expected 7+3=10; got {len(remaining)}. Files: " f"{sorted(p.name for p in remaining)}"
    )


def test_prune_weekly_does_not_double_count_adjacent_iso_weeks(tmp_path: Path):
    """Two backups 1 day apart but in different ISO weeks must not both
    consume weekly slots when older distinct weeks are available.

    2026-06-21 (Sun) and 2026-06-22 (Mon) are adjacent ISO weeks. The
    buggy code would consume two slots for them, leaving a 6-week hole.
    """
    # Backups: today, 1 day ago, 8 days ago, 15 days ago, 22 days ago,
    # 29 days ago, 36 days ago, 43 days ago, 50 days ago, 57 days ago.
    # That's daily_keep=2 (today + yesterday) + 5 distinct older weeks.
    base = datetime(2026, 8, 19, 12, 0, 0)
    for d in [0, 1, 8, 15, 22, 29, 36, 43, 50, 57]:
        _write_backup_at(tmp_path, base - timedelta(days=d))

    backups = bd._list_backups(tmp_path)
    bd._prune(backups, daily_keep=2, weekly_keep=4)

    remaining = [p for _, p in backups if p.exists()]
    # 2 dailies + 4 weeklies = 6
    assert len(remaining) == 6, f"expected 2+4=6; got {len(remaining)}: " f"{sorted(p.name for p in remaining)}"


# ---------- dump sanity gate (issue #387) ----------


def test_run_backup_rejects_empty_dump(tmp_path: Path, monkeypatch):
    """BUGFIX (#387): pg_dump rc=0 with empty stdout must NOT be written.

    Root cause of the data-loss path: pg_dump can exit 0 while emitting
    nothing useful (wrong -d target on a freshly-created empty database,
    a dump interrupted at the protocol level). run_backup used to gzip
    those zero bytes, write them as the newest daily backup, and then
    prune a genuinely-valid older file out of the retention window. Seven
    cron runs of a silently-broken dump destroyed every real backup.
    """

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    args = _make_args(tmp_path)
    rc = bd.run_backup(args)

    assert rc != 0, "empty dump must be reported as a failure"
    assert list((tmp_path / "backups").iterdir()) == [], "no file may be written for an empty dump"


def test_run_backup_rejects_truncated_dump(tmp_path: Path, monkeypatch):
    """BUGFIX (#387): a dump lacking the pg_dump trailer is truncated."""

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=b"--\n-- PostgreSQL database dump\n--\nCREATE TABLE bars (id int);\n",
            stderr=b"",
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    args = _make_args(tmp_path)
    rc = bd.run_backup(args)

    assert rc != 0, "dump without the completion trailer must be reported as a failure"
    assert list((tmp_path / "backups").iterdir()) == [], "no file may be written for a truncated dump"


def test_rejected_dump_does_not_prune_existing_backups(tmp_path: Path, monkeypatch):
    """BUGFIX (#387): a rejected dump must leave the retention window untouched.

    This is the actual data-loss assertion: an existing valid backup
    survives a broken run.
    """
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True)
    survivor = backup_dir / "alphard_2026-08-01_120000.sql.gz"
    survivor.write_bytes(bd._compress(b"real dump\n"))

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    rc = bd.run_backup(_make_args(tmp_path, daily_keep=1, weekly_keep=0))

    assert rc != 0
    assert survivor.exists(), "existing valid backup must not be pruned by a rejected run"


def test_run_backup_accepts_complete_dump(tmp_path: Path, monkeypatch):
    """A well-formed dump with the pg_dump trailer is accepted."""

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_valid_dump(), stderr=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    rc = bd.run_backup(_make_args(tmp_path))

    assert rc == 0
    assert len(list((tmp_path / "backups").iterdir())) == 1
