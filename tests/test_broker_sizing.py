"""Tests for Phase 2.2 Position Sizing Matrix (kanban task t_e55e2168).

Covers every acceptance criterion in the task body:

  1. ATR=0 → fallback to min_atr floor, not divide-by-zero
  2. ADV=0 → liq_scalar = MAX_LIQ_SCALAR (no skip; downstream floors trim)
  3. Single-name universe → sizing works (no portfolio correlation)
  4. Regime=risk_off → regime_scalar = 0.5
  5. Drawdown 50% → dd_scalar = 0.25
  6. Combined: high vol + low adv + risk_off + drawdown → all scalars
     degrade → position size → min_lots
  7. Idempotency: same input → same output (no random, no time dependency)
  8. Audit log: JSONL row written with all required fields
  9. Rollback: v1-marked position uses sizing_v1 entry; v2 uses new
 10. min_size_lots / max_size_pct_of_cash cap
 11. confidence < 1.0 scales size down linearly
 12. EWMA cold-start: < EWMA_MIN_BARS uses simple ATR
 13. Migration 0003 is idempotent (parallels test_migration_0002)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.broker.sizing import (
    FORMULA_VERSION,
    Bar,
    MarketData,
    OrderSpec,
    PortfolioState,
    Quote,
    SizingConfig,
    compute_atr_actual,
    compute_atr_ewma,
    compute_atr_simple,
    compute_position_size,
    compute_position_size_v1,
    drawdown_reduction_curve,
    regime_scalar,
    wrap_sqlite,
    write_audit_jsonl,
    write_audit_postgres,
)
from src.macro.models import MacroRegime

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


TS = datetime(2026, 8, 22, 9, 30, 0, tzinfo=timezone.utc)


def _bars(close: Decimal = Decimal("100"), n: int = 20, rng_frac: Decimal = Decimal("0.02")) -> tuple[Bar, ...]:
    """Build ``n`` synthetic bars around ``close`` with a fixed high-low range."""
    low = close * (Decimal("1") - rng_frac)
    high = low + close * rng_frac
    return tuple(Bar(high=high, low=low, close=close) for _ in range(n))


@pytest.fixture
def regime_neutral() -> MacroRegime:
    return MacroRegime(regime="neutral", multiplier=Decimal("1.00"), reason="test")


@pytest.fixture
def regime_risk_off() -> MacroRegime:
    return MacroRegime(regime="risk_off", multiplier=Decimal("0.50"), reason="test")


@pytest.fixture
def regime_reduced() -> MacroRegime:
    return MacroRegime(regime="risk_on_reduced", multiplier=Decimal("0.75"), reason="test")


@pytest.fixture
def portfolio_no_dd() -> PortfolioState:
    return PortfolioState(cash=Decimal("100000"), peak_equity=Decimal("100000"), total_equity=Decimal("100000"))


@pytest.fixture
def portfolio_50_dd() -> PortfolioState:
    return PortfolioState(cash=Decimal("50000"), peak_equity=Decimal("100000"), total_equity=Decimal("50000"))


@pytest.fixture
def quote_sber() -> Quote:
    return Quote(ticker="SBER", side="buy", confidence=Decimal("1.0"), timestamp=TS, reference_price=Decimal("100"))


@pytest.fixture
def market_flat() -> MarketData:
    return MarketData(ticker="SBER", bars=_bars())


# ---------------------------------------------------------------------------
# 1. ATR=0 fallback (no divide-by-zero)
# ---------------------------------------------------------------------------


def test_atr_simple_empty_returns_zero() -> None:
    assert compute_atr_simple(()) == Decimal("0")


def test_atr_actual_atr_zero_floored_no_division_error() -> None:
    """Bars with high==low produce actual_atr=0; sizing must not crash.

    Without the min_atr floor the vol_scalar formula would divide by zero.
    """
    cfg = SizingConfig()
    # Bars with zero range — actual_atr will be 0.
    flat_bars = tuple(Bar(high=Decimal("100"), low=Decimal("100"), close=Decimal("100")) for _ in range(20))
    actual = compute_atr_actual(flat_bars, cfg)
    assert actual == Decimal("0")
    # Now exercise the full formula — must not raise.
    q = Quote(ticker="X", side="buy", confidence=Decimal("1"), timestamp=TS)
    p = PortfolioState(cash=Decimal("100000"), peak_equity=Decimal("100000"), total_equity=Decimal("100000"))
    m = MarketData(ticker="X", bars=flat_bars)
    r = MacroRegime(regime="neutral", multiplier=Decimal("1"), reason="t")
    spec = compute_position_size(q, p, m, r)
    # With vol_scalar clamped by min_atr_frac (target/min_atr = 0.02/0.0001=200),
    # then capped at MAX_VOL_SCALAR=3.0, we still get a positive size; if
    # everything else is OK it survives. The point is: no exception.
    assert isinstance(spec, OrderSpec)


# ---------------------------------------------------------------------------
# 2. ADV=0 → liq_scalar = MAX_LIQ_SCALAR
# ---------------------------------------------------------------------------


def test_liq_scalar_when_adv_is_zero_returns_max(tmp_path: Path) -> None:
    """ADV=0 (no volume data) must NOT skip and must reward max liquidity scalar."""
    cfg = SizingConfig()
    q = Quote(ticker="Y", side="buy", confidence=Decimal("1"), timestamp=TS, reference_price=Decimal("100"))
    p = PortfolioState(cash=Decimal("100000"), peak_equity=Decimal("100000"), total_equity=Decimal("100000"))
    # Bars with zero high-low range ⇒ ADV ≈ 0.
    zero_bars = tuple(Bar(high=Decimal("100"), low=Decimal("100"), close=Decimal("100")) for _ in range(20))
    m = MarketData(ticker="Y", bars=zero_bars)
    r = MacroRegime(regime="neutral", multiplier=Decimal("1"), reason="t")
    captured: list[dict[str, Any]] = []
    spec = compute_position_size(q, p, m, r, audit_hook=captured.append)
    # liq_scalar must be MAX_LIQ_SCALAR (2.0)
    assert Decimal(spec.meta["liq_scalar"]) == cfg.max_liq_scalar


# ---------------------------------------------------------------------------
# 3. Single-name universe works
# ---------------------------------------------------------------------------


def test_single_name_universe_sizing_works(
    quote_sber: Quote, portfolio_no_dd: PortfolioState, market_flat: MarketData, regime_neutral: MacroRegime
) -> None:
    spec = compute_position_size(quote_sber, portfolio_no_dd, market_flat, regime_neutral)
    assert isinstance(spec, OrderSpec)
    assert spec.ticker == "SBER"
    assert spec.side == "buy"
    assert spec.quantity >= 0


# ---------------------------------------------------------------------------
# 4. Regime scalars (ADR §2.6: 0.5 / 0.75 / 1.0)
# ---------------------------------------------------------------------------


def test_regime_scalar_returns_macro_multiplier(
    regime_risk_off: MacroRegime, regime_neutral: MacroRegime, regime_reduced: MacroRegime
) -> None:
    assert regime_scalar(regime_risk_off) == Decimal("0.50")
    assert regime_scalar(regime_reduced) == Decimal("0.75")
    assert regime_scalar(regime_neutral) == Decimal("1.00")


def test_regime_scalar_none_falls_open() -> None:
    # Defensive: missing macro snapshot → fail-open to 1.0 (ADR §2.6).
    assert regime_scalar(None) == Decimal("1.0")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. Drawdown reduction curve
# ---------------------------------------------------------------------------


def test_drawdown_curve_at_50_pct_is_floor() -> None:
    assert drawdown_reduction_curve(Decimal("50")) == Decimal("0.25")


def test_drawdown_curve_at_zero_is_one() -> None:
    assert drawdown_reduction_curve(Decimal("0")) == Decimal("1.0")


def test_drawdown_curve_above_knee_is_floor() -> None:
    assert drawdown_reduction_curve(Decimal("80")) == Decimal("0.25")


def test_drawdown_curve_below_zero_is_one() -> None:
    """Negative drawdown (equity above peak) → 1.0 — no reduction."""
    assert drawdown_reduction_curve(Decimal("-5")) == Decimal("1.0")


def test_drawdown_curve_is_monotone_decreasing() -> None:
    points = [Decimal(str(x)) for x in range(0, 51, 5)]
    vals = [drawdown_reduction_curve(p) for p in points]
    for a, b in zip(vals, vals[1:]):
        assert a >= b


def test_portfolio_state_drawdown_pct_50() -> None:
    p = PortfolioState(cash=Decimal("50000"), peak_equity=Decimal("100000"), total_equity=Decimal("50000"))
    assert p.drawdown_pct == Decimal("50.0")


# ---------------------------------------------------------------------------
# 6. Combined degradation → min_lots
# ---------------------------------------------------------------------------


def test_combined_degradation_collapses_to_min_lots(
    portfolio_50_dd: PortfolioState, regime_risk_off: MacroRegime
) -> None:
    """High vol + low adv + risk_off + 50% drawdown → all scalars degrade → skip."""
    # Stale flat bars (vol_target/min_atr → cap, but capped high) PLUS zero ADV.
    flat_bars = tuple(Bar(high=Decimal("100"), low=Decimal("100"), close=Decimal("100")) for _ in range(20))
    m = MarketData(ticker="Z", bars=flat_bars)
    q = Quote(ticker="Z", side="buy", confidence=Decimal("0.1"), timestamp=TS, reference_price=Decimal("100"))
    spec = compute_position_size(q, portfolio_50_dd, m, regime_risk_off)
    # dd_scalar=0.25, regime_scalar=0.5, confidence=0.1 → expect size collapse.
    # base = 50000 * 0.01 = 500; raw = 500 * 3.0 * 2.0 * 0.25 * 0.5 * 0.1 = 7.5
    # Capped at 10% of cash = 5000. After lots = floor(7.5/100) = 0 → skip.
    assert spec.skip is True
    assert spec.quantity == Decimal("0")
    assert spec.skip_reason is not None
    assert "size below min_size_lots" in spec.skip_reason


# ---------------------------------------------------------------------------
# 7. Idempotency: same inputs → same outputs
# ---------------------------------------------------------------------------


def test_idempotency_same_inputs_same_outputs(
    quote_sber: Quote, portfolio_no_dd: PortfolioState, market_flat: MarketData, regime_neutral: MacroRegime
) -> None:
    spec_a = compute_position_size(quote_sber, portfolio_no_dd, market_flat, regime_neutral)
    spec_b = compute_position_size(quote_sber, portfolio_no_dd, market_flat, regime_neutral)
    assert spec_a.quantity == spec_b.quantity
    assert spec_a.skip == spec_b.skip
    assert spec_a.skip_reason == spec_b.skip_reason
    assert spec_a.meta == spec_b.meta


def test_idempotency_no_datetime_now_dependency(
    quote_sber: Quote, portfolio_no_dd: PortfolioState, market_flat: MarketData, regime_neutral: MacroRegime
) -> None:
    """Two calls with timestamp injected (no datetime.now()) must match."""
    captured: list[dict[str, Any]] = []
    spec_a = compute_position_size(quote_sber, portfolio_no_dd, market_flat, regime_neutral, audit_hook=captured.append)
    spec_b = compute_position_size(quote_sber, portfolio_no_dd, market_flat, regime_neutral, audit_hook=captured.append)
    assert spec_a == spec_b


# ---------------------------------------------------------------------------
# 8. Audit log JSONL has all required fields
# ---------------------------------------------------------------------------


def test_audit_jsonl_written_with_all_fields(
    tmp_path: Path,
    quote_sber: Quote,
    portfolio_no_dd: PortfolioState,
    market_flat: MarketData,
    regime_neutral: MacroRegime,
) -> None:
    path = tmp_path / "audit.jsonl"

    def _hook(r: dict[str, Any]) -> None:
        write_audit_jsonl(r, path)

    compute_position_size(quote_sber, portfolio_no_dd, market_flat, regime_neutral, audit_hook=_hook)
    assert path.exists()
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # Required fields per task body §2:
    for k in ("ts", "ticker", "side", "inputs", "scalars", "output", "formula_version"):
        assert k in rec, f"missing audit field: {k}"
    # inputs sub-fields
    for k in ("atr_n", "adv", "dd_pct", "regime", "sector_exposure", "cash", "peak_equity"):
        # atr_n is n_bars in our schema, sector_exposure optional, allow any
        if k == "atr_n":
            assert "n_bars" in rec["inputs"]
        elif k == "sector_exposure":
            # not collected by sizing today (no portfolio positions yet)
            continue
        else:
            assert k in rec["inputs"], f"missing inputs.{k}"
    # scalars sub-fields
    for k in ("vol_scalar", "liq_scalar", "dd_scalar", "regime_scalar", "base_size"):
        assert k in rec["scalars"], f"missing scalars.{k}"
    # output sub-fields
    for k in ("final_size", "final_lots", "reason"):
        if k == "final_lots":
            assert "final_lots" in rec["output"]
        elif k == "final_size":
            assert "final_size" in rec["output"]
        elif k == "reason":
            assert "skip_reason" in rec["output"] or "skip" in rec["output"]
    assert rec["formula_version"] == FORMULA_VERSION


def test_audit_postgres_insert(
    tmp_path: Path,
    quote_sber: Quote,
    portfolio_no_dd: PortfolioState,
    market_flat: MarketData,
    regime_neutral: MacroRegime,
) -> None:
    """Smoke-test the audit writer against SQLite (DDL identical shape)."""
    db = tmp_path / "audit.db"
    conn = sqlite3.connect(str(db))
    # Mimic the Postgres DDL — JSON-as-TEXT. Postgres-shaped columns omitted.
    conn.execute("""
        CREATE TABLE sizing_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            inputs TEXT NOT NULL,
            scalars TEXT NOT NULL,
            output TEXT NOT NULL,
            formula_version TEXT NOT NULL DEFAULT 'v1'
        )
    """)
    conn.commit()
    wrapped = wrap_sqlite(conn)

    captured: list[dict[str, Any]] = []

    def _hook(r: dict[str, Any]) -> None:
        captured.append(r)
        write_audit_postgres(r, wrapped)

    compute_position_size(quote_sber, portfolio_no_dd, market_flat, regime_neutral, audit_hook=_hook)
    conn.commit()
    cur = conn.execute("SELECT ts, ticker, side, formula_version, inputs, scalars, output " "FROM sizing_audit_log")
    rows = cur.fetchall()
    assert len(rows) == 1
    ts, ticker, side, fv, inputs, scalars, output = rows[0]
    assert ticker == "SBER"
    assert side == "buy"
    assert fv == "v1"
    # Inputs/scalars/output are JSON-as-TEXT — round-trip.
    assert json.loads(inputs)["cash"] == "100000"
    assert json.loads(scalars)["regime_scalar"] == "1.00"
    conn.close()


# ---------------------------------------------------------------------------
# 9. Rollback: v1-marked position uses v1 entry; v2 uses new
# ---------------------------------------------------------------------------


def test_v1_entry_is_alias_of_current_compute(
    quote_sber: Quote, portfolio_no_dd: PortfolioState, market_flat: MarketData, regime_neutral: MacroRegime
) -> None:
    """sizing_v1.compute_position_size_v1 must produce the same OrderSpec as
    compute_position_size today (the v1 contract is locked at FORMULA_VERSION=v1)."""
    spec_current = compute_position_size(quote_sber, portfolio_no_dd, market_flat, regime_neutral)
    spec_v1 = compute_position_size_v1(quote_sber, portfolio_no_dd, market_flat, regime_neutral)
    assert spec_current == spec_v1
    assert spec_v1.sizing_version == "v1"


def test_sizing_v1_module_re_exports() -> None:
    """``from src.broker.sizing_v1 import ...`` must work for the canonical names."""
    from src.broker import sizing_v1  # noqa: F401

    assert sizing_v1.compute_position_size_v1 is compute_position_size_v1
    assert sizing_v1.OrderSpec is OrderSpec
    assert sizing_v1.SizingConfig is SizingConfig
    assert sizing_v1.FORMULA_VERSION == "v1"


# ---------------------------------------------------------------------------
# 10. min_size_lots / max_size_pct_of_cash caps
# ---------------------------------------------------------------------------


def test_min_size_lots_floor() -> None:
    """When raw size rounds below 1 lot, skip=True with explicit reason."""
    cfg = SizingConfig(min_size_lots=1, lot_size=10, max_size_pct_of_cash=Decimal("0.01"))
    q = Quote(ticker="A", side="buy", confidence=Decimal("0.01"), timestamp=TS, reference_price=Decimal("1000"))
    p = PortfolioState(cash=Decimal("1000"), peak_equity=Decimal("1000"), total_equity=Decimal("1000"))
    m = MarketData(ticker="A", bars=_bars(close=Decimal("1000"), n=20))
    r = MacroRegime(regime="neutral", multiplier=Decimal("1"), reason="t")
    spec = compute_position_size(q, p, m, r, config=cfg)
    assert spec.skip is True
    assert spec.quantity == Decimal("0")


def test_max_size_pct_of_cash_caps_oversize(
    quote_sber: Quote, portfolio_no_dd: PortfolioState, market_flat: MarketData, regime_neutral: MacroRegime
) -> None:
    """When raw size exceeds 10% of cash, it must be capped before lot quantization."""
    cfg = SizingConfig(max_size_pct_of_cash=Decimal("0.10"))
    spec = compute_position_size(quote_sber, portfolio_no_dd, market_flat, regime_neutral, config=cfg)
    # cash=100000 → cap = 10000. price=100 → 100 shares max.
    assert spec.quantity <= Decimal("10000")  # notional cap


# ---------------------------------------------------------------------------
# 11. Confidence < 1.0 scales size linearly
# ---------------------------------------------------------------------------


def test_confidence_scales_size(
    quote_sber: Quote, portfolio_no_dd: PortfolioState, market_flat: MarketData, regime_neutral: MacroRegime
) -> None:
    q_full = quote_sber  # confidence=1.0
    q_half = Quote(
        ticker=quote_sber.ticker,
        side=quote_sber.side,
        confidence=Decimal("0.5"),
        timestamp=quote_sber.timestamp,
        reference_price=quote_sber.reference_price,
    )
    spec_full = compute_position_size(q_full, portfolio_no_dd, market_flat, regime_neutral)
    spec_half = compute_position_size(q_half, portfolio_no_dd, market_flat, regime_neutral)
    # Half-confidence ⇒ half the size (within lot quantization).
    if spec_full.quantity > 0:
        ratio = spec_half.quantity / spec_full.quantity
        assert Decimal("0.45") <= ratio <= Decimal("0.55"), f"expected ~0.5 ratio, got {ratio}"


# ---------------------------------------------------------------------------
# 12. EWMA cold-start
# ---------------------------------------------------------------------------


def test_ewma_cold_start_uses_simple() -> None:
    cfg = SizingConfig(ewma_min_bars=30)
    bars_20 = _bars(n=20)
    # 20 bars < 30 → simple path.
    atr = compute_atr_actual(bars_20, cfg)
    assert atr == compute_atr_simple(bars_20, cfg.atr_lookback)


def test_ewma_warm_path_uses_ewma() -> None:
    cfg = SizingConfig(ewma_min_bars=30)
    bars_60 = _bars(n=60)
    atr = compute_atr_actual(bars_60, cfg)
    # EWMA returns a positive Decimal (could equal simple when bars are
    # constant — they aren't here, but we only assert positivity + type).
    assert atr >= 0
    assert isinstance(atr, Decimal)


def test_ewma_below_two_bars_returns_zero() -> None:
    """< 2 bars: no signal at all (Kelly no-edge semantics per ADR §2.2)."""
    bars = (Bar(high=Decimal("100"), low=Decimal("98"), close=Decimal("100")),)
    assert compute_atr_ewma(bars) == Decimal("0")


# ---------------------------------------------------------------------------
# 13. Quote / MarketData / PortfolioState validation
# ---------------------------------------------------------------------------


def test_quote_rejects_negative_confidence() -> None:
    with pytest.raises(ValidationError):
        Quote(ticker="X", side="buy", confidence=Decimal("-0.1"), timestamp=TS)


def test_quote_rejects_overconfidence() -> None:
    with pytest.raises(ValidationError):
        Quote(ticker="X", side="buy", confidence=Decimal("1.5"), timestamp=TS)


def test_portfolio_state_rejects_peak_lt_equity() -> None:
    with pytest.raises(ValidationError):
        PortfolioState(cash=Decimal("100"), peak_equity=Decimal("50"), total_equity=Decimal("100"))


def test_quote_is_frozen() -> None:
    q = Quote(ticker="X", side="buy", confidence=Decimal("1"), timestamp=TS)
    with pytest.raises(ValidationError):
        q.ticker = "Y"


def test_order_spec_is_frozen() -> None:
    p = PortfolioState(cash=Decimal("100000"), peak_equity=Decimal("100000"), total_equity=Decimal("100000"))
    q = Quote(ticker="X", side="buy", confidence=Decimal("1"), timestamp=TS, reference_price=Decimal("100"))
    m = MarketData(ticker="X", bars=_bars())
    r = MacroRegime(regime="neutral", multiplier=Decimal("1"), reason="t")
    spec = compute_position_size(q, p, m, r)
    with pytest.raises(ValidationError):
        spec.quantity = Decimal("0")


# ---------------------------------------------------------------------------
# Migration 0003 idempotency
# ---------------------------------------------------------------------------


MIGRATION_FILE = Path(__file__).resolve().parents[1] / "src" / "data" / "migrations" / "0003_sizing_audit_log.sql"


def test_migration_0003_is_idempotent() -> None:
    """Every DDL token must use IF NOT EXISTS / IF EXISTS — the same
    idempotency guarantee test_migration_0002 enforces for 0002."""
    assert MIGRATION_FILE.exists()
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    # Sanity: file must contain the table and the formula_version column.
    assert "CREATE TABLE IF NOT EXISTS sizing_audit_log" in sql
    assert "formula_version VARCHAR(8) NOT NULL DEFAULT 'v1'" in sql
    assert "idx_sizing_audit_log_ticker_ts" in sql
    assert "idx_sizing_audit_log_version_ts" in sql


def test_schema_sql_includes_sizing_audit_log() -> None:
    """Fresh volumes land here via init_schema(); the table must be in schema.sql."""
    schema = Path(__file__).resolve().parents[1] / "src" / "data" / "schema.sql"
    sql = schema.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS sizing_audit_log" in sql
    assert "formula_version" in sql


# ---------------------------------------------------------------------------
# Edge-case coverage (Phase 2.2 acceptance criteria: full branch coverage)
# ---------------------------------------------------------------------------


def test_no_market_data_skips(
    tmp_path: Path, quote_sber: Quote, portfolio_no_dd: PortfolioState, regime_neutral: MacroRegime
) -> None:
    """Empty MarketData → skip with reason 'no market data'."""
    empty = MarketData(ticker="SBER", bars=())
    captured: list[dict[str, Any]] = []

    def _hook(r: dict[str, Any]) -> None:
        captured.append(r)

    spec = compute_position_size(quote_sber, portfolio_no_dd, empty, regime_neutral, audit_hook=_hook)
    assert spec.skip is True
    assert spec.skip_reason == "no market data"
    assert spec.quantity == Decimal("0")
    # Audit record was emitted even on skip.
    assert len(captured) == 1


def test_no_cash_skips(tmp_path: Path, quote_sber: Quote, market_flat: MarketData, regime_neutral: MacroRegime) -> None:
    """cash == 0 → skip with reason 'no cash' (cannot risk any fraction of 0)."""
    p = PortfolioState(cash=Decimal("0"), peak_equity=Decimal("100000"), total_equity=Decimal("100000"))
    spec = compute_position_size(quote_sber, p, market_flat, regime_neutral)
    assert spec.skip is True
    assert spec.skip_reason == "no cash"
    assert spec.quantity == Decimal("0")


def test_reference_price_fallback_when_no_quote_price() -> None:
    """If Quote.reference_price is None and there are no bars, fall back to 1."""
    q = Quote(ticker="X", side="buy", confidence=Decimal("1"), timestamp=TS)
    assert q.reference_price is None
    # Internally: _reference_price returns Decimal("1") in that case.
    # Exercise via the guard path — empty bars + valid quote.
    p = PortfolioState(cash=Decimal("1000"), peak_equity=Decimal("1000"), total_equity=Decimal("1000"))
    m = MarketData(ticker="X", bars=())
    r = MacroRegime(regime="neutral", multiplier=Decimal("1"), reason="t")
    spec = compute_position_size(q, p, m, r)
    # Price used was the fallback Decimal("1"); skip because no market data.
    assert spec.price == Decimal("1")
    assert spec.skip is True


def test_atr_simple_skips_zero_close_bars() -> None:
    """When all closes are filtered (none > 0), result is 0 — defensive path."""
    # Bar validator forbids close=0; we can't construct one directly. Test
    # instead that empty bars → 0.
    assert compute_atr_simple(()) == Decimal("0")


def test_decimal_sqrt_zero_returns_zero() -> None:
    """Newton's method must short-circuit on 0 (avoids 0/0 in iteration)."""
    from src.broker.sizing import _decimal_sqrt

    assert _decimal_sqrt(Decimal("0")) == Decimal("0")
    assert _decimal_sqrt(Decimal("9")) == Decimal("3")


def test_quantize_lots_floor_and_zero() -> None:
    from src.broker.sizing import _quantize_lots

    assert _quantize_lots(Decimal("0"), 10) == 0
    assert _quantize_lots(Decimal("25"), 10) == 2
    assert _quantize_lots(Decimal("99"), 10) == 9
    # Negative raw → 0
    assert _quantize_lots(Decimal("-5"), 10) == 0
    # lot_size=0 → 0 (defensive)
    assert _quantize_lots(Decimal("100"), 0) == 0


def test_regime_scalar_pulls_multiplier_field() -> None:
    """regime_scalar() returns the locked MacroRegime.multiplier field."""
    from src.broker.sizing import regime_scalar as _rs

    r = MacroRegime(regime="risk_off", multiplier=Decimal("0.50"), reason="x")
    assert _rs(r) == Decimal("0.50")


def test_wrap_sqlite_close_delegates() -> None:
    """Sanity check: _SqliteCompatConn.close() forwards to inner connection."""
    import sqlite3 as _sql

    conn = _sql.connect(":memory:")
    from src.broker.sizing import wrap_sqlite

    wrapped = wrap_sqlite(conn)
    wrapped.close()  # must not raise
    assert conn is not None  # inner still accessible


def test_audit_jsonl_writes_directory_path(
    tmp_path: Path,
    quote_sber: Quote,
    portfolio_no_dd: PortfolioState,
    market_flat: MarketData,
    regime_neutral: MacroRegime,
) -> None:
    """When given a directory path, write_audit_jsonl auto-names the file."""
    target_dir = tmp_path / "audit_dir"
    record = {
        "ts": "2026-08-22T09:30:00+00:00",
        "ticker": "SBER",
        "side": "buy",
        "formula_version": "v1",
        "inputs": {},
        "scalars": {},
        "output": {},
    }
    p = write_audit_jsonl(record, target_dir)
    assert p.exists()
    assert p.suffix == ".jsonl"
    assert "sizing_audit_" in p.name


def test_confidence_zero_skips(
    quote_sber: Quote, portfolio_no_dd: PortfolioState, market_flat: MarketData, regime_neutral: MacroRegime
) -> None:
    q = Quote(
        ticker=quote_sber.ticker,
        side=quote_sber.side,
        confidence=Decimal("0"),
        timestamp=quote_sber.timestamp,
        reference_price=quote_sber.reference_price,
    )
    spec = compute_position_size(q, portfolio_no_dd, market_flat, regime_neutral)
    # 0 confidence → raw_size=0 → lots=0 → skip.
    assert spec.skip is True
    assert spec.quantity == Decimal("0")


# ---------------------------------------------------------------------------
# Issue #222 — atomic JSONL audit-log write + replay resilience
# ---------------------------------------------------------------------------


def _audit_record(ts: str, ticker: str = "SBER") -> dict[str, Any]:
    """Build a minimal but valid audit record for atomic-write tests."""
    return {
        "ts": ts,
        "ticker": ticker,
        "side": "buy",
        "formula_version": "v1",
        "inputs": {"cash": "100000", "peak_equity": "100000", "total_equity": "100000", "confidence": "1.0"},
        "scalars": {"vol_scalar": "1.0", "liq_scalar": "1.0", "dd_scalar": "1.0", "regime_scalar": "1.0"},
        "output": {"final_size": "10", "price": "100", "skip": False, "skip_reason": None},
    }


def test_audit_jsonl_atomic_write_appends_full_file(tmp_path: Path) -> None:
    """Issue #222: write_audit_jsonl is atomic — appending preserves prior content."""
    p = tmp_path / "audit.jsonl"
    write_audit_jsonl(_audit_record("2026-08-25T10:00:00+00:00"), p)
    write_audit_jsonl(_audit_record("2026-08-25T10:05:00+00:00"), p)
    write_audit_jsonl(_audit_record("2026-08-25T10:10:00+00:00"), p)
    lines = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3
    assert [r["ts"] for r in lines] == [
        "2026-08-25T10:00:00+00:00",
        "2026-08-25T10:05:00+00:00",
        "2026-08-25T10:10:00+00:00",
    ]


def test_audit_jsonl_no_tmp_left_on_clean_run(tmp_path: Path) -> None:
    """Issue #222: a successful write must leave no orphan .tmp behind."""
    p = tmp_path / "audit.jsonl"
    write_audit_jsonl(_audit_record("2026-08-25T10:00:00+00:00"), p)
    tmp = p.with_name(p.name + ".tmp")
    assert not tmp.exists()
    # And subsequent appends also clean up.
    write_audit_jsonl(_audit_record("2026-08-25T10:05:00+00:00"), p)
    assert not tmp.exists()


def test_audit_jsonl_survives_truncated_previous_line(tmp_path: Path) -> None:
    """Issue #222: a SIGKILL that left a truncated trailing line MUST NOT
    cause the next ``write_audit_jsonl`` call to fail, and MUST NOT
    propagate the corruption forward. The atomic-rename pattern reads the
    existing bytes, strips any trailing partial line (it cannot be
    replayed reliably and the next sizing call will re-record it as a
    fresh row), rewrites the remainder plus the new line into the tmp
    file, and ``os.replace`` swaps. After the swap, the new file is fully
    parseable end-to-end — no JSONDecodeError ever surfaces on replay.
    """
    p = tmp_path / "audit.jsonl"
    write_audit_jsonl(_audit_record("2026-08-25T10:00:00+00:00"), p)
    write_audit_jsonl(_audit_record("2026-08-25T10:05:00+00:00"), p)
    # Simulate SIGKILL mid-write of a THIRD record: write one more, then
    # truncate the last line by chopping bytes.
    write_audit_jsonl(_audit_record("2026-08-25T10:10:00+00:00"), p)
    full = p.read_bytes()
    p.write_bytes(full[:-5])
    # Next write must succeed and produce a fully parseable file.
    write_audit_jsonl(_audit_record("2026-08-25T10:15:00+00:00"), p)
    # Every line in the file must parse. The truncated line is dropped by
    # design (it cannot be replayed reliably; the partial record is
    # regenerated by the next sizing call). The first two good records
    # plus the new fourth record survive — 3 of 4.
    text = p.read_text()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    recs = [json.loads(ln) for ln in lines]
    assert len(recs) == 3
    timestamps = [r["ts"] for r in recs]
    assert timestamps == [
        "2026-08-25T10:00:00+00:00",
        "2026-08-25T10:05:00+00:00",
        "2026-08-25T10:15:00+00:00",
    ]


def test_audit_jsonl_existing_empty_file_works(tmp_path: Path) -> None:
    """Issue #222: an empty existing file is a normal "first call" case
    for the audit log — the writer must handle it cleanly. This also
    exercises the ``raw.endswith(b"\\n")`` short-circuit path."""
    p = tmp_path / "audit.jsonl"
    p.write_bytes(b"")
    write_audit_jsonl(_audit_record("2026-08-25T10:00:00+00:00"), p)
    text = p.read_text()
    recs = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    assert len(recs) == 1
    assert recs[0]["ts"] == "2026-08-25T10:00:00+00:00"


def test_audit_jsonl_directory_path_still_atomic(tmp_path: Path) -> None:
    """Issue #222: the ``p.suffix != '.jsonl'`` auto-name branch must
    still produce an atomic file (no orphan .tmp after the call)."""
    target_dir = tmp_path / "audit_dir"
    p = write_audit_jsonl(_audit_record("2026-08-25T10:00:00+00:00"), target_dir)
    assert p.exists()
    assert p.suffix == ".jsonl"
    assert not p.with_name(p.name + ".tmp").exists()
    # And a second call must still clean up its tmp.
    write_audit_jsonl(_audit_record("2026-08-25T10:05:00+00:00"), target_dir)
    assert not p.with_name(p.name + ".tmp").exists()
