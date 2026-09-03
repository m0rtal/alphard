"""Regression tests for Issue #455 — wire `scripts/fetch_tinkoff_gost_ca.py`
into the build/operator path so the Russian GOST CA bundle is produced
on a fresh clone.

Background
----------

PR #444 + #454 shipped `scripts/fetch_tinkoff_gost_ca.py`, which writes
the Russian Trusted Root CA + Sub CA chain to
`docker/certs/tinkoff-gost-ca-bundle.txt`. Without that bundle every
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
6. `docker/certs/tinkoff-gost-ca-bundle.txt` exists in the repo tree as
   a regular (non-directory) file with PEM content (Issue #479). The
   bind-mount contract holds for any operator path — quickstart.sh,
   pre_pr_smoke.sh, OR direct `docker compose up -d` — without an
   implicit "run quickstart first" requirement.
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

BUNDLE_RELPATH = "docker/certs/tinkoff-gost-ca-bundle.txt"
BUNDLE_CONTAINER_PATH = "/etc/ssl/certs/tinkoff-gost-ca-bundle.txt"
REQUEST_CA_BUNDLE_ENV = "REQUESTS_CA_BUNDLE"


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
    standard certs directory at /etc/ssl/certs/tinkoff-gost-ca-bundle.txt.

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
    # `target: /etc/ssl/certs/tinkoff-gost-ca-bundle.txt`.
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


# --- contract 1b: REQUESTS_CA_BUNDLE env var (issue #468) ----------------


def test_compose_sets_requests_ca_bundle_env_var() -> None:
    """The bind-mount alone is NOT enough to make Python's `requests`
    library trust the GOST CA bundle. `requests.adapters.HTTPAdapter`
    uses the `certifi` bundle by default and only honors the
    `REQUESTS_CA_BUNDLE` env var as an override.

    Pin: `docker-compose.yaml` alphard-bot `environment:` block MUST
    contain `REQUESTS_CA_BUNDLE: /etc/ssl/certs/tinkoff-gost-ca-bundle.txt`
    so the bind-mounted file is actually consulted by the TLS client.

    We assert on the raw yaml text rather than `docker compose config`
    output because env vars survive normalization unchanged but
    `docker compose config` returns them as a nested list whose
    formatting varies across Compose v2 minor versions.
    """
    text = COMPOSE_PATH.read_text()
    expected = f"{REQUEST_CA_BUNDLE_ENV}: {BUNDLE_CONTAINER_PATH}"
    assert expected in text, (
        f"alphard-bot environment: block MUST contain `{expected}` "
        f"so Python's requests library trusts the bind-mounted GOST "
        f"bundle (issue #468). docker-compose.yaml excerpt around the "
        f"environment block:\n{text[text.find('environment:'):text.find('environment:')+1500]}"
    )


def test_compose_does_not_set_ssl_cert_file() -> None:
    """`SSL_CERT_FILE`, if set, REPLACES the default system trust
    store rather than augmenting it. Setting it would shadow
    `certifi`'s bundle (which includes the Russian Trusted Root CA
    baked in by t-tech-investments — see Dockerfile) for any
    subprocess that uses openssl / curl.

    Pin: REQUESTS_CA_BUNDLE is the narrowest correct fix. SSL_CERT_FILE
    is intentionally NOT set. A future refactor that adds
    SSL_CERT_FILE must justify the trade-off in a comment.
    """
    text = COMPOSE_PATH.read_text()
    # Note: assert the LITERAL is NOT present, ignoring comments.
    # Compose-valid env entries are `KEY: value` lines inside
    # `environment:` blocks. Comments start with `#`.
    env_block_match = re.search(
        r"environment:\s*\n(?P<body>(?:[ \t]+#[^\n]*\n|[ \t]+[^#\n][^\n]*\n)+)",
        text,
        flags=re.MULTILINE,
    )
    assert env_block_match, "alphard-bot environment: block not found"
    env_lines = [
        ln for ln in env_block_match.group("body").splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]
    env_keys = [
        re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*:", ln).group(1)
        for ln in env_lines
        if re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*:", ln)
    ]
    assert "SSL_CERT_FILE" not in env_keys, (
        f"SSL_CERT_FILE is set in alphard-bot environment: — this "
        f"SHADOWS the default trust store. Remove it; REQUESTS_CA_BUNDLE "
        f"is the narrowest correct fix for the .ru TLS clients. "
        f"env keys found: {env_keys}"
    )


def test_compose_requests_ca_bundle_only_on_alphard_bot() -> None:
    """REQUESTS_CA_BUNDLE must be set on alphard-bot only — postgres
    and redis don't talk to .ru endpoints and a global env would
    leak the bundle path into other containers.

    Pin: the alphard-bot service block (alphard-bot: ... volumes:)
    contains the env var; postgres / redis blocks do NOT.
    """
    text = COMPOSE_PATH.read_text()
    # Locate the alphard-bot service by top-level key under services:.
    bot_match = re.search(
        r"^  alphard-bot:\s*\n(?P<body>(?:[ \t]+[^\n]*\n)+)",
        text,
        flags=re.MULTILINE,
    )
    assert bot_match, "alphard-bot service block not found"
    bot_body = bot_match.group("body")
    expected = f"{REQUEST_CA_BUNDLE_ENV}: {BUNDLE_CONTAINER_PATH}"
    assert expected in bot_body, (
        f"REQUESTS_CA_BUNDLE env var MUST be set on alphard-bot "
        f"so its `requests` client trusts the GOST bundle. Bot block "
        f"excerpt:\n{bot_body[:2000]}"
    )


# --- contract 1c: scripts/pre_pr_smoke.sh pre-fetches the bundle (issue #469) --


def test_pre_pr_smoke_pre_fetches_gost_bundle() -> None:
    """`scripts/pre_pr_smoke.sh` runs `docker compose up` directly
    (without scripts/quickstart.sh), so it MUST pre-fetch the GOST
    bundle on a fresh checkout — otherwise compose's
    `create_host_path: true` default creates a directory at the
    bind-mount source, and ssl/requests fail at request time
    (issue #469).

    Pin: pre_pr_smoke.sh contains `python3 scripts/fetch_tinkoff_gost_ca.py`
    AND references the GOST_BUNDLE_PATH variable before `compose up`.
    """
    smoke_path = REPO_ROOT / "scripts" / "pre_pr_smoke.sh"
    text = smoke_path.read_text()
    assert "fetch_tinkoff_gost_ca.py" in text, (
        f"scripts/pre_pr_smoke.sh MUST pre-fetch the GOST bundle "
        f"(invoke fetch_tinkoff_gost_ca.py) so a fresh-clone smoke "
        f"run does not hit the create_host_path directory defect. "
        f"Current pre_pr_smoke.sh:\n{text}"
    )
    # The invocation must happen BEFORE the `compose up` line.
    fetch_idx = text.find("fetch_tinkoff_gost_ca.py")
    compose_up_idx = text.find('"${COMPOSE[@]}" up -d')
    assert compose_up_idx != -1, "compose up line not found in pre_pr_smoke.sh"
    assert fetch_idx != -1 and fetch_idx < compose_up_idx, (
        f"fetch_tinkoff_gost_ca.py invocation (idx {fetch_idx}) MUST "
        f"appear BEFORE compose up (idx {compose_up_idx}) in "
        f"pre_pr_smoke.sh. Otherwise the bind-mount source path "
        f"doesn't exist when compose reads it."
    )


def test_pre_pr_smoke_refuses_directory_shaped_bundle() -> None:
    """If `docker/certs/tinkoff-gost-ca-bundle.txt` already exists as a
    DIRECTORY (compose's `create_host_path: true` auto-created it on
    a prior run), pre_pr_smoke.sh MUST detect this and refuse to
    bring up the stack — otherwise the bot starts with a broken
    REQUESTS_CA_BUNDLE that points at a directory.

    Pin: pre_pr_smoke.sh contains an `-d` (directory) check on the
    GOST bundle path with an explicit failure message.
    """
    smoke_path = REPO_ROOT / "scripts" / "pre_pr_smoke.sh"
    text = smoke_path.read_text()
    # Look for a directory test (e.g. `[[ -d "$VAR" ]]` or `[ -d "$VAR" ]`)
    # on the GOST bundle path. `if [[ -d "$GOST_BUNDLE_PATH" ]]; then`
    # is the canonical form, but accept any shell quoting.
    #
    # The match must require the `-d` (or `! -d`) flag immediately
    # before the variable name. We pattern-match `-d "$<var>"` /
    # `-d $<var>` / `! -d "$<var>"` since shell-quoted forms vary.
    for var_name in ("GOST_BUNDLE_PATH", "GOST_BUNDLE"):
        pattern = rf'(?:^|\s)(?:-d|\!-d)\s+"?\${{?{re.escape(var_name)}}}?"?'
        if re.search(pattern, text):
            break
    else:
        raise AssertionError(
            f"pre_pr_smoke.sh MUST check for a directory-shaped GOST "
            f"bundle (`-d ${{GOST_BUNDLE_PATH:-...}}`) and refuse to "
            f"proceed (issue #469 Fix B). Current pre_pr_smoke.sh:\n{text}"
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
    """The fetch MUST write to docker/certs/tinkoff-gost-ca-bundle.txt —
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


# --- contract 6: bundle file actually committed to repo (Issue #479) ---


def test_gost_bundle_path_exists_in_repo_tree() -> None:
    """The bind-mount source path `docker/certs/tinkoff-gost-ca-bundle.txt`
    MUST exist in the repo tree as a regular file (not a directory).

    Without this PR #477's bind-mount contract has a chicken-and-egg
    on a fresh clone: the bind source is missing, and Docker's
    `create_host_path: true` default silently creates a DIRECTORY at the
    source path. The empty directory is then bind-mounted into the
    container as a file-shaped target, Python's `requests` (driven by
    `REQUESTS_CA_BUNDLE`) fails to parse it, and the sawtooth-restart
    loop #430 documents returns. The regression was filed as Issue #479
    after PR #477's four-wire design auto-closed #441/#455/#468/#469
    on textual contract without an assert on the file's presence.

    Pin: at least one of the following must return a blob line for
    `tinkoff-gost-ca-bundle.txt`:
    - `git ls-tree -r HEAD -- docker/certs/` (already-merged state)
    - `git ls-files --stage -- docker/certs/` (staged on the branch)
    The on-disk `Path.is_file()` check catches a future where someone
    `.gitignore`s the file post-commit. We accept either `.txt`
    (current state, chosen to comply with `.gitignore` `*.pem` exclusion)
    or `.pem` — but the path MUST resolve to a real regular file.
    """
    import subprocess

    # Check the committed state first.
    r_head = subprocess.run(
        ["git", "ls-tree", "-r", "HEAD", "--", "docker/certs/"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(REPO_ROOT),
    )
    in_head = "tinkoff-gost-ca-bundle" in r_head.stdout

    # Fall back to the staged state (branch commits land here first).
    r_index = subprocess.run(
        ["git", "ls-files", "--stage", "--", "docker/certs/"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(REPO_ROOT),
    )
    in_index = "tinkoff-gost-ca-bundle" in r_index.stdout

    assert in_head or in_index, (
        "docker/certs/tinkoff-gost-ca-bundle.txt is not in the repo "
        "tree or the staging index. Either commit the PEM (or rename "
        "to .txt to comply with .gitignore `*.pem` per this PR), or "
        "remove the bind-mount. Issue #479."
    )
    # And confirm the path actually points at a regular file on disk
    # (not just present in git history). `git ls-tree`/`ls-files` is
    # already enough for the failure mode #479 described, but the
    # on-disk check catches a future where someone `.gitignore`s the
    # file post-commit.
    bundle_path = REPO_ROOT / BUNDLE_RELPATH
    assert bundle_path.is_file(), (
        f"{bundle_path} is missing or not a regular file. "
        f"`docker compose up -d` will fail with bind-source-path-does-not-exist "
        f"or silently create a directory (`create_host_path: true`). "
        f"Either commit the bundle (issue #479 Option A) or remove the "
        f"bind-mount (Option B). Currently in HEAD as a committed file "
        f"(`.txt` extension chosen for .gitignore compliance)."
    )


def test_gost_bundle_file_contains_valid_pem() -> None:
    """The committed bundle file MUST contain at least one
    `-----BEGIN CERTIFICATE-----` ... `-----END CERTIFICATE-----` block
    parseable by Python's `ssl.SSLContext.load_verify_locations`.

    Otherwise the alphard-bot container trusts an empty CA bundle
    (the file exists from `git ls-tree` but `requests` cannot extract
    the chain) and the sawtooth-restart loop returns. This is the
    second half of the regression gap Issue #479 surfaced — a
    committed-but-empty file is no better than a missing one.

    Pin: parse the file with `ssl.SSLContext.load_verify_locations`
    and assert it raises nothing AND the context has at least one
    loaded CA (CA certs attribute).
    """
    import ssl

    bundle_path = REPO_ROOT / BUNDLE_RELPATH
    if not bundle_path.is_file():
        pytest.skip(f"{bundle_path} missing — covered by test_gost_bundle_path_exists_in_repo_tree")
    text = bundle_path.read_text()
    assert text.count("-----BEGIN CERTIFICATE-----") >= 1, (
        f"{bundle_path} contains zero PEM cert blocks; the bundle is "
        f"truncated or empty. Re-run scripts/fetch_tinkoff_gost_ca.py "
        f"and recommit."
    )
    # Now the more rigorous check: `load_verify_locations` parses the
    # PEM content. It silently ignores PEM blocks it can't decode but
    # raises nothing for a well-formed file. `ctx.cert_store_stats()`
    # returns (loaded_x509, loaded_x509_ca) on CPython 3.10+ — we
    # assert at least one X509 CA cert got loaded.
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(str(bundle_path))
    stats = ctx.cert_store_stats()
    # On Python 3.10+ `cert_store_stats()` returns either a dict
    # `{'x509_ca': N, 'x509': M, 'crl': K}` OR a 3-tuple
    # `(x509, x509_ca, crl)` depending on build options; both shapes
    # are valid. We pull the CA-cert count defensively, falling back
    # to 0 if neither shape matches. Anything ≥1 means the bundle
    # parsed cleanly and added at least one CA to the trust store.
    if isinstance(stats, dict):
        ca_count = int(stats.get("x509_ca", 0))
    else:
        ca_count = int(stats[1]) if len(stats) >= 2 else 0
    assert ca_count >= 1, (
        f"{bundle_path} parsed cleanly but zero X509 CA certs were "
        f"loaded into the trust store. ssl.context.cert_store_stats()={stats}. "
        f"Regenerate the bundle via scripts/fetch_tinkoff_gost_ca.py "
        f"and recommit."
    )
