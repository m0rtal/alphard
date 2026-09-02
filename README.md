# Alphard

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Tests](https://github.com/m0rtal/alphard/actions/workflows/ci.yml/badge.svg)](https://github.com/m0rtal/alphard/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-93%25-yellow.svg)](https://github.com/m0rtal/alphard)

Автономный multi-agent trading bot на MOEX/Tinkoff. Apache-2.0, self-hosted, Docker-only.

> **Статус (2026-08-20):** Phase 1 closed (10/10 gaps), Phase 2 = **6/10 merged**
> (2.6 step 1 cross-source, 2.7 delisted cron, 2.8 metrics, 2.9 step 1 backup,
> 2.5 step 1 split adjust, 2.1 sandbox-token redeploy in flight).
> Backfill resume-safe via `_backfill_supervisor_loop` (PR #47/#50,
> 3-layer deadlock fix). Health monitoring live (alphard-web operator
> dashboard on port 8081, alphard-bot /metrics :8765). Daily Postgres
> backup at /mnt/appdata/alphard-backups/.
> **Бот НЕ торгует на реальные деньги** — `LIVE_TRADING=false` hardlock
> в `src/broker/tinkoff_account.py`. Active phase: **2.3 Macro Agent** + **2.6 step 2
> multi-source schema** + **2.5 step 2b corporate-actions wiring**.

## Что это

Alphard — автономный multi-agent trading system:

- Сам принимает решения (event-driven, не scheduled)
- Сам исполняет через Tinkoff Invest API (sandbox-first, real после явного
  `LIVE_TRADING=true` в окружении)
- Hard risk gate (frozen pydantic, любая мутация post-construction = reject)
- Continuous monitoring + supervisor/respawn threads на каждом long-running daemon
- Cross-source validation (Tinkoff MD archive + MOEX ISS, 3-layer fallback)
- Self-validation (regime detection: CBR key rate + IMOEX + USD/RUB)
- ML pipeline (planned Phase 2.4)
- Sandbox token validated 2026-08-20: curl to
  `https://sandbox-invest-public-api.tinkoff.ru/history-data` → HTTP 404
  (figi unknown, но TLS handshake + token auth = OK)

## Архитектура

Краткий обзор: 8 агентов + Coordinator, один контейнер `alphard-bot`,
состояние в Postgres, метрики в `alphard-web` на .107:8081 (PR #394)
через прямой SQL к Postgres. Полная архитектура,
конвейер Coordinator (FETCH → VALIDATE → RISK → EXECUTE → AUDIT),
политики fail-open/fail-closed и failure modes — в
[`ARCHITECTURE.md`](ARCHITECTURE.md). Для публичного API контракта —
[`API.md`](API.md).

**Phase status (HEAD):** Phase 1 closed (10/10 gaps), Phase 2 = 7/10 merged
(2.6 step 1 cross-source, 2.7 delisted cron, 2.8 metrics, 2.9 step 1 backup,
2.5 step 1 split adjust, 2.1 sandbox-token redeploy, 2.8 step 2 ohlcv rows panel).
См. [`docs/PHASE2-ROADMAP.md`](docs/PHASE2-ROADMAP.md) для бэклога.

Defensive infrastructure (added 2026-08-19/20):

- `_backfill_supervisor_loop` (PR #47): spawn → waitpid → respawn 30s backoff →
  `os._exit(1)` при >10 смертях/час. Заменён `setsid ... &` zombie generator.
- `_daily_sync_loop` (Phase 1.6, PR #0dcf55b): Mon-Fri 19:00 MSK
- `_delisted_sync_loop` (PR #37): weekly, 7d cadence, 40min timeout
- `_macro_sync_loop` (Phase 2.3, planned): hourly
- `faulthandler.register(SIGUSR1)` модуль-level в backfill_history_md.py — dump
  stack на signal без отдельного subprocess.
- `TokenBucket` capacity ≥ 1.0 (PR #50) — нельзя застрять в `time.sleep(∞)`
- `connect_timeout=10` + `statement_timeout=60000` на psycopg connect (PR #46)
- `mark_terminally_failed.py` (commit 9d34663) — backfill пропускает no-data
  тикеры (Phase 1 gaps #2 + #7)

Coordinator, hard-gate, token-gate — подробности internal (не публикуются).

## Quickstart

```bash
# 1. Клонировать
git clone https://github.com/m0rtal/alphard.git
cd alphard

# 2. Скопировать .env.example и сгенерировать секреты
cp .env.example .env
# Сгенерируй пароли для postgres (≥16 chars):
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24)" >> .env
# Sandbox-токен берётся на https://www.tbank.ru/invest/settings/api:
echo "TINKOFF_SANDBOX_TOKEN=$(cat /tmp/your_real_token)" >> .env
# (Только для real-trading: echo "TINKOFF_REAL_TOKEN=..." >> .env)

# 3. Установить pre-commit hooks (gitleaks активен)
pip install pre-commit
pre-commit install

# 4. Запустить через Docker Compose
docker compose up -d

# 5. Проверить
docker compose ps
docker compose logs -f alphard-bot
# Health: curl http://192.168.1.107:8765/health → 200
# Metrics: curl http://192.168.1.107:8765/metrics
# Operator dashboard: http://192.168.1.107:8081/
```

```bash
# Запуск бэкфилла (resume-safe, пропускает complete-тикеры):
docker exec alphard-bot python3 scripts/backfill_history_md.py
# Текущий снапшот universe: SELECT COUNT(*) FROM ticker_universe;
# Backfill token bucket: 0.5 req/sec sustained (Tinkoff rate-limit).
# Один тикер ≈ 16 мин × 9 лет × 3253 тикеров ≈ ~40 дней полного universe.

# Mark no-data тикеры terminal (Phase 1 gaps #2, #7):
docker exec alphard-postgres psql -U alphard -d alphard \
  -f scripts/mark_terminally_failed.sql
```

> **One compose file. Anywhere.** `docker-compose.yaml` — single source of truth
> для local dev и Portainer deploy. Portainer: Add stack → Repository → Git URL
> `https://github.com/m0rtal/alphard.git` → Compose path `docker-compose.yaml`.

## Структура

```text
alphard/
├── .github/workflows/
│   ├── ci.yml                  # pytest + black + flake8 + mypy + gitleaks + PostgreSQL service
│   └── docker-image.yml        # GHCR pipeline → ghcr.io/m0rtal/alphard:latest
├── docker/
│   ├── Dockerfile              # Self-contained, deps baked at build time
│   └── entrypoint.sh
├── docs/
│   ├── SECURITY.md             # Threat model (5 layers + P0/P1/P2)
│   ├── RUNBOOK.md              # Incident response playbook
│   ├── PHASE2-ROADMAP.md       # Single source of truth for Phase 2+ planning
│   └── AUDIT-Phase0.md         # Phase 0 audit
├── src/
│   ├── main.py                 # Coordinator entry point + daemon threads
│   ├── broker/                 # TinkoffAccount (LIVE_TRADING=false hardlock)
│   ├── risk/                   # Risk Agent (fail-safe limits, frozen, drawdown tracker)
│   ├── data/
│   │   ├── tinkoff_loader.py   # gRPC GetCandles (Decimal precision, share/bond/ETF)
│   │   ├── tinkoff_md_loader.py # HTTP MD archive (Tinkoff history-data)
│   │   ├── moex_loader.py      # REST fallback (no-auth, public)
│   │   ├── fallback_loader.py  # 3-source fallback chain
│   │   ├── pg_store.py         # psycopg v3, ON CONFLICT, connect_timeout=10
│   │   ├── sqlite_store.py     # Local fallback (InMemorySQLiteStore for tests)
│   │   ├── adjustment.py       # apply_split_adjustment / apply_dividend_adjustment / apply_adjustment
│   │   ├── token_bucket.py     # Tinkoff rate-limit guard (capacity ≥ 1.0)
│   │   ├── quality/            # 3-tier quality gate
│   │   └── models.py           # TickerMeta, OHLCVRow, CorporateAction, SourceType
│   ├── metrics_server.py       # /health + /metrics on alphard-bot:8765 (PR #52/#53; observability surface — read by alphard-web directly since PR #399 dropped Prometheus)
│   ├── coordinator.py          # Data→Quality→Risk→Broker pipeline + decision log
│   └── _types.py
├── scripts/
│   ├── backfill_history_md.py         # Resume-safe backfill (PR #47/#50)
│   ├── daily_sync.py                  # Mon-Fri 19:00 MSK
│   ├── delisted_sync.py               # Weekly (PR #37)
│   ├── backup_database.py             # Daily pg_dump → /mnt/appdata/alphard-backups/ (PR #38)
│   ├── cross_source_smoke.py          # 3-scenario validation harness (PR #27)
│   ├── fetch_moex_corporate_actions.py # MOEX ISS splits+dividends fetcher
│   └── mark_terminally_failed.py      # Skip known no-data tickers
├── tests/                            # 1621+ tests, ~95% coverage
├── .dockerignore
├── .env.example                # Шаблон секретов (TINKOFF_SANDBOX_TOKEN/REAL_TOKEN, POSTGRES_PASSWORD)
├── docker-compose.yaml         # alphard-bot, postgres, redis, alphard-web
├── pyproject.toml              # Poetry (Phase 2+ deps)
├── requirements.txt            # Pinned CI deps
├── LICENSE                     # Apache-2.0 (canonical, 11.3 KB)
└── README.md
```

## Что НЕ в репо

- `.env` — реальные секреты
- `data/` — локальные данные (bind-mount)
- `models/` — обученные модели (Phase 2/3)
- `backups/` — pg_dump snapshots (local NAS, 7 daily + 4 weekly retention)
- Внутренние design docs (architecture, agent topology, signal logic) — не
  публикуются по соображениям конкурентной/стратегической безопасности

## Разработка

```bash
# Установить pre-commit hooks (gitleaks обязательно)
pip install pre-commit
pre-commit install

# Запустить тесты
python3 -m pytest
# 716+ tests, ~93% coverage (PG-dependent integration tests skip locally)

# Black / flake8 / mypy (CI pinned black==24.10.0 — match locally!)
python3 -m black --check src/ tests/
python3 -m flake8 src/ tests/
python3 -m mypy src/ --strict --ignore-missing-imports
```

CI workflow (`ci.yml`):

1. **Build + Push** — docker image → GHCR `ghcr.io/m0rtal/alphard:latest`
2. **Lint + Format** — black + flake8 + mypy
3. **Secrets scan** — gitleaks action v2 (блокирует PUSH если найдены credentials)
4. **Tests + Coverage** — pytest + coverage report (PostgreSQL service в GitHub Actions)
5. SCA (pip-audit) — удалён в PR #23 (--skip-prefix flag dropped, native fix =
   `requirements-ci.txt` filtered, без t-tech-investments).

Branch protection на `main`:

- `enforce_admins=false`, `required_approving_review_count=0`
- required checks: Build+Push, Lint+Format, Secrets scan, Tests+Coverage
- `required_linear_history=true` — только squash merge

## Мониторинг

- **alphard-bot** на .107 — `python3 -m src.main`, экспортирует metrics на :8765
- **alphard-web** — операторская панель на :8081 (PR #394), читает
  напрямую из `alphard-postgres` через SQL, отображает те же метрики,
  что и Grafana phase28 dashboard. Заменяет Grafana + Prometheus
  (PR #399).
- **alphard-postgres** — данные OHLCV + decision_log, отдельный bind-mount на
  `/mnt/appdata/alphard-postgres`
- **Backup** — `/mnt/appdata/alphard-backups/`, daily pg_dump gzip-6, retention
  7 daily + 4 weekly (ISO week)

Сетевой трафик через proxy egress (Tinkoff API + MOEX ISS), см. AGENTS.md.

## Безопасность

- `.env` исключён через `.gitignore` + `.dockerignore`, никогда не коммитится
- `gitleaks` pre-commit + GitHub Actions CI блокируют утечки секретов
- Контейнер работает от non-root user (UID 1000)
- Risk Agent — `RiskLimits` frozen=True, любая мутация post-construction → reject
- Сеть изолирована (postgres/redis только внутри `alphard-net`)
- Все credentials через `.env`, шаблон в `.env.example`
- `LIVE_TRADING=false` hardlock в `src/broker/tinkoff_account.py` — бот НЕ
  размещает real orders даже при подмене credentials
- `TinkoffAccount.place_order()` обёрнут в fail-safe check: ANY env-resolved
  real token + LIVE_TRADING=false → raise `BrokerError` без сетевого вызова
- `TINKOFF_SANDBOX_TOKEN` ≠ `TINKOFF_REAL_TOKEN`: sandbox token не работает
  на prod-эндпоинтах (`invest-public-api.tinkoff.ru`), только на
  `sandbox-invest-public-api.tinkoff.ru`. См. Tinkoff docs section
  "Использование токена песочницы".
- Audit reports: `docs/AUDIT-Phase0-FINAL.md` (8 critical, 10 high → все 8 fixed)

## Honest status (2026-08-20)

### Phase 1 — CLOSED (10/10 gaps)

| # | Gap | Fix |
|---|---|---|
| 1 | Network stall .107 | 3-layer: PR #47 tuple index, PR #50 token bucket, PR #46 statement_timeout + SIGUSR1 |
| 2 | ~250 no-data tickers | `mark_terminally_failed.py` (commit 9d34663) |
| 3 | Schema bootstrap on fresh volume | `init_schema()` in `docker/entrypoint.sh` (issue #347, runs BEFORE `auth_probe()`) |
| 4 | Observability | Phase 2.8 — PR #52 metrics + PR #53 Grafana provisioning (✅ MERGED; PR #399 dropped Grafana/Prometheus; PR #394 alphard-web reads metrics directly from Postgres) |
| 5 | Broker stub | Phase 1.3 TinkoffAccount + sandbox switch |
| 6 | Coordinator | Phase 1.5 + PR #30 coordinator smoke + PR #33 drawdown tracker |
| 7 | delisted_at sync | PR #37 (Phase 2.7) — merged |
| 8 | request-changes flow | t_5f16209f — Flow C live-tested 4 runs |
| 9 | Off-host backup infra | Phase 3+ (single-host NAS пока хватает) |
| 10 | Multi-region | Phase 3+ |

### Phase 2 — 6/10 merged

| Item | Status | Notes |
|---|---|---|
| 2.1 Real order placement | 🟡 in flight (t_e33eb24b) | sandbox token validated 2026-08-20 |
| 2.2 Quant Agent | ⏳ not started | Phase 2.4+ |
| 2.3 Macro Agent | 🟡 in flight (t_3ff3391f) | CBR+USD/RUB+IMOEX, regime classifier deterministic |
| 2.4 ML pipeline | ⏳ not started | |
| 2.5 Adjusted prices | ✅ step 1 (PR #45) + step 2b (PR #74) + step 2c (this PR) | apply_adjustment (splits+dividends) + MOEX fetcher + orchestrator |
| 2.6 Cross-source | ✅ step 1 (PR #27), 🟡 step 2 (t_5596e3ba) | multi-source schema migration |
| 2.7 Delisted cron | ✅ merged (PR #37) | |
| 2.8 Metrics /metrics + /health | ✅ merged (PR #52, #53); operator dashboard PR #394 | alphard-web live on .107:8081 |
| 2.9 Daily backup | ✅ step 1 (PR #38), ⏸ step 2 S3 sync | ждёт S3 bucket target |
| 2.10 Coordinator event loop | ⏳ not started | open-ended design |

### Backlog (Phase 2 → 3)

- Phase 2.6 step 3 — cross-source validation cron (depends on step 2)
- Backfill speed: 16 мин/тикер × 3253 ≈ 40 дней — non-production rate
- 18 old autostash entries (`git stash list`) — destructive cleanup pending user OK
- Phase 3: multi-region, news + RAG (pgvector), portfolio agent

## Лицензия

Apache-2.0. See [LICENSE](LICENSE).

## Контакты

Александр (m0rtal) — creator и maintainer.
Issues / discussions на GitHub.
