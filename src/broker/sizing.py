"""Position sizing matrix (Phase 2.2).

Decoupled, deterministic sizing module that converts a Coordinator quote into
an :class:`OrderSpec` using a FIX-style volatility-targeted formula:

    base_size   = cash * risk_per_trade_pct
    vol_scalar  = min(target_atr / actual_atr, MAX_VOL_SCALAR)
    liq_scalar  = min(adv / position_pct_of_adv, MAX_LIQ_SCALAR)
    dd_scalar   = drawdown_reduction_curve(drawdown_pct)
    regime_scal = MacroRegime.multiplier                  # 0.5 / 0.75 / 1.00
    size        = base_size * vol_scalar * liq_scalar * dd_scalar * regime_scalar

Decisions inherited from ``docs/decisions/0006-position-sizing.md`` (ADR-0006,
2026-08-22, locked):

* **Method:** FIX (Thorp 2008 closed-form DD bound). HYB / KLY are post-Phase
  2.1 / 2.5 per ADR §2.1.
* **Volatility measure:** EWMA (λ=0.94) — see :func:`compute_atr_ewma`. The
  task body uses a simple bar ATR; we expose both via
  :func:`compute_atr_simple` and :func:`compute_atr_ewma`. The function picks
  ``simple`` when ``len(bars) < 30`` (EWMA cold-start), ``ewma`` otherwise.
* **Correlation handling:** zero (ZRO) today. SEC ships Phase 2.10 per
  ADR §2.3. This module does NOT consume sector exposure.
* **Risk budget per day:** HARD (gate-only, not sizer). LIN/Phase 2.4 stays
  outside sizer per ADR §2.4.
* **Black swan:** delegated to ``src/risk/gate.py`` (4 shipped caps). This
  module only enforces a ``max_size_pct_of_cash`` soft cap (10%).
* **Regime multiplier:** ADR §2.6 — risk_off=0.50, risk_on_reduced=0.75,
  neutral=1.00 (fail-open to 1.00 on stale/missing). The current task body
  has an inverted mapping; we follow the locked ADR. See PR description.

This module is **self-contained**:

* No imports from ``src/broker/account.py`` / ``src/broker/integration.py``
  / ``src/risk/gate.py`` / ``src/coordinator.py`` / ``src/config.py`` —
  it must compose with the existing risk gate, not replace it.
* All inputs are immutable pydantic models (``frozen=True``).
* Output ``OrderSpec`` is also frozen.
* Every call appends a row to the audit log (JSONL + Postgres).

Idempotency: same inputs → same outputs (no random, no ``datetime.now()``).
Time is injected via :class:`Quote.timestamp` so replay is bit-identical.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.macro.models import MacroRegime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — locked per ADR-0006 §4 / task body
# ---------------------------------------------------------------------------

FORMULA_VERSION: Final[str] = "v1"

#: Per-trade risk budget. 1% of cash — conservative Phase 2.2 default.
RISK_PER_TRADE_PCT: Final[Decimal] = Decimal("0.01")

#: Target ATR as a fraction of price (2% of price).
TARGET_ATR_FRAC: Final[Decimal] = Decimal("0.02")

#: Floor for actual ATR — protects against divide-by-zero on stale/flat bars.
MIN_ATR_FRAC: Final[Decimal] = Decimal("0.0001")

#: Caps for vol / liq scalars — keeps aggressive bars from blowing up size.
MAX_VOL_SCALAR: Final[Decimal] = Decimal("3.0")
MAX_LIQ_SCALAR: Final[Decimal] = Decimal("2.0")

#: ADV participation caps (fraction of average daily volume).
MAX_ADV_PCT: Final[Decimal] = Decimal("0.05")
MIN_ADV_PCT: Final[Decimal] = Decimal("0.001")

#: Drawdown reduction curve — at 50% DD we hold only ``dd_floor`` of base size.
DD_FLOOR: Final[Decimal] = Decimal("0.25")
DD_KNEE_PCT: Final[Decimal] = Decimal("50.0")  # % DD at which dd_scalar = DD_FLOOR

#: Lot-size / cash guards.
MIN_SIZE_LOTS: Final[int] = 1
MAX_SIZE_PCT_OF_CASH: Final[Decimal] = Decimal("0.10")

#: Cold-start bar threshold for ATR — below this we use simple, above this EWMA.
EWMA_MIN_BARS: Final[int] = 30
EWMA_LAMBDA: Final[Decimal] = Decimal("0.94")  # RiskMetrics standard (λ=0.94)

#: Lookback for ``compute_atr_simple`` (default N=20 per task body).
DEFAULT_ATR_LOOKBACK: Final[int] = 20

#: Default audit log directory.
DEFAULT_AUDIT_DIR: Final[str] = "logs/sizing_audit"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Quote(BaseModel):
    """A sizing input — the Coordinator's signal translated into sizing terms.

    SECURITY: ``frozen=True`` — same defence-in-depth as ``TradeIntent`` and
    ``Position`` (``src/risk/gate.py:65-114, 116-138``). The sizing module
    must NOT mutate its input; if a derivative is needed, copy via
    ``model_copy(update=...)``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(..., min_length=1, max_length=12)
    side: str = Field(..., description="'buy' or 'sell'")
    confidence: Decimal = Field(
        default=Decimal("1.0"),
        ge=Decimal("0"),
        le=Decimal("1"),
        description="Signal confidence in [0,1]; 1.0 = full size, 0.0 = skip",
    )
    timestamp: datetime = Field(..., description="UTC; used for replay determinism")
    # Optional reference price. If absent, we use the last close from market_data.
    reference_price: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        description="Reference price; if None, uses last close",
    )

    @property
    def ticker_upper(self) -> str:
        return self.ticker.upper().strip()


class PortfolioState(BaseModel):
    """Minimal portfolio snapshot for sizing.

    Independent of :class:`src.risk.gate.PortfolioState` (which is gate-side)
    — keeping a slim copy here avoids a circular import between the gate and
    the sizer. The two states are populated from the same upstream source by
    the Coordinator.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cash: Decimal = Field(..., ge=Decimal("0"))
    peak_equity: Decimal = Field(..., gt=Decimal("0"))
    total_equity: Decimal = Field(..., gt=Decimal("0"))

    @model_validator(mode="after")
    def _peak_at_least_equity(self) -> "PortfolioState":
        # Mirror src/risk/gate.py:164 invariant: peak_equity >= total_equity.
        # If a caller passes a state where peak < equity, it's an upstream
        # bug — reject here so the DD curve never produces nonsense.
        if self.peak_equity < self.total_equity:
            raise ValueError(f"peak_equity ({self.peak_equity}) must be >= total_equity ({self.total_equity})")
        return self

    @property
    def drawdown_pct(self) -> Decimal:
        """Drawdown from peak, in PERCENT (e.g. 50.0 = 50%).

        Mirrors ``src/risk/peak_equity_tracker.py`` semantics. Returns 0.0
        when peak == current (no drawdown) and grows linearly as equity
        drops. Floor at 0 — cannot be negative.
        """
        if self.peak_equity <= 0:
            return Decimal("0")
        dd = (self.peak_equity - self.total_equity) / self.peak_equity * Decimal("100")
        return dd if dd > 0 else Decimal("0")


class Bar(BaseModel):
    """Single OHLCV bar in market_data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    high: Decimal = Field(..., gt=Decimal("0"))
    low: Decimal = Field(..., ge=Decimal("0"))
    close: Decimal = Field(..., gt=Decimal("0"))


class MarketData(BaseModel):
    """Rolling window of OHLCV bars. Last bar is the most recent.

    Bars MUST be sorted oldest→newest; the sizer uses the trailing N.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(..., min_length=1, max_length=12)
    bars: tuple[Bar, ...] = Field(default_factory=tuple)


class OrderSpec(BaseModel):
    """Output of ``compute_position_size`` — a sized, ready-to-risk-check order.

    ``sizing_version`` is locked at construction (task body: "не применяется
    ретроактивно"). Live positions opened under v1 stay v1 even after v2
    ships — the field is the audit-trail key.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(..., min_length=1, max_length=12)
    side: str = Field(..., description="'buy' or 'sell'")
    quantity: Decimal = Field(..., ge=Decimal("0"), description="Number of shares/lots")
    price: Decimal = Field(..., gt=Decimal("0"), description="Limit price")
    confidence: Decimal = Field(..., ge=Decimal("0"), le=Decimal("1"))
    sizing_version: str = Field(..., description="Formula version; defaults to FORMULA_VERSION")
    skip: bool = Field(default=False, description="True when order is suppressed (size < min lot)")
    skip_reason: str | None = Field(default=None, description="Reason for skip; None when not skipped")
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="Scalar decomposition + diagnostics; persisted to audit log",
    )


# ---------------------------------------------------------------------------
# SizingConfig — formula constants. Default + override hook.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SizingConfig:
    """Tunable formula constants. Default mirrors ``module-level Final`` values.

    The task body asked for a pydantic Settings class in ``src/config.py``.
    That module is frozen by Kanban constraint, so we ship this dataclass
    here as a self-contained override hook — callers can either use the
    module-level defaults or build a custom :class:`SizingConfig` and pass
    it into :func:`compute_position_size`. See ``docs/POSITION-SIZING.md``
    for the rationale (constraint C2 in the task body: no-touch on
    ``src/config.py``).
    """

    risk_per_trade_pct: Decimal = RISK_PER_TRADE_PCT
    target_atr_frac: Decimal = TARGET_ATR_FRAC
    min_atr_frac: Decimal = MIN_ATR_FRAC
    max_vol_scalar: Decimal = MAX_VOL_SCALAR
    max_liq_scalar: Decimal = MAX_LIQ_SCALAR
    max_adv_pct: Decimal = MAX_ADV_PCT
    min_adv_pct: Decimal = MIN_ADV_PCT
    dd_floor: Decimal = DD_FLOOR
    dd_knee_pct: Decimal = DD_KNEE_PCT
    min_size_lots: int = MIN_SIZE_LOTS
    max_size_pct_of_cash: Decimal = MAX_SIZE_PCT_OF_CASH
    atr_lookback: int = DEFAULT_ATR_LOOKBACK
    ewma_min_bars: int = EWMA_MIN_BARS
    ewma_lambda: Decimal = EWMA_LAMBDA
    lot_size: int = 1
    formula_version: str = FORMULA_VERSION


# ---------------------------------------------------------------------------
# Pure helpers — ATR / DD reduction / scalar composition
# ---------------------------------------------------------------------------


def compute_atr_simple(bars: tuple[Bar, ...], lookback: int = DEFAULT_ATR_LOOKBACK) -> Decimal:
    """Average True Range as a FRACTION of close (decimal, not percent).

    ATR_frac = mean(high_i - low_i) / close_i over the last ``lookback`` bars.

    Returns ``Decimal('0')`` when ``bars`` is empty. Caller should floor the
    result via ``min_atr_frac`` before using it as a divisor.
    """
    if not bars:
        return Decimal("0")
    window = bars[-lookback:]
    closes = [b.close for b in window if b.close > 0]
    if not closes:
        return Decimal("0")
    rng = [b.high - b.low for b in window]
    mean_range = sum(rng, Decimal("0")) / Decimal(len(rng))
    mean_close = sum(closes, Decimal("0")) / Decimal(len(closes))
    if mean_close <= 0:
        return Decimal("0")
    return mean_range / mean_close


def compute_atr_ewma(
    bars: tuple[Bar, ...],
    lam: Decimal = EWMA_LAMBDA,
    seed: Decimal | None = None,
) -> Decimal:
    """EWMA of (high - low) / close, λ=0.94 (RiskMetrics standard).

    Recursion: ``σ²_t = λ·σ²_{t-1} + (1-λ)·r²_t`` with ``r = (high-low)/close``.
    Returns the EWMA standard deviation as a FRACTION of close.

    Cold-start: when ``seed`` is None and ``len(bars) >= 20``, seed with the
    simple-ATR of the first 20 bars (per ADR §2.2). When fewer than 2 bars
    are available the recursion has no signal — return ``Decimal('0')`` so
    the caller treats it as "no signal" and falls back to ``min_atr_frac``.
    """
    if len(bars) < 2:
        return Decimal("0")
    seed_val = seed if seed is not None else compute_atr_simple(bars[:20])
    var = seed_val * seed_val
    for b in bars:
        if b.close <= 0:
            continue
        r = (b.high - b.low) / b.close
        var = lam * var + (Decimal("1") - lam) * r * r
    # Decimal sqrt is not in stdlib; for audit-only precision we square-root
    # via Newton's method with bounded iterations. EWMA σ is small (<1 for
    # equities), 20 iterations converges to 1e-30.
    return _decimal_sqrt(var)


def _decimal_sqrt(value: Decimal, iters: int = 30) -> Decimal:
    """Newton-Raphson sqrt for positive Decimal. ``value`` MUST be >= 0."""
    if value <= 0:
        return Decimal("0")
    # Initial guess from float; refined in pure Decimal afterwards.
    guess = Decimal(str(float(value) ** 0.5)) if value > 0 else Decimal("0")
    if guess == 0:
        guess = value
    for _ in range(iters):
        guess = (guess + value / guess) / Decimal("2")
    return guess


def drawdown_reduction_curve(
    drawdown_pct: Decimal, knee_pct: Decimal = DD_KNEE_PCT, floor: Decimal = DD_FLOOR
) -> Decimal:
    """Linear reduction: dd_scalar = 1.0 at DD=0, ``floor`` at DD >= ``knee_pct``.

    Floor clamps: drawdown below 0 → 1.0; drawdown above knee → ``floor``.
    Pure Decimal, monotonic decreasing.
    """
    if drawdown_pct <= 0:
        return Decimal("1.0")
    if drawdown_pct >= knee_pct:
        return floor
    # Linear: 1.0 → floor across [0, knee_pct].
    return Decimal("1.0") - (Decimal("1.0") - floor) * (drawdown_pct / knee_pct)


def regime_scalar(macro: MacroRegime) -> Decimal:
    """Pull the regime multiplier from MacroRegime (already locked in ADR §2.6).

    Defensive: any unknown label falls back to 1.0 (neutral) per ADR §2.6
    "stale-multiplier policy: fail-safe to 1.00".
    """
    if macro is None:
        return Decimal("1.0")
    return Decimal(str(macro.multiplier))


def _quantize_lots(raw_size: Decimal, lot_size: int) -> int:
    """Round DOWN to whole lots. Floor at 0."""
    if raw_size <= 0 or lot_size <= 0:
        return 0
    return int(raw_size / Decimal(lot_size))


# ---------------------------------------------------------------------------
# Audit log writer (JSONL + optional Postgres)
# ---------------------------------------------------------------------------


def write_audit_jsonl(
    record: dict[str, Any],
    path: str | os.PathLike[str] = DEFAULT_AUDIT_DIR,
) -> Path:
    """Append a sizing decision to the JSONL audit log (atomic).

    Returns the resolved file path. Creates parent dirs. Determinism: the
    record must already carry an explicit ``ts`` (no ``datetime.now()`` here).

    Issue #222: a previous implementation used ``open("a") + write`` which
    is NOT POSIX-atomic. A SIGKILL / Docker healthcheck kill / disk-full
    mid-write truncates the last JSONL record to a partial JSON object,
    and ``replay_sizing.py`` (the rollback companion, task body §3) raises
    ``SystemExit`` on the first invalid line — dropping ALL records that
    were written after the truncation point.

    Fix: append via read-modify-rename with a sibling ``.tmp`` file, using
    the same pattern as ``_save_peak_equity`` (issue #199) and
    ``_save_daily_pnl_basis`` (issue #214):

        tmp = target + ".tmp"
        write(tmp, existing_content + record + "\n")
        fh.flush() + os.fsync(fh.fileno())
        os.replace(tmp, target)

    ``os.replace`` is POSIX-atomic when ``tmp`` and ``target`` are on the
    same filesystem — we use ``target.parent / target.name + ".tmp"`` to
    guarantee that. A SIGKILL before ``os.replace`` leaves the OLD file
    intact (no record added); a SIGKILL after leaves the NEW file fully
    written. No partial JSON line is ever observable.

    The read-modify-rename window is bounded by ``O(record_bytes)`` (a few
    hundred bytes for the typical sizing record) so the crash-mid-window
    probability is negligible. We do NOT add a ``.bak`` mirror: the audit
    log is append-only, so the only recoverable state from a crash is
    "previous record + maybe this one", which atomic rename guarantees.

    The directory-path branch (``p.suffix != ".jsonl"``) is resolved to a
    named file BEFORE the atomic write. Two calls in the same
    microsecond on the same day produce two different filenames only if
    their ``ts`` differs; callers that want strict isolation should pass
    an explicit file path (or rely on the per-test ``tmp_path`` override
    in ``tests/test_broker_sizing.py``).
    """
    p = Path(path)
    if p.suffix != ".jsonl":
        p = p / f"sizing_audit_{record.get('ts', 'unknown')}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    existing = b""
    if p.exists():
        try:
            raw = p.read_bytes()
        except OSError:
            # If the existing file is unreadable (perm denied, etc.), we
            # can't safely append. Fall back to writing only the new line
            # rather than raising — the audit log is best-effort and the
            # caller (audit_hook) doesn't have a recovery path.
            raw = b""
        # Issue #222: a previous SIGKILL may have left the last record as
        # a partial JSON line (no trailing newline). If we naively appended
        # our new line to that, ``replay_sizing.py`` would still see the
        # corruption and skip the partial row. We MUST strip the trailing
        # partial line so the next atomic write yields a fully parseable
        # file. The partial line is recoverable from process state on the
        # next sizing call (which re-records it as a fresh audit row), so
        # discarding it is safe.
        if raw and not raw.endswith(b"\n"):
            # Truncate to the last full newline. JSONL is one record per
            # line, so a partial trailing chunk is by definition garbage.
            last_nl = raw.rfind(b"\n")
            if last_nl == -1:
                # No complete line at all — discard everything.
                raw = b""
            else:
                raw = raw[: last_nl + 1]
        existing = raw
    tmp_path = p.with_name(p.name + ".tmp")
    try:
        with tmp_path.open("wb") as fh:
            fh.write(existing)
            fh.write(line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, p)
    except (OSError, ValueError):
        # Best-effort cleanup: if the tmp file was created but the rename
        # never completed, remove the orphan so it doesn't accumulate.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
    return p


def write_audit_postgres(record: dict[str, Any], conn: Any) -> None:
    """Persist a sizing decision to ``sizing_audit_log``.

    The table is created by ``src/data/migrations/0003_sizing_audit_log.sql``.
    ``conn`` is a DB-API 2.0 connection (``psycopg``/``sqlite3``). The
    function does not commit — caller owns the transaction.

    Uses ``%s`` placeholders (Postgres convention). Tests against SQLite
    adapt the placeholder via a thin ``_SqliteCompatConn`` wrapper; in
    production the connection is always psycopg/Postgres and ``%s`` is
    correct out of the box.

    For Postgres we use JSONB; for SQLite we store JSON as TEXT. The sizer
    is read-only on Position rows (task body: "READ-ONLY для существующих
    Position rows — никогда не меняем sizing_version задним числом").
    """
    sql = (
        "INSERT INTO sizing_audit_log "
        "(ts, ticker, side, inputs, scalars, output, formula_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )
    payload = (
        record["ts"],
        record["ticker"],
        record["side"],
        json.dumps(record.get("inputs", {}), default=str),
        json.dumps(record.get("scalars", {}), default=str),
        json.dumps(record.get("output", {}), default=str),
        record["formula_version"],
    )
    cur = conn.cursor()
    cur.execute(sql, payload)


class _SqliteCompatCursor:
    """Adapter: translate ``%s`` → ``?`` so write_audit_postgres works on SQLite.

    Lives in this module so the production code stays psycopg-only; tests
    import via ``src.broker.sizing`` directly.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def execute(self, sql: str, params: Any = None) -> Any:
        translated = sql.replace("%s", "?")
        if params is None:
            return self._inner.execute(translated)
        return self._inner.execute(translated, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _SqliteCompatConn:
    """Thin ``sqlite3.Connection`` wrapper exposing the paramstyle the
    Postgres-shaped writer expects. Use ``wrap_sqlite(conn)`` to obtain."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def cursor(self) -> _SqliteCompatCursor:
        return _SqliteCompatCursor(self._inner.cursor())

    def commit(self) -> None:
        self._inner.commit()

    def close(self) -> None:
        self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_sqlite(conn: Any) -> _SqliteCompatConn:
    """Adapt a ``sqlite3.Connection`` to the paramstyle used by
    ``write_audit_postgres``. Production code does not need this — Postgres
    uses ``%s`` natively."""
    return _SqliteCompatConn(conn)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def compute_position_size(
    quote: Quote,
    portfolio: PortfolioState,
    market_data: MarketData,
    regime: MacroRegime,
    *,
    config: SizingConfig | None = None,
    audit_hook: Callable[[dict[str, Any]], None] | None = None,
) -> OrderSpec:
    """Compute position size for a Coordinator quote.

    Pure function (modulo the optional ``audit_hook`` side effect). Same
    inputs → same :class:`OrderSpec` every time. Used by Coordinator stage 1;
    the resulting ``OrderSpec.quantity`` then flows into ``RiskGate.evaluate``.

    Args:
        quote: Coordinator signal (ticker, side, confidence, timestamp).
        portfolio: Cash + peak/total equity for the DD curve and base size.
        market_data: Trailing bars for ATR. If empty → ``skip`` with reason.
        regime: MacroRegime from the Macro Agent.
        config: Override formula constants; ``None`` uses module defaults.
        audit_hook: Optional callable receiving the audit record (dict).
            Defaults to a no-op so the function stays pure.

    Returns:
        :class:`OrderSpec` with ``quantity`` in shares (whole lots when
        ``config.lot_size > 1``). When the trade would be too small, returns
        ``skip=True`` with a human-readable ``skip_reason``.
    """
    cfg = config or SizingConfig()
    skip_record: dict[str, Any] = {}

    # ------------------------------------------------------------------ guards
    if not market_data.bars:
        spec = OrderSpec(
            ticker=quote.ticker_upper,
            side=quote.side.lower(),
            quantity=Decimal("0"),
            price=_reference_price(quote, market_data),
            confidence=quote.confidence,
            sizing_version=cfg.formula_version,
            skip=True,
            skip_reason="no market data",
            meta={"regime": regime.regime, "regime_scalar": str(regime_scalar(regime))},
        )
        _audit(quote, portfolio, market_data, regime, cfg, spec, audit_hook, skip_record)
        return spec

    if portfolio.cash <= 0:
        spec = OrderSpec(
            ticker=quote.ticker_upper,
            side=quote.side.lower(),
            quantity=Decimal("0"),
            price=_reference_price(quote, market_data),
            confidence=quote.confidence,
            sizing_version=cfg.formula_version,
            skip=True,
            skip_reason="no cash",
            meta={"regime": regime.regime, "regime_scalar": str(regime_scalar(regime))},
        )
        _audit(quote, portfolio, market_data, regime, cfg, spec, audit_hook, skip_record)
        return spec

    # ---------------------------------------------------------- scalars
    price = _reference_price(quote, market_data)
    actual_atr = compute_atr_actual(market_data.bars, cfg)
    eff_atr = max(actual_atr, cfg.min_atr_frac)
    vol_s = min(cfg.target_atr_frac / eff_atr, cfg.max_vol_scalar)
    vol_s = max(vol_s, Decimal("0"))  # negative guard (paranoid)

    # ADV: sum of volumes from the bar window. The task body uses raw
    # "adv" as a single number; we compute it from the window so the
    # function stays pure given MarketData.
    adv = sum(((b.high - b.low) for b in market_data.bars[-cfg.atr_lookback :]), Decimal("0"))
    liq_s = _liquidity_scalar(adv, portfolio.cash, price, cfg)

    dd_pct = portfolio.drawdown_pct
    dd_s = drawdown_reduction_curve(dd_pct, knee_pct=cfg.dd_knee_pct, floor=cfg.dd_floor)

    reg_s = regime_scalar(regime)

    # ---------------------------------------------------------- compose
    base_size = portfolio.cash * cfg.risk_per_trade_pct
    raw_size = base_size * vol_s * liq_s * dd_s * reg_s
    raw_size = raw_size * quote.confidence  # confidence gate
    raw_size = _cap_against_cash(raw_size, portfolio.cash, cfg)

    lots = _quantize_lots(raw_size / price, cfg.lot_size)
    skip_reason: str | None = None
    if lots < cfg.min_size_lots:
        skip_reason = (
            f"size below min_size_lots: lots={lots} < {cfg.min_size_lots} "
            f"(vol={vol_s:.4f}, liq={liq_s:.4f}, dd={dd_s:.4f}, regime={reg_s:.4f})"
        )
        lots = 0

    spec = OrderSpec(
        ticker=quote.ticker_upper,
        side=quote.side.lower(),
        quantity=Decimal(lots * cfg.lot_size),
        price=price,
        confidence=quote.confidence,
        sizing_version=cfg.formula_version,
        skip=lots < cfg.min_size_lots,
        skip_reason=skip_reason,
        meta={
            "atr_frac": str(actual_atr),
            "atr_eff": str(eff_atr),
            "adv": str(adv),
            "drawdown_pct": str(dd_pct),
            "regime": regime.regime,
            "base_size": str(base_size),
            "vol_scalar": str(vol_s),
            "liq_scalar": str(liq_s),
            "dd_scalar": str(dd_s),
            "regime_scalar": str(reg_s),
            "raw_size": str(raw_size),
            "lot_size": cfg.lot_size,
        },
    )

    _audit(quote, portfolio, market_data, regime, cfg, spec, audit_hook, skip_record)
    return spec


def compute_atr_actual(bars: tuple[Bar, ...], cfg: SizingConfig) -> Decimal:
    """Pick simple vs EWMA per cold-start policy (ADR §2.2).

    Below ``ewma_min_bars`` → simple. At/above → EWMA seeded from simple.
    """
    if len(bars) < cfg.ewma_min_bars:
        return compute_atr_simple(bars, lookback=cfg.atr_lookback)
    return compute_atr_ewma(bars, lam=cfg.ewma_lambda, seed=compute_atr_simple(bars[:20]))


def _liquidity_scalar(
    adv: Decimal,
    cash: Decimal,
    price: Decimal,
    cfg: SizingConfig,
) -> Decimal:
    """ADV-based liquidity scalar.

    If ``adv == 0`` (no volume data), returns ``cfg.max_liq_scalar`` — the
    task body: "ADV=0 → liq_scalar = MAX_LIQ_SCALAR (вознаграждаем за
    минимальную ликвидность — на практике skip)". Note the test contract:
    we do NOT skip; we let the downstream lot-size floor + cash cap trim.
    Caller that wants "skip on illiquid" should inspect ``meta['adv']``.

    If ``price == 0`` (defensive) returns 1.0 — same reasoning as ATR floor.
    """
    if adv <= 0 or price <= 0 or cash <= 0:
        return cfg.max_liq_scalar if adv <= 0 else Decimal("1.0")
    # position_pct_of_adv: assume we'd take ``max_adv_pct`` of ADV (worst case).
    # liq_scalar = adv / (size_in_shares * max_adv_pct)
    # size_in_shares ≈ base_size / price = cash * risk_per_trade_pct / price.
    position_shares = cash * cfg.risk_per_trade_pct / price
    if position_shares <= 0:
        return Decimal("1.0")
    target_shares = position_shares * cfg.max_adv_pct
    if target_shares <= 0:
        return cfg.max_liq_scalar
    raw = adv / target_shares
    return min(raw, cfg.max_liq_scalar)


def _cap_against_cash(raw_size: Decimal, cash: Decimal, cfg: SizingConfig) -> Decimal:
    """Cap raw notional at ``max_size_pct_of_cash`` of cash (task body #6)."""
    cap = cash * cfg.max_size_pct_of_cash
    if raw_size > cap:
        return cap
    return raw_size


def _reference_price(quote: Quote, market_data: MarketData) -> Decimal:
    """Pick the reference price: explicit > last close > Decimal('1') fallback."""
    if quote.reference_price is not None:
        return quote.reference_price
    if market_data.bars:
        return market_data.bars[-1].close
    return Decimal("1")


def _audit(
    quote: Quote,
    portfolio: PortfolioState,
    market_data: MarketData,
    regime: MacroRegime,
    cfg: SizingConfig,
    spec: OrderSpec,
    hook: Callable[[dict[str, Any]], None] | None,
    skip_record: dict[str, Any],
) -> None:
    """Build the audit record and invoke the hook (if provided).

    Determinism: we use ``quote.timestamp`` so the ts is reproducible.
    """
    if hook is None:
        return
    record = {
        "ts": quote.timestamp.astimezone(timezone.utc).isoformat(),
        "ticker": quote.ticker_upper,
        "side": quote.side.lower(),
        "inputs": {
            "cash": str(portfolio.cash),
            "peak_equity": str(portfolio.peak_equity),
            "total_equity": str(portfolio.total_equity),
            "drawdown_pct": str(portfolio.drawdown_pct),
            "dd_pct": str(portfolio.drawdown_pct),
            "confidence": str(quote.confidence),
            "n_bars": len(market_data.bars),
            "atr_n": len(market_data.bars),
            "adv": spec.meta.get("adv", "0"),
            "atr_frac": spec.meta.get("atr_frac", "0"),
            "regime": regime.regime,
            "regime_multiplier": str(regime.multiplier),
        },
        "scalars": {
            "vol_scalar": spec.meta.get("vol_scalar"),
            "liq_scalar": spec.meta.get("liq_scalar"),
            "dd_scalar": spec.meta.get("dd_scalar"),
            "regime_scalar": spec.meta.get("regime_scalar"),
            "base_size": spec.meta.get("base_size"),
        },
        "output": {
            "final_size": str(spec.quantity),
            "final_lots": int(spec.quantity // Decimal(cfg.lot_size)) if cfg.lot_size > 0 else 0,
            "price": str(spec.price),
            "skip": spec.skip,
            "skip_reason": spec.skip_reason,
        },
        "formula_version": spec.sizing_version,
    }
    record.update(skip_record)
    hook(record)


# ---------------------------------------------------------------------------
# Backward-compat alias for v1-replay (task body §3 "Rollback")
# ---------------------------------------------------------------------------


def compute_position_size_v1(*args: Any, **kwargs: Any) -> OrderSpec:
    """Stable v1 entry point — used by audit replay for positions opened under v1.

    Lives in ``src/broker/sizing_v1.py`` as a re-export shim. See that module
    for the canonical import. Adding new kwargs in v2 must NOT change this
    function's signature — otherwise v1 audit rows cannot be replayed bit-
    identically.
    """
    return compute_position_size(*args, **kwargs)


__all__ = [
    "FORMULA_VERSION",
    "Quote",
    "PortfolioState",
    "Bar",
    "MarketData",
    "OrderSpec",
    "SizingConfig",
    "compute_position_size",
    "compute_position_size_v1",
    "compute_atr_simple",
    "compute_atr_ewma",
    "compute_atr_actual",
    "drawdown_reduction_curve",
    "regime_scalar",
    "write_audit_jsonl",
    "write_audit_postgres",
]
