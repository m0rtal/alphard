# Documentation Index

> Карта документации Alphard. Если вы не знаете, с чего начать —
> читайте `README.md` → эту страницу → нужный раздел ниже.
>
> **Legend:**
> - 🟢 **current** — отражает Phase 2.x реальность (на 2026-08-27)
> - 🟡 **historical snapshot** — описывает конкретную фазу, актуальна
>   для понимания эволюции, но **не для текущего кода**
> - 🔴 **legacy / superseded** — заменена более новым документом,
>   держим для аудита

---

## 🚀 Start here (новый контрибьютор / оператор)

| # | Документ | Зачем |
|---|---|---|
| 1 | [`README.md`](README.md) | что это за проект, статус, badges, Quickstart |
| 2 | [`CONTRIBUTING.md`](CONTRIBUTING.md) | workflow контрибуции (fork → branch → PR → CI gates) |
| 3 | [`docs/TESTING.md`](docs/TESTING.md) | как гонять тесты, мокать Tinkoff SDK / Postgres, skip policy 🟢 |
| 4 | [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | symptom→fix index для оператора 🟢 |
| 5 | [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | first-shot-friendly bring-up (`bash scripts/quickstart.sh`) 🟢 |

## 📚 Справочная документация

| Документ | Зачем | Status |
|---|---|---|
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | happy-path operations (start/stop/monitor) | 🟢 |
| [`docs/SECURITY.md`](docs/SECURITY.md) | threat model + defense layers + runbook triggers | 🟢 |
| [`docs/DEPLOY-ENV.md`](docs/DEPLOY-ENV.md) | production .107 deployment variables | 🟢 |
| [`docs/POSITION-SIZING.md`](docs/POSITION-SIZING.md) | position sizing policy (Phase 2.2) | 🟢 |
| [`docs/PHASE2-8-METRICS.md`](docs/PHASE2-8-METRICS.md) | Prometheus metrics + Grafana panels | 🟢 |
| [`docs/PHASE2-ROADMAP.md`](docs/PHASE2-ROADMAP.md) | Phase 2 roadmap (live, актуально на 2026-08-27) | 🟢 |

## 🏛 Архитектурные решения (ADR)

Все ADR живут в [`docs/decisions/`](docs/decisions/) и нумеруются
последовательно. Паттерн: каждое решение — отдельный файл с
header (`Status`, `Date`, `Deciders`).

| # | ADR | Тема |
|---|---|---|
| [0006](docs/decisions/0006-position-sizing.md) | Position Sizing Policy — Phase 2.2 | 🟡 Proposed (2026-08-22) |
| [0007](docs/decisions/0007-rebalance-scheduler.md) | Rebalance Scheduler — Phase 2.4 | 🟡 Proposed (2026-08-22) |

Что пишется в ADR vs что в коммитах:
- **ADR** — архитектурное решение с trade-offs (почему выбрано A
  вместо B). Code не прикладывается — implementation идёт отдельными
  PR.
- **Commit message** — что конкретно сделано в коде.

Если ваше изменение — architectural choice (новый паттерн,
изменение существующего, выбор между X и Y), откройте ADR первым.

## 🗃 Legacy / historical snapshots

**Не читать как current docs** — это аудит-trail прошлых фаз.
Полезны для понимания, почему что-то сделано именно так.

| Документ | Фаза | Когда актуален |
|---|---|---|
| `docs/AUDIT-CodeQuality.md` | Phase 1.0 | 🟡 Phase 1 audit; superseded by per-PR review |
| `docs/AUDIT-Phase0.md` | Phase 0 | 🔴 Pre-Phase 1.0; keep for audit |
| `docs/AUDIT-Phase0-FINAL.md` | Phase 0 | 🔴 Pre-Phase 1.0; keep for audit |
| `docs/PHASE1-6-SERVICE-DIAGRAM.md` | Phase 1.6 | 🔴 Phase 1.6 snapshot; current diagram is in `docs/PHASE2-8-METRICS.md` |
| `docs/PHASE1-AUDIT-2026-08-17.md` | Phase 1.0 | 🟡 2026-08-17 audit; one-time snapshot |

> **Примечание:** каждый legacy файл в шапке должен иметь баннер
> `[LEGACY]` с датой и причиной archive (issue #292 follow-up).
> Пока баннеры не проставлены — ориентируйтесь на эту таблицу.

## 📂 Другие директории

### `evidence/`

[`evidence/README.md`](evidence/README.md) — что сюда класть и когда.

Короткая версия: сюда пишут **CI / cron jobs и humans** во время
investigation. Файлы либо ADR-копии (для PR review), либо runtime
verification snapshots (`*_tests.txt`, `*_compose_config.txt`,
`*_docker_ps_state.txt`, `*_runtime_verification.txt`).
Retention: **не коммитим** в main; PR-by-PR; clean up после merge.

### `docs/decisions/`

См. раздел «Архитектурные решения (ADR)» выше.

### `backups/`

Postgres-бэкапы (daily cron, см. `scripts/backup_database.sh`).
**Не редактировать вручную.** `.gitignore` исключает содержимое.

### `data/`

Test fixtures и snapshots для `test_apply_corporate_actions.py`,
`test_peak_equity_tracker.py`. Реальные данные не хранить —
fixture-only.

## 🔍 Где искать что

| Я ищу | Куда смотреть |
|---|---|
| Что это за проект | [`README.md`](README.md) |
| Как внести вклад | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Как запустить тесты / мокать SDK | [`docs/TESTING.md`](docs/TESTING.md) |
| Production-like bring-up | [`docs/QUICKSTART.md`](docs/QUICKSTART.md) |
| Failure mode → fix | [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) |
| Incident response (security) | [`docs/SECURITY.md`](docs/SECURITY.md) + [`docs/RUNBOOK.md`](docs/RUNBOOK.md) |
| Threat model | [`docs/SECURITY.md`](docs/SECURITY.md) |
| Position sizing | [`docs/POSITION-SIZING.md`](docs/POSITION-SIZING.md) |
| Prometheus / Grafana | [`docs/PHASE2-8-METRICS.md`](docs/PHASE2-8-METRICS.md) |
| Roadmap / Phase status | [`docs/PHASE2-ROADMAP.md`](docs/PHASE2-ROADMAP.md) |
| .107 deployment variables | [`docs/DEPLOY-ENV.md`](docs/DEPLOY-ENV.md) |
| Архитектурное решение | [`docs/decisions/`](docs/decisions/) |
| Audit / phase history | `docs/AUDIT-*.md`, `docs/PHASE1-*.md` (legacy) |
| Runtime verification snapshots | [`evidence/`](evidence/README.md) |
| Postgres backups | `backups/` (gitignored) |

## 📋 Maintenance

Этот индекс обновляется при добавлении / удалении / archive'е
документа. Когда вы:

- **Добавляете новый документ** — добавьте строку в соответствующую
  таблицу + строку в «Где искать что».
- **Архивируете документ** — перенесите строку в раздел «Legacy»,
  добавьте `[LEGACY]` баннер в шапку файла (issue #292 follow-up).
- **Удаляете документ** — удалите строки + проверьте, что нет
  broken cross-links в остальных файлах (`grep -r "ARCHIVE_NAME" --include="*.md"`).

---

Last updated: 2026-08-27 (PRs #303, #304, #305)