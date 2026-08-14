# Runbook — Alphard Incident Response

> **Статус:** Phase 0.6 skeleton. Содержимое будет расширяться по мере того, как Risk Agent, monitoring и notification-каналы (Telegram / SMS) появятся в Phase 1+.
> **Связанные документы:** [SECURITY.md](SECURITY.md) (threat model + defense layers), [CONTRIBUTING.md](../CONTRIBUTING.md) (как сообщать об уязвимостях).

Этот runbook — операционная шпаргалка «что делать, когда сработал алерт или найдена уязвимость». Структура: классификация → kill-switches → rollback → контакты.

---

## 1. Incident Classification

Инциденты делятся на четыре уровня. Уровень определяет, кто реагирует и за сколько.

| Уровень | Триггер | Пример | Время реакции |
|---|---|---|---|
| **SEV-1 (Critical)** | Скомпрометирован broker token, bot торгует аномально (X trades/sec), DD > 5%, секрет ушёл в публичный git history | Tinkoff token в публичном issue, неожиданные сделки вне MOEX hours | **Немедленно** (auto-pause + alert юзеру) |
| **SEV-2 (High)** | Failed trades spike (5+ rejected/10 min), token usage anomaly, suspicious SSH login | 5 отказов подряд от Tinkoff API, SSH brute force на .107 | **≤ 1 час** |
| **SEV-3 (Medium)** | Risk gate violation в логах без исполнения, необычные LLM responses, single failed CI run с утечкой тестовых данных | Pydantic validator отклонил сделку, LLM выдал outlier signal | **≤ 24 часа** |
| **SEV-4 (Low)** | Минорные предупреждения (deprecation warning в логе, медленный Prometheus scrape, единичный flake в тестах) | `DeprecationWarning` от tinkoff-investments, flake8 W504 | **Следующий maintenance window** |

Правила эскалации:

- SEV-1 → немедленно halt + ping юзера через все доступные каналы (telegram + email + dashboard).
- SEV-2 → остановить автоматические действия (kill_switch `pause-bot`), разобраться до снятия с паузы.
- SEV-3 → собрать контекст, записать в decision lineage, решить в рабочем порядке.
- SEV-4 → backlog.

## 2. Kill Switches

Все kill-switches — **fail-safe**: дефолт = «выключено / остановлено», активация = явное действие пользователя.

| Switch | Где живёт | Что делает | Как активировать |
|---|---|---|---|
| **`alphard-bot.pause`** | `.env` (`BOT_PAUSED=true`) | Risk Agent отклоняет все orders, бот продолжает мониторинг | `docker compose stop alphard-bot` или set `BOT_PAUSED=true` + restart |
| **`alphard-net.isolate`** | docker network `alphard-net` | Отрезает бот от postgres + redis; оставляет только healthcheck | `docker network disconnect alphard-net alphard-bot` |
| **`alphard-token.rotate`** | Tinkoff UI + `.env` | Принудительная смена broker token + рестарт стека | Tinkoff Invest → Settings → Token → Revoke → вписать новый в `.env` → `docker compose up -d` |
| **`alphard-portainer.lockdown`** | Portainer .107 → RBAC | Временно блокирует все изменения в stack кроме read-only ops | Portainer UI → Settings → Disable API key для текущего пользователя |
| **`alphard-llm.cooloff`** | Coordinator config | Запрещает любые LLM-driven решения на N часов | `LLM_COOLOFF_HOURS=24` + restart |

> **Принцип:** kill-switches idempotent и stackable. Несколько switch могут быть активны одновременно; снятие их — тоже явное действие.

## 3. Rollback

Деплой Alphard = image + compose stack + Risk Agent config + `.env`. Rollback возможен на каждом из этих уровней.

### 3.1 Code rollback (изменения в `src/`)

```bash
# На .107 (Portainer host)
cd /opt/alphard  # или где лежит clone
git fetch origin
git checkout <previous-good-tag>  # теги создаются на каждый Phase
docker compose pull
docker compose up -d
```

Проверка: `docker compose ps` показывает `alphard-bot (healthy)`, `docker compose logs --tail=50 alphard-bot` не содержит ошибок инициализации.

### 3.2 Risk Agent config rollback (`.env`, RiskLimits)

```bash
# Сохранить текущее состояние
cp .env .env.broken.$(date +%Y%m%d_%H%M%S)

# Восстановить из заведомо рабочей версии
cp .env.good .env
docker compose restart alphard-bot
```

RiskLimits (`frozen=True`) нельзя менять без перезапуска контейнера; до перезапуска действует предыдущая копия в памяти процесса.

### 3.3 Postgres rollback (decision lineage / portfolio state)

```bash
# Только для read-only forensic, не для production!
docker compose exec postgres pg_dump -U alphard alphard > /tmp/alphard_pre_rollback.sql

# Восстановление из daily backup (Phase 2+):
docker compose exec -T postgres psql -U alphard alphard < /backup/alphard_YYYY-MM-DD.sql
```

### 3.4 Token rotation rollback (если новый token тоже скомпрометирован)

```bash
# Восстановить предыдущий .env (в нём старый token)
cp .env.backup_pre_rotation .env
docker compose restart alphard-bot
# Одновременно зайти в Tinkoff UI и revoke текущий token
```

### 3.5 Когда rollback НЕ помогает

- Если скомпрометирован весь .107 host (SSH brute force успешен) → это уже не rollback, это `incident_response/host_rebuild.md` (появится в Phase 2). В текущем Phase 0.6 план: `ssh-keygen -R .107` локально, перевыпустить ключи, переустановить Portainer stack с нуля.
- Если скомпрометирован GitHub account → см. [SECURITY.md §6](SECURITY.md#6-когда-стопор) + `git-filter-repo` для очистки истории.

## 4. Contact

| Роль | Кто | Канал | Когда писать |
|---|---|---|---|
| **Maintainer (Alphard)** | Александр (m0rtal) | GitHub @m0rtal, личные сообщения через GitHub | SEV-1, SEV-2, security advisories |
| **Tinkoff support** | Tinkoff Invest API | `https://tinkoff.ru/invest/`, чат в приложении, телефон горячей линии | SEV-1 (token compromise), нестандартные rejection codes |
| **GitHub Security** | GitHub | `https://github.com/security/advisories`, `security@github.com` | Уязвимости в зависимостях, compromised commit history |
| **Hosting / infra (.107)** | Portainer / VPS provider | Portainer UI logs, support провайдера | Host compromise, network outage |

> **Правило:** в первом сообщении об инциденте всегда указывать уровень SEV, временную метку (UTC), триггер и какие kill-switches уже активированы. Это ускоряет triage.

---

**Todo для Phase 1+:** добавить сюда post-mortem template, ссылку на decision lineage dump script, чек-лист «pre-mortem» для новых фич.