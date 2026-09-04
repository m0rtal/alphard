"""Regression tests for issue #491 — docker-compose.yaml GOST-bundle path drift.

PR #480 (Closes #479) renamed the GOST CA bundle from
``docker/certs/tinkoff-gost-ca-bundle.pem`` to ``.txt`` so the file would
not be swallowed by the ``.gitignore`` ``*.pem`` exclusion. The active
bind-mount and the ``REQUESTS_CA_BUNDLE`` env var were both updated, but
the env-var comment block still pointed the reader at the ``.pem`` path
that no longer exists inside the container.

These tests pin the post-#491 contract: every active (non-archaeology)
``tinkoff-gost-ca-bundle`` reference in ``docker-compose.yaml`` uses the
``.txt`` extension, so an operator tracing the env var back to its
bind-mount source lands on a path that actually exists.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

COMPOSE = REPO_ROOT / "docker-compose.yaml"

BUNDLE_STEM = "tinkoff-gost-ca-bundle"

# Archaeology framing that legitimately names the pre-#480 ``.pem`` path
# (e.g. "renamed from ...-bundle.pem in PR #480"). Such lines document
# history and must stay readable.
_ARCHAEOLOGY_PHRASES = (
    "pr #480",
    "pr #479",
    "renamed",
    "переимен",
)

_STALE_REF_RE = re.compile(rf"{re.escape(BUNDLE_STEM)}\.pem")

# Backward/forward context window used to attribute an archaeology marker
# to a neighbouring line (comment blocks soft-wrap across lines).
_WINDOW = 6


def _is_archaeology(blob: str) -> bool:
    lowered = blob.lower()
    return any(phrase in lowered for phrase in _ARCHAEOLOGY_PHRASES)


def _stale_pem_refs() -> list[tuple[int, str]]:
    """Return (lineno, line) for each active-prose ``.pem`` bundle ref."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    offenders: list[tuple[int, str]] = []

    for idx, line in enumerate(lines):
        if not _STALE_REF_RE.search(line):
            continue

        backward = lines[max(0, idx - _WINDOW) : idx]
        forward = lines[idx + 1 : idx + 1 + _WINDOW]
        if _is_archaeology("\n".join((line, *backward, *forward))):
            continue

        offenders.append((idx + 1, line.strip()))

    return offenders


def test_compose_has_no_stale_pem_bundle_refs() -> None:
    """Issue #491 AC #1: no active ``...-bundle.pem`` reference survives.

    Pre-fix, ``docker-compose.yaml:82`` sent the reader to
    ``/etc/ssl/certs/tinkoff-gost-ca-bundle.pem`` while the bind-mount
    (line 149) and the env var (line 96) both used ``.txt``.
    """
    offenders = _stale_pem_refs()
    assert not offenders, (
        "Issue #491: docker-compose.yaml references the pre-#480 "
        f"'{BUNDLE_STEM}.pem' path in active prose. PR #480 renamed the "
        "bundle to '.txt' to dodge the .gitignore '*.pem' exclusion; the "
        "comment block was left behind. Use '.txt' or add a PR #480 "
        "archaeology marker.\nOffenders:\n" + "\n".join(f"  L{ln}: {s}" for ln, s in offenders)
    )


def test_compose_env_var_matches_bind_mount_target() -> None:
    """Issue #491 AC #2: ``REQUESTS_CA_BUNDLE`` equals the bind-mount target.

    Guards the drift class rather than the single line: whatever path the
    env var names must be the container-side target of a real bind-mount,
    otherwise ``requests`` verifies against a file that is not there.
    """
    text = COMPOSE.read_text(encoding="utf-8")

    env_match = re.search(r"REQUESTS_CA_BUNDLE:\s*(\S+)", text)
    assert env_match is not None, "docker-compose.yaml must set REQUESTS_CA_BUNDLE"
    env_path = env_match.group(1)

    mount_match = re.search(rf"^\s*-\s*(\S*{re.escape(BUNDLE_STEM)}\S*):(\S+?):ro\s*$", text, re.MULTILINE)
    assert mount_match is not None, "docker-compose.yaml must bind-mount the GOST bundle read-only"
    mount_target = mount_match.group(2)

    assert env_path == mount_target, (
        f"Issue #491: REQUESTS_CA_BUNDLE={env_path} does not match the "
        f"bind-mount target {mount_target}. requests would verify TLS "
        "against a path that does not exist in the container."
    )


def test_compose_bundle_source_file_exists() -> None:
    """Issue #491 AC #3: the host-side bind-mount source is committed.

    Compose ``create_host_path: true`` would silently create a DIRECTORY
    at a missing source, mounting a directory where the container expects
    a file and breaking TLS at request time.
    """
    text = COMPOSE.read_text(encoding="utf-8")

    mount_match = re.search(rf"^\s*-\s*(\S*{re.escape(BUNDLE_STEM)}\S*):(\S+?):ro\s*$", text, re.MULTILINE)
    assert mount_match is not None, "docker-compose.yaml must bind-mount the GOST bundle read-only"

    source = (REPO_ROOT / mount_match.group(1).lstrip("./")).resolve()
    assert source.is_file(), f"Issue #491: bind-mount source {source} is not a committed file"
