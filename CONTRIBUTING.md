# Contributing to Alphard

Спасибо за интерес к проекту. Этот документ — 1-страничный quickstart для тех, кто хочет внести вклад в кодовую базу, документацию или security-политику. Подробные внутренние правила (AGENTS.md) появятся позже; пока этого файла достаточно, чтобы начать.

---

## 1. Где что лежит

```
alphard/
├── README.md            # публичный обзор + Quickstart
├── CONTRIBUTING.md      # этот файл
├── ARCHITECTURE.md      # canonical architecture (Phase 2.x)
├── API.md               # public Python contract (Coordinator, RiskGate, env vars)
├── LICENSE              # Apache-2.0
├── docs/
│   ├── SECURITY.md      # Threat model + Defense layers + Runbook triggers
│   └── RUNBOOK.md       # Incident response playbook (Phase 0.6 skeleton)
├── src/
│   ├── main.py          # Phase 0 heartbeat stub
│   └── risk/gate.py     # Risk Agent (frozen pydantic validators)
├── tests/               # pytest, gate.py ≥ 95% coverage required
├── docker/              # Dockerfile + entrypoint.sh
├── .github/workflows/   # CI (pytest + black + flake8 + mypy + gitleaks)
└── ...
```

Перед любым изменением прочитайте:

- [README.md](README.md) — что проект делает и в какой фазе находится.
- [ARCHITECTURE.md](ARCHITECTURE.md) — канонический архитектурный документ (Phase 2.x).
- [API.md](API.md) — публичный Python-контракт (Coordinator, RiskGate, env vars). Обязательно к прочтению перед добавлением нового агента.
- [docs/SECURITY.md](docs/SECURITY.md) — threat model и defense layers. Любой PR, затрагивающий сетевую поверхность, секреты или Risk Agent, должен ссылаться на соответствующий раздел SECURITY.md.
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — если ваше изменение вводит новый kill-switch или меняет процедуру rollback.

### 2.1 Adding an agent

См. [`API.md` §7 — Hello World Agent](API.md) для рабочего примера
Coordinator-интеграции. Ключевые ограничения:

- Агент НЕ ДОЛЖЕН вызывать `Coordinator._execute()` напрямую.
- Агент НЕ ДОЛЖЕН обходить `RiskGate.evaluate()`.
- Агент НЕ ДОЛЖЕН писать в `audit_log` напрямую — только через `Coordinator._audit()`.
- Агент ДОЛЖЕН эмитить counters через `_metrics_registry` (см. `src/main.py`).
  Primary reader — `alphard-web` (PR #394) на `:8081`. _(Исторический
  scraper снесён в PR #399; wire-format остаётся text exposition
  для совместимости.)_

## 2. Workflow

1. Fork → branch от `main` (формат: `task/<короткое-имя>` или `fix/<что-чиним>`).
2. Локально: `pip install pre-commit && pre-commit install` (gitleaks активен).
3. Перед коммитом убедитесь, что:
   - `python3 -m pytest` проходит локально.
   - `python3 -m pytest --cov=src --cov-report=html` показывает ≥ 95% coverage для всего, что вы трогали.
   - `python3 -m black --check src/ tests/` и `python3 -m flake8 src/ tests/` чистые.
   - `python3 -m mypy src/ --strict --ignore-missing-imports` без ошибок.
   - Никаких секретов в diff (`pre-commit run --all-files` ловит это через gitleaks).
4. Commit message — conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`).
5. Push → open Pull Request в `m0rtal/alphard:main`. В описании PR укажите: что меняется, почему, какие тесты прогнаны, есть ли impact на SECURITY.md / RUNBOOK.md.

## 3. Что НЕ принимается

- Любые коммиты, содержащие реальные секреты (`.env`, токены, ключи). Pre-commit + CI их зарежут, но не пытайтесь обойти.
- Изменения, отключающие Risk Agent (`RiskLimits.frozen=True`) или убирающие pydantic-валидаторы. Это архитектурный invariant.
- Зависимости без указания версии в `requirements.txt`. Phase 0 — pinned deps only.
- Бинарные ассеты, модели, дампы данных. Репо — код + docs, не артефакты.
- Документация, обещающая фичи, которых нет в коде. Phase markers в README — закон, не пожелание.

## 4. Сообщить об уязвимости

Не открывайте публичный issue для security-находок. Пишите напрямую мейнтейнеру (см. README → Контакты) или используйте GitHub Security Advisories для репозитория. Ожидаемый SLA на первый ответ — 72 часа. Подробности о threat model и incident response — в [docs/SECURITY.md](docs/SECURITY.md) и [docs/RUNBOOK.md](docs/RUNBOOK.md).

## 5. Лицензия

Любой вклад автоматически лицензируется под [Apache-2.0](LICENSE). Это совпадает с лицензией проекта; никаких CLA на текущий момент не требуется.

---

Если вы видите, что этот документ врёт относительно реального состояния репо — откройте PR с правкой, пожалуйста. Документация, которая расходится с кодом, хуже, чем отсутствие документации.