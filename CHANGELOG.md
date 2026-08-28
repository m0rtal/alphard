# Changelog

All notable changes to **Alphard** are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(`pyproject.toml` `version` is the source of truth for the current release).

> **Source of truth.** This file is curated by hand from `git log` (which is
> the authoritative source for SHAs) and cross-referenced with
> [`docs/PHASE2-ROADMAP.md`](docs/PHASE2-ROADMAP.md),
> [`docs/decisions/`](docs/decisions/) (ADRs), and the per-PR descriptions.
> Auto-extraction is left to a future release-notes tool (see
> [Issue #289](https://github.com/m0rtal/alphard/issues/289)).

## Versioning

| Phase | `version` | Released | Highlights |
|---|---|---|---|
| Phase 0 | `0.0.x` | 2026-08-14 | Bootstrap, compose stack, env wiring. Pre-coordinator. |
| Phase 1.0–1.6 | `0.1.0` | 2026-08-26 | 8-agent Coordinator pipeline, Tinkoff broker sandbox, fail-safe RiskGate, observability stack. **Current.** |
| Phase 2.x | `0.2.0` | TBD | Macro Agent, multi-source schema, metrics, sizing matrix, audit, delisted cron, split-adjust, sandbox→LIVE gate. See [`PHASE2-ROADMAP.md`](docs/PHASE2-ROADMAP.md). |

## [Unreleased]

> Entries below are landed on `main` since `0.1.0` but not yet tagged.
> Open PRs not yet on `main` are tracked on the [Kanban board](#) and
> will be added here once merged — do not list unmerged work.

### Added
- **`ohlcv_daily` row accumulation gauge** + Grafana panel — surfaces backfill drift between Tinkoff MD archive and broker gRPC. (PR #294, #290)
- **Universe coverage gauges** (`alphard_tickers_in_universe_total`, `alphard_tickers_with_full_history_total`, Phase 2.8 step 2). (PR #243)

### Fixed
- **`_log_returns` leading-bad bar emits NaN gap** instead of silently-poisoning downstream `cross_source` validation. (Closes #278, #280 — issue first identified in #271 follow-up.)
- **`cross_source` NaN-poison silent-pass on data-glitched bar** — fail-loud instead of fail-silent. (Closes #271, #274.)
- **`backfill_history_md.py` — drop `--on-empty-only`** (user rejected; conflates row count with MD archive completion). Per-ticker guard remains `_is_complete()` (expected-bars formula for full listed_at..today range, 15% HALTS_PCT slack). After backfill, `daily_sync` is broker-only. (Closes #276, #282 — issue #277 reverted.)
- **Audit log (`PostgresAuditLog`) — `CREATE SCHEMA` + `close()` now surfaces commit error**; tests verify commit actually persists. (Closes #265, #266, #267, #273.)
- **`prometheus.yml` bind-mount from repo** instead of `PROM_YML_B64` env var — Portainer's 60-char Go JSON unmarshal limit silently truncated the base64 payload, leaving Prometheus with no scrape targets. (Closes #283, #284.)
- **`daily_sync --min-bars` help text** clarified — was misleading operators on broker-only daily-sync contract. (Closes #279, #281.)
- **`log_returns` / `_zscore_threshold_filter` docstrings** corrected to reflect real pre-baseline vs post-baseline state machine. (Closes #272, #275.)
- **`CoordinatorConfig.__post_init__` normalises ticker to UPPERCASE** — symmetric with `AdvProvider.__call__` and `_ticker_to_figi` to eliminate case-mismatch class of bugs. (Closes #238, #239.)
- **`Position.symbol` normalised to UPPERCASE** at construction. (Closes #240, #241.)
- **`_universe_metrics_loop` routed through `connect_with_timeouts`** — was using bare `psycopg.connect`, defeating the connect_timeout guard. (Closes #244, #247.)
- **`psycopg connect_timeout+statement_timeout` guards** extended to every consumer (Coordinator, audit, metrics, sizing, daily_sync). (Closes #232, #233.)
- **`OrderFlow` — wire real ADV provider**; previous placeholder left `OrderSlicer` as dead code. (Closes #230, #231.)
- **ADV computed from `bar.volume`** instead of `bar.high - bar.low` (the latter was an obvious unit-confusion bug). (Closes #225, #227.)
- **`backfill_with_dedup` ticker normalisation** in SELECT + filter. (Closes #224, #226.)
- **`sizing_audit_log` — atomic write + truncate-tolerant replay** with `.bak` mirror. (Closes #222, #223.)
- **`daily-P&L basis` — atomic write + `.bak` mirror**. (Closes #214, #215.)
- **`daily-P&L basis` — fail-closed on stale/corrupt rollover** instead of silently trading with stale peak. (Closes #207, #208.)
- **`peak-equity` — atomic write + `.bak` fallback**. (Closes #199, #203.)
- **`_risk_check` — pre-validate `limit_price > 0`** before passing to broker. (Closes #211, #212.)
- **Sector exposure marked at `intent.price`**; sector-aware `trim_qty`; old non-economic clamp dropped. (Closes #204, #205.)
- **`tinkoff_md._fill_universe_cache` raises `LoaderError` on total broker gRPC auth outage** instead of silently returning `[]`. `FallbackDataLoader.list_tickers` now records `stats["tinkoff_md"]["error"] = 1` (truthful: source raised) instead of `["fallback"] = 1` (misleading: source legitimately has no data), so operator-facing dashboards surface broker gRPC outages as actionable signals rather than silently degrading to MOEX ISS (TQBR-only). Partial-failure resilience preserved — only ALL sub-calls failing triggers the guard. (Closes #319, #321.)

### Changed
- **Compose refactor 2.0** — Grafana env provisioning, `chown -R nobody:nobody` (Alpine uses `nobody`, not `nogroup`), bind-mount elimination for appdata. (`51a3c2c`, kanban `t_884fec4a`.)
- **First-shot-friendly quickstart** — `scripts/quickstart.sh` now produces a working stack on a fresh LXC in one command. (PR #246.)
- **`ALPHARD_PEAK_STORE_DIR` isolated per test session** — fixture-scoped tmpdir prevents cross-test contamination. (Closes #220, #221.)

### Tests
- **Defensive-branch coverage c1–c6** — `sqlite_store` (85→100%, #260), `tinkoff_md_loader` (91→99%, #261), `ingestion_gate` (91→100%, #268), `fallback_loader` (92→100%, #269), `historical` (93→100%, #270). 5 PRs, ~30 new test cases.
- **`test_check_md_links`** — CI gate that walks every tracked `*.md` under the repo and asserts each markdown link whose target ends in `.md` resolves on disk. Closes the same defect class as the hand-fixed #307: a markdown link to a file supplied only by a sibling PR is invisible to CI until the sibling merges, leaving `main` briefly carrying broken links. Sanity test catches vacuous-pass parametrization bugs. (Closes #320, #322.)

### Maintenance
- **Dead code dropped**: `deploy_monitoring.sh` + its tests (ADR-0008), dead `argparse` state in `bake_grafana_env` + dead doc link. (Closes #229.)
- **`.gitignore` extended** — ignore stray nested clones (e.g. `git clone . alphard/`) to prevent recursive repo corruption. (`b0d71a4`, #242.)

### Documentation
- **`CHANGELOG.md`** (this file) — aggregated release view reconstructed from `git log` + per-PR descriptions. Closes #289.

> **Note.** Legacy-banner work on `docs/AUDIT-Phase0.md`,
> `docs/AUDIT-Phase0-FINAL.md`, `docs/PHASE1-AUDIT-2026-08-17.md`,
> `docs/PHASE1-6-SERVICE-DIAGRAM.md` is owned by PR #306 (closes #292)
> — kept out of this PR to avoid branch-hygiene overlap. Likewise
> `ARCHITECTURE.md` (#285), `API.md` (#286), `TESTING.md` (#287),
> `TROUBLESHOOTING.md` (#288), `DOCS-INDEX.md` / `evidence/README.md`
> (#291, #293), Grafana provisioning bind-mount (#297), and the
> `entrypoint.sh` `/root/.env` source loop (#295) are in flight in
> their own PRs — they will be added here once those PRs are merged.

## [0.1.0] — 2026-08-26

> First tagged release. Captures everything shipped from Phase 0 bootstrap through Phase 1.6 daily-sync + in-process watchdog. Reconstructed from `git log`; PR numbers are best-effort (commit-history archaeology).

### Added
- **Coordinator** — 8-agent pipeline (`validate → ingest → signal → risk → size → execute → record → audit`) with `PipelineResult` dataclass return contract.
- **`CoordinatorConfig`** — `fetch_lookback_days`, `min_history_bars`, `live_trading` (sandbox/live gate), `risk_overrides` dict.
- **Tinkoff broker integration** (`src/broker/tinkoff_account.py`) — sandbox + live modes, real NAV, real quote, FIGI resolution from ticker.
- **`RiskGate`** (`src/risk/gate.py`) — 5 fail-safe limits: max position size, max sector exposure, max daily loss, drawdown trip, min liquidity (ADV).
- **Daily-sync orchestrator** (`scripts/daily_sync.py`) — broker-first + MD-archive fallback chain (zip → broker gRPC → MOEX ISS).
- **In-process watchdog** — `alphard_heartbeat_last_tick_timestamp` Prometheus gauge; supervisor restarts on stall.
- **Postgres audit log** (`src/data/quality/audit.py`) — every intent, every order, every fill, every risk veto.
- **Tinkoff MD archive loader** (`src/data/tinkoff_md_loader.py`) — historical OHLCV backfill from Tinkoff's `marketdata` protobufs.
- **MOEX ISS loader** (`src/data/moex_loader.py`) — public-website fallback for tickers Tinkoff archive misses.
- **`cross_source` validator** — compares `tinkoff_md` vs `tinkoff_account` vs `moex_iss` bar-by-bar; flags `> 0.1%` divergence as DATA_GLITCH.
- **Docker Compose stack** — `alphard-bot`, `alphard-postgres`, `alphard-prometheus`, `alphard-grafana`, all wired through `.env` + `docker/entrypoint.sh`.
- **CI pipeline** — `Tests + Coverage` (≥95% gate enforced by `--cov-fail-under=95` in `[tool.pytest.ini_options] addopts`), `Lint + Format` (black + flake8), `SCA (pip-audit)`, `Secrets scan (gitleaks)`, `Grafana secrets guard`, `Ops policy`, `Build + push`.
- **`pyproject.toml`** — Poetry-managed; `python = "^3.11"`.

### Fixed
- **TOCTOU race in Coordinator** — pre-validate `limit_price > 0` and normalise ticker before any broker call.
- **Fail-safe on VALIDATE/RISK exception** — Coordinator catches and converts to `PipelineResult(status=ERROR)` instead of crashing the loop.
- **Sandbox-token redeploy** — `LIVE_TRADING=false` is the default; `true` requires explicit `.env` opt-in.

### Tests
- **Phase 1.1 — Risk** — 35 tests, 97% coverage, all 5 fail-safe limits covered.
- **Phase 1.2 — Quality** — 3 severity tiers (`critical`, `high`, `medium`) verified by regression suite.

### Documentation
- **`README.md`** — quickstart + 8-agent architecture summary.
- **`CONTRIBUTING.md`** — dev setup, lint, test.
- **`docs/SECURITY.md`** — threat model, secrets handling, sandbox/live gate.
- **`docs/RUNBOOK.md`** — start/stop/monitor procedures.
- **`docs/PHASE1-6-SERVICE-DIAGRAM.md`** — Phase 1.6 architecture (now `[LEGACY]`, see PR #306).
- **`docs/AUDIT-Phase0.md` + `docs/AUDIT-Phase0-FINAL.md`** — Phase 0 bootstrap audit (now `[LEGACY]`, see PR #306).
- **`docs/AUDIT-CodeQuality.md`** — Phase 1 quality audit.
- **`docs/decisions/`** — ADR pattern (`0001-postgres.md`, etc.).

## [0.0.x] — 2026-08-14 (Phase 0)

> Bootstrap release. No changelog entries preserved from this period; reconstructed from `git log` archaeology where possible. Pre-coordinator.

### Added
- Repo skeleton, `src/` layout, `.env.example`, `requirements.txt`.
- Initial `docker-compose.yaml` with `alphard-bot` + `alphard-postgres`.
- `docker/entrypoint.sh` — Postgres wait-loop + `.env` source.
- `tests/` skeleton (pytest, no real coverage gate yet).
- `LICENSE` (Apache-2.0), initial `README.md`.

---

## How to add a new entry

1. Open the PR for your fix/feat.
2. After merge, add an entry to the `[Unreleased]` section above, grouped
   under `Added` / `Fixed` / `Changed` / `Tests` / `Maintenance` / `Documentation`.
3. Reference the PR number and (if applicable) the closing issue.
4. **Only list PRs that have actually merged.** Open PRs go in the Kanban
   board, not here, until they land.
5. On release: cut a new `## [X.Y.Z] - YYYY-MM-DD` section, move the
   `[Unreleased]` items under it, bump `pyproject.toml` `version`, tag the
   commit (`git tag -s vX.Y.Z -m 'release X.Y.Z'`).

A future release-notes extraction tool (per issue #289 acceptance criteria)
will auto-extract entries from `git log --grep '^fix\|^feat' vX.Y.Z..HEAD`
so this hand-curation step is the single source of truth only until then.
The script does not yet exist — it is the planned future automation.

---

## Path-existence guard (CI)

To prevent the regression filed in [Issue #310](https://github.com/m0rtal/alphard/issues/310)
(`CHANGELOG.md` claiming source paths that do not exist), the CI pipeline
runs the following check against every change to this file:

```bash
grep -oE '`(src|scripts|docs|tools)/[A-Za-z0-9_./-]+`' CHANGELOG.md \
  | tr -d '`' | sort -u | while read -r p; do
      [ -e "$p" ] || { echo "CHANGELOG references missing path: $p"; exit 1; }
  done
```

(The exact tool path is intentionally left un-quoted in this guard
description — see the actual shell command above.)

A future improvement (tracked under issue #289 acceptance criteria) is to
move this guard into the planned release-notes tool and run it on every
release.

---

## See also

- [`README.md`](README.md) — quickstart, high-level overview.
- [`docs/PHASE2-ROADMAP.md`](docs/PHASE2-ROADMAP.md) — Phase 2 sub-step status table.
- [`docs/decisions/`](docs/decisions/) — ADRs.