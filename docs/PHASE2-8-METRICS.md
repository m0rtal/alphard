# Phase 2.8 step 1 — Prometheus metrics + Grafana

## What

A lightweight stdlib-only HTTP server in `src/metrics_server.py` exposes
`/metrics` (Prometheus text exposition format) and `/health` on
`alphard-bot:8765`. A Prometheus + Grafana stack runs under the
`observability` profile in `docker-compose.yaml` and scrapes the bot.

## Why this exists

PR #34 (lost in 2026-08-19 cleanup) had the original implementation.
This PR (#50 in GitHub, commit `82907a5`) restores it from PR diff
+ CI logs + squash commit message — the design is faithful to the
original but typed for mypy --strict and tested at 24 tests / 100% line
coverage on `src/metrics_server.py`.

## Endpoints

- `GET /health` — returns `200 ok\n`. Cheap liveness probe.
- `GET /metrics` — Prometheus text format. Stdlib `ThreadingHTTPServer`,
  no `prometheus_client` dependency.
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
  Pair with `time() - alphard_heartbeat_last_tick_timestamp` in Prometheus
  for stale-heartbeat alerts (> 60s).
- `alphard_backfill_progress_{tickers_done,tickers_total,bars_written}`
  — declared; emitted by `_backfill_supervisor_loop`.
- `alphard_daily_sync_last_run_timestamp` + `..._status{status}` —
  declared; emitted by `_daily_sync_loop`.

## Deploy

Stack file already declares the profile (no change to `docker-compose.yaml`
besides the env var addition):

```yaml
prometheus:
  image: prom/prometheus:latest
  profiles: ["observability"]
  volumes:
    - ./docker/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    - /mnt/appdata/alphard/prometheus:/prometheus

grafana:
  image: grafana/grafana:latest
  profiles: ["observability"]
  volumes:
    - ./docker/grafana/provisioning:/etc/grafana/provisioning:ro
    - ./docker/grafana/dashboards:/var/lib/grafana/dashboards:ro
```

Bring up:

```bash
# 1. Ensure ALPHARD_METRICS_PORT=8765 in /root/projects/alphard/.env (default ok)
# 2. Redeploy alphard-bot to pick up src/metrics_server.py
docker compose -f /root/projects/alphard/docker-compose.yaml up -d alphard-bot
# 3. Bring up Prometheus + Grafana
docker compose -f /root/projects/alphard/docker-compose.yaml --profile observability up -d
# 4. Open Grafana at http://192.168.1.107:3000 (admin / $GRAFANA_ADMIN_PASSWORD)
```

## Verification

After step 2, scrape from `.107`:

```bash
curl -s http://alphard-bot:8765/health      # → ok
curl -s http://alphard-bot:8765/metrics    # → Prometheus exposition format
```

Prometheus should pick up the `alphard-bot` job within 15s
(`scrape_interval`). Grafana auto-loads the dashboard from
`docker/grafana/dashboards/alphard-phase28.json` (the provider reads
`/etc/grafana/provisioning/dashboards`).

## Risks

- Port collision on `8765` inside the alphard-net bridge. Mitigated by
  the `try/except OSError` wrapper in `src/main.py` — the bot continues
  without metrics if the bind fails (logged at WARNING).
- Observability is not a hard dependency for trading. The bot must
  remain operational even if Prometheus is down. The `inc_counter` /
  `set_gauge` calls in the heartbeat loop are guarded by a None-check on
  the registry.

## Out of scope (Phase 2.8 step 2+)

- Per-stage Prometheus histograms (backfill latency, daily_sync duration).
- Decision-pipeline counters (orders placed, RISK rejections).
- Alertmanager + Telegram alerts.
