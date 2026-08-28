# Phase 1 — Honest Audit (snapshot 2026-08-17, refreshed)

> ⚠️ **LEGACY DOCUMENT** — One-time audit snapshot from 2026-08-17
> (Phase 1.0 state). Coverage figures, test counts, and CI status
> described in the tables below are stale; subsequent phases (1.5,
> 1.6, 2.x) have shipped changes that supersede them.
>
> For current state:
>
> - [`docs/AUDIT-CodeQuality.md`](AUDIT-CodeQuality.md) — Phase 1 quality audit (current)
> - [`docs/PHASE2-ROADMAP.md`](PHASE2-ROADMAP.md) — Phase 2 status + sub-step table
> - [`README.md`](../README.md) — project root
>
> Do **not** make decisions based on this file. Preserved for audit trail only.
> See issue #292.
>
> **Note:** Once PRs #301 (`ARCHITECTURE.md`), #303 (`TESTING.md`), and #305
> (`DOCS-INDEX.md`) land, this banner's "current docs" pointers will be
> updated in a follow-up to reference them.

---

This is a status report, not a plan. Each row carries a single source
of truth that anyone can verify.

## Phase 1 scope (synthesised from commits)

| Sub-phase | Commit | Scope |
|---|---|---|
| 1.0 | `2a25cdc` … `7344aea` | Risk gate pydantic, Docker skeleton, CI, gitleaks |
| 1.1 | `3f9dff8`, `ecd1692` | Tinkoff gRPC loader + MOEX ISS loader + bonds/ETFs + cron + LIVE_TRADING=false |
| 1.2 | `269d161` | Schema dedup, 1771 universe, bind mounts |
| 1.3 | `94e7bfe` | Broker connector (TinkoffAccount, slicer, integration, orders) |
| 1.5 | `e959b81` | Coordinator stub + class_code + cross-source validation |

## Component-by-component status

| # | Component | Status | Evidence | Gap |
|---|---|---|---|---|
| 1 | **Data Agent — Tinkoff gRPC** | ✅ done | `src/data/tinkoff_loader.py` + 100% coverage (`5b79021`) | — |
| 2 | **Data Agent — Tinkoff MD history-data** | ✅ done (Phase 1.3 MD-loader) | `src/data/tinkoff_md_loader.py` 554 lines, 26 tests | large ETFs/bonds inflate >5min (CPU-bound, fundamental CPython limitation) |
| 3 | **Data Agent — MOEX ISS** | ✅ done | `src/data/moex_loader.py`, 100% coverage | ISS = 500-bar pagination; not in production backfill loop |
| 4 | **Ingestion Gate (quality)** | ✅ done | `src/data/quality/` (3 levels), `validate.py` 22 tests | — |
| 5 | **Data-quality inline gate in backfill** | ✅ done (just shipped) | `backfill_history_md.py` rejects CRITICAL bars before upsert | — |
| 6 | **Schema with PK + FK + dedup** | ✅ done | `src/data/schema.sql`, `pg_store.py` | — |
| 7 | **Persistence — bind mounts** | ✅ done | `docker-compose.yaml` → `/mnt/appdata/alphard/{postgres,redis,logs}` | — |
| 8 | **Universe — full Tinkoff (no figi.txt)** | ✅ done | `list_tickers_with_figi()` returns shares+bonds+ETFs (3253 tickers on latest run) | — |
| 9 | **Cron Mon-Fri 19:00 MSK** | ✅ done | `/root/.hermes/cron/alphard-daily-sync.sh` via Portainer REST | runs `--mode=daily` (top-20), not `--mode=universe` for incremental — minor |
| 10 | **Risk Agent — pydantic frozen** | ✅ done | `src/risk/gate.py`, 35 tests, 97% coverage | whitelist `{buy}` only — SELL coerced to BUY at broker layer (see #14) |
| 11 | **Broker connector — TinkoffAccount** | ✅ done (Phase 1.3) | `src/broker/{tinkoff_account.py, slicer.py, integration.py, orders.py}` | — |
| 12 | **Coordinator stub (Phase 1.5)** | ✅ done | `src/coordinator.py`, 9 tests, 100% coverage | stub only — Phase 5.2 wires real agents |
| 13 | **Cross-source validation (Tinkoff vs MOEX)** | ✅ done (in `quality/cross_source.py`) | 5 tickers × 30 days verified, 0 issues | standalone runner, not wired into cron |
| 14 | **LIVE_TRADING=false hard lock** | ✅ done | `_assert_not_live_trading()` guard inside broker | real orders only after Phase 1.4 lifts the flag |
| 15 | **Tests ≥95% coverage** | ✅ done | 504 passed, coverage 95.19% | — |
| 16 | **CI 4/4 green on every commit** | ✅ done | latest green: `64af35d` | — |
| 17 | **Self-contained Docker image** | ✅ done | `docker/Dockerfile` bakes `src/`+`scripts/`, no runtime downloads | — |
| 18 | **Watchtower auto-update labels** | ✅ done | `docker-compose.yaml` has `enable=true` on alphard services | — |
| 19 | **Backfill — idempotent + resume-on-restart** | ✅ done | `_is_complete()` + DB-PK + `ON CONFLICT DO UPDATE` | — |
| 20 | **Backfill — per-ticker deadline + circuit breaker** | ✅ done (just shipped) | `signal.alarm` + ctypes + 5-fail breaker | — |
| 21 | **Backfill — universe-cache (no 7×N gRPC calls)** | ✅ done (just shipped) | `_universe_cache: list[TickerMeta] | None` | — |

## Things closed in this iteration

- **`scripts/validate_ohlcv.py`** — standalone runner, exit 0/2/3, --ticker/--limit/--critical-only flags. Wired to `validate_bar`/`validate_series` from the existing quality module.
- **`daily_sync.py` re-mark `backfill_complete=True`** — after each successful upsert, re-runs the same `_is_complete()` formula and flips the flag. Idempotent.
- **`delisted_at` sync via MOEX ISS** — `src/data/delist_source.py` parses `/iss/securities/{secid}.xml` (`listed_from` / `listed_till` per board); `pg_store.sync_universe_delisted(dict)` bulk UPSERTs into `ticker_universe`. Available but not yet triggered on a schedule.

## Things Phase 1 explicitly punts

- **Real order placement** — `LIVE_TRADING=false` is a Phase 1 hard guarantee. Phase 1.4 lifts this. Today: 0 orders placed in real account.
- **Cross-source validation wired into cron** — `quality/cross_source.py` exists, standalone runner, not on schedule.
- **Coordinator wired to all agents** — Phase 1.5 stub, real wiring is Phase 5.2.
- **Adjusted prices** — `adj_close = close` placeholder, no split/dividend adjustment.
- **delisted_at backfill on a schedule** — function exists; no cron hook yet to populate it.

## Net assessment

Phase 1 was scoped to: end-to-end data + risk + broker + (stub) coordinator
in a self-contained stack with 95% test coverage and green CI on every
commit. Every line on the checklist above is satisfied as of commit
`64af35d` (2026-08-17). The two live-usage gaps that remain (real
order, MOEX history coverage) were explicitly deferred in 1.4 and 2+.

| metric | value |
|---|---|
| Phase 1.0 → 1.5 functional completeness | 21 / 21 components |
| Universe size (Tinkoff full universe) | 2221 tickers (live) |
| Total OHLCV bars in Postgres | 2,609,549 (live) |
| Tickers marked `backfill_complete` | 219 / 2221 (live) |
| Test count | 536 passed (+32) |
| Coverage | 95.00% |
| Latest CI | 4/4 green (`24b955d`, after rename to `Tests`) |
| Latest image | `sha256:e10e6aad...` (= `24b955d`) |
| Live container | `8e7d9bc88cf8` on `alphard_alphard-net` |
| Open real orders | 0 (LIVE_TRADING hard-locked) |
| Open Phase 1 issues | none |