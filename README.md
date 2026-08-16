# Alphard

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Tests](https://github.com/m0rtal/alphard/actions/workflows/ci.yml/badge.svg)](https://github.com/m0rtal/alphard/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-95%25-green.svg)](https://github.com/m0rtal/alphard)

Автономный multi-agent trading bot на MOEX/Tinkoff. Apache-2.0, self-hosted, Docker-only.

> **Статус: Phase 1.3 closed, Phase 2 pending.** Data Agent работает end-to-end
> через Tinkoff Invest (gRPC для live candles + history-data endpoint для
> backfill). Universe **динамический** — собирается на этапе бэкфила без
> фильтра по `trading_status` (TQBR + SPBXM + TQOB + TQCB + TQTE), а на этапе
> торгов сужается по реальной доступности (`qualifier_flags`, листинг).
> Текущий снапшот: см. `SELECT COUNT(*) FROM ticker_universe` в Postgres.
> Risk Agent enforce 5 fail-safe лимитов. **Бот НЕ торгует на реальные деньги**
> — `LIVE_TRADING=false` hardlock в `src/broker/tinkoff_account.py`.
> Real sandbox order placement ещё не выполнен (Phase 1.4).
> Quant Agent, Macro Agent, ML pipeline — Phase 2+.

## Что это

Alphard — автономный multi-agent trading system:

- Сам принимает решения (event-driven, не scheduled)
- Сам исполняет через Tinkoff Invest API (sandbox-first, real после Phase 1.4)
- Hard risk gate (frozen pydantic, любая мутация post-construction = reject)
- Continuous monitoring + 4-tier defensive rotation при кризисе
- Self-validation (baseline vs IMOEX TR, regime detection)
- ML pipeline (anonymized tickers, OHLCV-only features)

## Архитектура

8 агентов + Coordinator:

| Agent | Phase | Status |
|---|---|---|
| Data | 1.3 | ✅ Tinkoff (gRPC + history-data) + MOEX ISS, 2.6M+ bars |
| Risk | 1.1 | ✅ 35 tests, 97% coverage, 5 fail-safe limits |
| Quality | 1.2 | ✅ 3 уровня (CRITICAL/HIGH/MEDIUM/LOW) |
| Broker | 1.3 | ✅ TinkoffAccount, sandbox switch, LIVE_TRADING=false |
| Coordinator | 1.5 | ✅ fetch→validate→risk→execute→audit decision log |
| Quant | 2 | ⏳ ML pipeline |
| Macro | 2 | ⏳ CBR/IMOEX/USD-RUB regime |
| Portfolio | 3 | ⏳ |

Coordinator, hard-gate, token-gate — подробности internal (не публикуются).

## Quickstart

```bash
# 1. Клонировать
git clone https://github.com/m0rtal/alphard.git
cd alphard

# 2. Скопировать .env.example и сгенерировать секреты
cp .env.example .env
# Сгенерируй пароли для postgres/redis (≥16 chars каждый):
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24)" >> .env
echo "REDIS_PASSWORD=$(openssl rand -base64 24)" >> .env
echo "TINKOFF_SANDBOX_TOKEN=$(cat /tmp/your_real_token)" >> .env
# (Token берётся на https://www.tbank.ru/invest/settings/api)

# 3. Установить pre-commit hooks (gitleaks активен)
pip install pre-commit
pre-commit install

# 4. Запустить через Docker Compose
docker compose up -d

# 5. Проверить
docker compose ps
docker compose logs -f alphard-bot
# PG database: alphard-postgres:5432 (см. .env)
# Daily sync: Mon-Fri 19:00 MSK (см. /root/.hermes/cron/alphard-daily-sync.sh)
```

```bash
# Запуск бэкфилла (полный universe, 5y OHLCV — TQBR+SPBXM+TQOB+TQCB+TQTE):
docker exec alphard-bot python3 scripts/backfill_history_md.py
# Текущий снапшот universe: см. SELECT COUNT(*) FROM ticker_universe;
# Объём и время выполнения зависят от рыночной ситуации.
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
│   ├── AUDIT-Phase0.md         # Phase 0 audit
│   ├── AUDIT-Phase0-FINAL.md   # Phase 0 final synthesis
│   └── AUDIT-CodeQuality.md
├── src/
│   ├── main.py                 # Coordinator entry point
│   ├── broker/                 # TinkoffAccount (LIVE_TRADING=false hardlock)
│   ├── risk/                   # Risk Agent (5 fail-safe limits, 97% coverage)
│   ├── data/                   # TinkoffInvestDataLoader + MOEXDataLoader + PostgresDataStore
│   │   ├── tinkoff_loader.py   # gRPC, Decimal precision, share/bond/ETF
│   │   ├── moex_loader.py      # REST fallback (no-auth)
│   │   ├── pg_store.py         # psycopg v3, ON CONFLICT, search_path
│   │   ├── sqlite_store.py     # Local fallback
│   │   ├── quality/            # 3-tier quality gate
│   │   └── models.py           # TickerMeta, OHLCVRow, CorporateAction
│   ├── coordinator.py          # Data→Quality→Risk→Broker pipeline
│   └── _types.py
├── tests/
│   ├── test_pg_store_integration.py   # integration tests (run against real Postgres)
│   ├── test_risk_gate.py
│   ├── test_coordinator.py
│   ├── test_tinkoff_grpc.py
│   ├── test_broker_connector.py
│   ├── test_token_bucket.py           # Concurrency safety for rate-limited APIs
│   └── test_tinkoff_md_loader.py      # MD archive (history-data) parsing
├── scripts/
│   ├── daily_sync.py                  # Cron 19:00 MSK Mon-Fri
│   ├── backfill_history_md.py         # Full universe via Tinkoff history-data
│   ├── backfill_full_universe.py      # Combined MOEX+Tinkoff backfill
│   ├── backfill_spbxm_universe.py     # SPBXM-only (legacy entrypoint)
│   ├── ci_local.sh
│   └── backfill_delisted_via_tinkoff.py
├── .dockerignore
├── .env.example                # Шаблон секретов
├── docker-compose.yaml         # Single compose file
├── pyproject.toml              # Poetry (Phase 2+ deps)
├── requirements.txt            # Pinned CI deps
├── LICENSE                     # Apache-2.0 (canonical, 11.3 KB)
└── README.md
```

## Что НЕ в репо

- `.env` — реальные секреты
- `data/` — локальные данные (bind-mount)
- `models/` — обученные модели (добавятся в Phase 2/3)
- Внутренние design docs (architecture, agent topology, signal logic) — не публикуются
  по соображениям конкурентной/стратегической безопасности

## Разработка

```bash
# Установить pre-commit hooks (gitleaks обязательно)
pip install pre-commit
pre-commit install

# Запустить тесты
python3 -m pytest

# Coverage (CI gate ≥95%, локально PG-зависимые тесты skip)
python3 -m pytest --cov=src --cov-report=html

# Black / flake8 / mypy (CI)
python3 -m black --check src/ tests/
python3 -m flake8 src/ tests/
python3 -m mypy src/ --strict --ignore-missing-imports
```

CI workflow:

1. **lint** — black + flake8 + mypy --strict
2. **tests** — pytest + coverage (Postgres 16 service в GitHub Actions)
3. **secrets** — gitleaks action v2 (блокирует PUSH если найдены credentials)
4. **docker-image** — build + push GHCR `ghcr.io/m0rtal/alphard:latest`

## Безопасность

- `.env` исключён через `.gitignore` + `.dockerignore`, никогда не коммитится
- `gitleaks` pre-commit + GitHub Actions CI блокируют утечки секретов
- Контейнер работает от non-root user (UID 1000)
- Risk Agent — `RiskLimits` frozen=True, любая мутация post-construction → reject
- Сеть изолирована (postgres/redis только внутри `alphard-net`)
- Все credentials через `.env`, шаблон в `.env.example`
- `LIVE_TRADING=false` hardlock в `src/broker/tinkoff_account.py` — bot НЕ размещает
  real orders даже при подмене credentials
- `TinkoffAccount.place_order()` обёрнут в fail-safe check: ANY env-resolved
  real token + LIVE_TRADING=false → raise `BrokerError` без сетевого вызова
- Audit reports: `docs/AUDIT-Phase0-FINAL.md` (8 critical, 10 high → все 8 fixed)

## Honest gaps

| Gap | Phase | ETA |
|---|---|---|
| Real sandbox order placement (smoke test) | 1.4 | this week |
| Bond backfill complete (1,601 OFZ, 5y) | 1.5 | this week |
| Quant Agent (LightGBM panel features) | 2 | 2-3 weeks |
| Macro Agent (CBR, IMOEX, regime detection) | 2 | 2-3 weeks |
| Backtest framework (VectorBT) | 2/3 | 4 weeks |
| News + RAG (pgvector) | 3 | 6 weeks |
| Web UI | 4 | 8 weeks |
| Coordinator continuous loop (state machine) | 1.5 | next |
| ETF universe (Tinkoff doesn't return ETFs) | 2 | TBD |

## Лицензия

Apache-2.0. See [LICENSE](LICENSE).

## Контакты

Александр (m0rtal) — creator и maintainer.
Issues / discussions на GitHub.
