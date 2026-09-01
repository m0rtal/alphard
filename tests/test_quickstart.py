"""Tests for scripts/quickstart.sh.

Subprocess-level tests. We invoke the bash script with a synthesized
.env / compose file under a tmp dir, set ALPHARD_QUIET=1 to suppress
progress output, and assert exit codes + on-disk artifacts.

We do NOT test the actual docker compose up stage — that requires a
real Docker daemon and is covered by the live smoke run after the PR
lands. The tests focus on the deterministic parts:
  - .env bootstrap (cp from .env.example, password auto-gen, refusal of
    empty / historical-literal GRAFANA_ADMIN_PASSWORD)
  - Grafana B64 bake (skip if already populated, run if missing)
  - Prometheus B64 bake (skip if already populated, run if missing)
  - exit code semantics: 0=ok, 2=early-validation-error, 1=compose failed
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QS = REPO_ROOT / "scripts" / "quickstart.sh"


def _quickstart_skel(
    tmp_path: Path,
    with_gpw: bool = True,
    skip_env: bool = False,
) -> Path:
    """Build a minimal repo under tmp_path/alphard/ that the script
    will accept. Returns the path to env_dir (=tmp_path/alphard).
    """
    env_dir = tmp_path / "alphard"
    env_dir.mkdir(exist_ok=True)

    example = (
        "TINKOFF_SANDBOX_TOKEN=\n"
        "TINKOFF_REAL_TOKEN=\n"
        "POSTGRES_USER=alphard\n"
        "POSTGRES_PASSWORD=\n"
        "POSTGRES_DB=alphard\n"
        "POSTGRES_TRUST_SUBNET=172.16.0.0/12\n"
        "REDIS_PASSWORD=\n"
        "GRAFANA_ADMIN_PASSWORD=ci_test_password_DO_NOT_USE_IN_PRODUCTION\n"
        "PROMETHEUS_RETENTION_DAYS=30\n"
    )
    (env_dir / ".env.example").write_text(example, encoding="utf-8")

    if not skip_env:
        if with_gpw:
            env = (
                "GRAFANA_ADMIN_PASSWORD=ci_test_password_DO_NOT_USE_IN_PRODUCTION\n"
                "POSTGRES_PASSWORD=\n"
                "REDIS_PASSWORD=\n"
            )
        else:
            env = "GRAFANA_ADMIN_PASSWORD=\n" "POSTGRES_PASSWORD=\n" "REDIS_PASSWORD=\n"
        (env_dir / ".env").write_text(env, encoding="utf-8")

    (env_dir / "docker-compose.yaml").write_text("# stub\n", encoding="utf-8")

    prom_dir = env_dir / "docker" / "prometheus"
    prom_dir.mkdir(parents=True, exist_ok=True)
    # The stub must satisfy quickstart.sh's issue #283 sanity check:
    # "docker/prometheus/prometheus.yml must contain alphard-bot:8765".
    (prom_dir / "prometheus.yml").write_text(
        "# prom stub for fixture\n"
        "scrape_configs:\n"
        "  - job_name: alphard-bot\n"
        "    static_configs:\n"
        "      - targets: ['alphard-bot:8765']\n",
        encoding="utf-8",
    )

    tools_dir = env_dir / "tools"
    tools_dir.mkdir(exist_ok=True)
    real_bake = REPO_ROOT / "tools" / "bake_grafana_env.py"
    if real_bake.exists():
        shutil.copy(real_bake, tools_dir / "bake_grafana_env.py")

    # Also copy docker/grafana/ (real repo has it; without it the bake
    # step fails because the source files are missing). This is the
    # closest we can get to a "clean host" — every other file in the
    # repo is here.
    grafana_src = REPO_ROOT / "docker" / "grafana"
    if grafana_src.exists():
        shutil.copytree(grafana_src, env_dir / "docker" / "grafana")

    return env_dir


def _qs_link(env_dir: Path) -> Path:
    """Materialise a copy of quickstart.sh inside env_dir/scripts/.

    We copy (NOT symlink) the real script because issues #248/#249
    require that REPO_ROOT be derived from the real path of the
    script, not the symlink. With a symlink, `readlink -f` (or our
    python3 equivalent) resolves to the original location, and the
    script would then operate on /root/projects/alphard instead of
    the isolated test_dir. Copying keeps the test self-contained.

    The name `_qs_link` is retained for backwards compat with
    existing call-sites; the function actually copies now.
    """
    tmp_scripts = env_dir / "scripts"
    tmp_scripts.mkdir(exist_ok=True)
    tmp_qs = tmp_scripts / "quickstart.sh"
    if tmp_qs.exists() or tmp_qs.is_symlink():
        tmp_qs.unlink()
    shutil.copy(QS, tmp_qs)
    os.chmod(tmp_qs, 0o755)
    return tmp_qs


# Fake docker: sanity checks (version/info/compose version) succeed;
# the actual `docker compose up` always fails so the script exits with
# non-zero AFTER the bake stages (3/5 Grafana + 4/5 Prometheus) have
# completed. This lets us test the bake + bootstrap logic without a
# real Docker daemon.
#
# IMPORTANT: the real quickstart.sh invokes `docker compose --profile=X up -d`,
# so the fake must match `up` regardless of positional args. The previous
# version used `case "$2" in ... up)` which only matched when up was the
# first arg after `compose` — the test suite silently passed because the
# script's `set -e` would die on the pipeline before reaching the
# PIPESTATUS check (issue #250).
FAKE_DOCKER_SCRIPT = """#!/bin/sh
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
    # Match 'compose up' regardless of preceding flags (e.g. --profile=X).
    _has_up=0
    for a in "$@"; do
        if [ "$a" = "up" ]; then _has_up=1; break; fi
    done
    if [ "$_has_up" = "1" ]; then
        echo "fake compose up -d (always fails in test mode)" >&2
        exit 1
    fi
    # Other compose subcommands (version, ps, logs, inspect) succeed.
    exit 0
    ;;
  inspect)
    # No containers exist on this fake daemon: compose never actually ran.
    # Real docker exits 1 with "No such object", which is what the issue
    # #382 conflict precheck relies on to distinguish "nothing running"
    # from "foreign container holds the literal name". The health gate
    # never reaches here because every caller sets ALPHARD_TIMEOUT_SEC=0.
    echo "Error: No such object" >&2
    exit 1
    ;;
  ps)
    # ALPHARD_SKIP_COMPOSE path prints `docker ps` — return empty.
    exit 0
    ;;
  *)
    echo "fake docker $*"
    exit 0
    ;;
esac
"""


def _run_with_fake_docker(env_dir: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run quickstart.sh with fake 'docker' that passes sanity but
    fails `docker compose up`. The script will exit non-zero AFTER
    the bake stages have run.

    Uses a short timeout so that if the bake stages (3/5 or 4/5)
    unexpectedly fail in the fixture environment, we surface that as
    a real subprocess failure rather than a pytest-level hang. The
    bake stages are pure-CPU and finish in <1s on any host.
    """
    fake_docker_dir = tmp_path / "fakebin"
    fake_docker_dir.mkdir(exist_ok=True)
    fake_docker = fake_docker_dir / "docker"
    fake_docker.write_text(FAKE_DOCKER_SCRIPT, encoding="utf-8")
    fake_docker.chmod(0o755)

    _qs_link(env_dir)

    env = os.environ.copy()
    env["PATH"] = f"{fake_docker_dir}:{env['PATH']}"
    env["ALPHARD_QUIET"] = "1"
    env["HOME"] = str(tmp_path)
    # Disable the health-gate loop entirely so we don't sit through
    # 36 sleep() iterations if the fake docker never reports healthy.
    # The fake docker only handles the compose CLI, NOT the per-
    # container inspect calls that the health gate uses.
    env["ALPHARD_TIMEOUT_SEC"] = "0"

    return subprocess.run(
        ["/bin/bash", str(_qs_link(env_dir))],
        cwd=str(env_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _run_skip_compose(env_dir: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run quickstart.sh with ALPHARD_SKIP_COMPOSE=1. The script
    bakes .env and exits 0 without ever invoking `docker compose up`.

    Useful for testing the bake + bootstrap logic without the
    pipeline-failure noise. The fake docker is still on PATH so the
    sanity checks (and the post-skip `docker ps` summary) succeed.

    NOTE: we do NOT set ALPHARD_QUIET=1 here — the SKIP_COMPOSE
    path emits its key diagnostic ("compose not invoked") via the
    info/ok helpers, and QUIET suppresses them. Callers that want
    to assert on stdout content should use this helper without QUIET.
    """
    fake_docker_dir = tmp_path / "fakebin"
    fake_docker_dir.mkdir(exist_ok=True)
    fake_docker = fake_docker_dir / "docker"
    fake_docker.write_text(FAKE_DOCKER_SCRIPT, encoding="utf-8")
    fake_docker.chmod(0o755)

    _qs_link(env_dir)

    env = os.environ.copy()
    env["PATH"] = f"{fake_docker_dir}:{env['PATH']}"
    env["HOME"] = str(tmp_path)
    env["ALPHARD_SKIP_COMPOSE"] = "1"
    # Belt-and-suspenders: even if SKIP_COMPOSE early-exit is
    # accidentally bypassed, ALPHARD_TIMEOUT_SEC=0 ensures the
    # health gate never enters its polling loop.
    env["ALPHARD_TIMEOUT_SEC"] = "0"

    return subprocess.run(
        ["/bin/bash", str(_qs_link(env_dir))],
        cwd=str(env_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _run_via_symlink(env_dir: Path, tmp_path: Path, with_skip_compose: bool = True) -> subprocess.CompletedProcess[str]:
    """Invoke quickstart.sh through a SYMLINK (not the real path),
    from a directory OUTSIDE the repo root. This is the regression
    test for issues #248/#249: BASH_SOURCE[0] is the path as
    invoked, so deriving REPO_ROOT from it directly used to point at
    the symlink's parent directory. The fix is `readlink -f` (with
    a python3 fallback). We assert the script correctly bakes .env
    into the REAL repo dir.
    """
    fake_docker_dir = tmp_path / "fakebin"
    fake_docker_dir.mkdir(exist_ok=True)
    fake_docker = fake_docker_dir / "docker"
    fake_docker.write_text(FAKE_DOCKER_SCRIPT, encoding="utf-8")
    fake_docker.chmod(0o755)

    # Materialise the real quickstart.sh inside env_dir/scripts/ first.
    # (This is the test fixture's "real repo" — without it the
    # symlink in /tmp/.../bin/qs has nothing to point at.)
    _qs_link(env_dir)

    # Symlink setup:
    #   tmp_path/bin/qs                       → env_dir/scripts/quickstart.sh
    #   cwd = tmp_path/elsewhere/             (NOT the repo root)
    sym_dir = tmp_path / "bin"
    sym_dir.mkdir(exist_ok=True)
    qs_link = sym_dir / "qs"
    if qs_link.exists() or qs_link.is_symlink():
        qs_link.unlink()
    qs_link.symlink_to(env_dir / "scripts" / "quickstart.sh")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)

    env = os.environ.copy()
    env["PATH"] = f"{fake_docker_dir}:{env['PATH']}"
    env["ALPHARD_QUIET"] = "1"
    env["HOME"] = str(tmp_path)
    if with_skip_compose:
        env["ALPHARD_SKIP_COMPOSE"] = "1"
    env["ALPHARD_TIMEOUT_SEC"] = "0"

    return subprocess.run(
        ["/bin/bash", str(qs_link)],
        cwd=str(elsewhere),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _run_no_docker(env_dir: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run quickstart.sh with docker removed from PATH (so the very
    first sanity check fails).
    """
    real_path = os.environ.get("PATH", "")
    cleaned = ":".join(p for p in real_path.split(":") if not (Path(p) / "docker").exists())
    _qs_link(env_dir)
    env = {"PATH": cleaned, "ALPHARD_QUIET": "1", "HOME": str(tmp_path)}
    return subprocess.run(
        ["/bin/bash", str(_qs_link(env_dir))],
        cwd=str(env_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _fill_all_b64(env_dir: Path) -> None:
    """Pre-populate all 4 Grafana B64 vars in .env. PROM_YML_B64 is no
    longer needed — the prometheus config is bind-mounted from the repo
    (issue #283)."""
    text = (env_dir / ".env").read_text() if (env_dir / ".env").exists() else ""
    pairs = [
        ("PROVISIONING_DATASOURCES_YML_B64", "ZmFrZS1kYXRhc291cmNlLXltbA=="),
        ("PROVISIONING_DASHBOARDS_PROVIDER_YML_B64", "ZmFrZS1wcm92aWRlcg=="),
        ("DASHBOARD_PHASE0_JSON_B64", "e30="),
        ("DASHBOARD_PHASE28_JSON_B64", "e30="),
    ]
    for k, v in pairs:
        pattern = f"^{k}=.*$"
        replacement = f'{k}="{v}"'
        if re.search(pattern, text, re.MULTILINE):
            text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
        else:
            text = text + f"\n{replacement}\n"
    (env_dir / ".env").write_text(text)


# ---- Tests ----


def test_script_exists_and_executable() -> None:
    assert QS.exists(), f"quickstart.sh not found at {QS}"
    mode = QS.stat().st_mode
    assert mode & stat.S_IXUSR, "quickstart.sh must be executable by owner"
    assert mode & stat.S_IXGRP, "quickstart.sh must be executable by group"
    assert mode & stat.S_IXOTH, "quickstart.sh must be executable by others"


def test_bash_syntax() -> None:
    """bash -n must report no errors."""
    r = subprocess.run(["bash", "-n", str(QS)], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n failed: {r.stderr}"


def test_docker_missing_exits_2(tmp_path: Path) -> None:
    """If docker is absent, script must exit 2 with a clear error."""
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)
    r = _run_no_docker(env_dir, tmp_path)
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}; stderr: {r.stderr}"
    assert "docker not found" in (r.stderr + r.stdout)


def test_empty_gpw_exits_2(tmp_path: Path) -> None:
    """GRAFANA_ADMIN_PASSWORD empty must exit 2 (issue #55 guard)."""
    env_dir = _quickstart_skel(tmp_path, with_gpw=False)
    r = _run_with_fake_docker(env_dir, tmp_path)
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}; stderr: {r.stderr}"
    assert "GRAFANA_ADMIN_PASSWORD" in (r.stderr + r.stdout)


def test_historical_gpw_exits_2(tmp_path: Path) -> None:
    """GRAFANA_ADMIN_PASSWORD=alphard (historical literal) must exit 2."""
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)
    (env_dir / ".env").write_text("GRAFANA_ADMIN_PASSWORD=alphard\n", encoding="utf-8")
    r = _run_with_fake_docker(env_dir, tmp_path)
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}; stderr: {r.stderr}"
    combined = r.stderr + r.stdout
    assert "alphard" in combined
    assert "GRAFANA_ADMIN_PASSWORD" in combined


def test_env_created_from_example(tmp_path: Path) -> None:
    """If .env is missing, it should be created from .env.example
    (when the docker sanity check passes).
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True, skip_env=True)
    assert not (env_dir / ".env").exists()
    # Use fake docker (sanity checks pass; compose step fails AFTER .env
    # bootstrap runs, which is what we want to test).
    r = _run_with_fake_docker(env_dir, tmp_path)
    assert (
        env_dir / ".env"
    ).exists(), f".env should be created from .env.example; rc={r.returncode}; stderr={r.stderr[:500]}"
    # And the contents should be the .env.example text (with possibly added baked lines)
    text = (env_dir / ".env").read_text()
    assert (
        "GRAFANA_ADMIN_PASSWORD=ci_test_password_DO_NOT_USE_IN_PRODUCTION" in text
    ), "GPW should be copied from .env.example"


def test_postgres_password_autogenerated(tmp_path: Path) -> None:
    """POSTGRES_PASSWORD empty -> 24 random bytes injected.

    This test asserts the post-bake SUCCESS path returns rc=0,
    not rc in (1, 2). The previous permissive assertion masked
    the regression fixed by ALPHARD_SKIP_COMPOSE=1 (issue #251):
    with the old code, every bake-success run exited 1 with a
    misleading "compose step failed" message, so the test suite
    could never verify the bake path was actually clean.
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True, skip_env=False)
    (env_dir / ".env").write_text(
        "GRAFANA_ADMIN_PASSWORD=ci_test_password_DO_NOT_USE_IN_PRODUCTION\n" "POSTGRES_PASSWORD=\n" "REDIS_PASSWORD=\n",
        encoding="utf-8",
    )

    r = _run_skip_compose(env_dir, tmp_path)
    assert r.returncode == 0, f"expected exit 0 on bake-success path; got {r.returncode}; " f"stderr: {r.stderr[-500:]}"

    text = (env_dir / ".env").read_text()
    pgpw = re.search(r'^POSTGRES_PASSWORD="([^"]+)"', text, re.MULTILINE)
    rpw = re.search(r'^REDIS_PASSWORD="([^"]+)"', text, re.MULTILINE)
    assert pgpw and pgpw.group(1).strip(), f"POSTGRES_PASSWORD should be set; env:\n{text}"
    assert rpw and rpw.group(1).strip(), f"REDIS_PASSWORD should be set; env:\n{text}"
    assert pgpw.group(1).strip() != "alphard"


def test_skip_compose_exits_zero_and_skips_docker(tmp_path: Path) -> None:
    """ALPHARD_SKIP_COMPOSE=1: bake stages only, exits 0 BEFORE
    invoking docker compose up. Regression for issue #251.

    We additionally assert that the fake docker's `compose up`
    branch was NOT invoked — if it was, the script would have
    failed and exited 0 only by accident. We detect this by
    checking stdout: the SKIP_COMPOSE path prints 'compose not
    invoked' (rc=0), while a real `compose up` failure would print
    'docker compose up failed' (rc=2).
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)
    r = _run_skip_compose(env_dir, tmp_path)
    assert r.returncode == 0, f"ALPHARD_SKIP_COMPOSE=1 should exit 0; got {r.returncode}; " f"stderr: {r.stderr[-500:]}"
    combined = r.stdout + r.stderr
    assert (
        "compose not invoked" in combined
    ), f"expected 'compose not invoked' message; got stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "docker compose up failed" not in combined, (
        f"docker compose up should NOT have run under SKIP_COMPOSE; " f"got stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )


def test_compose_failure_exits_2_with_diagnostic(tmp_path: Path) -> None:
    """When `docker compose up` actually returns non-zero, the
    script must exit 2 AND print a useful diagnostic pointing at
    `docker compose logs` and `docker ps`. Regression for issue
    #250: under `set -euo pipefail`, the previous PIPESTATUS check
    was unreachable because `set -e` killed the script on the
    pipeline BEFORE the diagnostic branch could run. The test
    suite's `rc in (1, 2)` assertion masked this — the script was
    silently exiting 1 from the pipeline itself.
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)
    r = _run_with_fake_docker(env_dir, tmp_path)
    assert r.returncode == 2, (
        f"compose-up failure should exit 2; got {r.returncode}; "
        f"stdout: {r.stdout[-500:]}\nstderr: {r.stderr[-500:]}"
    )
    combined = r.stdout + r.stderr
    assert "docker compose up failed" in combined, f"expected 'docker compose up failed' diagnostic; got:\n{combined}"
    assert (
        "docker compose logs" in combined or "docker ps" in combined
    ), f"expected inspect-hint diagnostic; got:\n{combined}"


def test_timeout_zero_exits_1_with_honest_message(tmp_path: Path) -> None:
    """ALPHARD_TIMEOUT_SEC=0 (without SKIP_COMPOSE) means: bake +
    compose ran, but no health-gate polling. Exit 1 with an honest
    message. Regression for issue #251: the previous code conflated
    'compose failed' with 'compose skipped', printing
    'compose step failed; bakes ran' even when the bakes were the
    only thing that ran.
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)
    r = _run_with_fake_docker(env_dir, tmp_path)
    # Note: with our fake docker that fails `compose up`, the script
    # should exit 2 (compose-failure) BEFORE reaching the TIMEOUT_SEC=0
    # branch. So we instead exercise the timeout-0 path with a fake
    # docker that SUCCEEDS on compose up — see the next test.
    # For now, just assert the compose-failure path does NOT hit the
    # old "compose step failed; bakes ran; quickstart test mode active"
    # dead-code branch.
    combined = r.stdout + r.stderr
    assert "quickstart test mode active" not in combined, f"dead-code branch should be removed; got:\n{combined}"


def test_timeout_zero_with_successful_compose_exits_1(tmp_path: Path) -> None:
    """ALPHARD_TIMEOUT_SEC=0 + docker compose up SUCCESS = exit 1.

    This is the pure fast-fail-smoke path. With the fix, when
    compose succeeds and the operator asked for no health polling,
    the script should exit 1 with a message that's HONEST about
    what happened: 'fast-fail smoke mode', NOT 'compose step
    failed' (because compose didn't fail — the operator just asked
    us to skip the health check).
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)
    _fill_all_b64(env_dir)

    # Custom fake docker: ALL commands succeed (including `compose up`),
    # except `inspect`, which reports no such container — this fake models
    # an empty daemon, so the issue #382 conflict precheck must see nothing
    # holding the literal alphard-* names and fall through to compose.
    fake_docker_dir = tmp_path / "fakebin_success"
    fake_docker_dir.mkdir(exist_ok=True)
    fake_docker = fake_docker_dir / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "inspect" ]; then echo "Error: No such object" >&2; exit 1; fi\n'
        'echo "fake docker $*"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    _qs_link(env_dir)
    env = os.environ.copy()
    env["PATH"] = f"{fake_docker_dir}:{env['PATH']}"
    env["ALPHARD_QUIET"] = "1"
    env["HOME"] = str(tmp_path)
    env["ALPHARD_TIMEOUT_SEC"] = "0"

    r = subprocess.run(
        ["/bin/bash", str(_qs_link(env_dir))],
        cwd=str(env_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r.returncode == 1, (
        f"timeout-0 + compose-success should exit 1; got {r.returncode}; " f"stderr: {r.stderr[-500:]}"
    )
    combined = r.stdout + r.stderr
    assert "fast-fail" in combined.lower(), f"expected honest fast-fail message; got:\n{combined}"
    assert "ALPHARD_SKIP_COMPOSE" in combined, f"expected hint about ALPHARD_SKIP_COMPOSE alternative; got:\n{combined}"


def test_symlink_invocation_finds_real_repo(tmp_path: Path) -> None:
    """Invoking quickstart.sh through a SYMLINK from a directory
    OUTSIDE the repo root must find the real .env / docker-compose.yaml.

    Regression for issues #248 and #249. The script's REPO_ROOT
    must come from `readlink -f $BASH_SOURCE[0]`, not
    `${BASH_SOURCE[0]%/*}`. We verify by running the script via a
    symlink at tmp_path/bin/qs → env_dir/scripts/quickstart.sh with
    cwd=tmp_path/elsewhere/, then asserting .env was created in the
    real env_dir, not in the symlink's parent directory.
    """
    env_dir = _quickstart_skel(tmp_path, with_gpw=True, skip_env=True)
    assert not (env_dir / ".env").exists()

    r = _run_via_symlink(env_dir, tmp_path, with_skip_compose=True)
    assert r.returncode == 0, (
        f"symlinked invocation should succeed (rc=0); got {r.returncode}; " f"stderr: {r.stderr[-500:]}"
    )
    assert (env_dir / ".env").exists(), (
        f".env should be created in the REAL repo dir {env_dir}; " f"got stderr: {r.stderr[-500:]}"
    )
    # And NOT in the symlink's parent directory (tmp_path/bin/.. = tmp_path).
    assert not (tmp_path / ".env").exists(), ".env must NOT be created in the symlink's parent directory"
    # And the script must have found the real compose file.
    text = (env_dir / ".env").read_text()
    assert "GRAFANA_ADMIN_PASSWORD=ci_test_password_DO_NOT_USE_IN_PRODUCTION" in text
