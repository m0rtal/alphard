"""Daily PostgreSQL backup script (Phase 2.9 step 1).

Why pg_dump (not filesystem copy)?
- A filesystem copy of /var/lib/postgresql/data is inconsistent unless
  Postgres is shut down. pg_dump is the official, online-safe way to
  snapshot a Postgres database.
- The output is a single SQL file that's portable to any Postgres version.

Default target:
- /mnt/appdata/alphard-backups/alphard_YYYY-MM-DD_HHMMSS.sql
- Compression: gzip, ~3-5x smaller than raw pg_dump output for the
  alphard schema (lots of repetitive INSERTs of OHLCV rows).
- Retention: 7 daily + 4 weekly. Weekly = last backup of the week,
  kept for 4 weeks. Older files are pruned.

Phase 2.9 step 1 = this script.
Phase 2.9 step 2 (out of scope here) = sync to S3-compatible storage
when user provides credentials.

Tests:
- tests/test_backup_database.py: mock subprocess.run, verify the
  shell-out contract (pg_dump with correct args), verify retention
  pruning logic, verify directory creation, verify gzip handling.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("alphard.backup")


# Defaults are for .107 deployment. Override via env or CLI.
DEFAULT_CONTAINER = os.environ.get("ALPHARD_PG_CONTAINER", "alphard-postgres")
DEFAULT_BACKUP_DIR = os.environ.get("ALPHARD_BACKUP_DIR", "/mnt/appdata/alphard-backups")
DEFAULT_DB_NAME = os.environ.get("ALPHARD_DB_NAME", "alphard")
DEFAULT_DB_USER = os.environ.get("ALPHARD_POSTGRES_USER", "alphard")

# Retention: keep N most-recent files per type (daily / weekly).
DEFAULT_DAILY_KEEP = 7
DEFAULT_WEEKLY_KEEP = 4

# Dump sanity gate (issue #387). pg_dump can exit 0 while emitting an
# empty or truncated dump — a wrong -d target on a freshly-created
# database, or a stream cut short at the protocol level. Writing such a
# dump as the newest daily backup pushes a genuinely-valid older file out
# of the retention window, so seven broken cron runs destroy every real
# backup. pg_dump --format=plain always terminates a complete dump with
# this trailer, so its absence means the dump is not usable.
PG_DUMP_TRAILER = b"PostgreSQL database dump complete"

# rc for "pg_dump exited 0 but the dump is unusable". Distinct from
# pg_dump's own exit codes and from 124 (timeout).
RC_DUMP_REJECTED = 3

# Filename pattern: alphard_YYYY-MM-DD_HHMMSS.sql.gz
# Strict match requires date+time in name; the date_time portion is
# captured as group(1) so _parse_filename_timestamp can extract it.
# Loose match (alphard_*_*) is needed for files with corrupt names
# so the mtime fallback is exercised by tests — those have group(1)=None.
# Both alternatives are bounded: the loose `[a-zA-Z]+_[a-zA-Z0-9]+`
# uses two short segments so it can't swallow the .sql.gz suffix that
# the strict pattern anchors to.
FILENAME_RE = re.compile(r"alphard_(?P<strict>\d{4}-\d{2}-\d{2}_\d{6}|[a-zA-Z]+_[a-zA-Z0-9]+)\.sql\.gz$")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--container",
        default=DEFAULT_CONTAINER,
        help=f"Docker container running Postgres (default: {DEFAULT_CONTAINER})",
    )
    parser.add_argument(
        "--backup-dir",
        default=DEFAULT_BACKUP_DIR,
        help=f"Where to write backup files (default: {DEFAULT_BACKUP_DIR})",
    )
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME, help="Database name")
    parser.add_argument("--db-user", default=DEFAULT_DB_USER, help="Database user")
    parser.add_argument(
        "--daily-keep",
        type=int,
        default=DEFAULT_DAILY_KEEP,
        help=f"Number of daily backups to retain (default: {DEFAULT_DAILY_KEEP})",
    )
    parser.add_argument(
        "--weekly-keep",
        type=int,
        default=DEFAULT_WEEKLY_KEEP,
        help=(
            f"Number of weekly backups to retain beyond the daily window "
            f"(default: {DEFAULT_WEEKLY_KEEP}). Weekly = last backup of the ISO week."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the actions that would be taken without invoking docker or writing files",
    )
    return parser.parse_args()


def _run_pg_dump(container: str, db_name: str, db_user: str) -> bytes:
    """Run pg_dump via docker exec and return the raw SQL bytes.

    Raises subprocess.CalledProcessError if docker exec fails.
    Uses docker exec instead of direct connection because:
    - Backup runs from cron/operator shell, not from the bot container.
    - Postgres may be on a different host (.107) reachable via Docker
      socket from the operator host.
    - psql password handling is delegated to the container env, so we
      don't need to pass POSTGRES_PASSWORD over the wire.
    """
    cmd = [
        "docker",
        "exec",
        "-i",
        container,
        "pg_dump",
        "-U",
        db_user,
        "-d",
        db_name,
        "--no-owner",
        "--no-privileges",
        "--format=plain",
        "--encoding=UTF8",
    ]
    logger.info("running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        check=True,
        timeout=600,  # 10 min hard cap; longer = stuck
    )
    if result.stderr:
        # pg_dump writes notices to stderr; surface as info, not error.
        logger.info("pg_dump stderr (notices): %s", result.stderr.decode("utf-8", "replace"))
    return result.stdout


def _compress(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6)


def _dump_rejection_reason(sql: bytes) -> str | None:
    """Return why a pg_dump payload is unusable, or None if it is sound.

    Guards the data-loss path in issue #387: a dump that pg_dump exited 0
    on but that is empty or cut short must never be written, because
    writing it prunes a valid older backup out of the retention window.
    """
    if not sql:
        return "pg_dump exited 0 but produced no output"

    if PG_DUMP_TRAILER not in sql:
        return f"pg_dump output is missing the completion trailer ({PG_DUMP_TRAILER.decode()}) — dump is truncated"

    return None


def _backup_path(backup_dir: Path, when: datetime) -> Path:
    """Compute the canonical filename for a backup timestamp."""
    return backup_dir / f"alphard_{when:%Y-%m-%d_%H%M%S}.sql.gz"


def _list_backups(backup_dir: Path) -> list[tuple[datetime, Path]]:
    """List existing backups sorted by mtime DESC. Returns (datetime, path) tuples."""
    if not backup_dir.exists():
        return []
    out: list[tuple[datetime, Path]] = []
    for p in backup_dir.iterdir():
        m = FILENAME_RE.match(p.name)
        if not m:
            continue
        # Try parsing the strict YYYY-MM-DD_HHMMSS suffix.
        # group("strict") captures the date+time for canonical names;
        # for loose matches (corrupt names) it's None and we fall back
        # to the file's mtime.
        when = _parse_filename_timestamp(m, p)
        out.append((when, p))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _parse_filename_timestamp(m: "re.Match[str]", fallback_path: Path) -> datetime:
    """Extract a timestamp from a FILENAME_RE match.

    Strict pattern captures `2026-08-19_120000` as group("strict");
    loose pattern (for files with corrupt names) has group("strict")=None
    and we fall back to the file's mtime.
    """
    raw = m.group("strict")
    if raw and re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}", raw):
        try:
            date_str, time_str = raw.split("_", 1)
            return datetime.strptime(f"{date_str}_{time_str}", "%Y-%m-%d_%H%M%S")
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback_path.stat().st_mtime)


def _prune(
    backups: list[tuple[datetime, Path]],
    daily_keep: int,
    weekly_keep: int,
) -> list[Path]:
    """Delete old backups beyond the retention window.

    Strategy:
    - daily_keep <= 0: delete all (no retention).
    - Otherwise: keep the most recent `daily_keep` unconditionally. Among
      older backups, keep the most recent per ISO week for `weekly_keep`
      weeks back from the current week. Delete everything else.

    Returns the list of paths that were actually deleted from disk.
    """
    if daily_keep <= 0:
        return _delete_all(backups)

    keep: set[Path] = set()

    # Daily window: most recent N. ``backups`` is already sorted newest-first
    # (see _list_backups), so the head is the most recent.
    for _, p in backups[:daily_keep]:
        keep.add(p)

    # Weekly window: among backups NOT already kept daily, keep the most
    # recent per ISO week for `weekly_keep` weeks back from the current
    # week. Iterate newest-first among the older backups so the budget
    # collects the *most recent* older weeks, not the oldest (issue #41).
    # Only meaningful if there are backups older than the daily window.
    older = [b for b in backups[daily_keep:] if b[1] not in keep]
    if older and weekly_keep > 0:
        seen_weeks: set[tuple[int, int]] = set()
        kept = 0
        for when, p in older:
            year, week, _ = when.isocalendar()
            wk = (year, week)
            if wk in seen_weeks:
                continue
            seen_weeks.add(wk)
            keep.add(p)
            kept += 1
            if kept >= weekly_keep:
                break

    return _delete_not_in(backups, keep)


def _delete_all(backups: list[tuple[datetime, Path]]) -> list[Path]:
    """Delete every backup in the list. Returns the paths that were deleted."""
    deleted: list[Path] = []
    for _, p in backups:
        try:
            p.unlink()
            deleted.append(p)
        except FileNotFoundError:
            pass
    return deleted


def _delete_not_in(backups: list[tuple[datetime, Path]], keep: set[Path]) -> list[Path]:
    """Delete every backup not in `keep`. Returns the paths that were deleted."""
    deleted: list[Path] = []
    for _, p in backups:
        if p not in keep:
            try:
                p.unlink()
                deleted.append(p)
            except FileNotFoundError:
                pass
    return deleted


def run_backup(args: argparse.Namespace) -> int:
    """Run a single backup cycle. Returns 0 on success, non-zero on error."""
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    out_path = _backup_path(backup_dir, now)

    if args.dry_run:
        logger.info("[dry-run] would run pg_dump in container %s", args.container)
        logger.info("[dry-run] would write compressed dump to %s", out_path)
        logger.info("[dry-run] would prune backups in %s", backup_dir)
        return 0

    try:
        sql = _run_pg_dump(args.container, args.db_name, args.db_user)
    except subprocess.CalledProcessError as exc:
        logger.error("pg_dump failed (rc=%d): %s", exc.returncode, exc.stderr.decode("utf-8", "replace"))
        return exc.returncode or 1
    except subprocess.TimeoutExpired:
        logger.error("pg_dump timeout after 600s — container %s may be hung", args.container)
        return 124

    rejection = _dump_rejection_reason(sql)
    if rejection:
        logger.error("REFUSING to write backup: %s", rejection)
        logger.error("existing backups in %s left untouched (not pruned)", backup_dir)
        return RC_DUMP_REJECTED

    compressed = _compress(sql)
    out_path.write_bytes(compressed)
    logger.info("wrote backup: %s (%d bytes, gzip)", out_path, len(compressed))

    deleted = _prune(_list_backups(backup_dir), args.daily_keep, args.weekly_keep)
    for p in deleted:
        logger.info("pruned old backup: %s", p)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    return run_backup(args)


if __name__ == "__main__":
    sys.exit(main())
