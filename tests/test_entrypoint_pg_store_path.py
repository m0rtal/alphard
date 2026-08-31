"""Regression test for issue #363: entrypoint.sh must find data.pg_store reliably.

When scripts/pre_pr_smoke.sh bind-mounts ./src -> /app/src:ro on top of the
baked image, the relative sys.path.insert(0, 'src') call inside the
inline python -c blocks can resolve to a degraded view of /app/src (only
files baked into the lowest layer of the image are seen, not the
bind-mounted overrides). Symptom: container restart-loops with
ModuleNotFoundError: No module named 'data.pg_store'.

Fix: make path resolution absolute (/app/src) so the entrypoint works
regardless of whether a bind-mount shadowed the baked image or not.

This test pins the contract by reading entrypoint.sh as text and asserting
the structure. Pure pytest; no docker, no LXC/ZFS dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "docker" / "entrypoint.sh"


def _read_entrypoint() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def _sys_path_args(content):
    # Find arguments of all sys.path.insert(0, ...) calls in entrypoint.sh
    pattern = "sys.path.insert("
    out = []
    pos = 0
    while True:
        i = content.find(pattern, pos)
        if i < 0:
            break
        # find matching close-paren
        depth = 0
        start = i + len(pattern)
        end = start
        for j in range(start, len(content)):
            ch = content[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    end = j
                    break
                depth -= 1
        args = content[start:end]
        # args is comma-separated; first arg is "0", second arg is "..."
        parts = args.split(",", 1)
        if len(parts) >= 2:
            arg = parts[1].strip().strip("'").strip('"')
            out.append(arg)
        pos = end + 1
    return out


# r-string with backslashes intact for regex
from_data_pg_store_re = re.compile(r"from\s+data\.pg_store\s+import")


class TestEntrypointPgStorePath:
    """Cycle145 regression guard for issue #363."""

    def test_entrypoint_exists_and_is_executable(self) -> None:
        assert ENTRYPOINT.exists(), "missing entrypoint.sh at " + str(ENTRYPOINT)
        first_line = _read_entrypoint().splitlines()[0]
        assert first_line.startswith("#!"), "entrypoint.sh must have a shebang"

    def test_no_bare_relative_src_path_in_init_schema_block(self) -> None:
        content = _read_entrypoint()
        sys_paths = _sys_path_args(content)
        for sp in sys_paths:
            assert sp != "src", (
                "entrypoint.sh still uses bare sys.path.insert(0, 'src'): "
                "this is exactly the bug from issue #363. Replace with "
                "sys.path.insert(0, '/app/src') or a helper."
            )

    def test_no_bare_relative_src_path_in_auth_probe_block(self) -> None:
        content = _read_entrypoint()
        sys_paths = _sys_path_args(content)
        assert all(sp != "src" for sp in sys_paths), "all sys.path.insert calls must use absolute path, got: " + repr(
            sys_paths
        )

    def test_uses_absolute_app_src(self) -> None:
        content = _read_entrypoint()
        abs_paths = _sys_path_args(content)
        assert abs_paths, "expected at least one sys.path.insert call after fix"
        for ap in abs_paths:
            assert ap == "/app/src", "entrypoint.sh path-resolve must be exactly '/app/src', got " + repr(ap)

    def test_two_init_schema_and_auth_probe_blocks_still_present(self) -> None:
        content = _read_entrypoint()
        pgb_blocks = from_data_pg_store_re.findall(content)
        assert (
            len(pgb_blocks) >= 2
        ), "expected >=2 from data.pg_store imports in entrypoint.sh (init_schema + auth_probe), found " + str(
            len(pgb_blocks)
        )


class TestEntrypointPathResolution:
    """Pin helper semantics if a _resolve_src_path() helper is added later."""

    def test_no_residual_relative_src_string(self) -> None:
        content = _read_entrypoint()
        bad = re.findall(r"sys.path.insert\(\s*0\s*,\s*'src'\s*\)", content)
        assert bad == [], "residual bare 'src' literal: " + repr(bad)
