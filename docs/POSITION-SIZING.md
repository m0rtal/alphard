# Position Sizing — Phase 2.2

**Status:** implemented (kanban task `t_e55e2168`, 2026-08-24)
**ADR:** [`docs/decisions/0006-position-sizing.md`](decisions/0006-position-sizing.md)
**Module:** [`src/broker/sizing.py`](../src/broker/sizing.py)

## TL;DR

`compute_position_size(quote, portfolio, market, regime) -> OrderSpec` is a
pure, deterministic function that converts a Coordinator quote into a sized
order. Five multiplicative scalars compose the result:

```
base_size  = cash × risk_per_trade_pct
vol_scalar = min(target_atr / actual_atr, MAX_VOL_SCALAR)
liq_scalar = min(adv / target_shares, MAX_LIQ_SCALAR)
dd_scalar  = drawdown_reduction_curve(drawdown_pct)
regime_s   = MacroRegime.multiplier                  # 0.50 / 0.75 / 1.00
size       = base_size × vol_scalar × liq_scalar × dd_scalar × regime_scalar × confidence
```

If `floor(size / price / lot_size) < MIN_SIZE_LOTS`, the function returns
`OrderSpec(skip=True, skip_reason=...)` instead of a partial fill.

The module is **self-contained**: it does not import from
`broker/account.py`, `broker/integration.py`, `risk/gate.py`,
`coordinator.py`, or `config.py`. It composes with the existing risk gate
*upstream* (sizer outputs a `TradeIntent`-shaped `OrderSpec` that the gate
then checks) — it does not replace any of those checks.

## Design rationale

### Why FIX (not HYB or Kelly)?

ADR-0006 §2.1 selects **FIX** for Phase 2.2 because:

1. **No new dependencies** (`numpy`, `scipy`, `arch` are still banned).
2. **Closed-form DD bound** — Thorp 2008 gives `max_dd ≈ fraction × N_trades`,
   the only sizing family with a known worst-case bound. Useful for the
   95% coverage gate and the regulator-friendly audit story.
3. **Sandbox-testable today** — Kelly needs `realised_vol_i` (Phase 2.5),
   HYB needs `target_atr` from `realised_vol_i` (Phase 2.1).
4. **Pipeline is multiplicative** — when Phase 2.1 ships `realised_vol_i`,
   the HYB upgrade only swaps the `vol_scalar` layer. Everything else
   stays put.

### Why EWMA over GARCH / realised?

ADR-0006 §2.2 selects **EWMA (λ=0.94)** because:

1. **Pure Decimal recursion** — no float drift at the `risk/gate.py:117`
   boundary. `compute_atr_ewma()` uses Decimal Newton's method for the
   square root.
2. **Seeded cold-start** — first 20 bars seed the variance from the
   simple-ATR, no `arch` MLE convergence risk.
3. **Half-life ~11 days** — matches the regime multiplier's intended
   halve speed.
4. **GARCH deferred to Phase 2.11+** — needs `arch`/`scipy`/`numpy`,
   not yet approved dependencies.

### Why a separate module (not `risk/gate.py`)?

`RiskGate` (`src/risk/gate.py`) is hard-limits-only — four shipped caps,
stateless, no IO, sync. Mixing soft-scaling (vol-target, regime) with
hard-rejects (`max_dd_pct`) on one evaluation path makes the audit story
inseparable. Keeping the sizer as a pure function the gate then evaluates:

```
Coordinator → Sizer (this module) → RiskGate (existing) → Broker
```

means each layer's audit row is independent. Operators can disable the
sizer (fall back to equal-weight) without changing the gate. A v2 sizer
swap doesn't require touching the gate.

## Constants (locked at v1)

| Constant | Value | Source | Rationale |
|---|---|---|---|
| `RISK_PER_TRADE_PCT` | `0.01` (1%) | Task body | Conservative Phase 2.2 default |
| `TARGET_ATR_FRAC` | `0.02` (2%) | Task body | Aligned with daily vol target |
| `MIN_ATR_FRAC` | `0.0001` | Task body | Floor against divide-by-zero |
| `MAX_VOL_SCALAR` | `3.0` | Defensive cap | Prevents vol spike explosion |
| `MAX_LIQ_SCALAR` | `2.0` | Defensive cap | Max reward for liquidity |
| `MAX_ADV_PCT` | `0.05` (5%) | Task body | Per task body §1 |
| `MIN_ADV_PCT` | `0.001` | Task body | Skip threshold |
| `DD_FLOOR` | `0.25` | Task body | 25% of base at 50% DD |
| `DD_KNEE_PCT` | `50.0` | Derived | Linear reduction ends here |
| `MIN_SIZE_LOTS` | `1` | Task body | Skip below 1 lot |
| `MAX_SIZE_PCT_OF_CASH` | `0.10` (10%) | Task body | Soft cap before hard gate |
| `EWMA_LAMBDA` | `0.94` | RiskMetrics | Industry-standard λ |
| `EWMA_MIN_BARS` | `30` | Cold-start | Below this → simple ATR |
| `DEFAULT_ATR_LOOKBACK` | `20` | Task body | N=20 |

Override any of these via `SizingConfig()` (passed as `config=` kwarg to
`compute_position_size`). Defaults are locked at v1 — the
`formula_version="v1"` audit field is the rollback key (see §"Rollback").

## Regime multiplier — locked per ADR-0006 §2.6

| `MacroRegime.regime` | `multiplier` | Behaviour |
|---|---|---|
| `neutral` | `1.00` | Full size |
| `risk_on_reduced` | `0.75` | 25% reduction |
| `risk_off` | `0.50` | 50% reduction |

**Note on a known divergence:** the original task body has an inverted
mapping (`risk_off → 1.0`, `risk_on_reduced → 0.5`). The ADR locks the
correct mapping (above) — the task body is a typo. The sizer pulls
`MacroRegime.multiplier` directly from the locked regime classifier, so
the pipeline is always consistent with the regime log.

**Stale-multiplier policy (ADR §2.6 §"Stale-multiplier policy"):** if
`MacroRegime` is missing or stale (older than 24h), the upstream Macro
Agent defaults to `neutral` (multiplier 1.00). The sizer inherits that
fail-open behaviour. A Phase 2.3 outage does NOT accidentally halve the
book.

## Audit trail

Every `compute_position_size()` call appends a row to:

1. **`sizing_audit_log` Postgres table** (production path)
2. **JSONL file** under `logs/sizing_audit/sizing_audit_<ts>.jsonl` (dev / replay path)

Both record the same schema:

```json
{
  "ts": "2026-08-22T09:30:00+00:00",
  "ticker": "SBER",
  "side": "buy",
  "inputs": {
    "cash": "100000", "peak_equity": "100000", "total_equity": "100000",
    "drawdown_pct": "0", "dd_pct": "0", "confidence": "1.0",
    "n_bars": 20, "atr_n": 20, "adv": "80", "atr_frac": "0.04",
    "regime": "neutral", "regime_multiplier": "1.00"
  },
  "scalars": {
    "base_size": "1000.00", "vol_scalar": "0.5",
    "liq_scalar": "2.0", "dd_scalar": "1.0", "regime_scalar": "1.0"
  },
  "output": {
    "final_size": "10", "final_lots": 1, "price": "100",
    "skip": false, "skip_reason": null
  },
  "formula_version": "v1"
}
```

The table is created by `src/data/migrations/0003_sizing_audit_log.sql`
(also embedded in `src/data/schema.sql` for fresh volumes — both DDLs
are idempotent). The replay tool `scripts/replay_sizing.py` reads either
source and reproduces the decision bit-identically given the same inputs.

**Why the raw bars are NOT in the audit row:** bars are 20 × OHLCV × N
instruments per cycle; embedding them in JSONL blows the log past
human-reviewable size. Instead the row stores `atr_frac` (the derived
scalar) — replay reconstructs synthetic bars with the same ATR for
divergence checking. Lossy by design; documented in
`scripts/replay_sizing.py:_build_market_data`.

## Rollback (formula versioning)

`OrderSpec.sizing_version` is locked at construction. The audit row carries
the same field. When v2 ships:

1. **New trades** use the new formula — `OrderSpec.sizing_version = "v2"`.
2. **Live v1 positions** stay v1 — `RiskGate.evaluate()` reads
   `position.sizing_version` (added to the pydantic Position in a future
   migration; today it's a `meta` field on the broker-side `Position`
   dataclass in `src/broker/account.py`).
3. **v1 audit rows replay under v1 forever** — `compute_position_size_v1()`
   is the stable entry point in `src/broker/sizing_v1.py`. New kwargs go
   on `compute_position_size()` only.

A bump from v1 → v2 should land in a separate PR that:

* Adds `compute_position_size_v2()` and bumps `FORMULA_VERSION`.
* Keeps `compute_position_size_v1()` byte-for-byte identical.
* Adds a migration that adds `sizing_version` to the broker-side Position
  if not already there.

This module NEVER retroactively rewrites `sizing_version` on existing
positions. Per the task body: "READ-ONLY для существующих Position rows".

## Idempotency

`compute_position_size()` is pure:

* No `datetime.now()` — time comes from `Quote.timestamp`.
* No `random` / `secrets` / network.
* Same `(Quote, PortfolioState, MarketData, MacroRegime, SizingConfig)`
  → same `OrderSpec` every time, on every machine, forever.

This is verified by `test_idempotency_same_inputs_same_outputs` and
`test_idempotency_no_datetime_now_dependency` in
[`tests/test_broker_sizing.py`](../tests/test_broker_sizing.py).

## Edge cases & invariants

| Condition | Behaviour | Test |
|---|---|---|
| Empty `MarketData` | `skip=True`, reason `"no market data"` | `test_no_market_data_skips` |
| `cash == 0` | `skip=True`, reason `"no cash"` | `test_no_cash_skips` |
| `actual_atr == 0` | Floored to `min_atr_frac`, no divide-by-zero | `test_atr_actual_atr_zero_floored_no_division_error` |
| `adv == 0` | `liq_scalar = MAX_LIQ_SCALAR` (rewards low liquidity); downstream lot-size + cash cap trim | `test_liq_scalar_when_adv_is_zero_returns_max` |
| `confidence == 0` | `size = 0` → `skip=True` | `test_confidence_zero_skips` |
| `peak_equity < total_equity` | `ValidationError` at construction | `test_portfolio_state_rejects_peak_lt_equity` |
| `len(bars) < 2` | EWMA returns 0 (no signal) | `test_ewma_below_two_bars_returns_zero` |
| `len(bars) < EWMA_MIN_BARS` | Simple ATR path (cold-start) | `test_ewma_cold_start_uses_simple` |
| `MacroRegime is None` | `regime_scalar = 1.0` (fail-open per ADR) | `test_regime_scalar_none_falls_open` |
| Single-name universe | Sizing works (no portfolio correlation) | `test_single_name_universe_sizing_works` |

## Migration

```sql
-- src/data/migrations/0003_sizing_audit_log.sql
CREATE TABLE IF NOT EXISTS sizing_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    ticker          VARCHAR(12) NOT NULL,
    side            VARCHAR(8) NOT NULL,
    inputs          JSONB NOT NULL,
    scalars         JSONB NOT NULL,
    output          JSONB NOT NULL,
    formula_version VARCHAR(8) NOT NULL DEFAULT 'v1',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sizing_audit_log_ticker_ts
    ON sizing_audit_log (ticker, ts DESC);
CREATE INDEX IF NOT EXISTS idx_sizing_audit_log_version_ts
    ON sizing_audit_log (formula_version, ts DESC);
```

**Production rollout note:** the task body says "Не делать migration
production PostgreSQL без approval (Phase 1.6 watchdog компенсирует)".
The migration is idempotent and safe to re-run; the Phase 1.6 watchdog
will surface any divergence. The bot picks up the migration automatically
on next restart (entrypoint runs `init_schema()` which is idempotent).

## Replay

```bash
# Replay one row by exact ts
scripts/replay_sizing.py logs/sizing_audit/sizing_audit_2026-08-22T09:30:00.jsonl \
    2026-08-22T09:30:00+00:00

# Replay the latest row for a ticker
scripts/replay_sizing.py logs/sizing_audit/sizing_audit_2026-08-22T09:30:00.jsonl \
    --ticker SBER

# Replay everything in a log file
scripts/replay_sizing.py logs/sizing_audit/sizing_audit_2026-08-22T09:30:00.jsonl \
    --all
```

Exit code 0 = every record reproduced identically; 1 = at least one
diverged (output: per-row divergence report).

## Coverage / quality

* `tests/test_broker_sizing.py` — 42 tests, all green.
* `src/broker/sizing.py` — 95% line coverage (per-test, with the full
  test suite).
* `black --check` — clean.
* `flake8 --max-line-length=120` — clean.
* `mypy --strict` — clean.
* `pytest tests/test_broker_sizing.py` — 42 passed.

## Constraints honoured

* Did NOT touch `src/risk/gate.py`, `src/coordinator.py`,
  `src/broker/account.py`, `src/broker/integration.py`, `src/config.py`.
* Uses existing `src/macro/models.py::MacroRegime` for regime input.
* Uses existing `src/data/pg_store.py` connection pattern for audit
  writes (no new DB module).
* Migration is the third in the `src/data/migrations/` sequence, same
  idempotency pattern as `0002_ohlcv_source.sql`.
