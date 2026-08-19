"""Tests for Phase 2.6 cross-source validation smoke runner.

The smoke runner is a script, not a library, so tests focus on the
helper functions it exposes internally:

- ``_make_series`` — converts a close list into a SourceSeries.
- ``_random_walk_close`` — deterministic geometric Brownian motion.
- ``_issue_count`` — wraps ``QualityReport.by_severity(Severity)``.

End-to-end coverage of ``check_cross_source`` semantics lives in
``tests/test_cross_source.py`` (the real library). The smoke runner
is only re-tested here to guarantee the wiring against
``check_cross_source`` and ``Severity`` survives.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

# Load the smoke runner as a module without executing main().
_SMOKE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cross_source_smoke.py"
_loader = importlib.util.spec_from_file_location("cross_source_smoke", _SMOKE_PATH)
assert _loader is not None and _loader.loader is not None
smoke = importlib.util.module_from_spec(_loader)
sys.modules["cross_source_smoke"] = smoke
_loader.loader.exec_module(smoke)

from src.data.quality.severity import Severity  # noqa: E402


def test_make_series_builds_dated_pairs():
    closes = [100.0, 101.5, 99.8]
    series = smoke._make_series(closes, "tinkoff_md", date(2024, 1, 1))
    assert series.source_name == "tinkoff_md"
    assert len(series.bars) == 3
    assert series.bars[0] == (date(2024, 1, 1), 100.0)
    assert series.bars[1] == (date(2024, 1, 2), 101.5)
    assert series.bars[2] == (date(2024, 1, 3), 99.8)


def test_make_series_rejects_empty():
    with pytest.raises(ValueError, match="closes must be non-empty"):
        smoke._make_series([], "tinkoff_md", date(2024, 1, 1))


def test_random_walk_is_deterministic():
    a = smoke._random_walk_close(20, 250.0, 0.0005, 0.01, seed=42)
    b = smoke._random_walk_close(20, 250.0, 0.0005, 0.01, seed=42)
    assert a == b
    # Different seed -> different path
    c = smoke._random_walk_close(20, 250.0, 0.0005, 0.01, seed=43)
    assert a != c


def test_random_walk_length_and_start():
    closes = smoke._random_walk_close(15, 100.0, 0.0, 0.01, seed=1)
    assert len(closes) == 15
    assert closes[0] == 100.0


def test_issue_count_returns_zero_for_clean_report():
    import random as _random

    a = smoke._random_walk_close(60, 250.0, 0.0005, 0.01, seed=42)
    rng = _random.Random(7)
    b = [x * (1 + rng.gauss(0, 0.001)) for x in a]
    sa = smoke._make_series(a, "tinkoff_md", date(2024, 1, 1))
    sb = smoke._make_series(b, "moex_iss", date(2024, 1, 1))
    report = smoke.check_cross_source("SBER", sa, sb)
    assert smoke._issue_count(report, Severity.HIGH) == 0
    assert smoke._issue_count(report, Severity.MEDIUM) == 0


def test_issue_count_returns_one_for_insufficient_data():
    a = smoke._random_walk_close(3, 250.0, 0.0005, 0.01, seed=42)
    b = list(a)
    sa = smoke._make_series(a, "tinkoff_md", date(2024, 1, 1))
    sb = smoke._make_series(b, "moex_iss", date(2024, 1, 1))
    report = smoke.check_cross_source("SBER", sa, sb)
    assert smoke._issue_count(report, Severity.HIGH) >= 1


def test_issue_count_catches_split_divergence():
    a = smoke._random_walk_close(60, 250.0, 0.0005, 0.01, seed=42)
    b = list(a)
    for i in range(30, 60):
        b[i] = b[i] * 0.95
    sa = smoke._make_series(a, "tinkoff_md", date(2024, 1, 1))
    sb = smoke._make_series(b, "moex_iss", date(2024, 1, 1))
    report = smoke.check_cross_source("SBER", sa, sb)
    assert smoke._issue_count(report, Severity.HIGH) >= 1
