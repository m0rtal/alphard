# Alphard — Quickstart

| **Goal**: take a clean Docker host and a fresh `git clone`, run one
command, and have a fully functional `alphard` stack (postgres,
alphard-bot, alphard-web; alphard-redis removed in PR #426) on `localhost`.

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
| ~3 GiB free disk | `df -h /` (alphard-bot 405 MB, postgres 80 MB, alphard-web 120 MB; alphard-redis removed in PR #426) |
| Ports 5432, 8081 free on host (6379 freed after PR #426 removed alphard-redis) | `ss -ltn '( sport = :5432 or :8081 )'` |
| git | `git --version` |

**Minimum smoke**: 1 vCPU, 2 GiB RAM works; 4 GiB recommended.
**Memory profile**: bot ~200 MiB, postgres ~100 MiB,
alphard-web ~80 MiB (alphard-redis removed in PR #426).

## The one-command path

```bash
git clone https://github.com/m0rtal/alphard
cd alphard
./scripts/quickstart.sh
```

That is **all**. The script:

1. Sanity-checks Docker and Compose.
2. Creates `.env` from `.env.example` (if missing).
3. Auto-generates a 24-byte random `POSTGRES_PASSWORD`
   (if missing; `REDIS_PASSWORD` auto-gen removed in PR #426).
4. Fetches the Russian Trusted Root CA + Sub CA bundle from
   `invest-public-api.tinkoff.ru` / `iss.moex.com` and writes it to
   `docker/certs/tinkoff-gost-ca-bundle.txt` so the bot can verify
   their TLS chains. Idempotent — re-running skips the fetch if the
   bundle is <30 days old. (Issue #455.)
5. Runs `docker compose up -d`.
6. Waits up to 180 s for every long-running service to report
   `healthy`.
7. Prints the per-container status table and the URL of each
   service.

## What you should see on success

```
=== Final state ===
NAMES                STATUS                    PORTS
alphard-bot          Up 41 seconds (healthy)
alphard-postgres     Up 41 seconds (healthy)   5432/tcp
alphard-redis        (removed in PR #426) — Up via in-process token bucket
alphard-web          Up 41 seconds (healthy)   0.0.0.0:8081->8080/tcp
ok: stack is up
```

| Service | URL (from host) |
|---------|------------------|
| Bot `/health`, `/metrics` | in-network only — `docker exec alphard-bot curl localhost:8765/health` |
| alphard-web (operator dashboard) | http://localhost:8081/ |
| Postgres (alphard-redis removed in PR #426) | `docker exec alphard-postgres psql -U $POSTGRES_USER -d $POSTGRES_DB` |

## Failure modes the script catches

| Symptom | Exit code | Likely fix |
|---------|-----------|-----------|
| `docker not found in PATH` | 2 | Install Docker Engine 20.10+ |
| `docker compose plugin v2 not installed` | 2 | `apt install docker-compose-plugin` (Debian/Ubuntu) |
| `docker compose up failed` | 2 | Inspect `docker compose logs` for the failing service |
| Stack didn't reach healthy state in 180 s | 1 | `docker ps --filter name=alphard-; docker logs <service>` |

## Run-time knobs

All are optional env vars.

| Var | Default | Effect |
|-----|---------|--------|
| `ALPHARD_TIMEOUT_SEC` | `180` | Health-gate timeout. Set to `0` to skip polling and exit 1 after `compose up` (CI fast-fail smoke) |
| `ALPHARD_SKIP_COMPOSE` | `0` | Set to `1` to bake `.env` only — exit 0 without invoking `docker compose` (operator runs compose themselves) |
| `ALPHARD_QUIET` | `0` | Set to `1` to suppress progress dots and info banners |
| `APPDATA_DIR` | `/srv/alphard` | Host directory for persistent bind-mounts |

## What the script does **NOT** do

- Generate `TINKOFF_*` tokens. Get them from
  https://www.tbank.ru/invest/settings/api and put them into `.env`
  *after* `quickstart.sh` finishes; restart the bot with
  `docker compose up -d alphard-bot`.
- Configure alphard-web TLS. Plain HTTP, internal-only by default.
  Production must front this with a reverse proxy that terminates
  TLS and pins the dashboard behind an auth provider.
- Create the `alphard-postgres-data` named volume
  ahead of time (compose creates it on first run; alphard-redis-data removed in PR #426).
- Set up reverse-proxy / DNS / port forwarding. The default ports
  listen on `0.0.0.0` for 8081 (alphard-web operator dashboard).
  No host-network services anymore — see compose for the
  `network_mode` rationale.

## What it does that you probably forgot

- Refuses to run when foreign-owned `alphard-*` containers exist
  (issue #382 / PR #385).

## Day-2 quick reference

```bash
# Stop the stack (keep volumes):
docker compose down

# Stop and wipe volumes (DESTRUCTIVE — deletes all postgres data):
docker compose down -v

# View logs:
docker compose logs -f alphard-bot
docker compose logs -f alphard-web

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
| 2 | alphard-web fails to start with `apparmor_parser: Access denied` | alphard-web service is missing `security_opt: apparmor=unconfined`. **Fix**: added in compose. |

Issue: closes #243 (`Make alphard first-shot-friendly`).
