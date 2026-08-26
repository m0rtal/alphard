# Phase 2 Roadmap — Alphard

> Single source of truth for what Phase 1 explicitly punts and what Phase 2 plans to land.
> Synthesized from README.md, docs/PHASE1-AUDIT-2026-08-17.md, docs/SECURITY.md, docs/RUNBOOK.md.
> **Last updated:** 2026-08-26 (Phase 2.8 step 2: universe coverage gauges).

## Phase 2 — status table

Live PR-by-PR tracking for Phase 2 deliverables (sub-step granularity).
Each row carries the exact metric names + the PR that landed it so an
operator can correlate a Grafana panel with the code commit that emits it.

| Sub-step | Deliverable | Status | PR |
|---|---|---|---|
| 2.8 step 1 | Prometheus metrics HTTP server (`alphard_uptime_seconds`, `alphard_heartbeat_last_tick_timestamp`, `alphard_heartbeats_total`) + heartbeat dashboard | ✅ MERGEABLE | PR #TBD |
| 2.8 step 2 | Universe coverage gauges (`alphard_tickers_in_universe_total`, `alphard_tickers_with_full_history_total`) | ✅ MERGEABLE | PR #TBD |
| 2.7 step 1 | Weekly delisted_at cron (`_delisted_sync_loop`) | ✅ merged | (see history) |
| 2.6 step 1 | Cross-source validation cron + schema | ✅ merged | (see history) |
| 2.5 step 2b | `apply_corporate_actions` weekly orchestrator | ✅ merged | (see history) |
| 2.5 step 2c | Dividend handling in `apply_adjustment` | ✅ merged | (see history) |
| 2.3 step 1 | Macro Agent (CBR + USD/RUB + IMOEX regime) | ✅ merged | (see history) |
| 2.2 step 1 | Position Sizing Matrix (`src/broker/sizing.py`) | ✅ merged | (see history) |
| 2.1 step 1 | Sandbox-token redeploy / LIVE_TRADING gate | ⏳ in flight | (see kanban) |

## Status

| Phase | Scope | Status |
|---|---|---|
| 1.0 | Bootstrap (compose, env, schema) | ✅ done |
| 1.1 | Risk (35 tests, 97% coverage, 5 fail-safe limits) | ✅ done |
| 1.2 | Quality (3 severity tiers) | ✅ done |
| 1.3 | Data + Broker (sandbox) | ✅ done |
| 1.4 | **Real order placement (sandbox → LIVE_TRADING=true)** | ⏳ **next** |
| 1.5 | Coordinator stub | ✅ done (5/21 components, real wiring Phase 5.2) |
| 1.6 | Daily sync + in-process watchdog | ✅ done (commit `0dcf55b`, redeployed `sha-bc867a2`) |
| 2.x | (this document) | ⏳ pending |
| 3.x | (Portfolio, Prometheus, full audit) | ⏳ punted |

## Phase 1.4 — Real order placement (NEXT)

**Hard constraint:** `_assert_not_live_trading()` blocks every real-order path until `LIVE_TRADING=true`.
This gate exists by design — it is a Phase 1 guarantee, not a bug.

**Scope:**
1. Sandbox-first end-to-end test: Coordinator → Risk → Broker.place_order → audit_log row.
2. LIVE_TRADING=true for `TINKOFF_REAL_TOKEN` path; SANDBOX stays on for `TINKOFF_SANDBOX_TOKEN`.
3. Post-only / LIMIT-only orders in Phase 1.4 (no MARKET, no SHORT until Phase 2).
4. TinkoffInvestBroker already implements `place_order()` — Phase 1.4 is mostly **wiring**, not new code.

**Acceptance gate:**
- `Coordinator.place_order()` returns `broker_order_id` for a sandbox SBER LIMIT order.
- `audit_log` row with `outcome=success`, `bars_loaded > 0`, `risk_allowed=true`.
- Zero `LIVE_TRADING=true` deployments until integration test green for 7 days straight.

**ETA:** TBD — needs sandbox token + manual broker API smoke before wiring.

---

## Phase 2 — Agent activation + automated ops

### 2.1 — Quant Agent (ML pipeline)

**What:** Train ML models on the OHLCV history (currently 2.6M bars) to produce daily forecasts.
**Inputs:** `ohlcv_daily` per ticker, macro features (Phase 2.2).
**Outputs:** `signal=long|short|flat` + confidence in [0,1].
**Stack:** scikit-learn baseline → XGBoost → optional LSTM (Phase 2.5).
**Why Phase 2:** Requires 95% complete backfill (we're at 31/3257 today) + walk-forward validation harness.

### 2.2 — Macro Agent

**What:** Daily CBR rate, USD/RUB, IMOEX regime detection → modifies Coordinator risk budget.

**Inputs (all MOEX ISS, no paid vendor):**
- CBR key rate — `/engines/currency/markets/selt/...` (1×/день после заседания ЦБ)
- USD/RUB — `CETS` секция, daily candles
- IMOEX index — `/iss/engines/stock/markets/shares/indices/MOEX.csv`

**NOT in Phase 2 Macro scope** (deferred Phase 3+):
- Brent/oil — needs paid vendor (Argus, Platts); outside MOEX ISS
- CPI / GDP / PMI — quarterly/monthly with 1-2 month lag; useless for daily regime
- Anything minute-resolution — Macro is daily, not intraday (matches user's "daily+ only" preference)

**Regime classifier (deterministic, not ML):**
- `risk_off` if CBR > 15% OR IMOEX drawdown > 20% over 60d
- `risk_on_reduced` if USD/RUB Δ > 5% over 5d
- else `neutral`

**Output:** regime label + risk budget multiplier [0.5, 1.0] passed to Coordinator stage 0.

**Why Phase 2:** Single-feature regime is a clean unit test; multi-factor requires 6+ months of history.

### 2.3 — Macro Agent (CBR + USD/RUB + IMOEX regime classifier) — issue #70

**Status:** ✅ implemented (branch `fix/issue-70-macro-agent`).

**Goal:** Pull three macro inputs, compute a deterministic regime label +
risk-budget multiplier in [0.5, 1.0], persist to Postgres. Coordinator
(Phase 2.10) consumes the output.

**Inputs (no paid vendor):**
- CBR key rate — `cbr-xml-daily.ru` XML.
- USD/RUB CETS — `iss.moex.com/.../USD000000TOD/candles.csv`.
- IMOEX index — `iss.moex.com/.../MOEX.csv`.

**Regime classifier (pure Decimal math):**
- `risk_off`         if CBR > 15% OR IMOEX drawdown > 20% over 60d
- `risk_on_reduced`  if USD/RUB Δ > 5% over 5d
- `neutral`          otherwise

Multipliers: `risk_off` → 0.50, `risk_on_reduced` → 0.75, `neutral` → 1.00.

**Modules:**
- `src/macro/regime.py::classify()` — pure function, no IO. 4 branches + 4 edges covered by `tests/test_regime.py`.
- `src/macro/persistence.py` — store-agnostic upsert (`upsert_regime`, `latest_regime`). Tested against `InMemorySQLiteStore`.
- `src/data/macro_fetcher.py::build_snapshot()` — composes three fetchers with retry+backoff+cache; atomic `.tmp+rename` writes.
- `scripts/run_macro_sync.py` — CLI entrypoint with 1h skip-window gate; subprocess-isolated.
- `src/main.py::_macro_sync_loop` — hourly daemon thread, 5-min startup delay, 1h cadence, 5-min subprocess timeout.

**Schema:** `macro_regime_log` (one row per fetch, keyed by `fetched_at`). Mirrored in `InMemorySQLiteStore` for tests.

**Hard constraints honoured:**
- No scheduler changes to `_daily_sync_loop` / `_delisted_sync_loop` (separate daemon thread).
- `src/coordinator/*` untouched (Phase 2.10 scope).
- `models.py OHLCVRow` untouched (regime uses its own `MacroSnapshot`).
- Cache writes atomic via `.tmp + os.replace`.
- `regime.classify()` is PURE — no IO, no DB.

### 2.4 — Coordinator wired to all agents (Phase 5.2 in audit)

**What:** Coordinator calls Quant + Macro before Risk, not just Risk stub.
**PR #17 (commit `e406488`):** Added fail-safe on VALIDATE/RISK exception + TOCTOU guard.
**Next:** Wire Quant + Macro into the `stages` list. Coordinator `stage 0 = MACRO`, `stage 1 = QUANT`, `stage 2 = VALIDATE`, `stage 3 = RISK`, `stage 4 = EXECUTE`, `stage 5 = AUDIT`.

### 2.4 — Adjusted prices pipeline (Phase 2.5 step 2b/2c)

**What:** Phase 2.5 ships the full split+dividend adjustment pipeline:
splits + dividends + adjusted OHLCV bars land in Postgres, ready for
backtests.

**Steps:**
- **Step 1 — pure math (PR #45, merged 2026-08-20):**
  `src/data/adjustment.py::apply_split_adjustment(rows, actions)`.
  22 unit tests, 91% coverage, no IO. Scales prices by 1/R for
  pre-split bars; volume by R. Multiple splits compose multiplicatively.
- **Step 2a — fetcher (PR #54, merged 2026-08-20):**
  `scripts/fetch_moex_corporate_actions.py` — MOEX ISS splits + (optional)
  dividends into a JSON snapshot. 24 tests. Standalone utility.
- **Step 2b — apply orchestrator (PR #74 / issue #74, branch
  `feat/issue-74-corp-actions-apply`):**
  `scripts/apply_corporate_actions.py` — pulls splits per ticker from
  step 2a, applies step 1 to raw OHLCV, persists to a new parallel
  table `ohlcv_daily_adj` (FK to `ticker_universe`). 25 orchestrator
  tests + 9 daemon-thread tests. Daemon runs weekly via
  `src/main.py::_corp_actions_apply_loop` (mirrors `_delisted_sync_loop`,
  first run 24h after launch, then every 7 days).
- **Step 2c — dividend handling (PR for issue #74 follow-up, branch
  `feat/issue-dividend-apply`):** extends the pure-math stage with
  `apply_dividend_adjustment` (subtracts cumulative dividends from
  `adj_close` for bars before each ex-date; raw OHLC and volume
  untouched — dividends are a return-of-capital event, not a
  price-multiplier event) and introduces the unified
  `apply_adjustment()` entry point that runs splits first, then
  dividends. The orchestrator (`scripts/apply_corporate_actions.py`)
  now fetches dividends per ticker (`_fetch_dividends_for_ticker`) and
  feeds them into the same `apply_adjustment()` call as splits — no
  schema change, same parallel `ohlcv_daily_adj` table. 30 new tests
  in `tests/test_adjustment.py` + 13 new orchestrator tests in
  `tests/test_apply_corporate_actions.py`. PHASE2-ROADMAP deferred
  dividend handling past step 2b; step 2c lands it.
- **Step 3 — migration to ohlcv_daily (planned, not yet scheduled):**
  When PR #75 lands the `source` column on `ohlcv_daily`, the parallel
  `ohlcv_daily_adj` table is migrated:
  `INSERT INTO ohlcv_daily (..., source='tkf_adj') SELECT FROM ohlcv_daily_adj`
  and the parallel table is dropped. The migration is a single DDL +
  DML; it's auditable because the parallel table preserves every
  (ticker, ts) pair with its adjusted OHLCV even if the raw feed is
  later corrected.

**Why a parallel table for step 2b (not overwriting ohlcv_daily):**
1. Re-running the apply pipeline must never silently overwrite the raw
   feed (which is the audit trail for QA reconciliation).
2. PR #75 (branch feat/issue-68-ohlcv-source-column) is approved but
   not yet in main; until it merges, `ohlcv_daily` has no `source`
   column to tag adjusted bars. The parallel table is the storage
   target that does not depend on merge order.

**Done in step 2c (branch `feat/issue-dividend-apply`):** Dividend
adjustment (`kind='dividend'`) now flows through the same orchestrator
as splits. The pure-math stage (`apply_dividend_adjustment`) subtracts
cumulative dividends from `adj_close` on pre-ex-date bars; the
orchestrator's `_fetch_dividends_for_ticker` pulls dividends per
ticker from MOEX ISS alongside splits. 43 new tests
(`test_adjustment.py` + `test_apply_corporate_actions.py`) cover the
dividend path, end-to-end composition with splits, and defensive
filters (negative amounts, malformed dates).

**Sources:** `scripts/fetch_moex_corporate_actions.py` (step 2a fetcher),
`src/data/adjustment.py` (step 1 math), `scripts/apply_corporate_actions.py`
(step 2b orchestrator), and the test contracts at
`tests/test_apply_corporate_actions.py` +
`tests/test_main_corp_actions_apply.py`.

### 2.5 — Cross-source validation cron

**What:** `scripts/cross_source_validate.py` runs nightly, compares Tinkoff MD vs Tinkoff gRPC vs MOEX ISS per ticker.
**Tolerance:** ±0.5% per bar (handles timestamp end-of-day rounding).
**Why Phase 2:** Function `quality/cross_source.py` exists; needs scheduler hook + alert escalation.

### 2.6 — delisted_at backfill schedule

**What:** Cron job calls `scripts/backfill_delisted_via_tinkoff.py --years 1` weekly.
**Today:** script exists (202 lines, gRPC 1-year chunks), `sync_universe_delisted` in pg_store.py, but no schedule.
**Adjacent:** `mark_terminally_failed.py` (sha-`9d34663`) covers the no-data heuristic; this complements it for tickers that DID trade but are delisted.

### 2.7 — Prometheus + Grafana observability

**What:** Deploy `alphard-prometheus` + `alphard-grafana` (skeleton configs already in `docker/prometheus/` + `docker/grafana/dashboards/alphard-phase0.json`).
**Metrics:** backfill bars/sec, daily_sync duration, auth_probe success rate, ohlcv_daily count, broker reconnect count.
**Why Phase 2:** Need 4 weeks of operation before metrics are stable; Phase 2 is the natural deploy window.

### 2.8 — Daily backup (Postgres OHLCV data)

**What:** `pg_dump alphard` → `/backup/alphard_YYYY-MM-DD.sql.gz` daily at 02:00 MSK.
**Retention:** 7 daily, 4 weekly, 6 monthly.
**Restore:** already in RUNBOOK.md §"Восстановление из daily backup".

**Why Phase 2 even though GitHub backs up code:**
GitHub `git clone` recovers: src/, scripts/, tests/, docs/, CI config, Dockerfiles.
GitHub does NOT recover: OHLCV data (2.6M+ bars), `.env` (secrets), Postgres volume.
The OHLCV backfill took hours of Tinkoff MD downloads; if `.107` dies, that history is gone without `pg_dump` snapshots. Daily backup covers the **data single-point-of-failure**, not the source code (which GitHub already covers).

**Deferred to Phase 3+:** Off-host backup (S3/B2) — single-host NAS `/backup/` is fine until we have multi-host or DR requirements.

### 2.9 — Self-audit cron (6h)

**What:** Every 6h, verify the 5 defense layers (security.md P0 list) are intact:
1. Risk limits frozen (`RISK_*` env vars)
2. `.env` permissions `chmod 600`
3. No secrets in git (gitleaks)
4. `alphard-bot` runs as UID 1000 (not root)
5. Container restart policy `unless-stopped`

**Why Phase 2:** First self-audit is cheap; full coverage requires Phase 2 metrics.

### 2.10 — Event-driven decision loop

**What:** Replace 1h heartbeat with event-driven triggers (broker fill, news wire, macro release).
**Stack:** Redis Streams + in-process subscribers.
**Why Phase 2:** Needs Phase 2.1-2.3 agents active — without them there's nothing to react to.

### Phase 2 — items deferred to Phase 3+ (cross-ref README "Honest gaps")

These items live in README.md "Honest gaps" but are NOT Phase 2 critical path:

| Item | README Phase | Why deferred from Phase 2 |
|---|---|---|
| Backtest framework (VectorBT) | 2/3 | Needs Phase 2.1-2.3 agents active to backtest signal quality |
| News + RAG (pgvector) | 3 | pgvector extension setup + news API vendor selection; needs user input |
| Web UI | 4 | Operator console; doesn't affect trading logic |
| ETF universe (Tinkoff doesn't return ETFs) | 2 | TBD — separate issue; Phase 2 ETF work is "best-effort" not "delivered" |
| Coordinator continuous loop (state machine) | 1.5 | Phase 5.2 in audit doc; needs full agent wiring (Phase 2.3 prerequisite) |

When Phase 2.1-2.3 land, these unblock and graduate from "deferred" to "in scope".

---

## Phase 3 — deferred

| Item | Why deferred |
|---|---|
| Portfolio Agent | Needs real-money PnL to validate allocation algorithm |
| Off-host backup (S3/B2) | Premature until Phase 2.8 proves daily backup is reliable |
| Prometheus alert escalation (PagerDuty) | Single-operator (me); Telegram alerts suffice |
| LSTM / transformer models | Walk-forward XGBoost first; deep nets only if SOTA justifies cost |

---

## Tracking

- **GitHub Project:** not yet created — Phase 2 backlog lives in this doc until 2.1 starts.
- **Kanban board:** `alphard` (Hermes) — created 2026-08-19, empty.
- **Honcho:** user peer carries durable Phase 2 conclusions (regime features chosen, walk-forward window = 6mo, etc.).

## Open questions for user

1. ✅ **Sandbox vs real-account first run** — sandbox-first (user confirmed 2026-08-19). Phase 1.4 = sandbox end-to-end → 7-day soak → flip `LIVE_TRADING=true` on real account.
2. ✅ **Macro Agent data vendor** — MOEX ISS only (user confirmed 2026-08-19). CBR + USD/RUB + IMOEX; deferred Brent/CPI/GDP.
3. ✅ **Backup target** — GitHub as code backup is sufficient (user confirmed 2026-08-19). Local `/backup/` only if Phase 2.8 ships for hot data, not source-of-truth.

---

*This document is the source of truth. Update README.md "Honest gaps" table when items complete or scope shifts.*