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
        "PROVISIONING_DATASOURCES_YML_B64=\n"
        "PROVISIONING_DASHBOARDS_PROVIDER_YML_B64=\n"
        "DASHBOARD_PHASE0_JSON_B64=\n"
        "DASHBOARD_PHASE28_JSON_B64=\n"
    )
    (env_dir / ".env.example").write_text(example, encoding="utf-8")

    if not skip_env:
        if with_gpw:
            env = (
                "GRAFANA_ADMIN_PASSWORD=ci_test_password_DO_NOT_USE_IN_PRODUCTION\n"
                "POSTGRES_PASSWORD=\n"
                "REDIS_PASSWORD=\n"
                "PROVISIONING_DATASOURCES_YML_B64=\n"
                "PROVISIONING_DASHBOARDS_PROVIDER_YML_B64=\n"
                "DASHBOARD_PHASE0_JSON_B64=\n"
                "DASHBOARD_PHASE28_JSON_B64=\n"
                "PROM_YML_B64=\n"
            )
        else:
            env = (
                "GRAFANA_ADMIN_PASSWORD=\n"
                "POSTGRES_PASSWORD=\n"
                "REDIS_PASSWORD=\n"
                "PROVISIONING_DATASOURCES_YML_B64=\n"
                "PROVISIONING_DASHBOARDS_PROVIDER_YML_B64=\n"
                "DASHBOARD_PHASE0_JSON_B64=\n"
                "DASHBOARD_PHASE28_JSON_B64=\n"
                "PROM_YML_B64=\n"
            )
        (env_dir / ".env").write_text(env, encoding="utf-8")

    (env_dir / "docker-compose.yaml").write_text("# stub\n", encoding="utf-8")

    prom_dir = env_dir / "docker" / "prometheus"
    prom_dir.mkdir(parents=True, exist_ok=True)
    (prom_dir / "prometheus.yml").write_text("# prom stub\n", encoding="utf-8")

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
    """Return a symlink path to quickstart.sh inside env_dir/scripts/."""
    tmp_scripts = env_dir / "scripts"
    tmp_scripts.mkdir(exist_ok=True)
    tmp_qs = tmp_scripts / "quickstart.sh"
    if tmp_qs.exists() or tmp_qs.is_symlink():
        tmp_qs.unlink()
    tmp_qs.symlink_to(QS)
    os.chmod(tmp_qs, 0o755)
    return tmp_qs


# Fake docker: sanity checks (version/info/compose version) succeed;
# the actual `docker compose up` always fails so the script exits with
# non-zero AFTER the bake stages (3/5 Grafana + 4/5 Prometheus) have
# completed. This lets us test the bake + bootstrap logic without a
# real Docker daemon.
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
    case "$2" in
      version|--short)
        echo "v2.40.3"
        exit 0
        ;;
      up)
        echo "fake compose up -d (always fails in test mode)" >&2
        exit 1
        ;;
      *)
        echo "fake compose $*"
        exit 0
        ;;
    esac
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
    """Pre-populate all 4 Grafana B64 vars + PROM_YML_B64 in .env."""
    text = (env_dir / ".env").read_text() if (env_dir / ".env").exists() else ""
    pairs = [
        ("PROVISIONING_DATASOURCES_YML_B64", "ZmFrZS1kYXRhc291cmNlLXltbA=="),
        ("PROVISIONING_DASHBOARDS_PROVIDER_YML_B64", "ZmFrZS1wcm92aWRlcg=="),
        ("DASHBOARD_PHASE0_JSON_B64", "e30="),
        ("DASHBOARD_PHASE28_JSON_B64", "e30="),
        ("PROM_YML_B64", "Z2xvYmFsOgogIHNjcmF"),
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
    """POSTGRES_PASSWORD empty -> 24 random bytes injected."""
    env_dir = _quickstart_skel(tmp_path, with_gpw=True, skip_env=False)
    (env_dir / ".env").write_text(
        "GRAFANA_ADMIN_PASSWORD=ci_test_password_DO_NOT_USE_IN_PRODUCTION\n" "POSTGRES_PASSWORD=\n" "REDIS_PASSWORD=\n",
        encoding="utf-8",
    )

    r = _run_with_fake_docker(env_dir, tmp_path)
    assert r.returncode in (1, 2), "expect compose to fail (rc=1) or validation (rc=2)"

    text = (env_dir / ".env").read_text()
    pgpw = re.search(r'^POSTGRES_PASSWORD="([^"]+)"', text, re.MULTILINE)
    rpw = re.search(r'^REDIS_PASSWORD="([^"]+)"', text, re.MULTILINE)
    assert pgpw and pgpw.group(1).strip(), f"POSTGRES_PASSWORD should be set; env:\n{text}"
    assert rpw and rpw.group(1).strip(), f"REDIS_PASSWORD should be set; env:\n{text}"
    assert pgpw.group(1).strip() != "alphard"


def test_prom_b64_baked_when_missing(tmp_path: Path) -> None:
    """PROM_YML_B64 missing -> baked from docker/prometheus/prometheus.yml."""
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)
    _fill_all_b64(env_dir)
    text = (env_dir / ".env").read_text()
    text = re.sub(r"^PROM_YML_B64=.*$", "", text, flags=re.MULTILINE)
    (env_dir / ".env").write_text(text)

    _run_with_fake_docker(env_dir, tmp_path)  # noqa: F841 (side effects only)
    text = (env_dir / ".env").read_text()
    m = re.search(r'^PROM_YML_B64="([A-Za-z0-9+/=]+)"', text, re.MULTILINE)
    assert m, f"PROM_YML_B64 should be baked; env:\n{text}"


def test_grafana_b64_baked_when_missing(tmp_path: Path) -> None:
    """PROVISIONING_*_B64 missing -> baked via tools/bake_grafana_env.py."""
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)
    text = (env_dir / ".env").read_text()
    for k in (
        "PROVISIONING_DATASOURCES_YML_B64",
        "PROVISIONING_DASHBOARDS_PROVIDER_YML_B64",
        "DASHBOARD_PHASE0_JSON_B64",
        "DASHBOARD_PHASE28_JSON_B64",
    ):
        text = re.sub(rf"^{k}=.*$", "", text, flags=re.MULTILINE)
    (env_dir / ".env").write_text(text)

    _run_with_fake_docker(env_dir, tmp_path)  # noqa: F841 (side effects only)
    text = (env_dir / ".env").read_text()
    for k in (
        "PROVISIONING_DATASOURCES_YML_B64",
        "PROVISIONING_DASHBOARDS_PROVIDER_YML_B64",
        "DASHBOARD_PHASE0_JSON_B64",
        "DASHBOARD_PHASE28_JSON_B64",
    ):
        m = re.search(rf'^{k}="([A-Za-z0-9+/=]+)"', text, re.MULTILINE)
        assert m, f"{k} should be baked after quickstart; env:\n{text}"


def test_idempotent_no_rewrite_when_already_set(tmp_path: Path) -> None:
    """If PROVISIONING_*_B64 already populated, quickstart must NOT call bake_grafana_env.py."""
    env_dir = _quickstart_skel(tmp_path, with_gpw=True)
    _fill_all_b64(env_dir)
    _run_with_fake_docker(env_dir, tmp_path)  # noqa: F841 (side effects only)
    text = (env_dir / ".env").read_text()
    m = re.search(r'^PROVISIONING_DATASOURCES_YML_B64="([^"]+)"', text, re.MULTILINE)
    assert m, "PROVISIONING_DATASOURCES_YML_B64 should still be set"
    assert (
        m.group(1) == "ZmFrZS1kYXRhc291cmNlLXltbA=="
    ), "PROVISIONING_DATASOURCES_YML_B64 should NOT have been rewritten (idempotency)"
