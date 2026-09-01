# Troubleshooting

> Symptom → cause → fix для оператора Alphard. Если вы попали сюда
> из Slack/audit issue и не нашли своего симптома — откройте
> issue с лейблом `bug` и добавьте запись в этот документ по итогам
> разбора.

**Где искать логи / состояние:**

```bash
# 1. Состояние контейнеров
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# 2. Логи бота за последние 200 строк
docker logs alphard-bot --tail 200 --since 30m

# 3. Логи всех сервисов одной командой
docker compose logs --tail 100 --no-color

# 4. Текущая activity в Postgres
docker exec alphard-postgres psql -U alphard -d alphard -c \
  "SELECT pid, state, query_start, left(query, 80) FROM pg_stat_activity ORDER BY query_start;"

# 5. CI — последний failed run
gh run list --branch main --limit 5 --json databaseId,conclusion,name
gh run view <id> --log-failed
```

Если вы ничего не понимаете в выводе — начните с §1 (Quick diagnostics),
затем §2 (Symptom → fix table).

---

## Содержание

1. [Quick diagnostics](#1-quick-diagnostics)
2. [Symptom → cause → fix table](#2-symptom--cause--fix-table)
3. [Tinkoff API errors](#3-tinkoff-api-errors)
4. [RiskGate violations explained](#4-riskgate-violations-explained)
5. [Database / Postgres issues](#5-database--postgres-issues)
6. [Live vs sandbox confusion](#6-live-vs-sandbox-confusion)
7. [Container / supervisor / memory](#7-container--supervisor--memory)
8. [CI failures](#8-ci-failures)
9. [Recovery procedures](#9-recovery-procedures)
10. [Adding a new entry](#10-adding-a-new-entry)
11. [See also](#see-also)

---

## 1. Quick diagnostics

Перед погружением в конкретный симптом — соберите «фотографию момента»:

```bash
# Snapshot: containers, disk, memory, PG connections
{
  echo "=== docker ps ==="
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}'
  echo
  echo "=== alphard-bot tail 50 ==="
  docker logs alphard-bot --tail 50 2>&1
  echo
  echo "=== alphard-bot env (sourced files / secrets only) ==="
  docker exec alphard-bot sh -c 'env | grep -E "^(ALPHARD_|TINKOFF_|LIVE_)" | sort'
  echo
  echo "=== postgres connections ==="
  docker exec alphard-postgres psql -U alphard -d alphard -tAc \
    "SELECT count(*) || ' conn, ' || count(*) FILTER (WHERE state='active') || ' active' FROM pg_stat_activity;"
  echo
  echo "=== alphard-bot metrics ==="
  curl -s http://127.0.0.1:8765/metrics | grep -E "^alphard_(uptime_seconds|tickers_in_universe|ohlcv_rows|process_resident_memory_bytes)"
  echo
  echo "=== disk ==="
  df -h /var/lib/docker /srv/alphard 2>&1 || true
} | tee /tmp/alphard-snapshot.txt
```

Сохраните `/tmp/alphard-snapshot.txt` и приложите к issue/PR — это
ускоряет разбор на порядок.

Если бот упал и не поднимается:

```bash
# Проверить, не висит ли он в restart loop
docker inspect alphard-bot --format '{{.State.Status}} {{.State.RestartCount}} {{.State.ExitCode}}'

# Логи предыдущего инстанса (если был restart)
docker logs alphard-bot --previous --tail 200
```

---

## 2. Symptom → cause → fix table

| # | Symptom | Log signature | Likely cause | Fix |
|---|---|---|---|---|
| 1 | `universe-coverage gauges = 0` | `_universe_metrics_loop: ALPHARD_PG_DSN not set; loop disabled` | `ALPHARD_PG_DSN` не проброшен в контейнер (issue #295) | См. [§6 Live vs sandbox](#6-live-vs-sandbox-confusion) + issue #295 (PR #296 fix). Local dev: bind-mount `/root/.env` через compose, либо `ENV_FILE=/root/.env` в `.env`. |
| 2 | Container exit code `137` | OOM kill в `docker inspect ... ExitCode=137` | `_backfill_supervisor_loop` memory profile превысил лимит (default 2 GB на сервис) | Снизить параллелизм backfill (`BACKFILL_CONCURRENCY`), либо увеличить `memory:` лимит в `docker-compose.yaml` alphard-bot. |
| 3 | `_daily_sync_loop` hangs, last_successful_run_at > 24h | `watchdog: _daily_sync_health never stamped after 24h` в логах | daily_sync subprocess висит > `DAILY_SYNC_SUBPROCESS_TIMEOUT=600s` | `docker logs alphard-bot --tail 200 | grep daily_sync timeout`; обычно чинится рестартом контейнера. Persistent: проверить сетевую связность до Postgres + Tinkoff API. |
| 4 | `BrokerError("no quote for SBER")` | `BrokerError: no quote in response for SBER` | Tinkoff API не вернул `last_prices` для FIGI (тикер неизвестен или рынок закрыт) | Проверить FIGI через Tinkoff console; для pre-market — дождаться открытия MOEX. |
| 5 | `BrokerError: live quote fetch failed for SBER: ...` | SDK exception после retry | Network flake / Tinkoff API 5xx | Retry обрабатывается внутри SDK; persistent failure — проверить `curl https://invest-public-api.tinkoff.ru/health`. |
| 6 | `RISK_DD: drawdown 15.7% exceeds limit 15%` | строка в `coordinator` audit log | Peak equity упал ниже threshold | Не «чинить» — это правильное поведение. Если false positive: проверить, что `peak_equity_<acc>.json` в `/var/lib/alphard/` не повреждён (см. §5). |
| 7 | `RISK_DAILY_LOSS: daily P&L ... exceeds limit` | строка в audit log | Daily loss limit сработал | По дизайну. Проверить `daily_pnl_basis_<acc>.json` на calendar mismatch (issue #220 — fix в conftest autouse). |
| 8 | `RISK_POSITION: invalid portfolio state (total_equity <= 0)` | строка в audit log | total_equity ≤ 0 → invariant violation | Stop bot, ручная проверка: `docker exec alphard-postgres psql -U alphard -d alphard -c "SELECT * FROM peak_equity_<acc>"`. Если corrupted — удалить файл и дать боту пересчитать на следующем cycle. |
| 9 | `RISK_SECTOR: projected TECH exposure exceeds limit` | строка в audit log | Концентрация в одном sector > лимита | Снизить размер позиции; не override без ревью. |
| 10 | `RISK_MARKET_ORDER_NO_QUOTE: intent.price=Decimal('1') is the sentinel` | строка в audit log | upstream sentinel цена (см. issue про intent construction) | Смотреть вызывающий код; sentinel должен быть заменён реальной quote перед `place_order`. |
| 11 | `pytest` skips 5+ tests `Live integration test — requires TINKOFF_*_TOKEN and ALPHARD_PG_DSN` | `SKIPPED [1] reason: Live integration test ...` | Ожидаемо на dev host без secrets | Установить `TINKOFF_SANDBOX_TOKEN` и `ALPHARD_PG_DSN` в env, см. `tests/test_coordinator_live.py:26`. |
| 12 | Container restart loop | `State: restarting RestartCount: N` в `docker ps` | Любой из симптомов выше + supervisor respawn | `docker logs alphard-bot --previous --tail 100` покажет причину первого падения. |
| 13 | `psycopg.OperationalError: connection refused` | строка в `universe_metrics_loop` / `daily_sync` | Postgres контейнер не поднялся или DNS resolution сломался | `docker ps alphard-postgres`; если нет — `docker compose up -d alphard-postgres`. Если есть, но conn refused — проверить `ALPHARD_PG_DSN` host (должен быть `alphard-postgres`, не `localhost`). |
| 14 | `psycopg.OperationalError: too many connections` | строка в любом db-вызове | Connection storm (issue с pg_store reconnect на каждый запрос) | Снизить `_universe_metrics_loop` frequency; проверить, что `pool_min_size` задан в DSN. |
| 15 | _(removed, PR #399)_ Grafana datasource `Prometheus` пропала | — | Grafana removed in PR #399 | Use alphard-web on port 8081 instead. |
| 16 | _(removed, PR #399)_ `prometheus.yml` empty → no scrape targets | — | Prometheus removed in PR #399 | alphard-bot /metrics still live on 8765 inside alphard-net. |
| 17 | Tinkoff API 401 UNAUTHENTICATED | `BrokerError` с trace `invest-public-api.tinkoff.ru → 401` | Token revoked / expired в Tinkoff console | Rotация токена в Tinkoff console → `~/.env` → Portainer StackUpdate. См. §3.1. |
| 18 | `_backfill_supervisor_loop: child pid=N reaped by something other than supervisor` | строка в логе | external `docker kill` или OOM killer | Если ожидаемый restart — игнорировать. Если recurrent — проверить memory profile (см. symptom #2). |
| 19 | Sandbox token rejected в `LIVE_TRADING=true` | `broker_status: REJECTED_LIVE_TRADING_FALSE` | Hardcoded production-mode + sandbox token | Не пытаться запустить live с sandbox token. См. §6. |
| 20 | `git push` → gitleaks red | `Secrets scan (gitleaks)` failed | Реальный секрет в diff | `git log -p --diff-filter=A -- "*.env" "*.yaml" "*.yml" "*.json"`; `git reset HEAD~1 --hard`; заменить secret через env; force-push. **Не** пытаться обойти gitleaks — на main CI зарежет. |

---

## 3. Tinkoff API errors

### 3.1 `401 UNAUTHENTICATED` / `40003` (token revoked)

**Симптом:** `BrokerError: live quote fetch failed for ...: <exception with text "401" or "UNAUTHENTICATED">`.

**Cause:** Tinkoff token истёк или отозван в Tinkoff console. Sandbox token действует ~3 месяца; real token — пока не отозван.

**Fix:**
1. Зайти в Tinkoff console → раздел «Токены» → отозвать старый, выпустить новый.
2. В **sandbox** режиме: скопировать новый sandbox token в `~/.env` (`TINKOFF_SANDBOX_TOKEN=...`).
3. В **production** режиме (.107): обновить secret в Portainer (Stack → Environment → `TINKOFF_REAL_TOKEN`). Portainer зашифрует secret при сохранении.
4. `bash scripts/quickstart.sh` или `docker compose restart alphard-bot`.

### 3.2 `429 TooManyRequests`

**Симптом:** повторые `BrokerError` с `429` в логах за короткий промежуток.

**Cause:** Rate limit Tinkoff Invest API (sandbox: ~100 req/min, real: ~500 req/min). Alphard сам не rate-limits; burst случается в `_universe_metrics_loop` если PG вернул много tickers одновременно.

**Fix:** подождать 60 секунд (limit reset). Persistent → снизить `UNIVERSE_METRICS_REFRESH_SECONDS` или `BACKFILL_CONCURRENCY`.

### 3.3 `5xx ServerError`

**Симптом:** `BrokerError: ... <5xx>`.

**Cause:** Tinkoff API down или degraded. SDK имеет retry; persistent failure = real outage.

**Fix:** проверить [status.tinkoff.ru](https://status.tinkoff.ru) или `/health` endpoint. Persistent → review ADRs на impact, при необходимости `LIVE_TRADING=false` до восстановления.

### 3.4 `no quote for SBER` в sandbox

**Симптом:** `BrokerError: no quote for SBER (figi=...)` на sandbox token.

**Cause:** Sandbox universe не совпадает с реальным. Sandbox token даёт sandbox account с sandbox universe — некоторые тикеры могут отсутствовать.

**Fix:** см. §6 — не пытаться торговать production-тикеры в sandbox. Для интеграционных тестов использовать `tests/test_coordinator_live.py` с sandbox token + reduced universe.

---

## 4. RiskGate violations explained

Все violations логируются в Postgres audit log (`audit_events` table) с
`event_type='RISK_GATE'` и строками вида `RISK_*: ...`. Полный список:

### 4.1 `RISK_POSITION`

| Подтип | Причина | Triage |
|---|---|---|
| `invalid portfolio state (total_equity <= 0)` | `PortfolioState` corrupted или не инициализирован | Проверить `peak_equity_<acc>.json` и `daily_pnl_basis_<acc>.json` на повреждения. См. §5. |
| `intent notional {N} = {pct}% of equity exceeds limit {L}%` | Размер ордера превышает `RISK_MAX_POSITION_PCT` | Уменьшить `quantity` в `CoordinatorConfig`; не override без ревью. |

### 4.2 `RISK_DD`

| Подтип | Причина | Triage |
|---|---|---|
| `invalid portfolio state (peak_equity <= 0)` | peak equity не persisted или corrupted | Восстановить из бэкапа (см. `scripts/backup_database.sh`); если нет — дать боту пересчитать на следующем cycle после `LIVE_TRADING=true`. |
| `drawdown {pct}% exceeds limit {L}%` | Equity упал ниже peak на >`RISK_MAX_DD_PCT` | **По дизайну.** Это защита. Проверить реальный drawdown, не override. |

### 4.3 `RISK_DAILY_LOSS`

| Подтип | Причина | Triage |
|---|---|---|
| `daily P&L {N} = {pct}% loss exceeds limit {L}%` | Day's realized + unrealized P&L < -`RISK_DAILY_LOSS_PCT` | **По дизайну.** Если false positive — проверить `daily_pnl_basis_<acc>.json` (calendar mismatch, issue #220). |

### 4.4 `RISK_SECTOR`

| Подтип | Причина | Triage |
|---|---|---|
| `projected {SECTOR} exposure {pct}% exceeds limit {L}%` | Концентрация в одном sector > `RISK_MAX_SECTOR_PCT` | Снизить размер позиции; проверить sector classification в intent. |

### 4.5 `RISK_MARKET_ORDER_NO_QUOTE`

`intent.price=Decimal('1') is the sentinel — refusing market order` —
upstream sentinel цена не была заменена реальной quote перед `place_order`.
Баг в вызывающем коде, не в RiskGate. Открыть issue с reproduction.

---

## 5. Database / Postgres issues

### 5.1 Postgres недоступен из бота

**Symptom:** `psycopg.OperationalError: ... connection refused` или `... could not translate host name "alphard-postgres"`.

**Triage:**

```bash
# 1. Контейнер жив?
docker ps alphard-postgres
# → должен быть "Up" с health=healthy

# 2. DSN изнутри бота видит правильный host?
docker exec alphard-bot sh -c 'echo "$ALPHARD_PG_DSN"'
# → должно быть host=alphard-postgres (НЕ host=localhost!)

# 3. Postgres принимает соединения?
docker exec alphard-postgres pg_isready -U alphard
# → "accepting connections"

# 4. DNS изнутри compose-сети
docker exec alphard-bot getent hosts alphard-postgres
# → должен вернуть IP
```

**Fix для "could not translate host name":**
Перепроверить `.env` (`ALPHARD_PG_DSN=...`) и `docker-compose.yaml` —
внутри compose-сети host должен быть service name (`alphard-postgres`),
не `localhost` или IP.

### 5.2 Schema migration failure

**Symptom:** бот упал при старте с `relation "X" does not exist` или
`column "Y" does not have a default`.

**Triage:**

```bash
# Проверить текущую schema version
docker exec alphard-postgres psql -U alphard -d alphard -tAc \
  "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 5;"
```

**Fix:** обычно это PR в Phase 2.x, где миграция забыта или неверна.
`git log --grep="migration"` + проверить `src/data/migrations/`.
Не пытаться руками `ALTER TABLE` без ревью миграции.

### 5.3 Connection storm

**Symptom:** `psycopg.OperationalError: too many connections` в логах,
PG connections count = max (`SELECT count(*) FROM pg_stat_activity`).

**Cause:** Код не использует connection pool, открывает новый conn на
каждый query. Известные hot-spots см. в issue по pg_store.

**Fix:**
1. Краткосрочно: `docker restart alphard-postgres` сбрасывает connections.
2. Долгосрочно: открыть issue с reproduction; не пытаться «починить»
   увеличением `max_connections` без root cause analysis.

### 5.4 Redis eviction / cache lost

Alphard не использует Redis как state store (Postgres — primary),
поэтому Redis eviction не критичен. Если на стеке включён
alphard-redis (см. `docker-compose.yaml`) — eviction просто означает
потерю in-memory cache, не state.

---

## 6. Live vs sandbox confusion

Alphard по умолчанию **не торгует на реальные деньги**. Это enforced
двумя независимыми механизмами:

1. **`LIVE_TRADING` env var** (default `false`). Если `false`,
   `Coordinator.place_order` short-circuit'ит на строке
   `src/coordinator.py:521` и возвращает
   `"REJECTED_LIVE_TRADING_FALSE"` **до** вызова broker.
2. **`TINKOFF_SANDBOX_TOKEN` vs `TINKOFF_REAL_TOKEN`** — `_make_broker`
   смотрит на оба, приоритет у sandbox если оба заданы
   (`src/data/tinkoff_md_loader.py:607`).

### Симптомы путаницы

| Сценарий | Что произойдёт | Как увидеть |
|---|---|---|
| Sandbox token + `LIVE_TRADING=true` | Ордер отправлен в sandbox universe, audit log пишет `broker_status: SUBMITTED` | `SELECT broker_status, count(*) FROM audit_events GROUP BY 1` |
| Real token + `LIVE_TRADING=false` | Short-circuit; audit пишет `REJECTED_LIVE_TRADING_FALSE` | то же |
| Real token + `LIVE_TRADING=true` | Реальные деньги! | — |

### Checklist перед включением `LIVE_TRADING=true`

- [ ] В `~/.env` или Portainer env: `TINKOFF_REAL_TOKEN=...` (НЕ sandbox)
- [ ] `ALPHARD_PG_DSN` указывает на production Postgres
- [ ] `RiskLimits` (см. `src/risk/gate.py`) проверены и устраивают
- [ ] На review одобрен ADR о переходе в live mode
- [ ] Проведен dry-run на sandbox с тем же universe

**Не пытаться** запустить live с sandbox token — Tinkoff Invest API
вернёт order на sandbox account, но audit log не отличит от production;
это классический foot-gun, и `RiskGate` от него **не защищает**.

---

## 7. Container / supervisor / memory

### 7.1 Restart loop

`docker ps` показывает `restarting`, `RestartCount` растёт.

**Triage:**

```bash
# Exit code + reason
docker inspect alphard-bot --format '{{.State.ExitCode}} {{.State.Error}}'
# OOM killer? См. §7.2.
# Python exception? docker logs alphard-bot --previous --tail 200

# Supervisor log
docker logs alphard-bot 2>&1 | grep -E "supervisor|respawn|exit"
```

### 7.2 OOM kill

`ExitCode=137` (128 + SIGKILL 9). Docker OOM killer убил процесс,
когда RSS превысил cgroup memory limit.

**Default лимиты** (см. `docker-compose.yaml` alphard-bot memory):
4 GB на production-like stack.

**Fix:**
1. Краткосрочно: `docker compose up -d alphard-bot` после увеличения `memory:`.
2. Долгосрочно: backfill с большим universe перерасходует память —
   снизить `BACKFILL_CONCURRENCY` (по умолчанию 4). Memory profile
   `_backfill_supervisor_loop` обычно умещается в 2 GB при
   `BACKFILL_CONCURRENCY=2`.

### 7.3 Supervisor deadlock (legacy)

**Symptom:** `alphard-bot` Up, но `_universe_metrics_loop`,
`_daily_sync_loop`, `_backfill_supervisor_loop` все молчат — нет
log lines > 30 минут.

**Cause:** pre-PR #47/#50 supervisor deadlock (3-layer fix).
Если вы на main — это не повторится. Если у вас старая ветка — обновиться.

**Triage:**

```bash
# Health endpoints
docker exec alphard-bot sh -c 'curl -s http://127.0.0.1:8765/metrics | grep alphard_uptime'
```

---

## 8. CI failures

### 8.1 `Tests + Coverage` failed

**Самый частый root cause:** новый код не покрыт тестами (coverage
< 95%). См. `docs/TESTING.md` §4 — `# pragma: no cover` это last resort.

**Fix:**
1. Локально воспроизвести: `pytest --cov=src --cov-report=term-missing`.
2. Coverage report покажет uncovered lines.
3. Написать тест (см. `docs/TESTING.md` §9 — куда добавлять).
4. **Не** снижать `--cov-fail-under` (issue #257).

### 8.2 `Black formatting` failed

`black --check src/ tests/` упал.

**Fix:**

```bash
pip install 'black>=26.3.1,<27.0'
black src/ tests/
git add -u && git commit -m "style: black reformat"
```

### 8.3 `Flake8` failed

`flake8 src/ tests/ --max-line-length=120 --extend-ignore=E203,W503`.

**Fix:** править руками (E501 = line too long → split; F401 = unused
import → удалить или использовать; E711 = `== None` → `is None`).
Не игнорировать через `# noqa` без комментария почему.

### 8.4 `Mypy --strict` failed

`mypy src/ --strict --ignore-missing-imports`.

**Fix:** см. `pyproject.toml [tool.mypy]` — relaxed для scaffold.
Точечные фиксы: добавить type hints, использовать `cast()`, убрать
`# type: ignore` без причины.

### 8.5 `SCA (pip-audit)` failed

**Symptom:** новая CVE в transitive dep.

**Fix:**
1. `pip-audit -r requirements.txt` локально для деталей.
2. Обновить версию в `requirements.txt` (если есть patch) — pin с
   новой минорной.
3. Если только major upgrade — открыть issue, **не** мерджить без
   проверки на breaking changes.

### 8.6 `Grafana secrets guard` failed

**Cause:** literal Grafana admin password или anonymous auth
re-enabled. CI ripgrep'ом ищет `admin_password:` без `${...}`
interpolation.

**Fix:** все Grafana secrets через `${GRAFANA_ADMIN_PASSWORD}`,
`${GRAFANA_SECRET_KEY}` env vars. Подробнее см. `docs/SECURITY.md`
и issue #55.

### 8.7 `Ops policy` failed

Найден literal Grafana password, anonymous auth, или `0.0.0.0/0`
trust в Postgres. См. `tests/test_init_postgres_sh.py` для
ожидаемого формата.

### 8.8 Test fails only on CI

**Самый частый root cause** в 2026-08-х: test зависит от host state
(`/root/.env` exists, `/var/lib/alphard` writable, и т.п.). CI runner
этого не имеет.

**Fix pattern** (issue #298): заменить runtime-skip
(`if returncode != 0: pytest.skip`) на **host-state skip**
(`if not Path("/root/.env").exists(): pytest.skip(...)`). Подробнее
в `docs/TESTING.md` §5.

---

## 9. Recovery procedures

### 9.1 Restart всего стека

```bash
cd ~/projects/alphard
docker compose down          # останавливает все, volumes сохраняет
docker compose up -d         # поднимает обратно
bash scripts/quickstart.sh   # альтернатива: full smoke
```

### 9.2 Restart одного сервиса

```bash
docker compose restart alphard-bot
# или с пересборкой образа
docker compose up -d --build alphard-bot
```

### 9.3 Drain stuck order

1. `docker exec alphard-bot sh -c 'curl -s http://127.0.0.1:8765/health'` →
   посмотреть состояние.
2. Если есть pending order: `SELECT * FROM orders WHERE status='NEW'`.
3. **Никогда** не отменять ордер руками через Tinkoff console без
   проверки, что бот не пытается его repost — будет дубль.
4. Правильный путь: `LIVE_TRADING=false` → бот перестаёт place_order;
   затем отменить stuck order через Tinkoff console; затем
   `LIVE_TRADING=true` снова.

### 9.4 Roll back миграцию

**Не делать** без ADR. См. `src/data/migrations/` — каждая миграция
должна иметь DOWN-секцию в комментариях (если reversible). Если
DOWN нет — это forward-only change, откатывать через новую миграцию,
не через SQL напрямую.

### 9.5 Восстановить peak_equity / daily_pnl_basis

```bash
# Из бэкапа
ls /mnt/appdata/alphard-backups/ | tail -5
# выбрать дату, распаковать, скопировать в /var/lib/alphard/

# Или дать боту пересчитать:
rm /var/lib/alphard/peak_equity_<acc>.json
docker restart alphard-bot
# На следующем supervisor cycle (≤5 минут) бот пересчитает peak.
```

**Warning:** бот во время пересчёта **не** имеет исторического peak,
поэтому DD-лимит работает только от текущего equity. Это безопасно
(max-DD считается как `(peak - equity)/peak`, а без peak = 0%), но
на короткий период бот более «строг».

### 9.6 Перевести стек в read-only mode (аварийная остановка торговли)

```bash
# 1. Остановить place_order
docker exec alphard-bot sh -c 'export LIVE_TRADING=false'
docker restart alphard-bot
# Убедиться: docker logs alphard-bot --tail 50 | grep "LIVE_TRADING=false"

# 2. Оставить мониторинг и сбр данных
docker exec alphard-bot curl -s http://127.0.0.1:8765/metrics > /tmp/metrics.txt

# 3. (опционально) остановить бота полностью
docker compose stop alphard-bot
```

Read-only mode не трогает Postgres, Prometheus, Grafana — мониторинг
продолжает работать.

---

## 10. Adding a new entry

Нашли failure mode, которого нет в §2? Добавьте строку в таблицу:

1. Проверьте `git log --grep="<keyword>"` и `gh issue list --search="..."`
   — может, уже обсуждалось, но не попало в таблицу.
2. Запустите `docker logs alphard-bot 2>&1 | grep <signature>` —
   воспроизводится ли симптом по log signature?
3. Добавьте строку в §2 с **конкретной** log signature (не общим
   описанием) — оператор должен мочь grep'нуть и найти свой случай.
4. Если fix требует code change — откройте issue, **не** правите код
   в этом PR. Документ — это index, не source of truth.
5. Cross-link: если новый симптом связан с конкретным issue (#N) или
   PR (#N) — укажите в столбце «Fix».

---

## See also

- `docs/RUNBOOK.md` — happy-path operational procedures
  (start/stop/monitor), отличие от troubleshooting — RUNBOOK не
  описывает failure modes.
- `docs/SECURITY.md` — threat model + defense layers. Все
  security-related симптомы (token leak, auth bypass) описаны там,
  не здесь.
- `docs/TESTING.md` — как писать тесты, в т.ч. для новых симптомов
  (issue #298 — pattern host-state skip).
- `docs/decisions/0006-position-sizing.md`, `0007-rebalance-scheduler.md` —
  ADR-паттерн для архитектурных решений. Если fix требует
  архитектурного решения — открывайте ADR, а не правку кода.