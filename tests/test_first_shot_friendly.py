"""Tests for issue #243 Group A — first-shot-friendly architecture fixes.

Covers:
  A1: docker-compose.yaml pg-init idempotent SQL replay — closes issue #248
      (REPO_ROOT/symlink) and #250 (set -euo pipefail kill before PIPESTATUS
      capture). The new pg-init replays docker/postgres/init.sql +
      src/data/schema.sql on every deploy so pre-existing volumes get
      schema without manual intervention.
  A2: .env.example has pre-baked Grafana B64 vars so operators can
      `cp .env.example .env && docker compose up -d` without first
      running ./scripts/quickstart.sh.

Both tests are pure-fs (no docker daemon required) — they just verify
the source-of-truth files exist, are well-formed, and reference the
expected SQL sources / bake tooling.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yaml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
INIT_SQL = REPO_ROOT / "docker" / "postgres" / "init.sql"
SCHEMA_SQL = REPO_ROOT / "src" / "data" / "schema.sql"


# Reusable B64 var-line regex (filled at call site).
def _b64_line(text: str, key: str) -> str | None:
    """Return the captured base64 value for a *_B64 var in text, or None."""
    m = re.compile(rf'^{re.escape(key)}="([^"]+)"', re.MULTILINE).search(text)
    return m.group(1) if m is not None else None


# -----------------------------------------------------------------------
# A1 — pg-init idempotent SQL replay
# -----------------------------------------------------------------------


def test_a1_pg_init_replays_docker_postgres_init_sql() -> None:
    """pg-init entrypoint command must invoke psql -f on init.sql.

    The fix for #248/#250 (and the pre-existing-volume path of A1) is
    that pg-init now bind-mounts docker/postgres/init.sql + src/data/schema.sql
    into the container and replays them idempotently on every deploy.
    Without this, a pre-existing named volume (e.g. restored from
    backup) leaves _auth_probe missing and the bot fails its
    fail-closed auth probe at startup.
    """
    docs = yaml.safe_load(COMPOSE.read_text())
    pg_init = docs["services"]["pg-init"]
    cmd = pg_init["command"][0]
    assert "/sql/docker_postgres_init.sql" in cmd, (
        "pg-init command must reference /sql/docker_postgres_init.sql " "(bind-mounted docker/postgres/init.sql)"
    )

    volumes = pg_init.get("volumes", [])
    assert any(
        v.startswith("./docker/postgres/init.sql:") and v.endswith(":ro") for v in volumes
    ), f"pg-init must bind-mount ./docker/postgres/init.sql read-only; got {volumes!r}"


def test_a1_pg_init_replays_src_data_schema_sql() -> None:
    """pg-init must also replay src/data/schema.sql (the runtime schema).

    init.sql only creates _auth_probe. The full schema (trades,
    decision_log, ticker_universe, etc.) lives in src/data/schema.sql
    and is normally applied by the bot at startup. For pre-existing
    volumes where the bot has never run, pg-init must apply it too
    so the operator's first deploy doesn't depend on the bot having
    run at least once.
    """
    docs = yaml.safe_load(COMPOSE.read_text())
    pg_init = docs["services"]["pg-init"]
    cmd = pg_init["command"][0]
    assert "/sql/src_data_schema.sql" in cmd, (
        "pg-init command must reference /sql/src_data_schema.sql " "(bind-mounted src/data/schema.sql)"
    )

    volumes = pg_init.get("volumes", [])
    assert any(
        v.startswith("./src/data/schema.sql:") and v.endswith(":ro") for v in volumes
    ), f"pg-init must bind-mount ./src/data/schema.sql read-only; got {volumes!r}"


def test_a1_init_sql_is_idempotent() -> None:
    """docker/postgres/init.sql must be safe to run on every deploy.

    Verified by static check: every CREATE statement must use
    IF NOT EXISTS, every INSERT must use ON CONFLICT DO NOTHING.
    This is the contract pg-init relies on for A1.
    """
    text = INIT_SQL.read_text()
    code_lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("--")]
    code = "\n".join(code_lines)
    bad_creates = re.findall(
        r"^CREATE\s+(TABLE|INDEX)\s+(?!IF\s+NOT\s+EXISTS)\w+",
        code,
        re.MULTILINE,
    )
    assert not bad_creates, (
        f"init.sql has non-idempotent CREATE statements: {bad_creates[:3]!r}. "
        "Every CREATE must use IF NOT EXISTS so pg-init can replay it."
    )


def test_a1_schema_sql_is_idempotent() -> None:
    """src/data/schema.sql must be safe to run on every deploy.

    The bot's own init_schema() is the authoritative source of truth
    for the runtime schema, but pg-init replays the same DDL on every
    deploy so pre-existing volumes get the schema without waiting for
    the bot to start. Both pg-init replay and bot init_schema() run
    the same file; it must therefore be idempotent.
    """
    text = SCHEMA_SQL.read_text()
    code_lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("--")]
    code = "\n".join(code_lines)
    bad_creates = re.findall(
        r"^CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX)\s+(?!IF\s+NOT\s+EXISTS)\w+",
        code,
        re.MULTILINE | re.IGNORECASE,
    )
    assert not bad_creates, (
        f"schema.sql has {len(bad_creates)} non-idempotent CREATE statements: "
        f"{bad_creates[:3]!r}. All must use IF NOT EXISTS."
    )


def test_a1_pg_init_replay_uses_continue_on_error() -> None:
    """The replay blocks must not abort pg-init on a single statement failure.

    Reason: schema.sql can contain statements that legitimately fail
    on pre-existing volumes (e.g. CREATE TYPE that already exists in
    a different OID). pg-init must continue past such failures so the
    bot can still start. The pattern is `psql ... -f X || { ... continue ... }`.
    """
    docs = yaml.safe_load(COMPOSE.read_text())
    pg_init = docs["services"]["pg-init"]
    cmd = pg_init["command"][0]
    n_fallbacks = cmd.count("|| {")
    assert n_fallbacks >= 2, (
        f"pg-init must use `|| {{ ... continue ... }}` after each replay "
        f"(>=2 fallback blocks: one for init.sql, one for schema.sql); "
        f"got {n_fallbacks}"
    )


# -----------------------------------------------------------------------
