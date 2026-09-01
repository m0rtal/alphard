"""Regression tests for issue #382 — quickstart.sh conflict precheck.

`docker-compose.yaml` hardcodes `container_name:` for the six long-running
services. PR #380 scoped `scripts/pre_pr_smoke.sh` away from those literal
names via a per-PID `COMPOSE_PROJECT_NAME` override, but `quickstart.sh`
still runs `docker compose up -d` unscoped. On a host that already holds a
literal `alphard-bot` container owned by another compose project, compose
aborts with `Conflict. The container name "/alphard-bot" is already in use`
— an opaque failure for a first-time operator.

Contract asserted here:
  - a foreign-owned alphard-* container makes quickstart exit 9 with an
    actionable message naming the container, the owning project, and the
    opt-in escape hatch;
  - ALLOW_QUICKSTART_OVERWRITE=1 downgrades the refusal to a warning
    (same shape as ALLOW_NONLOCAL_SMOKE=1 in pre_pr_smoke.sh);
  - a container already owned by the quickstart project itself is a
    re-run, not a conflict, and must not trip the guard.

The fake docker here extends the FAKE_DOCKER_SCRIPT from
tests/test_quickstart.py with an `inspect` branch that reports a
pre-existing container, driven by env vars so each test can pick which
containers "exist" and who owns them.
"""

import os
import subprocess
from pathlib import Path

from tests.test_quickstart import _qs_link, _quickstart_skel

REPO_ROOT = Path(__file__).resolve().parent.parent

QUICKSTART_PROJECT = "alphard"
CONFLICT_EXIT_CODE = 9

# Fake docker whose `inspect` branch is driven by two env vars:
#   FAKE_EXISTING   space-separated container names that exist
#   FAKE_OWNER      value reported for the compose-project label
# Every other subcommand behaves like the base fake: sanity checks pass,
# `compose up` fails so we never need a real daemon.
# `docker inspect NAME --format TPL` passes the template as a SEPARATE arg,
# so a naive "last non-flag wins" scan captures the template, not the name.
# Skip the value that follows a bare --format, and take the FIRST remaining
# positional as the container name.
FAKE_DOCKER_WITH_EXISTING = """#!/bin/sh
_name=""
_skip=0
_first=1
for a in "$@"; do
    if [ "$_skip" = "1" ]; then _skip=0; continue; fi
    case "$a" in
      inspect) continue ;;
      --format) _skip=1; continue ;;
      -*) continue ;;
    esac
    if [ "$_first" = "1" ]; then _name="$a"; _first=0; fi
done

case "$1" in
  version|--version)
    echo "Client: Version: 29.1.3 Server: Dummy/0.0.0"
    exit 0
    ;;
  info)
    echo "Server: Server Version: 29.1.3 Storage Driver: vfs"
    exit 0
    ;;
  compose)
    for a in "$@"; do
        if [ "$a" = "up" ]; then
            echo "fake compose up -d (always fails in test mode)" >&2
            exit 1
        fi
    done
    exit 0
    ;;
  inspect)
    for e in ${FAKE_EXISTING:-}; do
        if [ "$e" = "$_name" ]; then
            case "$*" in
              *compose.project*) echo "${FAKE_OWNER:-someone-else}" ;;
              *) echo "running" ;;
            esac
            exit 0
        fi
    done
    echo "Error: No such object: $_name" >&2
    exit 1
    ;;
  ps)
    exit 0
    ;;
  *)
    echo "fake docker $*"
    exit 0
    ;;
esac
"""


def _run(
    env_dir: Path,
    tmp_path: Path,
    existing: str,
    owner: str = "someone-else",
    allow_overwrite: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run quickstart.sh against a fake docker that reports `existing`
    containers as up and owned by `owner`.
    """
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir(exist_ok=True)
    fake_docker = fake_dir / "docker"
    fake_docker.write_text(FAKE_DOCKER_WITH_EXISTING, encoding="utf-8")
    fake_docker.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_dir}:{env['PATH']}"
    env["HOME"] = str(tmp_path)
    env["ALPHARD_QUIET"] = "1"
    env["ALPHARD_TIMEOUT_SEC"] = "0"
    env["FAKE_EXISTING"] = existing
    env["FAKE_OWNER"] = owner

    if allow_overwrite:
        env["ALLOW_QUICKSTART_OVERWRITE"] = "1"

    return subprocess.run(
        ["/bin/bash", str(_qs_link(env_dir))],
        cwd=str(env_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_refuses_foreign_owned_container(tmp_path: Path) -> None:
    """A literal alphard-bot owned by another compose project must abort
    the bootstrap with exit 9 before `docker compose up` runs.
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)

    r = _run(env_dir, tmp_path, existing="alphard-bot", owner="alphard-smoke-4242")

    combined = r.stdout + r.stderr
    assert r.returncode == CONFLICT_EXIT_CODE, (
        f"expected exit {CONFLICT_EXIT_CODE} on container conflict; " f"got {r.returncode}\n{combined[-800:]}"
    )
    assert "alphard-bot" in combined, f"error must name the conflicting container:\n{combined[-800:]}"
    assert "alphard-smoke-4242" in combined, f"error must name the owning project:\n{combined[-800:]}"
    assert "ALLOW_QUICKSTART_OVERWRITE" in combined, f"error must name the opt-in escape hatch:\n{combined[-800:]}"
    assert "fake compose up" not in combined, "guard must run BEFORE docker compose up"


def test_refuses_foreign_owned_postgres(tmp_path: Path) -> None:
    """The guard covers every hardcoded container_name, not just the bot."""
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)

    r = _run(env_dir, tmp_path, existing="alphard-postgres", owner="legacy-stack")

    combined = r.stdout + r.stderr
    assert r.returncode == CONFLICT_EXIT_CODE, f"got {r.returncode}\n{combined[-800:]}"
    assert "alphard-postgres" in combined, combined[-800:]


def test_overwrite_optin_downgrades_to_warning(tmp_path: Path) -> None:
    """ALLOW_QUICKSTART_OVERWRITE=1 proceeds past the conflict, warning
    loudly — same shape as ALLOW_NONLOCAL_SMOKE=1 in pre_pr_smoke.sh.

    The fake `compose up` still fails, so the script exits 2 from the
    compose stage; the point is that it got PAST the guard.
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)

    r = _run(
        env_dir,
        tmp_path,
        existing="alphard-bot",
        owner="someone-else",
        allow_overwrite=True,
    )

    combined = r.stdout + r.stderr
    assert (
        r.returncode != CONFLICT_EXIT_CODE
    ), f"opt-in must bypass the guard, not exit {CONFLICT_EXIT_CODE}:\n{combined[-800:]}"
    assert "WARN" in combined, f"opt-in must still warn loudly:\n{combined[-800:]}"
    assert "fake compose up" in combined, f"expected compose stage to be reached:\n{combined[-800:]}"


def test_own_project_container_is_a_rerun_not_a_conflict(tmp_path: Path) -> None:
    """quickstart.sh is documented as idempotent. A container already
    owned by the quickstart project itself is a re-run, so the guard must
    stay silent and let compose reconcile the stack.
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)

    r = _run(env_dir, tmp_path, existing="alphard-bot", owner=QUICKSTART_PROJECT)

    combined = r.stdout + r.stderr
    assert r.returncode != CONFLICT_EXIT_CODE, f"own-project container must not trip the guard:\n{combined[-800:]}"
    assert "fake compose up" in combined, f"expected compose stage to be reached:\n{combined[-800:]}"


def test_guard_is_documented_in_script_header(tmp_path: Path) -> None:
    """The knob must be discoverable in the run-time knobs block, not
    just in code — operators read the header, not the guard.
    """
    text = (REPO_ROOT / "scripts" / "quickstart.sh").read_text(encoding="utf-8")

    assert "ALLOW_QUICKSTART_OVERWRITE" in text
    knobs_block = text.split("# Why this script exists")[0]
    assert (
        "ALLOW_QUICKSTART_OVERWRITE" in knobs_block
    ), "ALLOW_QUICKSTART_OVERWRITE must be documented in the run-time knobs header block"
