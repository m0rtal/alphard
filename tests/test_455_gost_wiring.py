"""Regression tests for Issue #455 — wire `scripts/fetch_tinkoff_gost_ca.py`
into the build/operator path so the Russian GOST CA bundle is produced
on a fresh clone.

Background
----------

PR #444 + #454 shipped `scripts/fetch_tinkoff_gost_ca.py`, which writes
the Russian Trusted Root CA + Sub CA chain to
`docker/certs/tinkoff-gost-ca-bundle.pem`. Without that bundle every
HTTPS call to `invest-public-api.tinkoff.ru` and `iss.moex.com` fails
with `CERTIFICATE_VERIFY_FAILED` because the standard `certifi` trust
store does not include the Russian Ministry of Digital Development's
CA (issue #430 / #455).

The script was unwired — no Dockerfile, compose, entrypoint, or
quickstart step invoked it; nothing told operators to run it manually.
This module pins the wiring so a future refactor cannot silently break
the contract.

Pinned contracts
----------------

1. `docker-compose.yaml` bind-mounts the bundle file into the alphard-bot
   service at the standard `/etc/ssl/certs/` location so the OpenSSL /
   `requests` clients trust the Russian endpoints out of the box.
2. `scripts/quickstart.sh` invokes the fetch script before `compose up`
   so a fresh clone has the bundle file before the bind-mount is read.
3. The fetch step is idempotent — re-running the script when the bundle
   already exists and is fresh does NOT re-fetch.
4. The Dockerfile continues to install the t-tech-investments-shipped
   Russian Trusted Root CA so the system trust store ALSO has the root
   CA, but the bind-mounted bundle adds the Sub CAs that aren't in
   t-tech-investments.
5. README/QUICKSTART/SECURITY docs mention the script + bundle so
   operators discovering the sawtooth-restart bug know the fix exists.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yaml"
QUICKSTART_PATH = REPO_ROOT / "scripts" / "quickstart.sh"
SCRIPT_PATH = REPO_ROOT / "scripts" / "fetch_tinkoff_gost_ca.py"
DOCKERFILE_PATH = REPO_ROOT / "docker" / "Dockerfile"
README_PATH = REPO_ROOT / "README.md"
QUICKSTART_MD_PATH = REPO_ROOT / "docs" / "QUICKSTART.md"

BUNDLE_RELPATH = "docker/certs/tinkoff-gost-ca-bundle.pem"
BUNDLE_CONTAINER_PATH = "/etc/ssl/certs/tinkoff-gost-ca-bundle.pem"


# --- contract 1: docker-compose.yaml bind-mount -------------------------


def _render_compose() -> str:
    r = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), "config"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, (
        f"docker compose config failed (rc={r.returncode}):\n" f"STDOUT:\n{r.stdout}\n\nSTDERR:\n{r.stderr}"
    )
    return r.stdout


def test_compose_bind_mounts_gost_bundle_into_alphard_bot() -> None:
    """alphard-bot MUST bind-mount the host bundle file into the
    standard certs directory at /etc/ssl/certs/tinkoff-gost-ca-bundle.pem.

    Without this mount the bot's ssl clients fall back to the system
    certifi store, which lacks the Russian Trusted Root CA — issue
    #430 returns and backfill silently logs "no data in window" on
    every ticker.

    `docker compose config` renders bind-mounts as separate
    `source:`/`target:` keys (long-form `path:path:ro` becomes a
    structured dict), so we pin on `target:` rather than the
    long-form string.
    """
    text = _render_compose()
    target_marker = f"target: {BUNDLE_CONTAINER_PATH}"
    assert target_marker in text, (
        f"alphard-bot MUST bind-mount the GOST CA bundle at " f"{BUNDLE_CONTAINER_PATH}; compose config output:\n{text}"
    )


def test_compose_bind_mount_is_read_only() -> None:
    """The bind-mount MUST be :ro so the in-container processes cannot
    accidentally mutate the host bundle file. Only the host-side
    `scripts/fetch_tinkoff_gost_ca.py` invocation regenerates it.

    `docker compose config` renders `:ro` as ``read_only: true`` in
    the YAML output.
    """
    text = _render_compose()
    # Find the bundle entry. Compose renders volumes as a list of
    # dicts after `config`; the bind-mount we care about has
    # `target: /etc/ssl/certs/tinkoff-gost-ca-bundle.pem`.
    target_marker = f"target: {BUNDLE_CONTAINER_PATH}"
    idx = text.find(target_marker)
    assert idx != -1, f"bundle bind-mount not found; compose config:\n{text}"
    # Look at the surrounding 200 chars for `read_only: true` (compose
    # always emits the read_only flag before any other mount keys).
    window = text[max(0, idx - 200) : idx + 200]
    assert "read_only: true" in window, (
        f"GOST CA bundle bind-mount MUST be :ro (read_only: true); " f"compose rendered:\n{window}"
    )


def test_compose_bind_mount_targets_only_alphard_bot() -> None:
    """The bundle must be mounted into alphard-bot specifically.

    postgres + redis don't talk to tinkoff.ru / iss.moex.com, so
    mounting the bundle into them is dead weight and creates a
    precedent for accidental over-sharing.
    """
    text = _render_compose()
    # Parse the rendered config; find the alphard-bot service block
    # and assert it has the bundle target. Other services should NOT.
    bot_block_match = re.search(r"alphard-bot:.*?(?=^  \S|\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert bot_block_match, "alphard-bot service block not found in compose config"
    bot_block = bot_block_match.group(0)
    target_marker = f"target: {BUNDLE_CONTAINER_PATH}"
    assert target_marker in bot_block, (
        f"alphard-bot MUST bind-mount the bundle ({target_marker}); " f"bot block:\n{bot_block}"
    )

    # And make sure postgres / redis do NOT have it.
    # Anchor on `^  <svc>:` (no alphard- prefix) so the regex does not
    # match top-level volume entries like `alphard-postgres-data:`.
    for svc in ("postgres", "redis"):
        m = re.search(
            rf"^  {re.escape(svc)}:\s",
            text,
            flags=re.MULTILINE,
        )
        assert m, f"{svc} service block not found in compose config"
        # Grab from this match until the next 2-space-indented top-level key.
        end = re.search(r"^  \S", text[m.end() :], flags=re.MULTILINE)
        block_end = m.end() + (end.start() if end else len(text) - m.end())
        block = text[m.start() : block_end]
        assert target_marker not in block, (
            f"{svc} MUST NOT bind-mount the GOST bundle; "
            f"only alphard-bot talks to tinkoff.ru / iss.moex.com. "
            f"Block:\n{block}"
        )


# --- contract 2: scripts/quickstart.sh invokes the fetch ----------------


def test_quickstart_invokes_fetch_script() -> None:
    """quickstart.sh MUST call scripts/fetch_tinkoff_gost_ca.py before
    `docker compose up` so a fresh clone has the bundle file in place
    when compose reads the bind-mount.

    Pin on the path literal: the script path appears verbatim with the
    repo root prepended. We avoid checking for `python3` prefixes or
    any specific bash quoting style because both can change.
    """
    text = QUICKSTART_PATH.read_text()
    assert "scripts/fetch_tinkoff_gost_ca.py" in text, (
        "quickstart.sh MUST invoke scripts/fetch_tinkoff_gost_ca.py " "before compose up (issue #455)."
    )


def test_quickstart_fetch_runs_before_compose_up() -> None:
    """The fetch invocation MUST appear before the `docker compose up`
    line. Otherwise the bind-mount fails on the first run with
    `Bind source path does not exist`.

    The `docker compose up -d` literal appears several times in the
    script (header comment, info banners, the actual code line). We
    pin on the ACTUAL code invocation, which is the one inside the
    `$( ... )` command substitution. The first occurrence in the
    header comment (line ~17, before any code) would let a refactor
    regress the order without failing this test.
    """
    text = QUICKSTART_PATH.read_text()
    fetch_idx = text.find("scripts/fetch_tinkoff_gost_ca.py")
    # The actual code line: `_compose_output="$(docker compose up -d 2>&1)"`.
    # Look for `$(docker compose up` (the substitution form) which
    # appears exactly once in the file.
    compose_code_idx = text.find("$(docker compose up")
    assert fetch_idx != -1, "fetch invocation not found"
    assert compose_code_idx != -1, (
        "expected `$(docker compose up ...)` command substitution not " "found in quickstart.sh"
    )
    assert fetch_idx < compose_code_idx, (
        f"fetch (offset {fetch_idx}) MUST appear before the `docker compose up -d` "
        f"code line (offset {compose_code_idx}); otherwise bind-mount fails on fresh clone."
    )


def test_quickstart_fetch_outputs_to_correct_path() -> None:
    """The fetch MUST write to docker/certs/tinkoff-gost-ca-bundle.pem —
    the same path compose bind-mounts. A typo (e.g. /tmp/...) would
    silently pass quickstart but break compose up.
    """
    text = QUICKSTART_PATH.read_text()
    assert BUNDLE_RELPATH in text, (
        f"quickstart.sh MUST write the bundle to {BUNDLE_RELPATH} "
        f"(same path compose bind-mounts); got script:\n{text[:3000]}"
    )


def test_quickstart_fetch_step_emits_info_banner() -> None:
    """An operator looking at quickstart.sh output should see the new
    GOST bundle step as a numbered banner. The info() helper prints
    `=== N/M <title> ===`. We pin on the title text.
    """
    text = QUICKSTART_PATH.read_text()
    assert "Russian GOST CA bundle" in text, (
        "quickstart.sh MUST emit a banner that names the new GOST bundle step "
        "(operator UX — operators should know the script is doing network I/O)"
    )


# --- contract 3: idempotency --------------------------------------------


def test_quickstart_fetch_is_idempotent() -> None:
    """If the bundle file already exists, quickstart.sh MUST skip the
    fetch (it should not re-download the chain on every compose-up).
    We assert on the existence of an mtime / existence check before
    the python3 invocation, regardless of the specific implementation
    (`find -mtime`, `stat -c %Y`, etc).
    """
    text = QUICKSTART_PATH.read_text()
    # Find the ACTUAL python3 invocation (not the comment that mentions
    # the script name). The header comment on the previous comment line
    # of the script block mentions the script by name without invoking
    # it — searching for the comment would let us "see" GOST_BUNDLE
    # BEFORE its definition, which is the false positive this test
    # was producing.
    invocation_idx = text.find('python3 "$REPO_ROOT/scripts/fetch_tinkoff_gost_ca.py"')
    if invocation_idx == -1:
        # Fallback: accept any `python3 ... fetch_tinkoff_gost_ca.py` invocation.
        invocation_idx = text.find("fetch_tinkoff_gost_ca.py")
        # Skip past the comment line if it landed there.
        while invocation_idx != -1 and not text[max(0, invocation_idx - 10) : invocation_idx].strip().endswith(
            ("python3", "||", "&&", "|")
        ):
            invocation_idx = text.find("fetch_tinkoff_gost_ca.py", invocation_idx + 1)
    assert invocation_idx != -1, (
        "quickstart.sh MUST contain a `python3 ... scripts/fetch_tinkoff_gost_ca.py` "
        "invocation (the comment-only reference is not enough)."
    )
    preceding = text[:invocation_idx]
    assert "GOST_BUNDLE" in preceding, (
        "quickstart.sh MUST define a GOST_BUNDLE path variable " "(idempotency check needs a stable path)"
    )
    # `[[ -f "$GOST_BUNDLE" ]]` or equivalent existence check.
    assert re.search(r"-f\s+\"?\$GOST_BUNDLE\"?", preceding), (
        "quickstart.sh MUST check for the bundle's existence before " "running the fetch (idempotency)."
    )
    # An mtime / age check using `find -mtime -N` or `stat` is the
    # conventional way to implement "skip if fresh".
    assert "mtime" in preceding or "-mtime" in preceding, (
        "quickstart.sh MUST skip the fetch if the bundle is fresh "
        "(30-day window via `find -mtime -N` or equivalent)."
    )


# --- contract 4: Dockerfile keeps t-tech root CA install ----------------


def test_dockerfile_installs_russian_root_ca_from_t_tech() -> None:
    """The Dockerfile installs RussianTrustedRootCA.pem from
    t-tech-investments. The new GOST bundle bind-mount ADDS the Sub CAs
    that aren't in t-tech-investments, but the root CA install must
    stay — it's the system-trust-store half of the contract.

    If a future refactor removes this step the bot's `requests`
    client may still verify against the bind-mounted bundle, but
    anything using the system certifi path (subprocess calls into
    openssl, etc.) breaks.
    """
    text = DOCKERFILE_PATH.read_text()
    assert "RussianTrustedRootCA" in text, (
        "docker/Dockerfile MUST continue installing the Russian Trusted "
        "Root CA from t-tech-investments; the new GOST bind-mount does "
        "NOT replace this step."
    )
    assert "t-bank" in text.lower() or "tbank" in text.lower(), (
        "docker/Dockerfile MUST install the root CA into the system trust "
        "store via update-ca-certificates (t-bank root CA path)."
    )


# --- contract 5: docs mention the script + bundle -----------------------


@pytest.mark.parametrize(
    "doc_path",
    [
        README_PATH,
        QUICKSTART_MD_PATH,
    ],
)
def test_doc_mentions_gost_bundle_or_script(doc_path: Path) -> None:
    """README.md and docs/QUICKSTART.md MUST mention either the bundle
    path, the script name, or both — so an operator hitting the
    sawtooth-restart bug can find the fix via docs alone.

    SECURITY.md is allowed to omit this if the operator UX isn't its
    concern; we don't pin it.
    """
    text = doc_path.read_text()
    mentions_bundle = "tinkoff-gost-ca-bundle" in text or "GOST" in text
    mentions_script = "fetch_tinkoff_gost_ca" in text
    assert mentions_bundle or mentions_script, (
        f"{doc_path.name} MUST mention the GOST bundle or the fetch script "
        f"so operators can discover the fix from docs."
    )


# --- cross-cutting: script + bundle paths agree -------------------------


def test_fetch_script_default_out_matches_compose_bind_mount() -> None:
    """`scripts/fetch_tinkoff_gost_ca.py` default output and
    docker-compose.yaml bind-mount source must be the same path —
    otherwise quickstart.sh has to pass an explicit `--out` to keep
    them in sync, which is a silent footgun.

    Pin on `DEFAULT_OUT = Path(...)` literal.
    """
    text = SCRIPT_PATH.read_text()
    assert BUNDLE_RELPATH in text, (
        f"scripts/fetch_tinkoff_gost_ca.py DEFAULT_OUT must equal "
        f"{BUNDLE_RELPATH} (compose bind-mount source). Got script:\n{text[:2000]}"
    )
