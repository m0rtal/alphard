"""Regression tests for issue #347 — drop pg-init, rely on init_schema().

Issue #347 was filed because ``scripts/pre_pr_smoke.sh`` could not bring up
``alphard-bot`` on this LXC host: ``pg-init``'s single-file bind-mounts
(``./docker/postgres/init.sql:/sql/docker_postgres_init.sql:ro`` and
``./src/data/schema.sql:/sql/src_data_schema.sql:ro``) render as
**directories** on LXC, so the schema is never applied and ``_auth_probe``
is missing. The bot's entrypoint guard then fails closed and the smoke
gate refuses the sentinel, blocking every PR push on this host.

Fix (per issue #347 recommendation 1): **drop pg-init entirely**. The
bot's ``init_schema()`` is already idempotent and runs from
``docker/entrypoint.sh`` on every boot. To make the bot actually start on
a fresh volume (where ``_auth_probe`` doesn't exist yet), ``init_schema()``
must run **before** ``auth_probe()`` so the probe table exists by the time
the probe runs.

These tests pin the post-fix contract:

  1. ``docker-compose.yaml`` MUST NOT define a ``pg-init`` service.
  2. ``docker-compose.yaml`` MUST NOT reference ``pg-init`` from
     ``alphard-bot.depends_on``.
  3. ``docker/entrypoint.sh`` MUST call ``init_schema()`` (or an
     idempotent equivalent) BEFORE ``auth_probe()``.
  4. ``scripts/pre_pr_smoke.sh`` MUST NOT bring up ``pg-init``.
  5. ``src/data/schema.sql`` MUST still create ``_auth_probe`` (so a
     fresh volume is fully covered by ``init_schema()`` alone — pg-init
     is not doing it any more).

Tests are pure-fs (no docker daemon) — they just verify the
source-of-truth files have the expected shape.
"""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yaml"
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"
PRE_PR_SMOKE = REPO_ROOT / "scripts" / "pre_pr_smoke.sh"
SCHEMA_SQL = REPO_ROOT / "src" / "data" / "schema.sql"


def test_compose_no_pg_init_service() -> None:
    """docker-compose.yaml MUST NOT define pg-init (issue #347 fix).

    Pre-fix, pg-init bind-mounted single SQL files that render as
    directories on LXC hosts, so the schema was never applied and
    _auth_probe was missing. Dropping pg-init removes the broken
    bind-mount entirely; init_schema() in the bot's entrypoint
    replaces it.
    """
    docs = yaml.safe_load(COMPOSE.read_text())
    services = docs.get("services", {})
    assert "pg-init" not in services, (
        "docker-compose.yaml still defines a 'pg-init' service. "
        "Issue #347: pg-init's single-file bind-mounts render as "
        "directories on LXC, so init.sql/schema.sql never apply. "
        "Drop pg-init — the bot's init_schema() is the authoritative "
        "schema source."
    )


def test_compose_alphard_bot_no_pg_init_dep() -> None:
    """alphard-bot.depends_on MUST NOT reference pg-init.

    With pg-init removed, listing it in alphard-bot's depends_on would
    make compose try to start a missing service and fail with
    "service pg-init not found". Pin its absence so a future
    "let's add it back" PR is blocked unless it also re-introduces
    the service.
    """
    docs = yaml.safe_load(COMPOSE.read_text())
    bot = docs["services"]["alphard-bot"]
    deps = bot.get("depends_on", [])
    # depends_on can be a list or a dict; normalise to a list of names.
    if isinstance(deps, dict):
        dep_names = list(deps.keys())
    else:
        dep_names = list(deps)
    assert "pg-init" not in dep_names, (
        f"alphard-bot.depends_on still references pg-init ({dep_names!r}). "
        "Issue #347: pg-init was dropped; remove the dead dep."
    )


def test_entrypoint_runs_init_schema_before_auth_probe() -> None:
    """docker/entrypoint.sh MUST call init_schema() BEFORE auth_probe().

    Issue #347 root cause: auth_probe() INSERTs into _auth_probe, but
    _auth_probe is created by init_schema() (which used to run AFTER
    the probe). On a fresh volume the probe fails because the table
    doesn't exist yet, and the bot exits fail-closed.

    Pre-fix ordering was: probe → init_schema. Post-fix ordering
    must be: init_schema → probe.

    The check looks for the literal Python invocations in the
    entrypoint shell script and asserts init_schema's line number is
    strictly less than auth_probe's. Comment-only mentions are ignored
    by looking for the actual `init_schema()` call (with parentheses,
    no leading '#').
    """
    text = ENTRYPOINT.read_text()
    # Match the Python -c block that calls init_schema(). We ignore
    # comment lines (starting with '#') so the descriptive BUGFIX
    # comment block doesn't show up as a call site.
    init_lines = [
        line_no
        for line_no, line in enumerate(text.splitlines(), start=1)
        if "init_schema()" in line and not line.lstrip().startswith("#")
    ]
    # Match the Python -c block that calls auth_probe(). Same
    # comment-strip.
    probe_lines = [
        line_no
        for line_no, line in enumerate(text.splitlines(), start=1)
        if "auth_probe(" in line and not line.lstrip().startswith("#")
    ]
    assert init_lines, (
        "docker/entrypoint.sh does not call init_schema() at all. "
        "Issue #347 fix requires init_schema() to run from the "
        "entrypoint so a fresh volume gets the schema before the "
        "auth probe."
    )
    assert probe_lines, (
        "docker/entrypoint.sh does not call auth_probe(). Did the "
        "fail-closed smoke gate get removed? Re-add it (post-init_schema)."
    )
    # The first init_schema call must come before the first auth_probe
    # call. Use `min` of each in case of multiple call sites.
    assert min(init_lines) < min(probe_lines), (
        f"init_schema() runs on line(s) {init_lines}; auth_probe() on "
        f"line(s) {probe_lines}. init_schema MUST run first (it creates "
        "_auth_probe which auth_probe INSERTs into). "
        "Issue #347: pre-fix ordering fails on fresh volumes."
    )


def test_pre_pr_smoke_drops_pg_init() -> None:
    """scripts/pre_pr_smoke.sh MUST NOT bring up pg-init.

    The pre-PR smoke gate runs ``docker compose up -d ...``; with pg-init
    gone, listing it in the bring-up would fail with "no such service:
    pg-init". The fix is to drop it from the bring-up list.

    Pin the absence so a future "let me add it back" tweak is blocked.
    We scan every non-comment line for a `up -d` bring-up pattern and
    assert none of them list pg-init among the service names.
    """
    text = PRE_PR_SMOKE.read_text()
    bad = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Heuristic: any line that has the `up -d` bring-up idiom
        # (with -d as its own token, not embedded in another flag).
        if " up -d " not in line and not line.endswith(" up -d"):
            continue
        # Drop bash redirections / continuations and split on
        # whitespace. Service names appear as tokens after `up -d`.
        tokens = line.split()
        if "up" not in tokens or "-d" not in tokens:
            continue
        up_idx = tokens.index("up")
        # Service names are tokens after `-d` until any shell
        # redirection (>& or /dev/null) or pipe.
        services: list[str] = []
        for tok in tokens[up_idx + 2 :]:
            if tok.startswith(">"):
                break
            services.append(tok)
        if "pg-init" in services:
            bad.append((line_no, line.rstrip(), services))
    assert not bad, (
        f"scripts/pre_pr_smoke.sh still references pg-init in a "
        f"`compose ... up -d` invocation: {bad!r}. Issue #347: pg-init "
        "was dropped from docker-compose.yaml; remove it from the smoke "
        "bring-up too."
    )


def test_schema_sql_creates_auth_probe() -> None:
    """src/data/schema.sql MUST still create _auth_probe.

    Pre-#347, _auth_probe was created by pg-init's docker/postgres/init.sql
    replay (which was the broken part). Post-#347, pg-init is gone and
    init_schema() is the only path that creates _auth_probe, so
    src/data/schema.sql must contain the table definition. If someone
    ever moves _auth_probe creation back into a separate file, this
    test fires — and rightly so: pg-init's whole problem was "two
    files to keep in sync, both bind-mounted".
    """
    text = SCHEMA_SQL.read_text()
    assert "CREATE TABLE IF NOT EXISTS _auth_probe" in text, (
        "src/data/schema.sql no longer creates _auth_probe. Issue #347: "
        "pg-init (which used to create it via init.sql) is gone; "
        "init_schema() reads schema.sql — if _auth_probe isn't there, "
        "auth_probe() fails on every fresh volume and the bot won't "
        "start. Restore the _auth_probe CREATE TABLE in schema.sql."
    )
