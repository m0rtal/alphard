# OPSEC Plan for Alphard

**Дата:** 2026-08-13
**Контекст:** живой торговый бот = реальные деньги = реальные угрозы
**Принцип:** assume breach, defense in depth, least privilege, audit everything

---

## Содержание

Документ состоит из шести разделов: **§1 Threat Model** (активы, угрозы по STRIDE, adversaries), **§2 Defense Layers** (5 уровней — от secrets hygiene до recovery), **§3 Конкретные меры** (P0 / P1 / P2 по приоритету), **§4 Что я делаю сейчас (Phase 0.6 — OPSEC basics)**, **§5 Honest limitations** (что OPSEC не покрывает), **§6 Когда стопор** (triggers для rotate / pause / halt). Incident response playbook см. в [docs/RUNBOOK.md](RUNBOOK.md).

---

## 1. Threat Model

### 1.1 Активы

| Актив | Ценность | Risk |
|---|---|---|
| **Tinkoff API token** | Доступ к деньгам | КРИТИЧНО |
| **Postgres credentials** | Portfolio state, trade history | HIGH |
| **Redis password** | Caching, rate limits | MEDIUM |
| **LLM API keys** (routerai) | Operational, ~$50/mo | LOW |
| **GitHub repo** | Public, IP exposure | LOW |
| **Server credentials** (.107, .110) | Infrastructure access | HIGH |
| **Decision lineage** | Audit trail, forensics | MEDIUM |
| **Bot config** (.env) | Trading rules, risk limits | MEDIUM |

### 1.2 Угрозы (STRIDE)

| Threat | Vector | Impact | Likelihood |
|---|---|---|---|
| **Secret leak в git** | Accidental commit, gitleaks bypass | CRITICAL | MEDIUM |
| **Broker token compromise** | Token theft via logs/error msgs | CRITICAL | MEDIUM |
| **Database dump** | SQL injection, leaked backup | HIGH | LOW |
| **Server compromise** | SSH brute force, CVE | HIGH | LOW |
| **LLM prompt injection** | Malicious news/macro data | MEDIUM | HIGH |
| **Insider (LLM agent) mistake** | Wrong trade decision | CRITICAL | MEDIUM |
| **Market data poisoning** | Corrupted Tinkoff/MOEX feed | HIGH | LOW |
| **DDoS / network** | Tinkoff API unavailable | MEDIUM | LOW |
| **Time bomb** | Old code triggers bad action | HIGH | LOW |
| **Replay attack** | Old orders re-executed | HIGH | LOW |

### 1.3 Adversaries (realistic for this project)

- **Casual attacker** — scripts, opportunistic. Likely.
- **Semi-skilled attacker** — knows MOEX/Tinkoff API. Possible.
- **Sophisticated attacker** — financial industry background, targets specifically. Unlikely (not enough money).
- **Insider** — user themselves (accidental action) OR AI agent (LLM mistake). LIKELY.
- **Compromised dependency** — tinkoff-investments SDK, Riskfolio compromised. LOW but real.

---

## 2. Defense Layers (5 levels)

### Level 1: Secrets hygiene

**Уже сделано:**
- ✅ `.env.example` без секретов
- ✅ `.gitignore` запрещает .env, *.pem, *.key
- ✅ gitleaks pre-commit hook
- ✅ GitHub secret scanning (если включишь в Settings)

**Phase 1 нужно:**
- 🔄 `.env` создаётся ТОЛЬКО через `make init-env` скрипт (с chmod 600)
- 🔄 Tinkoff token rotate каждый месяц (настраиваемый в .env)
- 🔄 Никаких секретов в логах — даже частично. Используй redact middleware.
- 🔄 `make audit-secrets` — CI/CD проверка перед каждым PR

### Level 2: Network isolation

**Уже есть:**
- ✅ .107 Docker-only host (no SSH)
- ✅ Portainer на .107 с RBAC

**Phase 1 нужно:**
- 🔄 Alphard stack в **отдельной Docker network** (alphard-net, не bridge)
- 🔄 Никаких прямых ports наружу кроме healthcheck (8080 → только localhost на .107)
- 🔄 postgres доступен ТОЛЬКО из alphard-bot контейнера, не наружу
- 🔄 redis same
- 🔄 Tinkoff API calls — ТОЛЬКО через HTTPS, проверка сертификата
- 🔄 Outbound: allowlist только tinkoff.ru, moex.com (NO general internet)

### Level 3: Broker-specific hardening

**Tinkoff sandbox vs real:**
- ✅ Sandbox token для testing
- ✅ Real token через `.env`, никогда в коде
- 🔄 **Mandatory:** real token ТОЛЬКО в production stack, sandbox token в dev/sandbox stack
- 🔄 IP whitelist (если Tinkoff поддерживает) — ограничить API access с IP .107

**Order validation:**
- ✅ Risk gate hard limits (max position, DD, sector)
- 🔄 **Whitelist tradeable tickers** — НЕ allow bot to trade unknown instruments
- 🔄 **Daily volume cap** — бот не может торговать > X% от своего обычного объема

### Level 4: Monitoring & detection

**Уже есть:**
- ✅ Prometheus metrics endpoint
- ✅ Decision lineage в Postgres

**Phase 1 нужно:**
- 🔄 **Anomaly detection** — если бот внезапно делает X trades/min → alert
- 🔄 **Loss alerts** — если daily_pnl < -1% → telegram/SMS alert
- 🔄 **DD alerts** — drawdown > 3% → critical alert (auto-stop если > 5%)
- 🔄 **Failed trades spike** — если 5+ rejected orders за 10 минут → investigate
- 🔄 **Token usage anomaly** — если Tinkoff API calls > N/min → possible compromise
- 🔄 **Unusual hours** — trades вне MOEX hours → CRITICAL alert
- 🔄 **Self-audit cron** — каждые 6 часов: проверка что все 5 layers intact

### Level 5: Recovery & incident response

**Phase 2+:**
- 🔄 **Auto-pause** при любом CRITICAL alert → бот останавливается, ждёт user
- 🔄 **Backup Postgres daily** — separate volume, off-host
- 🔄 **Snapshot config** — каждое изменение risk_limits → versioned в git
- 🔄 **Runbook** для каждого CRITICAL alert (что делать юзеру)
- 🔄 **Post-mortem template** — после каждого инцидента

---

## 3. Конкретные меры (по приоритету)

### P0 (сделать перед live)

| # | Мера | Где | Статус |
|---|---|---|---|
| 1 | Secrets в git scan в CI | .github/workflows/ci.yml | TODO |
| 2 | Risk gate 95% coverage | tests/ | ✅ Phase 0 |
| 3 | Network isolation (alphard-net) | docker-compose | ✅ Phase 0 |
| 4 | `.env` permissions (chmod 600) | Makefile init | TODO |
| 5 | Tinkoff token rotate monthly | config + reminder | TODO |
| 6 | Postgres password > 16 chars | .env.example | TODO |
| 7 | Redis password > 16 chars | .env.example | TODO |
| 8 | NO direct ports to outside | docker-compose ports | ✅ Phase 0 |
| 9 | HTTPS only для Tinkoff | code | TODO |
| 10 | Decision lineage в Postgres | code + schema | TODO Phase 1 |

### P1 (сделать в sandbox phase)

| # | Мера | Где |
|---|---|---|
| 11 | Anomaly detection alerts | Prometheus + AlertManager |
| 12 | Loss alerts (telegram/SMS) | Phase 4 |
| 13 | Failed trades spike detector | Phase 4 |
| 14 | Token usage anomaly | Tinkoff wrapper |
| 15 | Unusual hours detector | Coordinator |
| 16 | Self-audit cron (6h) | Phase 2 |
| 17 | Daily backup Postgres | Phase 2 |
| 18 | Whitelist tradeable tickers | Data Agent |
| 19 | Daily volume cap | Risk gate |

### P2 (сделать в live phase)

| # | Мера | Где |
|---|---|---|
| 20 | Auto-pause при CRITICAL | Coordinator |
| 21 | Post-mortem template | docs/ |
| 22 | IP whitelist (если Tinkoff) | broker config |
| 23 | Snapshot config в git | git tags |
| 24 | Penetration test (basic) | manual |
| 25 | Security audit (LLM-as-judge weekly) | Phase 4 |

---

## 4. Что я делаю сейчас (Phase 0.6 — OPSEC basics)

- [ ] `make init-env` скрипт с chmod 600
- [ ] CI workflow с gitleaks и pytest
- [ ] `.env.example` усилить (длинные passwords, комментарии)
- [ ] `docs/SECURITY.md` — публичный security policy
- [ ] `docs/RUNBOOK.md` skeleton — incident response

---

## 5. Honest limitations

**Что OPSEC НЕ покрывает:**
- Insider attack через LLM prompt injection (только detection)
- Zero-day в Tinkoff SDK
- User сам делает bad config и запускает
- Market manipulation upstream (Tinkoff/MOEX сами плохие)

**Что зависит от тебя (юзер):**
- Сильный пароль на сервер .107
- 2FA на GitHub
- 2FA на Tinkoff account
- Не запускать код с реальными секретами в dev

---

## 6. Когда стопор

Если вижу:
- Секреты в git history (даже в старых commits) → СТОП, rotate secrets, [git-filter-repo](https://github.com/newren/git-filter-repo)
- Бот торгует аномально (X trade/sec) → auto-pause + alert
- DD > 5% → halt + alert
- API calls spike без trading reason → investigate
