"""Regression tests for issues #402 and #403 — quickstart.sh post-#399 hygiene.

PR #399 deleted the Grafana, Prometheus, and chownfix services from the compose
stack, along with `docker/grafana/`, `docker/prometheus/`, and the `tools/`
base64 env-bake pipeline. It did not audit `scripts/quickstart.sh`, which still

  - names `tools/` as a live python3 dependency (#403) — the directory is gone,
  - advertises `alphard-web` at `http://localhost:8080/` in its success message
    (#402) — that service is not in `docker-compose.yaml`, so an operator who
    follows the printed next-steps gets connection-refused,
  - defaults `ALPHARD_PROFILE` to `observability` and branches on it (#402) —
    no service declares `profiles:` post-#399, so both profile values select
    the identical three-service set and the branch is dead code.

These are prose-and-scaffolding defects, not runtime failures, which is exactly
why they survive: `test_quickstart.py` drives the script's exit codes and .env
side effects, so a comment that lies or a URL that 404s passes every gate.
The assertions below read the script as text and pin the removals.

Guarded contract: any URL the success path prints must belong to a service that
`docker-compose.yaml` actually declares.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
QUICKSTART = REPO_ROOT / "scripts" / "quickstart.sh"
COMPOSE = REPO_ROOT / "docker-compose.yaml"

# `http://localhost:8080/`, `http://127.0.0.1:9090` — host-facing URLs the
# success message hands an operator.
LOCALHOST_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1):(\d+)")


@pytest.fixture(scope="module")
def script() -> str:
    return QUICKSTART.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> dict:
    import yaml  # local import: pyyaml ships with alphard dev deps

    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}


def test_no_tools_dir_references(script: str) -> None:
    """#403: `tools/` was removed with the B64 bake pipeline."""
    offenders = [f"{i}: {ln.strip()}" for i, ln in enumerate(script.splitlines(), 1) if "tools/" in ln]

    assert not offenders, "scripts/quickstart.sh references the removed tools/ dir:\n" + "\n".join(offenders)


def test_no_stale_pr_396_reference(script: str) -> None:
    """#403/#400: the Grafana removal shipped as PR #399, not #396."""
    offenders = [f"{i}: {ln.strip()}" for i, ln in enumerate(script.splitlines(), 1) if "#396" in ln]

    assert not offenders, "scripts/quickstart.sh cites PR #396; the real PR is #399:\n" + "\n".join(offenders)


def test_printed_urls_map_to_compose_ports(script: str, compose: dict) -> None:
    """#402: a printed localhost URL must resolve to a published compose port.

    The #402 defect is the `alphard-web: http://localhost:8080/` next-steps line
    surviving a PR that never added `alphard-web` to compose. Deriving the valid
    set from the compose file keeps the guard honest if alphard-web later ships.
    """
    published = set()
    for svc in (compose.get("services") or {}).values():
        for mapping in (svc or {}).get("ports") or []:
            host = str(mapping).split(":")[0].strip('"')
            if not host.isdigit():
                continue

            published.add(host)

    dangling = sorted({port for port in LOCALHOST_URL.findall(script) if port not in published}, key=int)

    assert not dangling, (
        "scripts/quickstart.sh prints localhost URLs on ports no compose service "
        f"publishes: {', '.join(dangling)} (published: {sorted(published) or 'none'})"
    )


def test_no_dead_observability_profile(script: str, compose: dict) -> None:
    """#402: profile scaffolding is dead once no service declares `profiles:`.

    Only executable lines are checked. Comments that *document* the removal are
    the intended outcome, so flagging them would make the fix unachievable.
    """
    declares_profiles = any((svc or {}).get("profiles") for svc in (compose.get("services") or {}).values())
    if declares_profiles:
        pytest.skip("a compose service declares profiles: — the knob is live again")

    offenders = []
    for i, line in enumerate(script.splitlines(), 1):
        code = line.strip()
        if not code or code.startswith("#"):
            continue

        if "--profile" not in code and "ALPHARD_PROFILE" not in code and "$PROFILE" not in code:
            continue

        offenders.append(f"{i}: {code}")

    assert (
        not offenders
    ), "no compose service declares profiles:, so quickstart.sh must not thread " "a --profile filter:\n" + "\n".join(
        offenders
    )
