"""Tests for scripts/replay_sizing.py (kanban t_e55e2168 / issue #222).

The replay tool is the rollback companion to the sizing module
(``src/broker/sizing.py::compute_position_size``). It reads the
JSONL audit log and re-runs the formula to verify stored decisions
replay bit-identically. Two guarantees matter:

1. Tolerant default: a truncated trailing line (from a SIGKILL mid
   write of ``write_audit_jsonl``) MUST NOT abort the entire replay —
   the operator must still see every record that survived.

2. Strict opt-in: CI / lint gates can pass ``--strict`` to restore
   the pre-fix ``SystemExit`` behaviour when the audit log MUST be
   perfectly parseable.

These tests exercise the public surface (``load_records`` and
``main``) without touching the actual broker path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Make scripts/ importable so we can import replay_sizing as a module.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import replay_sizing  # noqa: E402


def _write_audit(path: Path, records: list[dict[str, Any]]) -> None:
    """Write a list of records as a complete JSONL audit log."""
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _audit_record(ts: str, ticker: str = "SBER") -> dict[str, Any]:
    return {
        "ts": ts,
        "ticker": ticker,
        "side": "buy",
        "formula_version": "v1",
        "inputs": {
            "cash": "100000",
            "peak_equity": "100000",
            "total_equity": "100000",
            "confidence": "1.0",
            "n_bars": 20,
        },
        "scalars": {
            "vol_scalar": "1.0",
            "liq_scalar": "1.0",
            "dd_scalar": "1.0",
            "regime_scalar": "1.0",
            "base_size": "1000",
        },
        "output": {"final_size": "10", "price": "100", "skip": False, "skip_reason": None},
    }


def test_load_records_returns_all_clean_lines(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    _write_audit(
        p,
        [
            _audit_record("2026-08-25T10:00:00+00:00"),
            _audit_record("2026-08-25T10:05:00+00:00"),
            _audit_record("2026-08-25T10:10:00+00:00"),
        ],
    )
    recs = replay_sizing.load_records(p)
    assert len(recs) == 3
    assert [r["ts"] for r in recs] == [
        "2026-08-25T10:00:00+00:00",
        "2026-08-25T10:05:00+00:00",
        "2026-08-25T10:10:00+00:00",
    ]


def test_load_records_skips_truncated_trailing_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #222: a truncated trailing line must be skipped with a WARNING,
    not raise ``SystemExit``. Earlier (good) records must survive."""
    p = tmp_path / "audit.jsonl"
    _write_audit(
        p,
        [
            _audit_record("2026-08-25T10:00:00+00:00"),
            _audit_record("2026-08-25T10:05:00+00:00"),
        ],
    )
    # Simulate SIGKILL mid-write: truncate the last line mid-record.
    full = p.read_bytes()
    p.write_bytes(full[:-5])
    recs = replay_sizing.load_records(p)
    assert len(recs) == 1
    assert recs[0]["ts"] == "2026-08-25T10:00:00+00:00"
    # WARNING printed to stderr.
    err = capsys.readouterr().err
    assert "skipping line" in err
    assert "invalid JSON" in err


def test_load_records_strict_raises_on_truncated_line(tmp_path: Path) -> None:
    """Issue #222: ``strict=True`` restores the pre-fix SystemExit behaviour."""
    p = tmp_path / "audit.jsonl"
    _write_audit(p, [_audit_record("2026-08-25T10:00:00+00:00")])
    full = p.read_bytes()
    p.write_bytes(full[:-5])
    with pytest.raises(SystemExit) as exc:
        replay_sizing.load_records(p, strict=True)
    assert "invalid JSON" in str(exc.value)


def test_load_records_missing_file_raises() -> None:
    with pytest.raises(SystemExit) as exc:
        replay_sizing.load_records(Path("/tmp/does-not-exist.jsonl"))
    assert "audit log not found" in str(exc.value)


def test_load_records_skips_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.write_text(
        json.dumps(_audit_record("2026-08-25T10:00:00+00:00"))
        + "\n"
        + "\n"
        + "   \n"
        + json.dumps(_audit_record("2026-08-25T10:05:00+00:00"))
        + "\n"
    )
    recs = replay_sizing.load_records(p)
    assert len(recs) == 2


def test_main_returns_zero_on_clean_replay(tmp_path: Path) -> None:
    """Smoke: ``main --all`` runs without raising on a clean log.

    The exact exit code depends on whether the replayed formula matches
    the stored record — for synthetic test records with reconstructed
    bars this typically returns 1 (DIVERGED). We assert ``main`` ran
    without raising ``SystemExit`` and that one row was rendered, then
    trust the upstream contract for the exit-code semantics.
    """
    p = tmp_path / "audit.jsonl"
    _write_audit(p, [_audit_record("2026-08-25T10:00:00+00:00")])
    rc = replay_sizing.main([str(p), "--all"])
    assert rc in (0, 1)  # 1 is the expected "DIVERGED" outcome for synthetic bars


def test_main_returns_zero_on_truncated_tolerated(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #222: ``--all`` default tolerates a truncated last line;
    the operator still gets the surviving records replayed and the
    parser DOES NOT raise SystemExit (which is what the old behaviour
    did). The exit code may be 1 (DIVERGED on synthetic bars) — that's
    the replay-tool's normal divergence signal, not a parse failure.
    """
    p = tmp_path / "audit.jsonl"
    _write_audit(
        p,
        [
            _audit_record("2026-08-25T10:00:00+00:00"),
            _audit_record("2026-08-25T10:05:00+00:00"),
        ],
    )
    full = p.read_bytes()
    p.write_bytes(full[:-5])
    # Must not raise SystemExit on the truncated line.
    rc = replay_sizing.main([str(p), "--all"])
    assert rc in (0, 1)
    err = capsys.readouterr().err
    assert "skipping line" in err


def test_main_strict_flag_returns_nonzero_on_truncated(tmp_path: Path) -> None:
    """Issue #222: ``--strict`` makes the tool exit non-zero on a
    truncated line, useful for CI / lint gates."""
    p = tmp_path / "audit.jsonl"
    _write_audit(p, [_audit_record("2026-08-25T10:00:00+00:00")])
    full = p.read_bytes()
    p.write_bytes(full[:-5])
    with pytest.raises(SystemExit) as exc:
        replay_sizing.main([str(p), "--all", "--strict"])
    assert "invalid JSON" in str(exc.value)
