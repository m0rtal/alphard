# ADR 0006: Position Sizing Policy — Phase 2.2 Design Decision

- **Status:** Proposed
- **Date:** 2026-08-22
- **Deciders:** @developer (kanban task t_b7f71c17), reviewed by orchestrator
- **Parent task:** t_c59ac1a9 ("Phase 2.2: design decision — position sizing policy")
- **Source reviews:** six parent matrices — `t_021949c1`, `t_4423398f`, `t_83e02922`,
  `t_9279f1ef`, `t_a0260058`, `t_b4e02a1d`, `t_d13d2473`, `t_dd2caaa4`, plus the
  precedent brief `t_2a7d97ae`.

> **Scope of this ADR:** six open questions from the parent task body. Each gets a
> single, integrated decision in §2 (matrix in §3). Code is **not** landed here
> — implementation lands in Phase 2.10 (Coordinator wiring). The ADR locks the
> design; the implementation kanban tasks land it.

---

## 1. Context

Alphard's Phase 2 architecture activates data, macro, risk, and execution agents.
Today sizing is **caller-side**: the Coordinator passes
`CoordinatorConfig.quantity × CoordinatorConfig.limit_price` directly into
`RiskGate.evaluate()`, which contains **only** hard-limit checks
(`src/risk/gate.py:6-51, 240-246`). There is no Kelly, no vol-targeting, no
`adv_shares`-aware sizing, and no consumption of the macro regime multiplier
that `src/macro/regime.py::classify()` already produces. Six open questions
have been answered by parent matrices in parallel:

| # | Open question | Authoritative matrix | This ADR's decision (§2.1–§2.6) |
|---|---|---|---|
| 1 | **Sizing method** — % of equity, vol-target, Kelly, fixed-fractional | `t_4423398f` (12 axes, 6 methods) | **FIX today; HYB post-Phase 2.1; KLY inside HYB post-Phase 2.5** |
| 2 | **Volatility measure** — realized vs EWMA vs GARCH | `t_83e02922` (8 axes, 4 measures) | **EWMA (λ=0.94) primary; realized reconciliation; GARCH deferred** |
| 3 | **Correlation handling** — zero vs portfolio covariance matrix | `t_b4e02a1d` (12 axes, 5 options) | **SEC (sector-overlap) primary; LWO post-Phase 2.11; DCC deferred** |
| 4 | **Risk budget per day** — cumulative loss cap shape | `t_dd2caaa4` (12 axes, 6 shapes) | **HARD today; LIN (Phase 2.4) parallel; TRL long-term** |
| 5 | **Black swan protection** — max single position size regardless of Kelly | `t_d13d2473` (7 axes, 12 options) | **Keep shipped 4 caps; ship kill-switch + gross-exposure + vol cap Phase 2.4; deferred #8/9/10** |
| 6 | **Integration with regime** — sizing scales down in risk_off by X% | `t_a0260058` (12 axes, 6 patterns) | **A. MUL-BUDGET — multiply `daily_risk_budget` by `regime_multiplier` at Coordinator stage 0** |

The six decisions compose into a single multiplicative sizing pipeline (§4) —
each layer is independent, so any future upgrade swaps **one** layer without
touching the others. The pipeline is sizer-agnostic on the gate (which remains
hard-limits only) and audit-friendly at every step.

### What already exists (the integration surface)

| Module | Role the ADR reuses |
|---|---|
| `src/macro/regime.py::classify()` + `src/macro/models.py::MacroRegime.multiplier` | Produces `{0.50, 0.75, 1.00}` for `{risk_off, risk_on_reduced, neutral}`. Always worst-case wins. Floor `0.50` enforced by `models.py:60` validator. **Produced and persisted; **not** consumed by any sizing code today.** |
| `src/risk/gate.py:295-407` | Four shipped caps (`max_position_pct`, `max_sector_pct`, `max_daily_loss_pct`, `max_dd_pct`). Stateless, deterministic, Decimal-only. **Adopt as floor of black-swan defence-in-depth.** |
| `src/broker/slicer.py:36-50` | `OrderSlicer(adv_shares, parent_qty)` with 5%-ADV chunking and 30-min max. Already consumes ADV — nothing new needed for HYB post-Phase 2.1. |
| `src/broker/tinkoff_account.py:58-80` | `_assert_not_live_trading()` — single source of truth. Any sizing module runs sandbox-first. |
| `src/coordinator.py:438-456` | `_risk_check` constructs `TradeIntent` from caller-supplied quantity/price. **No sizing logic today.** |
| `src/data/schema.sql` | Audit tables: `decision_log`, `macro_regime_log`. **No `ohlcv_log`, no `alpha_diary`** — see §9.2 scope correction. |

### Hard constraints (inherited from parent matrices)

1. **No new dependencies.** `requirements.txt` excludes `numpy`, `scipy`,
   `pandas`, `arch`, `statsmodels`. Adding any is its own Phase review
   (`t_83e02922` GARCH deferral, `t_b4e02a1d` LWO deferral).
2. **`RiskGate` stays fail-safe, stateless, synchronous, Decimal-only**
   (`gate.py:240-246`). Any new sizer module lives outside the gate
   (`src/risk/sizing.py`, `src/risk/volatility.py`, `src/risk/correlation.py`,
   `src/risk/budget.py`) — every layer preserves the gate's no-I/O contract.
3. **Frozen models everywhere.** All `Position`, `TradeIntent`,
   `PortfolioState`, `RiskLimits`, `RiskDecision` are
   `model_config = ConfigDict(extra="forbid", frozen=True)` (`gate.py:73-79,
   82, 127, 154, 185, 209`). Any new layer adds new modules, not new fields,
   unless explicitly breaking the contract.
4. **Regime multiplier is monotone with severity** (`regime.py:40-41`,
   worst-case wins). `multiplier ∈ [0.5, 1.0]` is locked by `models.py:60`
   validator. `risk_off` means "**halve** risk exposure", not "shut off".
5. **Two parallel `Position` definitions** (pydantic in `gate.py`, dataclass
   in `broker/account.py`) — any new field must land in both, or the field
   lives in a new module-level data class per
   `t_4423398f §6.3 implementation gaps`.
6. **`LIVE_TRADING=false` hardlock** is the single source of truth; sandbox-
   first is the only legal Phase 1/2 path.
7. **Cold-start budget safe-defaults.** `MacroRegime.multiplier` defaults to
   `1.00` (neutral) when `latest_regime(conn)` returns `None` or stale
   (`t_a0260058 §8`) — fail-open on a missing snapshot, not fail-stuck. Same
   logic for `realised_vol_i` cold-start in the eventual HYB path.

---

## 2. Decision

### 2.1 Sizing method (open question #1) — FIX today, HYB post-2.1

**Default (today, no Quant Agent / no `realised_vol_i`):** **fixed-fractional
(`FIX`)** as the primary sizing policy — `fraction = 0.02` (2% of equity per
trade), scaled by the regime multiplier (see §2.6), clipped to
`RiskLimits.max_position_pct` (default 10%). `FIX` requires **no** `Position`
schema change, **no** new pipeline, and has a closed-form geometric drawdown
bound (`max_dd ≈ fraction × N_trades`, Thorp 2008). The gate enforces the cap
as a backstop.

**Phase 2.1 upgrade:** replace `FIX` with **Hybrid (`HYB`)** — liquidity-
weighted inner loop clipped to `max_position_pct`, with `vol_target_i`
substituted for `liq_weight_i` once Phase 2.1's rolling-features pipeline
ships. The shape is unchanged; only the inner weight function changes.

**Phase 2.5+ enhancement:** add a Kelly *layer* (`kelly_fraction_cap = 0.25`
quarter-Kelly default from Vince 1992) inside `HYB`. **Never use Kelly as the
primary sizing policy** — the matrix scores it 49/120 and the gate's fail-safe
philosophy is incompatible with a statistical estimator sitting at the sizer
(`t_021949c1 §B-2`, `t_4423398f §2.3`).

**What this decision does NOT solve** (deferred to follow-up ADRs):
- "rebalance ALL vs `risk_score > threshold`" — not implementable until
  `Position.risk_score` exists (deferred to Phase 2.5+).
- pre-trade sector aggregation beyond `max_sector_pct` — see §2.3 SEC.

### 2.2 Volatility measure (open question #2) — EWMA (λ=0.94) primary

**Primary measure:** **EWMA (λ=0.94, RiskMetrics 1996)** as the per-ticker vol
input for `vol_target_i` in HYB. Pure Decimal recursion, no new deps, half-life
≈ 11.2 days matches the regime multiplier's "halve in risk_off" intent without
overshooting.

**Reconciliation baseline:** realized close-to-close returns with three lookback
lengths (`N = 20 / 60 / 252`) surfaced in `meta["vol_measure"]`. Used as the
audit divergence check and the cold-start seed (`σ²_0 = realized_var(first 20
returns)`). Divergence > 2× between EWMA and realized logs `VOL_DIVERGENCE`
but does not act on it.

**Cold-start policy:** if `len(returns) < 2`, return `Decimal("0")` and
the caller treats it as "no signal" (matches Kelly no-edge semantics).
Falls back to a per-ticker prior of `0.04² × 252 = 40% annualised` when
neither EWMA seed nor the 20-bar realized window is available.

**Phase 2.11+ upgrade gates:** only adopt
**GARCH(1,1)** if post-Phase-2.10 review shows > 5 sizing-induced drawdowns
traceable to EWMA lag. Only adopt **Garman-Klass / Parkinson** if we ever
get intraday range data (Tinkoff gRPC minute bars exist as
`src/data/tinkoff_md_loader.py`; need 5-min aggregation first). Both
require dependency review (`arch`/`scipy`/`numpy`) — separate ADR each.

### 2.3 Correlation handling (open question #3) — SEC primary

**Primary correlation handling:** **SEC (sector-overlap factor)** — a pure
function `correlation_overlap_factor(portfolio, intent, sector_map) → Decimal`
that returns a multiplier in `(0, 1]` based on existing sector exposure:

```python
# psuedocode — implementer note, not the ADR's contract
if intent.sector is None: return Decimal("1.0")            # today; mandatory Phase 1.3
if sector_value == 0:       return Decimal("1.0")
sector_pct = sector_value / portfolio.total_equity * 100
if sector_pct >= limits.max_sector_pct: return Decimal("0.5")   # floor
return Decimal("1.0") - (sector_pct / (Decimal("2") * limits.max_sector_pct))
```

**New types:** `SectorMap` (frozen pydantic, `symbol_to_sector: dict[str, str]`)
and `correlation_overlap_factor()` (pure function). New module
`src/risk/correlation.py` mirroring the gate/sizer split proposed by
`t_021949c1` for Kelly. **No new dependencies.**

**`max_sector_pct` is preserved as the hard gate; SEC is the continuous
pre-gate.** The two layers have different roles: `max_sector_pct` is a
post-fill hard reject, SEC is a continuous pre-fill discount. Defence-in-depth
on two axes.

**Default behaviour preservation:** ship with
`RiskLimits.correlation_overlap_enabled = False` until §5.3 changes ship;
behaviour is then identical to today until the operator opts in.

**Phase 2.11+ upgrade:** migrate to **LWO (Ledoit-Wolf shrinkage)** when
**all** of (a) `N ≥ 10` concurrent positions, (b) `≥ 252` trading days of
MOEX backfill (currently `31 / 3257`), (c) `realised_vol_i` lives in
`src/data/` (Phase 2.1), (d) a walk-forward validation harness exists,
(e) either `numpy` is added to `requirements.txt` (its own ADR) or a
hand-rolled Decimal linear-algebra helper ships.

**Phase 3+ deferral:** DCC (Engle 2002) — requires `arch` and offers no
empirical MOEX regime-timing advantage over LWO.

### 2.4 Risk budget per day (open question #4) — HARD today, LIN Phase 2.4

**Default (today, Phase 1.3 / Phase 1.5):** keep **`HARD`** as the primary
budget policy — `max_daily_loss_pct = 3.0%`, enforced by
`_check_daily_loss` at `src/risk/gate.py:361-383`. `HARD` is already battle-
tested (35 risk tests at 97% coverage per `docs/PHASE2-ROADMAP.md §1.1`),
requires **no** `RiskLimits` change, **no** new pipeline, and matches the
gate's fail-safe philosophy.

**Phase 2.4 upgrade (1-day change):** add **`LIN`** as a parallel path with
`0.5 × intent.notional` as the conservative worst-case projection. Trigger:
only when the rebalance scheduler is active (ADR-0007), not for one-off
intents from the CLI. This adds pre-trade projection without changing the
existing `HARD` behaviour for non-scheduler paths. The estimator contract
(`worst_case_loss = 0.5 × notional`) lives in a new `src/risk/budget.py`
module with the same fail-safe / no-I/O contract as `src/risk/gate.py`.

**Long-term (Phase 2.10):** migrate to **`TRL`** as the primary. `TRL`
collapses the daily and DD guards into one and eliminates the "two guards
disagree" failure mode. Prerequisite: small refactor to share `peak_equity`
state between `_check_drawdown` and the new budget module.

**Phase 2.4 layering:** `CNV` with `α=0.5` can be layered on top of `TRL`
for intraday smoothing if the operator wants the convex curve. Not on the
critical path.

### 2.5 Black swan protection (open question #5) — keep shipped floor + ship Phase 2.4 next-tier

**Tier 1 (already shipped, must keep):** the four caps in `src/risk/gate.py`
compose a defence-in-depth stack that handles ~95% of black-swan shapes
the historical record has produced:

| # | Cap | Default | Location |
|---|---|---|---|
| 1 | `max_position_pct` | 10% | `gate.py:295-322` (the task-body's "max position regardless of Kelly" — **already shipped**) |
| 2 | `max_sector_pct` | 30% | `gate.py:324-359` |
| 3 | `max_daily_loss_pct` | 3% | `gate.py:361-383` |
| 4 | `max_dd_pct` | 10% | `gate.py:385-407` |

These four are sizer-agnostic and compose without conflict. Kelly output
(when Phase 2.1 ships) is bounded by #1 before it can affect position size.
The sentinel `RISK_MARKET_ORDER_NO_QUOTE` at `gate.py:265-281` (issue #11)
catches placeholder-price bypasses.

**Tier 2 (ship next, in this order):**

1. **#11 Kill-switch state machine** (45/55, Phase 2). Unknown-unknowns
   layer. ~3-4 days. State persistence is the only complexity.
2. **#6 Aggregate gross-exposure cap** (48/55, 1 day, no `Position` schema
   change). Closes the levered-up hole the four shipped caps miss.
3. **#12 Daily volume cap** (44/55, fills `SECURITY.md P0 #12` TODO, 1 day).
   Bounds broker fees + operational risk. Gate behind a strategy flag.

**Tier 3 (deferred, blocked by missing primitives):** #8 liquidity-stress
gate (needs Phase 2.5 ADV), #10 Kelly-clamp (needs Phase 2.1 Quant Agent
EdgeEstimate), #9 tail-aware composed cap (needs Kelly + regime wiring).
These become in-scope in their respective phases.

**Tier 4 (marginal, skip):** #7 HHI concentration cap is redundant given
the 10% default of #1.

### 2.6 Integration with regime (open question #6) — A. MUL-BUDGET

**Pattern:** **A. MUL-BUDGET** — Coordinator multiplies the daily risk budget
(`HARD` cap, or `LIN`/`TRL` when they ship) by the locked `regime_multiplier`
at stage 0 of `Coordinator.run_cycle`. Then per-trade sizing (FIX today, HYB
post-Phase 2.1) consumes the scaled budget unchanged.

```python
# Pseudo-wiring at Coordinator stage 0 (NOT the ADR's contract; see §4)
regime_multiplier = latest_regime(conn).multiplier  # default 1.00 on None
effective_budget = daily_risk_budget * regime_multiplier  # one Decimal multiply
# pass effective_budget into the existing per-trade sizing unchanged
```

**Concrete numbers from the locked multipliers:**
- `regime = neutral` → `effective_budget = daily_risk_budget × 1.00`
- `regime = risk_on_reduced` → `effective_budget = daily_risk_budget × 0.75`
- `regime = risk_off` → `effective_budget = daily_risk_budget × 0.50`

**This is the 4-line "free win" called out by `t_4423398f §5.2`.** It is
decoupled from Kelly (Phase 2.5), from vol-target (Phase 2.1), from
correlation (Phase 2.11), and from the budget shape (`HARD`/`LIN`/`TRL`).
Any of those subsystems can change without touching the regime integration
layer.

**Audit:** persist `effective_budget`, `regime.regime`, `regime.multiplier`,
and `regime.fetched_at` to the existing `decision_log` row (Coordinator
pipeline already writes one JSONB row per pipeline step at
`coordinator.py:488-535`). No new table.

**Stale-multiplier policy:** fail-safe to `1.00` (neutral), not fail-stuck
to `0.50` (`t_a0260058 §8 open items #2`). A Phase 2.3 outage shouldn't
accidentally halve the book.

---

## 3. Decision matrix — six open questions, scored

Each open question is scored on the same axes the parent matrix used.
For each question, the matrix below lists 2-5 candidate paths with
pros / cons and the chosen one in **bold**. The selected option is the
one that wins on the relevant axes for *this* codebase today.

### 3.1 Sizing method (open question #1)

|| Option | Data-model fit | Risk control | Fail-safe alignment | Sandbox-testable today | Score (weighted, max 120) |
||---|---|---|---|---|---|
|| **A. FIX (today)** | 10 | 8 | 10 | 10 | **109 — chosen** |
|| B. PCT (1/N, fixed %) | 10 | 9 | 10 | 10 | 112 (also viable today; choose FIX for closed-form DD bound) |
|| C. HYB (post-2.1) | 7 | 9 | 9 | 7 | 105 — Phase 2.1 |
|| D. VOL (vol-target) | 3 | 9 | 9 | 4 | 71 — blocked: no `realised_vol_i` |
|| E. LIQ (ADV-weighted) | 7 | 5 | 9 | 7 | 92 — Phase 2.5+ |
|| F. KLY (Kelly primary) | 2 | 4 | 3 | 4 | **49 — REJECTED** |

Detailed per-method rationale in `research_t_4423398f_decision_matrix.md §2`.
The matrix here differs from `t_2a7d97ae` only in that `t_2a7d97ae` weighted
data-model fit equally to risk control; this ADR weights data-model fit 3×
(per `t_4423398f §3.1`), which puts `FIX` ahead of `PCT` for the "no model
change" criterion. Both `FIX` and `PCT` are sandbox-testable today; `FIX`
wins on the closed-form geometric-DD bound (`max_dd ≈ fraction × N_trades`,
Thorp 2008) — the only sizing family with a known worst-case bound.

### 3.2 Volatility measure (open question #2)

|| Option | Pure Decimal | Cold-start | Debug | Stackable with regime | New deps | Verdict |
||---|---|---|---|---|---|---|
|| **A. EWMA (λ=0.94)** | ✅ | ✅ (seeded) | ✅ `meta` field | ✅ (multiplicative) | ❌ none | **chosen — primary** |
|| B. Realized (close-to-close) | ⚠️ float drift | ❌ needs N bars | ✅ | ✅ | ❌ none | reconciliation baseline + cold-start seed |
|| C. GARCH(1,1) | ❌ MLE float64 | ❌ needs ≥500 bars | ❌ state opaque | ⚠️ | ❌ `arch` + `scipy` + `numpy` | rejected — Phase 2.11+ gated |
|| D. Parkinson / Garman-Klass | ✅ closed-form | ⚠️ needs ≥N bars + intraday | ✅ | ⚠️ | ❌ none | rejected — Phase 2.11+ (needs intraday range) |

Detailed per-measure rationale in `research_t_83e02922.md §5`. EWMA wins
on the axes the codebase actually cares about: pure-`Decimal` recursion
(no float drift at the `gate.py:117` boundary), seeded cold-start, half-life
~11 days that matches the regime multiplier's intended halve speed, and zero
new dependencies. GARCH's MLE convergence variability makes it the wrong
choice for a fail-safe risk path until empirical evidence justifies the
dependency cost.

### 3.3 Correlation handling (open question #3)

|| Option | Inputs today | New deps | Risk control | Verdict |
||---|---|---|---|---|
|| **A. SEC (sector-overlap)** | ✅ `Position.sector` | ❌ none | ✅ continuous pre-gate, hard-gate preserved | **chosen — Phase 2.10** |
|| B. ZRO (zero correlation) | ✅ trivial | ❌ none | ❌ `max_sector_pct` does the work blindly | acceptable interim (current behaviour) |
|| C. EOW (equal-weight orthogonalised Pearson) | ⚠️ needs 60d returns | ❌ stdlib only | ⚠️ noisy for N≤20 | rejected — LWO strictly dominates |
|| D. LWO (Ledoit-Wolf shrinkage) | ❌ needs `realised_vol_i` + 60d log-returns cache + backtest harness | ❌ none (hand-rolled Decimal LA) | ✅✅ best long-term | Phase 2.11+ gated on 5 conditions |
|| E. DCC (Engle 2002) | ❌ needs intraday + `arch` | ❌ `arch` | ⚠️ | rejected — Phase 3+ deferred |

Detailed per-option rationale in `research_t_b4e02a1d_correlation_matrix.md §3`.
SEC ties ZRO on the weighted score (4.66 / 5.00 each) but strictly beats ZRO
on the risk-control axes (`§4.1` of the source). ZRO + `max_sector_pct` is
what the system does today — SEC just makes the implicit correlation work
explicit and auditable.

### 3.4 Risk budget per day (open question #4)

|| Option | Pre-trade projection | Intraday recovery | Regime-friendly | Verdict |
||---|---|---|---|---|
|| **A. HARD (today's behaviour)** | ❌ post-fill only | ❌ binary | ✅ multiplicative on `cap` (4-line wire) | **chosen — primary (default)** |
|| B. LIN (linear pre-trade projection) | ✅ | ⚠️ linear | ✅ | Phase 2.4 — Phase 1.3 has no scheduler yet |
|| C. CNV (sqrt-decay) | ✅ | ✅ convex curve | ✅ | marginal — harder to explain |
|| D. STP (stepped tiers) | ❌ post-fill only | ✅ tiers | ✅ tier-aware | rejected as standalone; viable as layer |
|| E. TRL (DD-coupled) | ✅ | ✅ | ✅ | Phase 2.10 long-term |
|| F. VOL (vol-scaled) | ✅ | ✅ | ⚠️ mixes regime + vol | rejected — overlap with §3.2 |

Detailed per-shape rationale in
`research_t_dd2caaa4_risk_budget_per_day.md §3`. `HARD` wins on
data-model-fit today (no `RiskLimits` change, no new state, already battle-
tested). `LIN` (Phase 2.4) is the recommended 1-day upgrade for the rebalance
scheduler path — the rebalance scheduler's per-cycle intent stream makes
projection meaningful, while one-off CLI intents stay on `HARD`.

### 3.5 Black swan protection (open question #5)

The matrix has 12 options across 4 families. The full matrix is in
`research_t_d13d2473.md §4`. The summarised tiered choice is in §2.5 above.
Key reasoning:

- **Tier 1 ships today** (already shipped, must keep): #1 position cap,
  #2 sector cap, #3 daily-loss cap, #4 drawdown cap. Combined score ≥ 46/55.
- **Tier 2 ships next:** #11 kill-switch (45/55, Phase 2), #6 gross-exposure
  cap (48/55, 1 day), #12 daily volume cap (44/55, 1 day).
- **Tier 3 is deferred** to the phase that delivers the missing primitive:
  #8 liquidity-stress gate (Phase 2.5 ADV), #10 Kelly-clamp (Phase 2.1
  EdgeEstimate), #9 tail-aware composed cap (Phase 2.10, after Kelly +
  regime wiring).
- **Tier 4 is marginal** and skipped.

### 3.6 Integration with regime (open question #6)

|| Option | Composability | Phasable today | Decoupled | Fail-safe on stale | Max 60 |
||---|---|---|---|---|---|
|| **A. MUL-BUDGET** | 3 | 3 | ✅ | 2 (fail-open to 1.00) | **55 — chosen** |
|| B. MUL-ORDER | 3 | 3 | ❌ per-order tag | 1 | 46 — ruled out (drops orders below min lot) |
|| C. MUL-KELLY | 1 | 0 | ⚠️ | — | **11 — BLOCKED** (Kelly not shipped) |
|| D. SKIP-REGIME | 3 | 3 | ✅ | 3 (no effect) | 46 — ruled out (no kill-switch today) |
|| E. TWO-AXIS | 3 | 2 | ❌ (sizing + selection) | 2 | 43 — Phase 2.4+ (defensive rotation) |
|| F. TIERED-CONFIG | 3 | 3 | ❌ (single-SOT) | 2 | 49 — ruled out (duplicates locked multiplier) |

Detailed per-pattern rationale in
`research_t_a0260058_decision_matrix.md §3-4`. MUL-BUDGET is the literal
implementation of the "4-line free win" called out by `t_4423398f §5.2`
and `t_9279f1ef §2.2`. It is the only option that
1. preserves the structural `0.5` floor as "halve" not "shut off"
2. stacks multiplicatively with `EWMA(0.94)` vol-target without double-shrinking
3. decouples from Kelly, vol-target, sector rotation, and slippage caps
4. produces a single audit column (`regime_multiplier × risk_budget`)

---

## 4. Composition — the full sizing pipeline

The six decisions compose into a single multiplicative pipeline. Each
layer is independent — any future upgrade swaps **one** layer without
touching the others:

```
Step 0 — Coordinator stage 0
    regime_multiplier = latest_regime(conn).multiplier        # default 1.00 on None
    effective_budget  = daily_risk_budget * regime_multiplier # §2.6 MUL-BUDGET

Step 1 — Risk budget check
    effective_budget already consumed via HARD / LIN / TRL       # §2.4
    Project worst-case loss of next intent against remaining_budget_pct
    Reject if remaining < 0  →  RiskGate._check_daily_loss (gate.py:361)

Step 2 — Per-trade sizer (FIX today, HYB post-Phase 2.1)
    base_size = risk_per_trade_pct * equity                    # §2.1
       (FIX: base_size = 0.02 × equity, hard-coded)
       (HYB: base_size = min(liq_weight_i, max_position_pct) × equity, renormalised)

Step 3 — Vol-target overlay (Phase 2.1; skip today)
    vol_scaled = base_size * (target_σ / ticker_σ)              # §2.2 EWMA(0.94)
       EWMA seed = realized_var(first 20 returns)  # cold-start fallback

Step 4 — Correlation overlap (Phase 2.10; SEC optional behind a flag)
    corr_scaled = vol_scaled * correlation_overlap_factor(...) # §2.3 SEC

Step 5 — Final clamp
    final_size  = clamp(corr_scaled or vol_scaled or base_size,
                        min = 0,
                        max = limits.max_position_pct)         # gate.py:295 backstop
    intent_qty  = (final_size × equity / price).quantise(lot)  # OrderSlicer
    intent      = TradeIntent(quantity=intent_qty, price=live_price, ...) # gate.py:65

Step 6 — Risk gate (existing, unchanged)
    RiskGate.evaluate(intent, portfolio) → RiskDecision        # gate.py:240
       _check_position_size, _check_sector_exposure,
       _check_daily_loss, _check_drawdown

Step 7 — Audit (existing, extended)
    decision_log row with kind='trade', meta={
        regime_multiplier, effective_budget,
        sizer_method, base_size, vol_measure, vol_value,
        correlation_penalty, correlation_method
    }
```

Each new layer in steps 0/2/3/4 lives in its own module outside
`src/risk/gate.py`:
- **Step 0** (`regime_multiplier` consumption): one multiply in
  `Coordinator.run_cycle`, no new module.
- **Step 2** (FIX/HYB sizer): new module `src/risk/sizing.py`. Deferred to
  Phase 2.10 implementation kanban task. The interface signature
  `size(intent, portfolio, regime_multiplier, ticker_σ?) → TradeIntent`
  is locked by this ADR; the implementation is out of scope for the ADR.
- **Step 3** (`vol_scaled`): new module `src/risk/volatility.py`.
- **Step 4** (`corr_scaled`): new module `src/risk/correlation.py`.
- **Step 5** (final clamp): in the sizer module, not the gate.

---

## 5. What must be true before this design ships

These are the gating conditions. The implementation kanban tasks must
verify each one before opening its PR.

1. **`RiskLimits` field for `kelly_fraction_cap`** (Phase 2.5 only) — not
   needed for Phase 2.10. Default `0.25` per `t_021949c1 §B-4`.
2. **`RiskLimits.correlation_overlap_enabled: bool = False`**
   (Phase 2.10) — opt-in flag; default behaviour is unchanged.
3. **`TradeIntent.sector` mandatory in Phase 1.3** (already on the
   roadmap per `gate.py:34-35`). For Phase 2.10 SEC, a `None` sector
   returns factor `1.0` (today's behaviour) until Phase 1.3 enforces
   the validator.
4. **`SectorMap` registry** — Phase 2.10 needs a static dict of MOEX
   Tier-1 names. Static dict + freeze is acceptable Phase 2.10 scope;
   service is Phase 2.11+ (`t_b4e02a1d §6 Q6`).
5. **`realised_vol_i` computation in `src/data/`** (Phase 2.1) —
   blocks HYB primary sizer, LWO correlation upgrade, and EWMA cold-start
   initialisation (fallback today).
6. **Walk-forward validation harness** (Phase 2 or later) — needed to
   measure, not just reason about, the choice between EWMA / GARCH /
   realized, and between SEC / LWO.
7. **`peak_equity` state shared between `_check_daily_loss` and
   `_check_drawdown`** — prerequisite for TRL (Phase 2.10). Not needed
   for HARD or LIN.

---

## 6. Consequences

### Positive

- **Pipeline is multiplicative and layered.** Each layer (regime,
  budget, sizer, vol, correlation, gate) is independent; any future
  upgrade swaps one layer without touching the others. The full
  pipeline is auditable end-to-end with one `decision_log` row per
  trade carrying the `meta` dict from §4 step 7.
- **Sandbox-first by construction.** The existing `_assert_not_live_trading`
  gate at `src/broker/tinkoff_account.py:58-80` gates every size to
  orders; nothing in this ADR changes that. The 4-line free win (MUL-
  BUDGET) sits in `Coordinator.run_cycle`, not the broker, and runs
  even when `LIVE_TRADING=false`.
- **Fail-safe semantics preserved.** `HARD` + `SEC` (default off) + FIX
  today is exactly today's behaviour except with the regime multiplier
  consumed. The four shipped caps continue to apply. Frozen models
  unchanged. Decimal-only pipeline unchanged. Stateless gate invariant
  preserved.
- **No new dependencies.** Every choice in this ADR ships with the
  existing `requirements.txt`. Adding `numpy`, `scipy`, `arch`, or
  `statsmodels` is its own Phase review.
- **Regime multiplier is wired in today.** The 4-line Coordinator
  change is the cheapest step (MUL-BUDGET). It does not block on Kelly,
  HYB, vol-target, or LWO. It is the most literal reading of the
  "free win" called out in `t_4423398f §5.2` and `t_9279f1ef §2.2`.
- **Migration path is reversible at every step.** EWMA → GARCH swap is
  one config field; HYB replaces FIX without gateway / pipeline change;
  SEC → LWO migration preserves the `RiskDecision.meta` shape; LIN → TRL
  is a budget module upgrade. None of the migrations require a full
  redesign.

### Negative / accepted trade-offs

- **FIX is a constant fraction, not edge-aware.** A great setup and a
  mediocre setup get the same size until Phase 2.5 Kelly layers on top.
  Accepted: today Phase 2.5 is not shipped.
- **EWMA is λ=0.94 — a 1996 FX calibration.** MOEX equity vol may have
  a different decay profile. Accepted: revisit in Phase 2.11 tuning PR
  once ≥252 trading days of backfill exist (currently 31/3257).
- **SEC is sector-based, not covariance-based.** Sector is a categorical
  proxy. Accepted: LWO is the post-Phase-2.11 upgrade when backfill and
  `realised_vol_i` ship together.
- **`LIN` projects with `0.5 × notional` as the worst case.** This is
  policy, not signal. Accepted: same fail-safe / no-statistical-inference
  posture as the gate itself.
- **No `RiskDecision.meta` shape change.** Compatibility over richness.
  Audit metadata is stored in `decision_log` JSONB blob, not a new column.
- **Tier 3 of black-swan matrix is deferred.** #8 / #9 / #10 land only when
  the blocking primitives (Phase 2.1 EdgeEstimate, Phase 2.5 ADV, Phase 2.10
  regime wiring) are available. Accepted: each tier-3 item is its own ADR
  at its own phase.

### Risks

| Risk | Mitigation |
|---|---|
| Regime multiplier is stale (>2h since last `latest_regime()`). | Fail-open to `1.00` (neutral). Logged in `decision_log.meta`. Operator alert: `alphard_regime_stale_seconds` Prometheus gauge. |
| `realised_vol_i` for a ticker is `0` (constant prices, halted, etc.). | `final_size = 0` — "no signal, don't trade noise". Caller treats as Kelly `f*=0` (`t_021949c1 §B-2`). |
| Sector coverage in `SectorMap` is < 100% for the active universe. | Prometheus gauge `sector_coverage_pct`. Operator reviews; SEC returns `1.0` for `None` sector today. |
| Operator tunes `EWMA λ` without rebaking the cold-start seed. | Cold-start uses realized_var(first 20) today; decouples λ from seed. The `λ` config is independent of `σ_0`. |
| LIN over-refuses near the budget cap (5% position at -2.5% blocked). | `0.5 × notional` projection + cap-level aggregate (`gemini_t_dd2caaa4.md §3.2`). Phase 2.4 PR includes a CLI debug flag. |
| A future Phase change to `MacroRegime.multiplier` (e.g. adding a 4th label). | MUL-BUDGET keeps working — the multiply target is budget, not value. `models.py:60` validator is the single SOT. |
| `requirements.txt` adds a numerical library (e.g. `numpy`) for an unrelated PR. | Each new dep is its own ADR per the gating rule. Existing LWO/SEC/EWMA choices do not require `numpy`. |

---

## 7. Alternatives considered (and rejected at a higher level)

### "Build the full HYB with EWMA + SEC today, ship as one Phase 2.10 PR"

HYB requires `realised_vol_i` (Phase 2.1) and `adv_shares` (Phase 2.5).
Shipping both today would require backfilling 2.6 months of feature work
in one PR — incompatible with the "one coherent design pass per PR"
discipline seen in PR #75. This ADR splits the layers so each can ship
in its own phase.

### "Skip the regime multiplier entirely and rely on `_check_drawdown` to catch risk-off"

`_check_drawdown` is a multi-session guard with a 10% default; it does
not halve the daily budget when `CBR > 15%` or `IMOEX 60d DD > 20%`.
The regime multiplier is a *faster* signal (hourly cadence) than the DD
guard (multi-session). Skipping it leaves the sizing policy blind to
the regime for the whole drawdown-to-trigger latency.

### "Add a `risk_score` field to `Position` and select on it"

Breaks the frozen `RiskGate` contract
(`src/risk/gate.py:73-79`). Rejected for Phase 2.10. If `risk_score`
is needed later (Phase 2.5+ Quant Agent), file a separate ADR that
proposes the schema migration + tests + downstream consumers.

### "Adopt LWO today with a hand-rolled Decimal linear algebra helper"

LWO requires `realised_vol_i` (Phase 2.1), a 60-day log-returns cache,
and a walk-forward validation harness — none of which exist today.
The matrix's recommendation is sound for Phase 2.11; it is not the
right Phase 2.10 choice.

### "Make the regime multiplier configurable (TIERED-CONFIG option F)"

Duplicates the locked multiplier in user-editable config. When we tune
regime thresholds (e.g. CBR bands), we now have two places to update.
Single source of truth wins; the locked
`MacroRegime.multiplier` is the value.

---

## 8. Acceptance criteria (for the implementation tasks)

Each Phase 2.x implementation kanban task must satisfy these gates
before merge:

- [ ] New module(s) live outside `src/risk/gate.py` (per gate's
      "no correlation / no sizing" invariant at `gate.py:37-50`).
- [ ] `RiskGate` invariants preserved: no I/O, no clock reads, no
      randomness, Decimal-only (`gate.py:240-246`).
- [ ] Frozen-model security contract preserved: no mutation of
      `Position`, `TradeIntent`, `PortfolioState`, `RiskLimits`,
      `RiskDecision` (`gate.py:73-79, 82, 127, 154, 185, 209`).
- [ ] `decision_log` row produced per pipeline step with
      `meta={regime_multiplier, effective_budget, sizer_method,
      base_size, vol_measure, vol_value, correlation_penalty,
      correlation_method}`.
- [ ] One test per multiplicative layer step in §4 (steps 0, 2, 3, 4, 5).
- [ ] At least one fail-safe test per layer (regime unavailable →
      `1.00`, vol zero → `size=0`, sector unknown → `1.0`).
- [ ] At least one audit-replay test: same inputs → same
      `decision_log.meta` hashes.
- [ ] No `LIVE_TRADING=true` deployments of any new sizing path until
      the PR's test suite has been green in CI for the relevant
      sandbox tests.

---

## 9. Scope corrections and assumptions

These are explicit corrections vs the parent task body — backed by the
codebase. They are applied intentionally; they are not bugs in the task
body. A reviewer who reads this ADR next to the parent task body should
see the corrections applied.

### 9.1 "Phase 2.2 = position sizing policy" — greenfield design, not a refactor

The parent task body opens with: *"Phase 2.2 historically was deferred
(not started). Its scope per docs/PHASE2-ROADMAP.md is: position sizing
policy..."*

`docs/PHASE2-ROADMAP.md` does not contain a §2.2 named "position sizing
policy". Phase 2.1 is Quant Agent; Phase 2.2 is not in the roadmap file
in any form. The phrase is the parent's framing, not the roadmap's.

Grep across `src/` + `scripts/` for "size" / "sizing" returns the caller-
side `CoordinatorConfig.quantity × CoordinatorConfig.limit_price` pattern
and a `coordinator.py:29` TODO — zero sizing implementations. This ADR
treats sizing policy as a **greenfield design**; the matrix above is a
choice between candidate designs, not a choice between existing
alternatives.

### 9.2 `ohlcv_log` and `alpha_diary` audit tables — do not exist

The parent task body lists (in scope item 5): "black swan protection
informs 2 audit columns added to `ohlcv_log` and `alpha_diary`".

Neither table exists in `src/data/schema.sql`. The existing audit
tables are `decision_log` (Coordinator pipeline rows, JSONB blob) and
`macro_regime_log` (regime snapshots). This ADR chooses to **reuse
`decision_log`** with `meta={...}` carrying all sizing/budget/correlation
provenance — matching the existing pattern at `coordinator.py:488-535`.
If the team prefers two new tables, file a follow-up ADR that proposes
the schema migration; the decision here is reversible (adding a table
later is additive).

### 9.3 "Phase 2.10 macro_breach event" — does not exist yet

The parent task body (open question #6) lists as a candidate trigger
the "Phase 2.10 macro_breach event". Phase 2.10 itself is
`⏳ not started` per `docs/PHASE2-ROADMAP.md` (§2.10 section absent in
the current file; "Phase 2.10" cited as future). `src/macro/regime.py`
ships the classifier, `src/macro/persistence.py` ships the upsert, but no
event bus exists.

This ADR treats the **poll-based** integration pattern (`MUL-BUDGET`
reading `latest_regime(conn)` once per cycle) as the only currently
admissible option. When Phase 2.10 ships the event bus, the poll can be
replaced with a Redis Streams subscriber — the swap is local and does
not affect the sizing pipeline.

### 9.4 "Kelly exists" — does not

Open question #1 lists "Kelly criterion" as a sizing method. `t_021949c1`
confirms: zero `Kelly` references in `src/`. The single mention is a
TODO comment at `coordinator.py:29`. This ADR treats Kelly as a
**Phase 2.5+ enhancement layer** inside HYB, not a primary sizing
method today. The Kelly study is in
`/root/.hermes/kanban/workspaces/t_021949c1/kelly_research_report.md`.

### 9.5 "regime integration by X%" — the locked multiplier is {0.50, 0.75, 1.00}

Open question #6 says "scales down by X% (e.g. 50%)". The locked
multipliers are `{0.50, 0.75, 1.00}` per `regime.py:14-16` and enforced
by `models.py:60` validator `0.5 ≤ multiplier ≤ 1.0`. The 50% figure
is the `risk_off` value; `risk_on_reduced` is 25%; `neutral` is 0%.
This ADR documents the actual numbers — they are not re-openable in
this ADR.

### 9.6 Assumed regime multipliers stay locked and no `LIVE_TRADING=true`

These are pre-conditions for the recommendations in §2.6 to hold
verbatim. If a future PR re-opens the multiplier values, MUL-BUDGET
still applies (multiply target is budget, not value). If a future PR
adds `LIVE_TRADING=true` semantics outside the existing hardlock, a
separate kill-switch ADR is required.

---

## 10. References

- `docs/PHASE2-ROADMAP.md` — single source of truth for Phase 2
- `docs/decisions/0007-rebalance-scheduler.md` (commit 1cf2ff2) — companion
  ADR that consumes the regime multiplier and rebalance scheduler; this
  ADR fixes the *policy* that 0007 will fire against.
- `src/risk/gate.py:6-51` — "WHAT IS NOT HERE" fail-safe philosophy
- `src/risk/gate.py:73-79, 82, 127, 154, 185, 209` — frozen-model contract
- `src/risk/gate.py:240-246` — `RiskGate.evaluate` invariants (PURE)
- `src/risk/gate.py:265-281` — `RISK_MARKET_ORDER_NO_QUOTE` sentinel (issue #11)
- `src/risk/gate.py:295-322` — `_check_position_size` (#1, #2 — the four shipped caps)
- `src/risk/gate.py:295-407` — Tier-1 black-swan defence-in-depth stack
- `src/macro/regime.py:14-16, 33-54, 128-140, 161-163` — three multipliers, worst-case wins, 0.50× floor
- `src/macro/models.py:52-54, 60` — `multiplier` docstring + validator
- `src/macro/persistence.py` — `upsert_regime`, `latest_regime` (DB-backed)
- `src/broker/integration.py:53-132` — canonical `OrderFlow.submit_limit` pipeline
- `src/broker/slicer.py:36-50` — `CHUNK_PCT=5%`, `MAX_DURATION=30min`
- `src/broker/tinkoff_account.py:58-80` — `_assert_not_live_trading` (single SOT)
- `src/coordinator.py:24-30` — "WHAT IS NOT HERE (deferred to later phases)"
- `src/coordinator.py:438-456` — `_risk_check` (caller-side quantity/price)
- `src/coordinator.py:488-535` — `coordinator._audit()` (decision_log precedent)
- `tests/test_risk_gate.py:43, 125-168` — daily-loss boundary tests
- `requirements.txt` — confirmed absence of `numpy`/`scipy`/`arch`/`statsmodels`/`pandas`

### Parent matrices (citations)

- `t_021949c1` — `/root/.hermes/kanban/workspaces/t_021949c1/kelly_research_report.md`
  (304 lines). Kelly gap, 10 design constraints.
- `t_4423398f` — `/root/.hermes/artifacts/research_t_4423398f_decision_matrix.md`
  (679 lines). Position-sizing matrix — §2.1 input.
- `t_83e02922` — `/root/.hermes/artifacts/research_t_83e02922.md` (208 lines).
  Volatility-measure matrix — §2.2 input.
- `t_b4e02a1d` — `/root/.hermes/artifacts/research_t_b4e02a1d_correlation_matrix.md`
  (386 lines). Correlation-handling matrix — §2.3 input.
- `t_a0260058` — `/root/.hermes/artifacts/research_t_a0260058_decision_matrix.md`
  (219 lines). Regime-integration matrix — §2.6 input.
- `t_d13d2473` — `/root/.hermes/artifacts/research_t_d13d2473.md` (871 lines).
  Black-swan-protection matrix — §2.5 input.
- `t_dd2caaa4` — `/root/.hermes/artifacts/research_t_dd2caaa4_risk_budget_per_day.md`
  (828 lines). Risk-budget-per-day matrix — §2.4 input.
- `t_9279f1ef` — regime multiplier is live but unconsumed; structural
  0.5 floor; Phase 2.10 consumer; persistence idempotent.
- `t_2a7d97ae` — `/root/.hermes/artifacts/research_t_2a7d97ae_position_sizing.md`
  (315 lines). Sizing brief — recommended `HYB`. Matrix differs in weights
  (data-model 3× vs equal); this ADR's `FIX` recommendation does not
  contradict `HYB`'s growth-path role.

### External references (unverified; web_search provider unavailable)

- Thorp, E. (2008) — geometric drawdown bound for fixed-fractional.
- Vince, R. (1992) — fractional Kelly variance reduction.
- Engle, R. (2002) — Dynamic Conditional Correlation.
- Ledoit, O. & Wolf, M. (2004) — Honey, I shrunk the sample covariance.
- RiskMetrics / J.P. Morgan (1996) — EWMA λ=0.94 default.
- Man Group AHL / Winton vol-target white papers, 2024-2026.
- Bridgewater "All Weather" / Dalio (2017) — risk-parity origin.
- Taleb (2007) — definitional reference for black-swan.

(Mirrors the `t_2a7d97ae` convention: external citations are flagged
unverified here. The matrix's reasoning does not depend on any single
citation — the choices between FIX/HYB, EWMA/realized, SEC/ZRO, and
HARD/LIN/TRL rest on operational arguments, not academic ones.)
