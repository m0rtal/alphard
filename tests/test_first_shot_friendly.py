"""Tests for issue #243 Group A — first-shot-friendly architecture fixes.

Covers:
  A1: REMOVED in issue #347 — pg-init was dropped entirely (its single-file
      bind-mounts render as directories on LXC, so the schema never applied).
      Schema application now happens inside the bot's entrypoint via
      ``init_schema()`` before the fail-closed ``auth_probe()``. See
      ``tests/test_347_pg_init_removal.py`` for the post-#347 contract.
  A2: .env.example has pre-baked Grafana B64 vars so operators can
      `cp .env.example .env && docker compose up -d` without first
      running ./scripts/quickstart.sh.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yaml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SCHEMA_SQL = REPO_ROOT / "src" / "data" / "schema.sql"


# Reusable B64 var-line regex (filled at call site).
def _b64_line(text: str, key: str) -> str | None:
    """Return the captured base64 value for a *_B64 var in text, or None."""
    m = re.compile(rf'^{re.escape(key)}="([^"]+)"', re.MULTILINE).search(text)
    return m.group(1) if m is not None else None


# A1 tests removed in issue #347 — see tests/test_347_pg_init_removal.py
# for the post-fix contract.


# -----------------------------------------------------------------------
