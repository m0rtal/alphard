# Alphard — Public API contract

**Last updated:** 2026-08-27
**Audience:** Agent authors, integrators, code reviewers
**Status:** canonical. Any public signature listed here is part of the
stable contract; breaking changes require a major version bump.

This document specifies the **public** Python API that Alphard exposes
to in-tree callers, third-party agents, and integration tests. Internal
helpers (`_fetch`, `_validate`, `_risk_check`, etc.) are NOT public and
may change without notice.

For system-level architecture (containers, threads, deployment
topology) see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Table of contents

1. [Coordinator contract](#1-coordinator-contract)
2. [RiskGate contract](#2-riskgate-contract)
3. [Agent interface (planned)](#3-agent-interface-planned)
4. [Environment contract](#4-environment-contract)
5. [AuditLog contract](#5-auditlog-contract)
6. [Versioning policy](#6-versioning-policy)
7. [Hello World Agent (example)](#7-hello-world-agent-example)

---

## 1. Coordinator contract

The Coordinator is the only code path that places a trade. It is a
fail-safe pipeline that takes a frozen `CoordinatorConfig`, runs
FETCH → VALIDATE → RISK → EXECUTE → AUDIT, and returns a frozen
`PipelineResult`.

### 1.1 `CoordinatorConfig` (dataclass, frozen)

**Location:** `src/coordinator.py:93`

```python
@dataclass(frozen=True)
class CoordinatorConfig:
    ticker: str                              # uppercase + stripped at __post_init__
    side: CoordinatorSide                    # BUY | SELL
    quantity: Decimal                        # > 0
    limit_price: Decimal                     # > 0
    risk_limits: RiskLimits                  # see §2
    portfolio_equity: Decimal                # > 0
    portfolio_cash: Decimal                  # >= 0
    portfolio_peak: Decimal                  # >= portfolio_equity
    portfolio_daily_pnl: Decimal = Decimal("0")  # issue #197
    fetch_lookback_days: int = 5 * 365
    live_trading: bool = False               # SAFETY: refuse ALL orders if false
    store_dsn: str | None = None
    toctou_max_seconds: float = 0.100        # RISK→EXECUTE freshness window
```

**Invariants:**
- `ticker` is normalised to uppercase + stripped at construction
  (`src/coordinator.py:129` `__post_init__`).
- Empty ticker raises `ValueError`.
- `frozen=True` → any post-construction assignment raises
  `FrozenInstanceError`.

### 1.2 `CoordinatorSide` (enum, str)

**Location:** `src/coordinator.py:77`

```python
class CoordinatorSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
```

### 1.3 `PipelineStage` (enum, str)

**Location:** `src/coordinator.py:82`

```python
class PipelineStage(str, Enum):
    FETCH = "fetch"
    VALIDATE = "validate"
    RISK = "risk"
    EXECUTE = "execute"
    AUDIT = "audit"
    DONE = "done"
    SKIPPED = "skipped"
```

The `PipelineResult.stages_completed` tuple records which stages ran
in this pipeline iteration. A `SKIPPED` appended after a partial
sequence indicates the pipeline was blocked (see §1.5 below).

### 1.4 `Coordinator.run_once()`

**Location:** `src/coordinator.py:253`

```python
def run_once(self) -> PipelineResult:
```

**Returns:** a fully-populated `PipelineResult`. The method NEVER raises
on ordinary pipeline failures (FETCH error, gate rejection, TOCTOU
staleness, `LIVE_TRADING=false`, broker `ERROR`). It raises only on
programmer error (e.g., constructing an invalid `CoordinatorConfig`).

**Error semantics:**

| Stage | Failure | `risk_violations` |
|---|---|---|
| FETCH | exception | `("FETCH_ERROR",)` |
| VALIDATE | HIGH/CRITICAL | `("VALIDATE_CRITICAL",)` |
| VALIDATE | exception | `("VALIDATE_EXCEPTION",)` |
| RISK | exception | `("RISK_EXCEPTION",)` |
| RISK | violation list non-empty | `tuple(violations)` from RiskGate |
| TOCTOU | elapsed > `toctou_max_seconds` | `("TOCTOU_STATE_STALE",)` |
| EXECUTE | `LIVE_TRADING=false` | (none; `broker_status = "REJECTED_LIVE_TRADING_FALSE"`) |
| EXECUTE | `LIVE_TRADING=true` + risk rejected | (none; `broker_status = "REJECTED_RISK_GATE"`) |
| EXECUTE | broker error | (none; `broker_status = "ERROR:<Exc>"`) |
| EXECUTE | success | `broker_status` = `"FILLED"` \| `"NEW"` \| `"PARTIALLY_FILLED"` \| `"REJECTED_BY_EXCHANGE"` |

### 1.5 `PipelineResult` (dataclass, frozen)

**Location:** `src/coordinator.py:157`

```python
@dataclass(frozen=True)
class PipelineResult:
    config: CoordinatorConfig
    stages_completed: tuple[PipelineStage, ...]
    bars_loaded: int
    risk_allowed: bool
    risk_violations: tuple[str, ...]
    broker_status: str | None           # None if blocked before broker
    audit_log_id: int | None
    timestamp: datetime                 # auto: datetime.now(timezone.utc)
```

**Properties:**

- `result.decided: bool` — `True` iff the pipeline produced a real
  broker response (not a local refusal, not an alphard-internal
  ERROR). See `src/coordinator.py:170`.
- `result.to_dict() -> dict[str, Any]` — JSON-friendly serialisation.

**Decision rule:**

```
decided = (
    broker_status is not None
    and not broker_status.startswith("ERROR")
    and broker_status not in {"REJECTED_LIVE_TRADING_FALSE", "REJECTED_RISK_GATE"}
)
```

---

## 2. RiskGate contract

The RiskGate is the only component that decides whether a trade is
allowed. It is pure (no I/O, no clock reads, no randomness, no logging)
and deterministic.

**Location:** `src/risk/gate.py`

### 2.1 Pydantic models (all `frozen=True`)

| Model | Purpose | Key invariants |
|---|---|---|
| `TradeIntent` | the proposed trade | `quantity >= 0`, `price > 0`, `side in {"buy","sell"}`, `symbol` non-empty after strip+upper |
| `Position` | open position in portfolio | `quantity >= 0`, `avg_price > 0`, `symbol` non-empty after strip+upper |
| `PortfolioState` | snapshot of portfolio | `peak_equity >= total_equity > 0`, `cash >= 0` |
| `RiskLimits` | gate thresholds | `0 < max_dd_pct <= 100`, all percentages in % units |
| `RiskDecision` | gate output | `allowed=True` implies `violations == ()` |

All five are `ConfigDict(extra="forbid", frozen=True)`. Post-construction
mutation raises `FrozenInstanceError` (defence-in-depth, issues #98,
#238, #240).

### 2.2 `RiskGate` (public class)

```python
class RiskGate:
    def __init__(self, limits: RiskLimits) -> None: ...
    def evaluate(self, intent: TradeIntent, state: PortfolioState) -> RiskDecision: ...
```

**`RiskGate.evaluate`** runs four checks in order:

1. **`_check_position_size`** — single-position notional ≤
   `max_position_pct * equity`. Sentinel guard: rejects
   `price=Decimal('1') AND quantity > 1` as
   `RISK_MARKET_ORDER_NO_QUOTE` (historical bug, issues #11/#13).
2. **`_check_sector_exposure`** — sector-level aggregate ≤
   `max_sector_pct * equity`.
3. **`_check_daily_loss`** — kill-switch if `daily_pnl` <
   `-max_daily_loss_pct * equity`.
4. **`_check_drawdown`** — kill-switch if drawdown from peak exceeds
   `max_dd_pct`.

**`RiskDecision` semantics:**

- `allowed=True` ⇔ `violations == ()`. Enforced by the
  `_allowed_implies_no_violations` model validator
  (`src/risk/gate.py:238`).
- `violations` is a tuple of stable string codes (e.g.,
  `RISK_POSITION`, `RISK_SECTOR`, `RISK_DAILY_LOSS`,
  `RISK_DRAWDOWN`, `RISK_MARKET_ORDER_NO_QUOTE`). Codes are
  contract-stable across versions.

---

## 3. Agent interface (planned)

A formal Agent base class is **planned for Phase 2.10**. Until then,
the integration pattern is duck-type:

```python
class MyAgent:
    def fetch_signals(self, bars: list[Bar]) -> list[Signal]:
        """Return 0..N signals from the latest bars."""
        ...

    # Coordinators that consume this agent call fetch_signals() in their
    # event loop and translate Signal -> TradeIntent -> CoordinatorConfig.
```

**Constraints** (until Phase 2.10 ships):

- Agents MUST NOT call `Coordinator._execute()` directly.
- Agents MUST NOT bypass `RiskGate.evaluate()`.
- Agents MUST NOT write to `audit_log` directly; route through
  `Coordinator._audit()`.
- Agents SHOULD emit counters via `_metrics_registry` (see `src/main.py`);
  primary reader is `alphard-web` (PR #394) on `.107:8081` post-PR #399.
  _(Prometheus scraper removed, PR #399.)_

---

## 4. Environment contract

The following environment variables are part of the public contract.
Any variable not listed here may change without notice.

### 4.1 Required

| Var | Type | Purpose | Failure mode |
|---|---|---|---|
| `ALPHARD_PG_DSN` | `str` | Postgres DSN, `postgresql://user:***@host:5432/db` | unset → most loops no-op (with logged warning) |
| _Removed after PR #426:_ `ALPHARD_REDIS_URL` | `str` | Redis URL | unset → token bucket falls back to in-process |

### 4.2 Safety locks

| Var | Type | Default | Effect |
|---|---|---|---|
| `LIVE_TRADING` | `bool` (`"true"`/`"false"`) | `false` | hardlock: if `false`, Coordinator short-circuits EXECUTE with `REJECTED_LIVE_TRADING_FALSE` and never touches Tinkoff |

### 4.3 Tinkoff

| Var | Purpose |
|---|---|
| `TINKOFF_TOKEN` | Tinkoff Invest API token (sandbox or production) |
| `TINKOFF_ACCOUNT_ID` | Account ID for portfolio queries |
| `TINKOFF_SANDBOX` | `"true"` for sandbox-only operations |

Tokens are loaded by `entrypoint.sh` from the first-existing source
(`$ENV_FILE` → `/run/secrets/alphard.env` → `/root/.env` →
`/tmp/alphard.env`). See [`ARCHITECTURE.md` §7.4](ARCHITECTURE.md).

### 4.4 Optional knobs

| Var | Default | Purpose |
|---|---|---|
| `UNIVERSE_METRICS_REFRESH_SECONDS` | 300 | `_universe_metrics_loop` cadence |
| `DAILY_SYNC_INTERVAL_SECONDS` | 1800 | `_daily_sync_loop` cadence |
| `BACKFILL_MAX_DEATHS_PER_HOUR` | 10 | supervisor restart budget |

---

## 5. AuditLog contract

**Location:** Postgres table `audit_log`, written by
`src/coordinator.py:544` `Coordinator._audit()`.

### 5.1 Schema

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PK | |
| `ticker` | `text` | normalised to UPPERCASE |
| `side` | `text` | `"buy"` / `"sell"` |
| `quantity` | `numeric` | string-encoded Decimal |
| `limit_price` | `numeric` | string-encoded Decimal |
| `stages_completed` | `text[]` | PipelineStage enum values |
| `bars_loaded` | `integer` | from FETCH stage |
| `risk_allowed` | `boolean` | from RISK stage |
| `risk_violations` | `text[]` | violation codes (may be empty) |
| `broker_status` | `text` nullable | `"FILLED"` \| `"NEW"` \| ... \| `NULL` |
| `decided` | `boolean` | derived: real broker response? |
| `created_at` | `timestamptz` | server-side default `now()` |

### 5.2 When rows are written

One row per `Coordinator.run_once()` call, **always**. Even if every
stage failed, the audit row exists. This is the fail-safe invariant:
if a pipeline ran, it was recorded.

### 5.3 Who reads it

- `replay_sizing.py` — for position-size backtesting.
- _(Grafana panels were planned readers, removed, PR #399.)_ The
  active reader is `alphard-web` (PR #394): it pulls `decided=true`
  counts and `risk_violations` histograms directly from the
  Postgres-resident state.
- Incident-response scripts in `docs/RUNBOOK.md`.

---

## 6. Versioning policy

Alphard is pre-1.0 (version `0.1.0` in `pyproject.toml`). Until 1.0:

- **Major bump** (`0.X.0`): any breaking change to `CoordinatorConfig`
  shape, `PipelineResult` field removal, `RiskLimits` field removal,
  or removal of any public class.
- **Minor bump** (`0.X.Y`): new optional fields with defaults, new
  violation codes, new metric names.
- **Patch bump** (`0.X.Y.Z`): bug fixes, performance, internal
  refactors.

All breaking changes require a migration note in `CHANGELOG.md`
(issue #289).

---

## 7. Hello World Agent (example)

A minimal signal-emitter agent that integrates with the Coordinator:

```python
# File: my_project/hello_agent.py
from decimal import Decimal
from src.coordinator import Coordinator, CoordinatorConfig, CoordinatorSide
from src.risk.gate import RiskLimits

# 1. Define risk limits (frozen after construction).
limits = RiskLimits(
    max_dd_pct=Decimal("15.0"),
    max_position_pct=Decimal("10.0"),
    max_sector_pct=Decimal("30.0"),
    max_daily_loss_pct=Decimal("3.0"),
)

# 2. Build a CoordinatorConfig (frozen after construction).
config = CoordinatorConfig(
    ticker="SBER",                            # normalised to UPPERCASE
    side=CoordinatorSide.BUY,
    quantity=Decimal("10"),
    limit_price=Decimal("280.50"),
    risk_limits=limits,
    portfolio_equity=Decimal("1000000"),
    portfolio_cash=Decimal("500000"),
    portfolio_peak=Decimal("1000000"),
    live_trading=False,                       # SAFE DEFAULT — never set True without QA sign-off
)

# 3. Run the pipeline once.
coordinator = Coordinator(config)
result = coordinator.run_once()

# 4. Inspect the outcome.
print(result.to_dict())
# {'ticker': 'SBER', 'side': 'buy', 'quantity': '10', 'limit_price': '280.50',
#  'stages_completed': ('fetch', 'validate', 'risk', 'audit', 'done'),
#  'bars_loaded': 1234, 'risk_allowed': True, 'risk_violations': [],
#  'broker_status': 'REJECTED_LIVE_TRADING_FALSE', 'audit_log_id': 42,
#  'decided': False, 'timestamp': '2026-08-27T17:00:00+00:00'}
```

**Expected behaviour with `live_trading=False`:** the pipeline runs
all 5 stages, the broker is short-circuited with
`REJECTED_LIVE_TRADING_FALSE`, `decided=False`, and one row appears
in `audit_log` with `broker_status='REJECTED_LIVE_TRADING_FALSE'`.