# ADR 0007: Rebalance Scheduler — Phase 2.4 Design Decision

- **Status:** Proposed
- **Date:** 2026-08-22
- **Deciders:** @developer (kanban task t_f9807eea), reviewed by orchestrator
- **Parent task:** t_2c7b8808 ("Phase 2.4: design decision — rebalance scheduler")
- **Source review:** `scheduler_risk_state_review.md` (task t_0c78e74a, ~19 KB, 385 lines)

> **Important up-front note:** This ADR is a **greenfield design**, not a
> decision between existing alternatives. Three place names in the original
> task body (`"Phase 2.4 rebalance scheduler"`, `ohlcv_log` / `alpha_diary`
> audit tables, and `Phase 2.10 macro_breach event`) **do not exist** as
> written. The roadmap and codebase contradict the parent task body in three
> concrete places (see [§10 Scope corrections](#10-scope-corrections)).
> A reviewer who reads this ADR next to the task body will see three
> corrections applied — they are intentional, evidence-backed, and
> necessary to keep the ADR consistent with the codebase.

---

## 1. Context

Alphard's Phase 2 architecture activates autonomous agents (data, macro,
risk, execution). The macro agent already ships a deterministic regime
classifier (`src/macro/regime.py::classify()`) and a Postgres-backed
regime log (`src/macro/persistence.py`). When the regime flips to
`risk_off`, the trading book needs to recompute position sizes and emit
orders. **That rebalancing step is the responsibility of a rebalance
scheduler** — the topic of this ADR.

The roadmap historically punts this: `src/main.py:23` says verbatim
`Rebalance logic: Phase 4.`, and a full grep of `src/` + `scripts/`
returns zero other rebalance references (per
`scheduler_risk_state_review.md` §0, item 1). No code, no table, no
schedule, no subprocess exists today for rebalancing. We are
**designing from scratch** with the existing modules as integration
surface.

### What already exists (the integration surface)

| Module | Role rebalance can reuse |
|---|---|
| `src/main.py::_macro_sync_loop` (lines 459–513) | Subprocess + `_sleep_interruptible()` + cadence constants at `src/main.py:75-78` — the in-codebase template for a long-running scheduler |
| `src/macro/persistence.py::latest_regime(conn)` | Read most-recent `MacroRegime` (label + multiplier + snapshot) — the gating signal for "risk_off → rebalance now" |
| `src/broker/integration.py::OrderFlow.submit_market` (lines 53–132) | Canonical Universe → RiskGate → OrderSlicer → Broker pipeline |
| `src/broker/slicer.py::OrderSlicer` | 5%-ADV chunking, 30-min max, rate-limit-aware |
| `src/data/fallback_loader.py::FallbackDataLoader.iter_ohlcv` | Live-price fetch chain (`tinkoff_md → tinkoff_grpc → moex_iss`) |
| `src/data/schema.sql::decision_log` (line ~155) | JSONB blob audit table — existing precedent for "decision-lineage" rows |
| `src/risk/gate.py::RiskGate.evaluate` | Five fail-safe checks, including `RISK_MARKET_ORDER_NO_QUOTE` at line 274 |

### Hard constraints the scheduler must honour

1. **Frozen models.** All five models in `src/risk/gate.py` are
   `model_config = ConfigDict(extra="forbid", frozen=True)` (line 73-79).
   Any new field on `Position` / `TradeIntent` is a breaking change to
   the gate contract.
2. **`LIVE_TRADING=false` hardlock** at `src/broker/tinkoff_account.py:58-80`.
   `_assert_not_live_trading()` is the **single source of truth**. The
   rebalance scheduler cannot ship orders without an explicit operator
   opt-in. Sandbox-first is the only legal Phase 1/2 path.
3. **`RISK_MARKET_ORDER_NO_QUOTE`** at `src/risk/gate.py:274`: every
   `TradeIntent` with `intent.price == Decimal("1") AND quantity > 1`
   is rejected. Rebalance orders MUST populate price from a real fetch.
4. **TokenBucket floor** at `src/data/token_bucket.py:65-78`:
   `capacity >= 1.0` is a hard contract. The scheduler inherits whatever
   the loader's bucket is.
5. **`allow_short=False` default** at `src/risk/gate.py::RiskLimits`.
   A `risk_off` strategy that needs to flatten longs (sell-only) is fine;
   any strategy that needs to short (e.g. inverse ETFs) is blocked until
   the operator opts in.
6. **Survivorship filter** at `src/data/store.py:18-23`: `upsert_ohlcv`
   never deletes delisted rows. The scheduler must filter
   `TickerMeta.delisted == True` out of the candidate set itself.

### The Coordinator one-shot constraint

`src/coordinator.py:30` says explicitly:

> *"this stub handles ONE symbol at a time"*

A rebalance fires **N trades** (one per held ticker). The scheduler
must either invoke `Coordinator` N times (one trade per subprocess) or
add a new bulk path (`Coordinator.run_multi()`). N invocations is the
conservative answer — TOCTOU window is 100ms by default, fine for
sequential orders.

### The one-symbol-per-call pattern

> Each invocation runs:
>
> `Universe → RiskGate.evaluate() → OrderSlicer.slice() → broker.place_order() → audit_log`

### Why now, not later

The macro classifier already runs hourly (`src/main.py:76`). When the
regime flips, *something* must decide whether to rebalance, when, and
how often. Without an ADR, the natural drift is for a developer to wire
a cron + ad-hoc Python script with no review of risk-gate invariants.
The ADR locks in a sandbox-first, subprocess-isolated, audit-logged
pattern before the wiring happens.

---

## 2. Decision

**Adopt a subprocess-isolated, poll-based, sandbox-first rebalance
scheduler with the following properties:**

1. **Trigger:** poll `latest_regime(conn)` on a 5-minute cadence inside
   the subprocess. When the regime flips to `risk_off` AND the
   "rebalance-cooldown" gate has elapsed, run a rebalance. Defer
   event-driven triggers (Phase 2.10 Redis Streams) to Phase 2.10 —
   this ADR does not block on it.
2. **Frequency cap:** **max 1 rebalance per trading day per regime
   flip** + a 4-hour hard cooldown between consecutive rebalances.
3. **Position sizing:** **rebalance ALL positions** to a defensive
   weight scheme computed from `regime.multiplier` (currently
   `[0.50, 0.75, 1.00]`) × `risk_limits.max_position_pct`. Do NOT
   introduce `risk_score` on `Position` — that would break the frozen
   gate contract (`src/risk/gate.py:73-79`).
4. **Order type:** **LIMIT-only with slippage cap of 0.5%**. The
   Phase 1.4 hard rule is "post-only / LIMIT-only" (`docs/PHASE2-ROADMAP.md:29`).
   No market orders in Phase 2.
5. **Audit trail:** write to `decision_log` with
   `kind='rebalance'`, `ticker=NULL`, `decision=<JSONB blob containing
   regime, target_weights, fills, slippage, errors>`. Reuse the
   existing `decision_log` row pattern (`coordinator.py:488-535`); do
   NOT add a new `ohlcv_log` / `alpha_diary` table.
6. **Rollback / failure:** **no retry**. On Tinkoff reject or
   `RiskGate.allowed=False`, log the failure to `decision_log` with
   `outcome='rejected'` and exit the subprocess. The next 5-min poll
   re-evaluates the regime — if the regime hasn't flipped back, the
   cooldown gate suppresses another rebalance attempt; if it has, the
   cooldown is irrelevant.

The implementation is **out of scope** for this ADR — this document
locks the design decisions; code lands in a separate kanban task.

---

## 3. Decision matrix — six open questions, scored

For each open question from the parent task body, the matrix lists
2–3 candidate paths with pros / cons and the chosen one in **bold**.
Scoring axes: **Simplicity**, **Risk-gate compatibility**, **Operational
blast radius**, **Audit completeness**, **Phase-2 fit**.

### 3.1 Scheduler trigger — how does the rebalance start?

| Option | Simplicity | Risk-gate compat | Blast radius | Audit completeness | Phase-2 fit | Score |
|---|---|---|---|---|---|---|
| **A. Poll `latest_regime(conn)` on 5-min cadence** | ✅ no event bus needed | ✅ no gate change | ✅ subprocess crash → restart only | ✅ every poll logs | ✅ fits `_macro_sync_loop` template | **CHOSEN** |
| B. Event from Phase 2.10 macro_breach | ❌ requires Redis Streams (not shipped) | ✅ no gate change | ✅ subprocess crash → restart only | ✅ events log naturally | ❌ blocks on Phase 2.10 | rejected |
| C. Cron @ -15min fixed schedule | ✅ trivial | ✅ no gate change | ⚠️ fires regardless of regime | ⚠️ regime log needed anyway | ⚠️ ignores Phase 2.3 outputs | rejected |

**Rationale:** Phase 2.10 event-driven design is `⏳ not started` per
`docs/PHASE2-ROADMAP.md:209-211`. Building the rebalance scheduler
around an event bus that doesn't exist would block this work on an
unrelated phase. Polling at 5-min cadence costs nothing — a single
`SELECT ... FROM macro_regime_log ORDER BY fetched_at DESC LIMIT 1`
on a 5-min cadence is ~300 queries/day, each sub-millisecond on a
warm Postgres connection. The poll loop mirrors the proven
`_macro_sync_loop` pattern (`src/main.py:459-513`).

### 3.2 Frequency cap — how often can a rebalance fire?

| Option | Simplicity | Risk-gate compat | Blast radius | Audit completeness | Phase-2 fit | Score |
|---|---|---|---|---|---|---|
| A. Uncapped | ✅ trivial | ❌ violates `RISK_DAILY_LOSS` | ❌ infinite loop risk | ❌ noise | ❌ no | rejected |
| **B. Max 1 per regime flip + 4h cooldown** | ⚠️ one extra state read | ✅ respects all 5 checks | ✅ bounded | ✅ clear log | ✅ defensive | **CHOSEN** |
| C. Max 1 per trading day | ⚠️ day-boundary logic | ✅ respects all 5 checks | ✅ bounded | ⚠️ miss urgent flips | ⚠️ coarse | rejected |
| D. Max 3 per day | ✅ trivial | ⚠️ could trigger 3x RISK_DAILY_LOSS | ⚠️ moderate | ⚠️ noisy | ⚠️ speculative | rejected |

**Rationale:** A pure "max 1 per trading day" (option C) misses urgent
flips (e.g., risk_off at 14:00 then a deeper drawdown at 17:00 needs
another reduction). Uncapped (A) violates `RISK_DAILY_LOSS` at
`src/risk/gate.py:RISK_DAILY_LOSS`. The 1-flip + 4h-cooldown gate
(option B) is the simplest cap that respects both axes — the cooldown
is enforced by checking `now() - last_rebalance_at > 4h` in a tiny
`rebalance_state` row or in-memory dict keyed by regime-flip timestamp.

### 3.3 Position sizing — what weight scheme?

| Option | Simplicity | Risk-gate compat | Blast radius | Audit completeness | Phase-2 fit | Score |
|---|---|---|---|---|---|---|
| **A. Rebalance ALL to `multiplier × max_position_pct`** | ✅ single formula | ✅ no gate change | ✅ predictable | ✅ full audit | ✅ uses Phase 2.3 output | **CHOSEN** |
| B. Rebalance only `risk_score > threshold` | ❌ no `risk_score` field exists | ❌ breaks frozen `Position` model | ⚠️ subset unknown | ⚠️ partial | ⚠️ would need new field | rejected |
| C. Rebalance only `Position.lot > N` (liquidity filter) | ⚠️ depends on universe | ✅ no gate change | ⚠️ subset | ⚠️ partial | ⚠️ ignores macro | rejected |

**Rationale:** `Position` (`src/risk/gate.py:116-132`) has `sector`
only, no `risk_score`. Adding `risk_score` would require modifying
the frozen gate models (`model_config = ConfigDict(frozen=True)`,
line 73) — a hard project-wide invariant. The state review at
`scheduler_risk_state_review.md` §4e flags this explicitly. The
chosen option (A) uses **only** the existing `MacroRegime.multiplier`
[0.50, 0.75, 1.00] field — no schema change, no gate change.

### 3.4 Order type — market, limit, or hybrid?

| Option | Simplicity | Risk-gate compat | Blast radius | Audit completeness | Phase-2 fit | Score |
|---|---|---|---|---|---|---|
| A. Market orders | ⚠️ needs live quotes | ⚠️ RISK_MARKET_ORDER_NO_QUOTE rejects `Decimal("1")` | ❌ slippage risk | ✅ clean log | ❌ violates Phase 1.4 hard rule | rejected |
| **B. LIMIT-only with 0.5% slippage cap** | ⚠️ needs price fetch | ✅ limit price satisfies gate | ✅ bounded slippage | ✅ cap is auditable | ✅ matches Phase 1.4 | **CHOSEN** |
| C. Hybrid: market if not filled in 5 min | ❌ two code paths | ❌ market path still triggers RISK_MARKET_ORDER_NO_QUOTE | ⚠️ mid-fill risk | ⚠️ partial fill log | ❌ speculative | rejected |

**Rationale:** The Phase 1.4 hard rule (`docs/PHASE2-ROADMAP.md:29`)
is "post-only / LIMIT-only ... no MARKET, no SHORT until Phase 2".
Option A violates this. Option C's "5 min market fallback" reintroduces
the very `RISK_MARKET_ORDER_NO_QUOTE` rejection the gate was designed
to catch (issue #11, `src/risk/gate.py:265-281`). Option B is the only
choice that satisfies the gate, the Phase 1.4 rule, and is auditable.
The 0.5% slippage cap is consistent with the `±0.5% per bar` tolerance
already documented in `docs/PHASE2-ROADMAP.md:188`.

### 3.5 Audit trail — where do rebalance events live?

| Option | Simplicity | Risk-gate compat | Blast radius | Audit completeness | Phase-2 fit | Score |
|---|---|---|---|---|---|---|
| **A. Reuse `decision_log` (JSONB blob, `kind='rebalance'`)** | ✅ precedent at `coordinator.py:488-535` | ✅ no gate change | ✅ no migration | ✅ matches existing lineage | ✅ same lineage as trades | **CHOSEN** |
| B. New `ohlcv_log` table | ❌ does not exist in `schema.sql` | ✅ no gate change | ⚠️ schema migration | ✅ OHLCV-centric | ❌ duplicates `decision_log` | rejected |
| C. New `alpha_diary` table | ❌ does not exist in `schema.sql` | ✅ no gate change | ⚠️ schema migration | ✅ diary-style | ❌ duplicates `decision_log` | rejected |
| D. Log-only (Postgres + journald) | ⚠️ dual storage | ✅ no gate change | ⚠️ split between layers | ⚠️ not queryable | ⚠️ inconsistent with trade audit | rejected |

**Rationale:** `src/data/schema.sql` has `decision_log` (Coordinator
pipeline rows, JSONB blob) and `macro_regime_log` (regime snapshots)
as its audit tables. There is no `ohlcv_log`, no `alpha_diary`. The
parent task body quotes both nonexistent tables; reusing `decision_log`
with `kind='rebalance'` matches the existing pattern at
`src/coordinator.py:488-535` (`coordinator._audit()` writes one JSONB
row per pipeline step). Schema additions (options B/C) require a
migration + review — out of scope for a Phase 2.4 design decision.

### 3.6 Rollback — what if Tinkoff rejects an order?

| Option | Simplicity | Risk-gate compat | Blast radius | Audit completeness | Phase-2 fit | Score |
|---|---|---|---|---|---|---|
| A. Retry 3× with exponential backoff | �️ retry logic | ⚠️ retry could trigger `RISK_DAILY_LOSS` cumulatively | ⚠️ retry could exceed cooldown | ✅ retry log | ⚠️ speculative | rejected |
| **B. No retry, log to `decision_log`, exit subprocess** | ✅ trivial | ✅ no gate interaction | ✅ bounded | ✅ clean reject log | ✅ respects Phase 1.4 conservatism | **CHOSEN** |
| C. Roll back to previous portfolio state | ❌ complex | ❌ requires state snapshots | ❌ partial fills complicate | ✅ state-level log | ❌ speculative | rejected |

**Rationale:** A retry loop (option A) accumulates slippage + risk
exposure on each attempt and could itself trip `RISK_DAILY_LOSS`
cumulatively. A rollback (option C) requires portfolio state snapshots
that don't exist today and adds a recovery path that has not been
reviewed. The chosen path (option B) — log reject, exit subprocess —
is the conservative Phase 1/2 default: the 5-min poll loop re-evaluates
the regime on the next tick; if the regime hasn't changed, the cooldown
gate suppresses another attempt; if it has changed, the new regime
triggers a fresh rebalance with fresh orders.

---

## 4. Implementation outline (subprocess pattern, no code in this ADR)

The rebalance subprocess follows the proven `_macro_sync_loop` template:

```
def _rebalance_loop():
    logger.info("rebalance scheduled: every REBALANCE_CADENCE_SECONDS, "
                "subprocess_timeout=REBALANCE_SUBPROCESS_TIMEOUT")
    _sleep_interruptible(REBALANCE_FIRST_RUN_DELAY_SECONDS)  # 5 min
    while not _shutdown_event.is_set():
        try:
            subprocess.run(
                ["python", "scripts/run_rebalance.py"],
                capture_output=True, text=True,
                timeout=REBALANCE_SUBPROCESS_TIMEOUT,
                cwd="/app",
            )
        except subprocess.TimeoutExpired: ...
        except Exception: ...
        _sleep_interruptible(REBALANCE_CADENCE_SECONDS)  # 5 min
```

Constants at `src/main.py` (alongside `MACRO_SYNC_CADENCE_SECONDS`):

```python
REBALANCE_CADENCE_SECONDS = 300            # 5 min poll
REBALANCE_FIRST_RUN_DELAY_SECONDS = 5 * 60  # 5 min after launch
REBALANCE_SUBPROCESS_TIMEOUT = 600          # 10 min hard cap (single rebalance)
REBALANCE_COOLDOWN_SECONDS = 4 * 3600       # 4 h between consecutive rebalances
REBALANCE_MAX_PER_DAY = 1                   # one per trading day per flip
```

Subprocess entrypoint `scripts/run_rebalance.py` (pseudocode):

1. Open `PostgresDataStore` (`src/data/pg_store.py`).
2. `latest_regime(conn)` from `src/macro/persistence.py`.
3. If regime is `neutral` AND no flip since last poll → exit 0 (no-op).
4. If `now() - last_rebalance_at < REBALANCE_COOLDOWN_SECONDS` → exit 0 (cooldown).
5. Otherwise: enumerate `Position` from broker snapshot, compute target
   weights = `multiplier × max_position_pct`, build N `TradeIntent`s with
   live LIMIT prices fetched via `FallbackDataLoader.iter_ohlcv(ticker, today, today)`,
   call `Coordinator` N times (one per ticker), audit each result to
   `decision_log` with `kind='rebalance'`.

This pattern reuses `src/main.py:_sleep_interruptible` (line 516-530)
verbatim and adds no new failure modes beyond the existing
subprocess-isolated pattern.

---

## 5. Consequences

### Positive

- **Sandbox-first by construction.** The rebalance subprocess calls
  the existing `_assert_not_live_trading` gate at
  `src/broker/tinkoff_account.py:58-80`. With `LIVE_TRADING=false`,
  the subprocess exits cleanly with `LIVE_TRADING=false — refusing
  rebalance ...` logged. No production code path changes.
- **Crash isolation.** Subprocess + timeout mirrors `_macro_sync_loop`
  (issue #70 hard constraint). A network hang in Tinkoff gRPC does
  not kill the heartbeat — exactly the failure mode
  `_macro_sync_loop` was designed to defend against.
- **Auditable.** Every rebalance attempt (including no-op polls)
  produces one `decision_log` row with `kind='rebalance'`. The
  existing `coordinator._audit()` precedent (`coordinator.py:488-535`)
  makes the schema-side integration trivial.
- **No gate mutation.** All five `RiskGate` checks continue to apply.
  No new field on `Position` / `TradeIntent`; no `RISK_*` enum
  extension. The frozen contract (`src/risk/gate.py:73-79`) is
  preserved.
- **Phase-2.10-ready.** When Phase 2.10 ships the event bus, the poll
  inside the subprocess can be replaced by a Redis Streams subscriber
  with no change to the rebalance logic itself — the subprocess is
  already the unit of scheduling.

### Negative / accepted trade-offs

- **5-minute detection latency** for a regime flip. Acceptable because
  the macro classifier itself runs hourly (`src/main.py:76`), and a
  5-min poll after a 1h classification is a 1h+5min worst-case latency
  — well below the daily-cadence decision cycle.
- **Subprocess overhead per poll.** Each 5-min poll is a fresh Python
  process + Postgres connection. ~300/day. Acceptable because the
  `run_macro_sync.py` precedent shows the same pattern works (issue #70).
- **No `risk_score` semantics.** Operators cannot ask "rebalance only
  the high-risk positions". Accepted because `Position` is frozen; the
  question is deferred to a future ADR if the field is added.
- **No retry on reject.** A single Tinkoff reject skips the rebalance
  for the rest of the day. Accepted because (a) the next poll re-checks
  the regime, (b) the cooldown gate prevents burst behaviour, and (c)
  Phase 1.4 conservatism is "fail safe, log everything" rather than
  "retry until success".
- **No market-order fallback.** A LIMIT order that doesn't fill within
  the day is left alone (not converted to market). Accepted because
  Phase 1.4 prohibits market orders (`docs/PHASE2-ROADMAP.md:29`).

### Risks

| Risk | Mitigation |
|---|---|
| TokenBucket starvation across rebalance + macro + corp-actions subprocs | Each subprocess opens its own bucket; no shared state. Monitor `alphard_heartbeats_total` for macro, add a parallel `alphard_rebalances_total` counter for rebalance. |
| `Coordinator` invocation N times means N audit rows | Already the pattern (one `decision_log` row per `Coordinator.run_once()`). The rebalance audit row is one extra `kind='rebalance'` summary row. |
| Phase 2.10 Redis Streams ships before this lands | The poll-based trigger is a strict subset of "react to a stream"; swapping is a one-line change. No migration needed. |
| A `risk_off` flip during MOEX closed hours (after 18:40 MSK) | The 5-min poll still detects it; the rebalance runs at next open with LIMIT orders. This is the correct behaviour — no after-hours trading in Phase 1/2. |

---

## 6. Alternatives considered (and rejected at a higher level)

### "Wait for Phase 2.10 and build event-driven from day one"

Phase 2.10 is `⏳ not started` (`docs/PHASE2-ROADMAP.md:209-211`).
Building the rebalance scheduler on top of a not-yet-designed event
bus would block this work on Phase 2.10. The poll-based design is a
strict subset of event-driven — the swap is local.

### "Skip the scheduler entirely; do rebalances manually via CLI"

Operator-driven rebalances do not need an ADR. They do, however, lose
the audit trail, the risk-gate invariants, and the cooldown gate —
and they scale linearly with operator attention. The automated
subprocess is cheaper in the long run and consistent with
`_macro_sync_loop` and `_corp_actions_apply_loop` (`src/main.py:413-456`).

### "Rebalance on every regime flip with no cooldown"

Violates `RISK_DAILY_LOSS` (`src/risk/gate.py:RISK_DAILY_LOSS`) and
the Phase 1.4 conservatism principle. Rejected.

### "Add a `risk_score` field to `Position` and select on it"

Breaks the frozen `RiskGate` contract
(`src/risk/gate.py:73-79`). Rejected for Phase 2.4. If the field is
needed later, file a separate ADR that proposes the schema migration
+ tests + downstream consumers.

---

## 7. Phase 2.4 vs Phase 4 — clarifying the roadmap contradiction

`src/main.py:23` says `Rebalance logic: Phase 4.` This ADR lands the
**design** for Phase 2.4. Implementation can land in either Phase 2.4
(sandbox-only, gated by `LIVE_TRADING=false`) or Phase 4 (production
after the integration-test gate at `docs/PHASE2-ROADMAP.md:34-35`).
The design is identical; the gating is identical (`LIVE_TRADING` +
`RISK_*` env vars). No conflict with the Phase 4 deferral.

---

## 8. Acceptance criteria (for the implementation task)

- [ ] `src/main.py` exposes `REBALANCE_*` constants alongside
      `MACRO_SYNC_*` (mirror style at `src/main.py:75-78`).
- [ ] `_rebalance_loop()` daemon thread in `src/main.py` mirrors
      `_macro_sync_loop` (lines 459-513) line-for-line except for
      the cadence constants and the script name.
- [ ] `scripts/run_rebalance.py` exists, runs idempotently, and
      respects `LIVE_TRADING=false`.
- [ ] Every rebalance attempt writes one `decision_log` row with
      `kind='rebalance'`.
- [ ] No change to `src/risk/gate.py`, no change to
      `src/broker/tinkoff_account.py`, no change to `src/data/schema.sql`.
- [ ] Tests: at least one test for "regime is neutral → no-op",
      one for "regime flips to risk_off and cooldown elapsed →
      rebalance fires", one for "rebalance fires and is rejected by
      `RiskGate` → `decision_log` row with `outcome='rejected'`",
      one for `LIVE_TRADING=false` no-trade gate.

---

## 9. References

- `docs/PHASE2-ROADMAP.md` — single source of truth for Phase 2
- `src/main.py:23` — "Rebalance logic: Phase 4." (deferral marker)
- `src/main.py:75-78` — cadence constants template (`MACRO_SYNC_*`)
- `src/main.py:459-513` — `_macro_sync_loop` subprocess template
- `src/main.py:516-530` — `_sleep_interruptible()` helper
- `src/broker/tinkoff_account.py:58-80` — `_assert_not_live_trading` (single source of truth)
- `src/broker/integration.py:53-132` — canonical `OrderFlow` pipeline
- `src/broker/slicer.py:36` — `CHUNK_PCT = 5%` (5%-ADV slicing)
- `src/risk/gate.py:73-79` — frozen-model security contract
- `src/risk/gate.py:265-281` — `RISK_MARKET_ORDER_NO_QUOTE` (issue #11)
- `src/risk/gate.py:116-132` — `Position` dataclass (no `risk_score` field)
- `src/data/token_bucket.py:65-78` — `capacity >= 1.0` contract
- `src/data/schema.sql` — `decision_log`, `macro_regime_log` (existing audit tables; no `ohlcv_log` or `alpha_diary`)
- `src/macro/regime.py:33-54` — classifier (4 branches, 4 edges)
- `src/macro/persistence.py` — `upsert_regime`, `latest_regime` (store-agnostic)
- `src/coordinator.py:30` — "this stub handles ONE symbol at a time"
- `src/coordinator.py:488-535` — `coordinator._audit()` precedent for `decision_log`
- `scheduler_risk_state_review.md` (task t_0c78e74a, 385 lines) — full state map

---

## 10. Scope corrections

These three corrections are applied intentionally. They are not bugs in
the parent task body — they are evidence from the codebase that
contradicts three place names quoted verbatim. A reviewer who reads
this ADR next to the task body should see the corrections applied.

### 10.1 "Phase 2.4 = rebalance scheduler" — greenfield, not a refactor

The parent task body opens with:
> *"Phase 2.4 historically was deferred (not started). Its scope per
> docs/PHASE2-ROADMAP.md is: rebalance scheduler — when macro regime
> enters risk_off, recompute position sizes and emit orders."*

This is **wrong about the roadmap**. `docs/PHASE2-ROADMAP.md` has two
§2.4 sections: (a) "Coordinator wired to all agents" and (b) "Adjusted
prices pipeline (split adjustment)". Neither is a rebalance scheduler.
`src/main.py:23` explicitly punts: `Rebalance logic: Phase 4.`. Grep
across `src/` + `scripts/` returns zero other rebalance references.

This ADR treats the rebalance scheduler as a **greenfield concept**.
The decision matrix above is therefore a choice between **candidate
designs**, not a choice between **existing alternatives**.

### 10.2 `ohlcv_log` and `alpha_diary` audit tables — do not exist

The parent task body (Scope item 5) says:
> *"Audit trail: every rebalance logged to ohlcv_log + alpha_diary"*

Neither table exists in `src/data/schema.sql`. The existing audit
tables are `decision_log` (Coordinator pipeline rows, JSONB blob) and
`macro_regime_log` (regime snapshots).

This ADR chooses to **reuse `decision_log`** with `kind='rebalance'`.
If the team prefers two new tables (`ohlcv_log` for OHLCV-context,
`alpha_diary` for narrative), file a follow-up ADR that proposes
the schema migration. The decision here is reversible — adding a
table later is additive; naming a nonexistent table in this ADR
would force a migration now.

### 10.3 "Phase 2.10 macro_breach event" — does not exist yet

The parent task body (Scope item 1) lists as a candidate trigger:
> *"event from Phase 2.10 macro_breach"*

Phase 2.10 itself is `⏳ not started` per
`docs/PHASE2-ROADMAP.md:209-211`. `src/macro/regime.py` ships the
classifier, `src/macro/persistence.py` ships the upsert, but no event
bus exists.

This ADR chooses **poll on a 5-min cadence** inside the subprocess
(see §3.1 option A). When Phase 2.10 ships, the poll can be replaced
with a Redis Streams subscriber — the swap is local and does not
affect the rebalance logic.

---

## 11. Decision record (sign-off)

- **Proposed by:** @developer (kanban task t_f9807eea)
- **Date:** 2026-08-22
- **Status:** Proposed (pending orchestrator review at task t_2c7b8808)
- **Next review:** when implementation lands; ADR may be superseded if
  Phase 2.10 ships a different event-driven primitive

If a reviewer rejects the poll-based trigger, the recommended
fallback is "defer to Phase 2.10" rather than "ship event-driven
in Phase 2.4" — see §6 for the rationale.
