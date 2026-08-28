# Evidence

> Директория для runtime verification snapshots и ADR-копий.
> Зеркало [`docs/decisions/`](../docs/decisions/) для удобства review.

## Что сюда класть

### ADR-копии (`0006-*.md`, `0007-*.md`)

Канонические ADR живут в `docs/decisions/`. Сюда копируются **на время
PR review**, чтобы ревьюеру не приходилось прыгать между
директориями. После мёрджа PR копия удаляется.

### Runtime verification snapshots (`NN_*.txt`)

Файлы, которые пишут CI / cron jobs / humans во время
investigation. Соглашение об именовании: `NN_short_slug.txt`, где
`NN` — двузначный порядковый номер (начиная с 01).

Типичные типы:

| Slug | Что внутри | Кто пишет |
|---|---|---|
| `*_compose_config.txt` | `docker compose config` вывод + разобранный prometheus block | Human investigator (PR review) |
| `*_compose_tests.txt` | вывод pytest test_compose_structure + summary | Human / CI |
| `*_docker_ps_state.txt` | `docker ps -a` состояние контейнеров на момент проблемы | Human investigator |
| `*_runtime_verification.txt` | ENVIRONMENT CONSTRAINT описания, repro для LXC-специфичных проблем | Human investigator |
| `*_full_tests.txt` | полный pytest output (>1500 строк), обычно для случаев когда нужны traceback'и | Human / CI |

## Что НЕ сюда

- **Реальные секреты** (`.env`, `*.key`, токены) — никогда.
  `.gitignore` исключает основные расширения, но если вы работаете
  с новым форматом — проверьте, что он не попадёт в коммит.
- **Postgres-бэкапы** — в `backups/` (отдельная директория, тоже gitignored).
- **Бинарные ассеты** (скриншоты Grafana, профилировщик heap dumps) —
  см. issue про артефакты; обычно прикладываются к PR comment,
  не в репо.
- **ML-модели, обученные веса, большие файлы данных** — в git LFS
  или внешнем хранилище, не здесь.

## Retention

| Тип | Retention | Cleanup |
|---|---|---|
| `0006-*.md`, `0007-*.md` (ADR-копии) | только на время PR review | Удалить в том же PR, что и ADR-merge, или отдельным `chore(cleanup)` PR |
| `01_*.txt`, `02_*.txt` (snapshots) | ad hoc | Удалить после закрытия issue, на которое snapshot ссылается |
| `_archive/` (если создадите) | indefinitely | never delete |

Если директория распухает (>30 файлов) — откройте `chore` issue
для cleanup wave.

## Convention

- **Naming:** `NN_short_slug.txt` или `NNNN-slug.md` (ADR-зеркало).
- **Header:** первая строка каждого файла — что внутри и кто/когда
  создал. Пример:
  ```
  # Evidence: docker compose config + prometheus block (PR #284 review)
  # Captured: 2026-08-27 by @alphard-maintainer
  ```
- **No real secrets** — даже в diff. Если в выводе есть токен —
  замените на `<REDACTED>` перед сохранением.
- **PR review only** — ADR-копии удаляются после мёрджа, не
  «улучшают историю» main.

## История содержимого

На 2026-08-27 (последний cleanup: PR #284 review wave):

| Файл | Назначение | Issue/PR |
|---|---|---|
| `0006-position-sizing.md` | ADR 0006 copy для review | PR #249 (Phase 2.2) |
| `0007-rebalance-scheduler.md` | ADR 0007 copy для review | PR #256 (Phase 2.4) |
| `01_compose_config.txt` | prometheus bind-mount repro | PR #284 (issue #283) |
| `02_compose_tests.txt` | compose structure tests output | PR #284 |
| `02_docker_ps_state.txt` | container state при issue #283 | PR #284 |
| `03_full_tests.txt` | pytest full output при LXC OOM repro | PR #284 follow-up |
| `03_runtime_verification.txt` | LXC AppArmor constraint + Prometheus healthcheck repro | PR #284 |

Все файлы помечены как `git status` clean по результатам cleanup PR.
Если вы видите файл, который не соответствует `NN_*.txt` или
`NNNN-*.md` — откройте issue.

## Связанные доки

- [`DOCS-INDEX.md`](../DOCS-INDEX.md) — общий индекс документации
- [`docs/decisions/`](../docs/decisions/) — канонические ADR
- [`docs/TROUBLESHOOTING.md`](../docs/TROUBLESHOOTING.md) — symptom→fix
- [`docs/RUNBOOK.md`](../docs/RUNBOOK.md) — operational procedures