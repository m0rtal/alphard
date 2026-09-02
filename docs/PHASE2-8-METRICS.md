# Phase 2.8 step 1 — Metrics endpoint + `alphard-web` reader

## What

A lightweight stdlib-only HTTP server in `src/metrics_server.py` exposes
`/metrics` (text-format exposition, historically called "Prometheus
text exposition format") and `/health` on `alphard-bot:8765`.

_(Post-PR #399, the historical scraper was removed. The
primary reader is `alphard-web` (PR #394) on `.107:8081`, which pulls
counters and gauges from the same `/metrics` endpoint via SQL on the
Postgres-resident state. The `/metrics` route stays — the wire format
is unchanged so any future scraper can consume it.)_

## Why this exists

PR #34 (lost in 2026-08-19 cleanup) had the original implementation.
This PR (#50 in GitHub, commit `82907a5`) restores it from PR diff
+ CI logs + squash commit message — the design is faithful to the
original but typed for mypy --strict and tested at 24 tests / 100% line
coverage on `src/metrics_server.py`.

## Endpoints

- `GET /health` — returns `200 ok\n`. Cheap liveness probe.
- `GET /metrics` — Prometheus text format. Stdlib `ThreadingHTTPServer`,
  no `prometheus_client` dependency. _(Retained post-PR #399; format
  unchanged for compatibility with `alphard-web` reader.)_
- `GET /anything-else` — `404 not found`.

## Metrics exposed

Counters:
- `alphard_heartbeats_total` — incremented every heartbeat tick (60s).
- `alphard_backfill_total{result="ok|skip|error|delisted"}` — declared
  in the registry; emitted once the supervisor starts labelling
  per-ticker outcomes.
- `alphard_daily_sync_total{result="ok|failed|timeout"}` — declared;
  emitted by `_daily_sync_loop`.

Gauges:
- `alphard_uptime_seconds` — process uptime.
- `alphard_heartbeat_last_tick_timestamp` — unix epoch of last tick.
  Pair with `time() - alphard_heartbeat_last_tick_timestamp` for
  stale-heartbeat alerts (> 60s). _(Served via the same endpoint; the
  consumer is `alphard-web` post-PR #399.)_
- `alphard_backfill_progress_{tickers_done,tickers_total,bars_written}`
  — declared; emitted by `_backfill_supervisor_loop`.
- `alphard_daily_sync_last_run_timestamp` + `..._status{status}` —
  declared; emitted by `_daily_sync_loop`.

## Deploy

The metrics endpoint is part of `alphard-bot`; no separate service to
bring up. Post-PR #399 there is no `observability` profile in
`docker-compose.yaml`. To surface the metrics visually, run
`alphard-web` (PR #394) on `.107:8081`:

```sh
docker compose -f /root/projects/alphard/docker-compose.yaml up -d alphard-web
```

The `alphard-web` reader pulls `/metrics` on each render and shows
the same counters / gauges that a scraper would. _(Historical: pre-#399
a Prometheus + Grafana stack ran under the `observability` profile.
Both services were removed in PR #399 because they duplicated the
read-path that `alphard-web` now serves directly.)_

## Verification

After deploy, scrape from `.107`:

```sh
curl -s http://192.168.1.107:8765/health      # → ok
curl -s http://192.168.1.107:8765/metrics     # → exposition format
```

Then open <http://192.168.1.107:8081/> (after auth-prompt with your
`ALPHARD_WEB_TOKEN`); the dashboard renders the same metrics from
`alphard-web`'s SQL-backed reader. _(Historical: pre-#399 the dashboard
served from Grafana at `:3300`; that service no longer exists.)_

## Risks

- Port collision on `8765` inside the alphard-net bridge. Mitigated by
  the `try/except OSError` wrapper in `src/main.py` — the bot continues
  without metrics if the bind fails (logged at WARNING).
- Observability is not a hard dependency for trading. The bot must
  remain operational even if the reader is down. The `inc_counter` /
  `set_gauge` calls in the heartbeat loop are guarded by a None-check on
  the registry.
- `alphard-web` on `:8081` is LAN-exposed. Mitigated by bearer-token
  gate (PR #406 / #411) — see `docs/SECURITY.md` §4.2 for the current
  threat model.

## Out of scope (Phase 2.8 step 2+)

- Per-stage Prometheus histograms (backfill latency, daily_sync duration).
- Decision-pipeline counters (orders placed, RISK rejections).
- Alertmanager + Telegram alerts. _(Post-#399 alert delivery routes
  through `alphard-web`'s tile-level alerts; Alertmanager service is
  not on the roadmap.)_
