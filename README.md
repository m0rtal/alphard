# Alphard

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Tests](https://github.com/m0rtal/alphard/actions/workflows/ci.yml/badge.svg)](https://github.com/m0rtal/alphard/actions/workflows/ci.yml)

Autonomous trading bot на MOEX. Apache-2.0, self-hosted, Docker-only.

> **⚠️ Phase 0 skeleton (commit 3c48d23).** Это bootstrap: Risk Agent scaffold, Docker stack, docs. **Бот НЕ торгует.** Phase 1+ добавят Data Agent, Quant Agent, Macro Agent, Execution Agent, Coordinator. Все claims в этом README про "live trading" относятся к Phase 4+, не к текущему state.

## Что это

Alphard — автономный multi-agent trading system:
- Сам принимает решения (без approval)
- Сам исполняет через Tinkoff API + sandbox validation
- Hard risk gate (невозможно override)
- Continuous monitoring (event-driven, не scheduled)
- Self-validation (baseline vs IMOEX TR, LLM-as-judge weekly)
- Defensive rotation (4-tier при кризисе)

## Архитектура

8 агентов + Coordinator (state machine). Подробности — internal.

## Quickstart

```bash
# 1. Клонировать
git clone https://github.com/m0rtal/alphard.git
cd alphard

# 2. Скопировать .env и заполнить
cp .env.example .env
# Edit .env — заполнить TINKOFF_SANDBOX_TOKEN обязательно

# 3. Установить pre-commit hooks (gitleaks активен)
pip install pre-commit
pre-commit install

# 4. Запустить через Docker Compose
docker compose up -d

# 5. Проверить
docker compose ps
docker compose logs -f alphard-bot
# NB: Phase 0 — stub. /health endpoint появится в Phase 1.
# Сейчас "увидеть alive" можно только через docker compose logs.
```

## Структура

Фактический layout репо (Phase 0):

```
alphard/
├── .github/workflows/ci.yml   # CI (pytest + black + flake8 + mypy + gitleaks)
├── docker/                    # Dockerfile + entrypoint.sh
├── docs/
│   ├── SECURITY.md            # Threat model (5 layers + P0/P1/P2)
│   ├── AUDIT-CodeQuality.md   # Phase 0 audit reports
│   └── AUDIT-Phase0-FINAL.md
├── src/
│   ├── main.py                # Phase 0 heartbeat stub
│   └── risk/
│       └── gate.py            # Risk Agent (frozen pydantic validators, 97% coverage)
├── tests/test_risk_gate.py    # 35 tests, 97% coverage gate.py
├── .dockerignore              # Excludes .env, .git, build cache
├── .env.example               # Шаблон секретов
├── docker-compose.yaml        # Локальный stack
├── portainer-stack.yaml       # Stack для .107 Portainer
├── pyproject.toml             # Poetry (Phase 1+ deps)
├── requirements.txt           # Phase 0 deps (pinned)
├── LICENSE                    # Apache-2.0 (canonical, 11.3 KB)
└── README.md
```

## Что НЕ в репо

- `.env` — реальные секреты
- `data/` — локальные данные (bind-mount)
- `models/` — обученные модели (добавятся в Phase 2/3)
- Внутренние design docs (architecture, agent topology) — не публикуются по соображениям конкурентной/стратегической безопасности

## Разработка

```bash
# Установить pre-commit hooks (gitleaks обязательно)
pip install pre-commit
pre-commit install

# Запустить тесты
python3 -m pytest

# Проверить coverage (gate required ≥95%)
python3 -m pytest --cov=src --cov-report=html

# Black / flake8 / mypy (запускаются автоматически в CI)
python3 -m black --check src/ tests/
python3 -m flake8 src/ tests/
python3 -m mypy src/ --strict --ignore-missing-imports
```

## Безопасность

- ✅ `.env` в `.gitignore`, никогда не коммитится
- ✅ `gitleaks` pre-commit + GitHub Actions CI блокируют утечки секретов
- ✅ Контейнер работает от non-root user (UID 1000)
- ✅ Risk Agent — `RiskLimits` frozen=True, любая мутация post-construction отклоняется
- ✅ Сеть изолирована (postgres/redis только внутри `alphard-net`)
- ✅ Все credentials через `.env`, шаблон в `.env.example`
- ⚠️ Audit: Phase 0 финальный отчёт → `docs/AUDIT-Phase0-FINAL.md` (8 critical, 10 high)

## Honest gaps

- Phase 0: только Risk Agent. Data Agent, Quant Agent, Macro Agent, Portfolio, Execution, Coordinator — в Phase 1+
- Брокер-абстракция, Tinkoff connector — Phase 1.3
- Backtest framework (VectorBT) — Phase 2/3
- ML pipeline (LightGBM) — Phase 2
- News + RAG (pgvector) — Phase 3
- `/health` HTTP endpoint не реализован (Phase 1)
- Prometheus/Grafana observability — Phase 3
- AGENTS.md для OSS contributors — на стадии подготовки

## Лицензия

Apache-2.0. См. [LICENSE](LICENSE).

## Контакты

Александр (m0rtal) — creator и maintainer.
Вопросы → issues или discussions на GitHub.
