# Alphard

Autonomous trading bot на MOEX. Apache-2.0, self-hosted, Docker-only.

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

# 3. Запустить через Docker Compose
docker compose up -d

# 4. Проверить
docker compose ps
docker compose logs -f alphard-bot
curl http://localhost:8080/health
```

## Структура

```
alphard/
├── src/                      # Код бота (агенты)
│   └── risk_layer.py         # Risk Agent (Phase 0)
├── tests/                    # Тесты
├── docs/                     # Документация
│   ├── ARCHITECTURE.md
│   ├── RISK.md
│   ├── RUNBOOK.md
│   ├── BROKER.md
│   └── DATA.md
├── AGENTS.md                 # Правила для AI-агентов
├── docker-compose.yaml       # Stack
├── pyproject.toml            # Poetry
└── .env.example              # Шаблон секретов
```

## Что НЕ в репо

- `rf-trading-agent-converged.md` — внутренний design doc, не для публичного доступа
- `.env` — реальные секреты
- `data/` — локальные данные
- `models/` — обученные модели (в git-lfs или отдельно)

## Разработка

```bash
# Установить pre-commit hooks
poetry install
pre-commit install

# Запустить тесты
poetry run pytest

# Проверить coverage
poetry run pytest --cov=src --cov-report=html
```

## Безопасность

- ✅ `.env` в `.gitignore`, никогда не коммитится
- ✅ `gitleaks` pre-commit hook блокирует утечки секретов
- ✅ Все credentials через `.env`, шаблон в `.env.example`
- ⚠️ Audit: `make audit-secrets` (TODO: добавить)

## Honest gaps

- Phase 0: только skeleton Risk Agent
- Нет брокер-абстракции, ML pipeline, backtest framework — в следующих фазах
- Backtest на истории НЕ запущен (нужен OHLCV дамп)
- Tinkoff API capabilities НЕ протестированы hands-on (нужен токен)

## Лицензия

Apache-2.0. См. [LICENSE](LICENSE).

## Контакты

Александр (m0rtal) — creator и maintainer.
Вопросы → issues или discussions на GitHub.
