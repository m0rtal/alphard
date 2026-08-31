# Alphard — Quickstart

> **Goal**: take a clean Docker host and a fresh `git clone`, run one
> command, and have a fully functional `alphard` stack (postgres,
> redis, alphard-bot, prometheus, grafana) on `localhost`.

This document covers the **single-command first-shot path**. For
day-2 operations (re-deploy, scale, custom domains, multi-host,
production tuning), see [RUNBOOK.md](RUNBOOK.md) and the ADRs under
`docs/decisions/`.

## Prerequisites

| Requirement | How to verify |
|-------------|---------------|
| Linux host (kernel ≥4.x) | `uname -r` |
| Docker Engine 20.10+ | `docker version` |
| Docker Compose plugin v2 | `docker compose version` |
| ~3 GiB free disk | `df -h /` (alphard-bot 405 MB, postgres 80 MB, redis 40 MB, prometheus 200 MB, grafana 1.4 GB, chownfix 80 MB) |
| Ports 5432, 6379, 9090, 3300 free on host | `ss -ltn '( sport = :5432 or :6379 or :9090 or :3300 )'` |
| git | `git --version` |

**Minimum smoke**: 1 vCPU, 2 GiB RAM works; 4 GiB recommended.
**Memory profile**: bot ~200 MiB, postgres ~100 MiB, redis ~20 MiB,
prometheus ~200 MiB, grafana ~250 MiB.

## The one-command path

```bash
git clone https://github.com/m0rtal/alphard
cd alphard
./scripts/quickstart.sh
```

That is **all**. The script:

1. Sanity-checks Docker and Compose.
2. Creates `.env` from `.env.example` (if missing).
3. Refuses to start with an empty or historical
   `GRAFANA_ADMIN_PASSWORD=alphard` (issue #55).
4. Auto-generates a 24-byte random `POSTGRES_PASSWORD` /
   `REDIS_PASSWORD` (if missing).
5. (No Grafana bake needed — provisioning + dashboards are
   bind-mounted from `./docker/grafana/{provisioning,dashboards}`
   in compose; nothing for the script to do here. See PR #297.)
7. Verifies `docker/prometheus/prometheus.yml` exists and contains the
   `alphard-bot:8765` scrape target (issue #283 — bind-mounted into the
   prometheus container, no env-based config).
8. Runs `docker compose --profile observability up -d`.
9. Waits up to 180 s for every long-running service to report
   `healthy` (one-shot services — `alphard-chownfix` — are checked
   separately for `Exited(0)`; `pg-init` was dropped in PR #351 /
   issue #347).
10. Prints the per-container status table and the URL of each
    service.

## What you should see on success

```
=== Final state ===
NAMES                STATUS                    PORTS
alphard-bot          Up 41 seconds (healthy)
alphard-grafana      Up 41 seconds (healthy)
alphard-postgres     Up 41 seconds (healthy)   5432/tcp
alphard-prometheus   Up 41 seconds (healthy)   0.0.0.0:9090->9090/tcp
alphard-redis        Up 41 seconds (healthy)   6379/tcp
ok: stack is up
```

| Service | URL (from host) |
|---------|------------------|
| Bot `/health`, `/metrics` | in-network only — `docker exec alphard-bot curl localhost:8765/health` |
| Prometheus | http://localhost:9090/ |
| Grafana | http://localhost:3300/ (admin / `$GRAFANA_ADMIN_PASSWORD`) |
| Postgres + Redis | `docker exec alphard-postgres psql -U $POSTGRES_USER -d $POSTGRES_DB` |

## Failure modes the script catches

| Symptom | Exit code | Likely fix |
|---------|-----------|-----------|
| `docker not found in PATH` | 2 | Install Docker Engine 20.10+ |
| `docker compose plugin v2 not installed` | 2 | `apt install docker-compose-plugin` (Debian/Ubuntu) |
| `GRAFANA_ADMIN_PASSWORD is empty in .env` | 2 | Set the var or run `openssl rand -base64 24` |
| `GRAFANA_ADMIN_PASSWORD is set to the historical literal 'alphard'` | 2 | Replace with a new password |
| `docker compose up failed` | 2 | Inspect `docker compose logs` for the failing service |
| Stack didn't reach healthy state in 180 s | 1 | `docker ps --filter name=alphard-; docker logs <service>` |
| `grafana` has no datasource / "No data" on every panel | 1 | Verify `./docker/grafana/provisioning/datasources/prometheus.yml` exists. (issue #297: bind-mount from repo, no env-based config.) |

## Run-time knobs

All are optional env vars.

| Var | Default | Effect |
|-----|---------|--------|
| `ALPHARD_PROFILE` | `observability` | Set to `data` to skip Prometheus + Grafana; bot + postgres + redis only |
| `ALPHARD_TIMEOUT_SEC` | `180` | Health-gate timeout. Set to `0` to skip polling and exit 1 after `compose up` (CI fast-fail smoke) |
| `ALPHARD_SKIP_COMPOSE` | `0` | Set to `1` to bake `.env` only — exit 0 without invoking `docker compose` (operator runs compose themselves) |
| `ALPHARD_QUIET` | `0` | Set to `1` to suppress progress dots and info banners |
| `APPDATA_DIR` | `/srv/alphard` | Host directory for persistent bind-mounts |

## What the script does **NOT** do

- Generate `TINKOFF_*` tokens. Get them from
  https://www.tbank.ru/invest/settings/api and put them into `.env`
  *after* `quickstart.sh` finishes; restart the bot with
  `docker compose up -d alphard-bot`.
- Configure Grafana SSO / TLS. Plain HTTP, admin user only.
  Production must front this with a reverse proxy that terminates
  TLS and pins Grafana behind an auth provider.
- Create the `alphard-prometheus-data`, `alphard-postgres-data`,
  `alphard-redis-data` named volumes ahead of time. Compose
  creates them on first run.
- Set up reverse-proxy / DNS / port forwarding. The default ports
  listen on `0.0.0.0` for 9090 (Prometheus) and the host network
  namespace for 3300 (Grafana, see compose for the
  `network_mode: host` rationale).

## What it does that you probably forgot

- Verifies `docker/prometheus/prometheus.yml` exists with the
  `alphard-bot:8765` scrape target (issue #283 — bind-mounted, no env
  config).
- (No Grafana bake step — provisioning + dashboards are bind-mounted
  directly from `./docker/grafana/{provisioning,dashboards}`. Same
  pattern as PR #284 for prometheus.yml.)
- Refuses to run with empty GPW (issue #55). The first version of
  the deploy script allowed an empty password and silently deployed
  with the historical literal `alphard` — that path is closed.

## Day-2 quick reference

```bash
# Stop the stack (keep volumes):
./scripts/quickstart.sh && docker compose --profile observability down
# (or just) docker compose --profile observability down

# Stop and wipe volumes (DESTRUCTIVE — deletes all postgres data):
docker compose --profile observability down -v

# View logs:
docker compose logs -f alphard-bot
docker compose logs -f alphard-grafana

# Shell into postgres:
docker exec -it alphard-postgres psql -U $POSTGRES_USER -d $POSTGRES_DB

# Re-run quickstart (idempotent):
./scripts/quickstart.sh
```

## Why does this exist?

Without this script, `git clone && docker compose up` on a clean
host fails in 4 places (PR #228 / this PR's notes):

| # | Failure | Root cause |
|---|---------|-----------|
| 1 | `init_schema()` fails: `_auth_probe` missing | Fresh `ohlcv_daily` volume where the bot's entrypoint guard fired before schema apply. **Fix**: `init_schema()` in `docker/entrypoint.sh` reads `src/data/schema.sql` and runs BEFORE `auth_probe()` (issue #347); if it didn't, rebuild the image so the entrypoint sequence is restored. |
| 2 | `grafana` fails to start with `apparmor_parser: Access denied` | Grafana service was missing `security_opt: apparmor=unconfined`. **Fix**: added in compose. |
| 3 | Prometheus starts with empty config, zero targets | `docker/prometheus/prometheus.yml` missing or wrong content. **Fix**: check the bind-mount target exists and contains the `alphard-bot:8765` scrape target. |
| 4 | Grafana "No data" on every panel | `./docker/grafana/provisioning/datasources/prometheus.yml` missing. **Fix**: bind-mount from repo (issue #297). |

Issue: closes #243 (`Make alphard first-shot-friendly`).
