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
- **`scripts/daily_incremental.py`** — daily closed-bar refresh orchestrator. For each `backfill_complete=TRUE` ticker: `start = latest_db_ts + 1`, `end = today - 1`, source = `tinkoff_grpc` (fallback `moex_iss`). Never inserts today's bar (still forming); never re-fetches history already in DB. Defensive `b.ts <= end` filter guards against Tinkoff occasionally returning today's incomplete bar. Driven by cron once per day post-market-close. (PR #332.)
- **`ohlcv_daily` row accumulation gauge** + Grafana panel — surfaces backfill drift between Tinkoff MD archive and broker gRPC. (PR #294, #290)
- **Universe coverage gauges** (`alphard_tickers_in_universe_total`, `alphard_tickers_with_full_history_total`, Phase 2.8 step 2). (PR #243)

### Fixed
- **`pg-init` service dropped from `docker-compose.yaml`; schema application moved into `docker/entrypoint.sh` before the fail-closed auth probe.** Pre-#347, pg-init bind-mounted single-file SQL paths (`./docker/postgres/init.sql:/sql/docker_postgres_init.sql:ro` and `./src/data/schema.sql:/sql/src_data_schema.sql:ro`) that render as **directories** on PVE LXC hosts — the schema was never applied and `_auth_probe` was missing, so the bot's fail-closed auth probe aborted every container start and the pre-PR smoke gate could not bring up `alphard-bot`. With pg-init removed, the bot's own `init_schema()` (already idempotent — `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` throughout `src/data/schema.sql`) is the only schema source, and the entrypoint now runs it before `auth_probe()` so the probe table exists on every boot, fresh volume or preserved volume alike. `tests/test_347_pg_init_removal.py` (5 pure-fs tests) pins the post-fix contract: no `pg-init` service, no `pg-init` in `alphard-bot.depends_on`, `init_schema()` precedes `auth_probe()`, smoke gate does not bring up `pg-init`, and `_auth_probe` is still created by `src/data/schema.sql`. 1548 tests pass / 37 skip (delta −2 from removed pg-init tests; net +5 new tests). (Closes #347, PR #349.)
- **`scripts/quickstart.sh` ONE_SHOT array and `scripts/init_postgres.sh` docstring updated to drop the dropped `alphard-pg-init` reference** (cycle126 lesson). Post-#347/PR-#351 the `pg-init` service is gone from `docker-compose.yaml`, but `quickstart.sh` still listed it in `ONE_SHOT=("alphard-chownfix" "alphard-pg-init")`, so every `quickstart.sh` run emitted a false-positive `one-shot alphard-pg-init missing (compose didn't run it?)` warning. Likewise `init_postgres.sh` still described the compose `pg-init` service as the active schema path. Both now point at the post-#347 contract (`init_schema()` from `docker/entrypoint.sh` is the active schema path; `pg-init` is archaeology). Two new regression tests in `tests/test_347_pg_init_removal.py` pin both contracts. (Closes #355.)
- **`FallbackDataLoader.iter_corporate_actions` now chunks windows exceeding a source's `MAX_LOOKBACK`** — same defect class as #346 (closed by PR #348 for OHLCV) but on the corporate-actions path. Pre-fix, the outer window was passed straight to every source in the chain; `MOEXDataLoader` enforces a 1825-day cap on `iter_corporate_actions` (line 202) and any supervisor call whose broker gRPC fetch returned 0 actions fell through to `moex_iss` and aborted with `LoaderError: range ... exceeds upstream max lookback 1825d`, silently losing the synthetic delisted-event signal the backtester needs to flag a ticker as no longer tradeable. Fix mirrors PR #348's OHLCV fix: a new `_iter_source_corp_actions` helper, wired through `_source_max_lookback`, splits the request into `<= cap` sub-ranges and concatenates them; sources without a declared cap receive the full window. Partial-chunk success followed by a later-chunk raise marks the source failed (no rows yielded). Three regression tests in `tests/test_fallback_loader.py` cover: 9-year-window chunking into two sub-ranges, no-chunking-when-window-fits-cap, and partial-failure-marks-source-failed. 1544 tests pass / 37 skip (delta −1 vs cycle133: net +3 new tests). (Closes #349.)
- **`scripts/daily_incremental.py` — `_fetch_with_fallback` now routes through `FallbackDataLoader`** instead of inlining its own broker-first → MOEX chain. Pre-fix, the inline chain on line 89 called `MOEXDataLoader().iter_ohlcv(ticker, start, end)` directly with the full outer window; MOEX enforces a 1825-day cap and any longer window raised `LoaderError: range ... exceeds upstream max lookback 1825d`. For delisted tickers with stale `latest_db_ts` the window is often years long, so each daily-incremental run silently lost every such ticker's incremental update on the days broker gRPC happened to fail. The chain uses `FallbackDataLoader.iter_ohlcv` so per-source lookback-aware chunking (PR #348) applies automatically, and broker construction is wrapped in `try/except` so the documented `ALLOW_NO_BROKER=true` Phase 0 stub mode / no-token dev runs silently degrade to MOEX (`FallbackDataLoader._resolve` skips `None` sources). Four regression tests in `tests/test_daily_incremental.py` cover: 9-year-window chunking into multiple sub-ranges, chain-routing (broker `fetch_ohlcv` is not called directly), no-token broker constructor failure skips to MOEX, and MOEX not invoked on the happy path. (Closes #350, #354.)
- **`_is_complete()` rebases floor on earliest DB bar**, not on the optimistic `listed_at`. The previous floor-source was `ticker_universe.listed_at`, populated by `Instrument.list_*` falling back to `first_1min_candle_date` / `ipo_date` — those fields predate real OHLCV availability by years for SPBXM ETFs and many similar instruments, so `expected_bars` ballooned to ~5000 when only ~570 are fetchable, leaving 27/3265 (0.8%) tickers stuck in perpetually-incomplete state with 58k bars written. After-load rebase asks the right question: "have we pulled all the data the API will actually give us?" instead of "have we pulled data that doesn't exist?". (Closes #319, #332.)
- **`_log_returns` leading-bad bar emits NaN gap** instead of silently-poisoning downstream `cross_source` validation. (Closes #278, #280 — issue first identified in #271 follow-up.)
- **`backfill_history_md._resolve_universe` calls `tinkoff_md` directly** instead of routing through `FallbackDataLoader.list_tickers()`. The chain returned only the gRPC 252-TQBR subset even when `tinkoff_md` returned the full ~3264-ticker universe (TQBR + SPBXM + TQCB + TQOB) — local smoke 2026-08-28 confirmed 3264 vs 252. Treats "successful-but-empty" direct MD as a fallback signal so the chain IS consulted; raises `LoaderError` if BOTH paths return empty. `iter_ohlcv` keeps the chain (per-ticker-per-year resilience preserved). (Closes #319, #326.)
- **`test_check_md_links` strips backtick-wrapped inline code spans** before matching — CommonMark says content inside backticks is literal and must not be parsed as a link. Closes the false-positive class on docs that document link syntax (e.g. `` `[text](relative/path.md)` ``). Also handles triple-backtick / triple-tilde fenced code blocks as a free extension. (Closes #324, #325.)
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
- **Universe-discovery chain reordered** — `FallbackDataLoader.FALLBACK_ORDER = (tinkoff_grpc, moex_iss)`. The deprecated Tinkoff history-data HTTP archive (`/history-data`) is removed from the chain because it returns 0 bytes for most FIGIs older than 2y; broker gRPC is the canonical source, MOEX ISS is the public-website fallback. `backfill_history_md.py` still constructs `tinkoff_md` locally for the retry path (one-off downloads when broker gRPC is down). (PR #332.)
- **Compose refactor 2.0** — Grafana env provisioning, `chown -R nobody:nobody` (Alpine uses `nobody`, not `nogroup`), bind-mount elimination for appdata. (`51a3c2c`, kanban `t_884fec4a`.)
- **First-shot-friendly quickstart** — `scripts/quickstart.sh` now produces a working stack on a fresh LXC in one command. (PR #246.)
- **`ALPHARD_PEAK_STORE_DIR` isolated per test session** — fixture-scoped tmpdir prevents cross-test contamination. (Closes #220, #221.)

### Tests
- **`test_daily_incremental.py`** (145 LOC) — covers daily-closed-bar refresh contract: never inserts today's bar, never re-fetches existing history, gracefully handles new tickers (no DB rows yet), broker-fallback path. (PR #332.)
- **`test_pg_store_integration.py`** + **`test_pg_store_mocked.py`** (151 LOC combined) — covers `PostgresDataStore` (`ALPHARD_PG_DSN`-driven) for daily-incremental's storage layer. (PR #332.)
- **`test_fallback_loader.py`** (heavily rewritten, 358 LOC touched) — reflects the new `(tinkoff_grpc, moex_iss)` order and removal of `tinkoff_md` from the chain. (PR #332.)
- **Defensive-branch coverage c1–c6** — `sqlite_store` (85→100%, #260), `tinkoff_md_loader` (91→99%, #261), `ingestion_gate` (91→100%, #268), `fallback_loader` (92→100%, #269), `historical` (93→100%, #270). 5 PRs, ~30 new test cases.
- **`test_check_md_links`** — CI gate that walks every tracked `*.md` under the repo and asserts each markdown link whose target ends in `.md` resolves on disk. Closes the same defect class as the hand-fixed #307: a markdown link to a file supplied only by a sibling PR is invisible to CI until the sibling merges, leaving `main` briefly carrying broken links. Sanity test catches vacuous-pass parametrization bugs. (Closes #320, #322.)

### Maintenance
- **Pre-PR smoke gate** — `scripts/pre_pr_smoke.sh` + `scripts/hooks/pre-push` enforce that every branch pushed to the repo has been exercised against a real stack (compose bring-up + bind-mount of `src/` + `scripts/` + pytest + `daily_incremental --dry-run`). The pre-push hook refuses `git push` if the per-branch sentinel `/tmp/.alphard-pr-smoke-pass.<branch>` is missing or stale (>30 min). Bypass requires `ALPHARD_SKIP_SMOKE=1` set in the environment. Reason: cycle110 reproducer in PR #332 showed pure Python tests can pass while a column-order / schema bug in `daily_incremental` only surfaces against live Postgres — the smoke gate is the layer the existing test gap. (PR #332.)
- **Dead code dropped**: `deploy_monitoring.sh` + its tests (ADR-0008), dead `argparse` state in `bake_grafana_env` + dead doc link. (Closes #229.)
- **`.gitignore` extended** — ignore stray nested clones (e.g. `git clone . alphard/`) to prevent recursive repo corruption. (`b0d71a4`, #242.)
- **`CHANGELOG.md [Unreleased]` backfilled** — entries for the cycle103 PRs (#319 fix → #321, #320 fix → #322) added to keep the file in sync with `main`. Bundled with a `.gitignore` rule for the local-override `.claude/settings.local.json` (operator convenience, never committed). (PR #323.)
- **Stale "in flight" note removed** from `CHANGELOG.md` — the referenced PRs (legacy banners #306, `ARCHITECTURE.md` #301, `API.md` #302, `TESTING.md` #303, `TROUBLESHOOTING.md` #304, `DOCS-INDEX.md` + `evidence/README.md` #305, Grafana provisioning #297/#300, `entrypoint.sh` `/root/.env` loop #296) have all landed on `main`. The note no longer reflects reality.
- **`_fill_shares_all` docstring aligned with the #319 fix** — the docstring on `src/data/tinkoff_loader.py:412` previously listed three fallbacks (`first_1min_candle_date` / `ipo_date` / `first_1day_candle_date`) but the actual code only reads `first_1day_candle_date` (the other two fields predate real OHLCV availability for SPBXM-style instruments and were the root cause of #319). Added a `TestListedAtAnchor` regression test that pins the floor field to `first_1day_candle_date` so the docstring/code drift cannot recur. (Closes #339, #340.)
- **CI `black` + `flake8` lint scope widened to `scripts/`** — both invocations in `.github/workflows/ci.yml` previously checked only `src/` and `tests/`, leaving the six production entrypoints the supervisor spawns (`backfill_history_md.py`, `daily_sync.py`, `daily_incremental.py`, `backfill_delisted_via_tinkoff.py`, `apply_corporate_actions.py`, `run_macro_sync.py`) unlinted. The widened scope immediately surfaced two real F401 dead imports (`typing.Any`, `src.data.models.OHLCVRow`) in `apply_corporate_actions.py`, both dropped. New `tests/test_ci_lint_scope.py` pins the scope and the clean state — re-injecting either defect reproduces the failure (RED/GREEN verified). `mypy` deliberately stays `src/`-only (would need `--explicit-package-bases` plumbing; out of scope). (Closes #341, #342.)

### Documentation
- **`CHANGELOG.md`** (this file) — aggregated release view reconstructed from `git log` + per-PR descriptions. Closes #289.

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