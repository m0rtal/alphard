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

**Частично реализовано (Phase 0.6 → 1.6):**
- ✅ `.env` исключён через `.gitignore` (защита от коммита секретов)
- ✅ `TINKOFF_SANDBOX_TOKEN fail-fast` в `src/main.py:29` (entrypoint sanity gate — бот не стартует без токена)
- ✅ **gitleaks-action** в `.github/workflows/ci.yml` — авто-скан каждого PR (commit `eab5e40`)
- ❌ `make init-env` (скрипт-обёртка) — не реализован, см. issue #12
- ❌ Tinkoff token rotate monthly — нет automation, manual user action (см. issue #12)
- 📅 Phase 2+: redact middleware для логов (сейчас есть только risk-gate check)

### Level 2: Network isolation

**Уже есть:**
- ✅ .107 Docker-only host (no SSH)
- ✅ Portainer на .107 с RBAC

**Частично реализовано:**
- ⚠️ Docker network в `docker-compose.yaml` — default bridge (не dedicated separate network). Минимальная изоляция, host не exposure'нут на 0.0.0.0.
- ✅ Tinkoff API calls — ТОЛЬКО HTTPS через `requests` library, default cert verification ON (`src/broker/tinkoff_account.py`)
- ✅ Postgres — bound в compose, internal network only (не exposed наружу)
- ❌ Dedicated bridge `alphard-net` — нет отдельного network, всё в default bridge
- 📅 Phase 2+: dedicated bridge, separate redis net, outbound allowlist (нет файрвола в compose)

### Level 3: Broker-specific hardening

**Tinkoff sandbox vs real:**
- ✅ Sandbox token для testing
- ✅ Real token через `.env`, никогда в коде
- ✅ **Mandatory:** `TinkoffConfig` отказывает `sandbox=False` (Phase 1.3 fail-fast, `src/broker/tinkoff_account.py`)
- ❌ IP whitelist (если Tinkoff поддерживает) — Tinkoff sandbox API НЕ поддерживает IP-allowlist (проверено)

**Order validation:**
- ✅ Risk gate hard limits (max position, DD, sector)
- ✅ RiskGate refuse MarketOrder with placeholder price (`qty > 1 AND price == 1`) → `RISK_MARKET_ORDER_NO_QUOTE` (issue #11 closed by PR #18 — fix landed in main via commit `8e8d400` after PR was closed-not-merged; original PR #18 was bypass-merged to avoid duplicate commit)
- ✅ RiskGate pydantic v2 with `extra="forbid"` — no silent ALLOWED=true with violations
- ✅ TOCTOU guard `Coordinator._validate_state_for_execute()` ≤ 100ms via `time.monotonic()` (issue #15, PR #17)
- 📅 Phase 2+: tradeable tickers whitelist, daily volume cap (см. issue #26)

### Level 4: Monitoring & detection

**Частично реализовано:**
- ✅ `src/data/quality/audit.py` — `PostgresAuditLog` + `InMemoryAuditLog` (audit_log table в Postgres)
- ✅ `src/main.py:81 _daily_sync_loop()` — daily 20:00 MSK watchdog thread
- ❌ Prometheus metrics endpoint — нет в compose, Phase 3+
- ❌ Decision lineage в Postgres — частично (audit_log есть, но не structured_jsonb lineage)
- 📅 Phase 2+: anomaly detection, loss alerts, daily volume cap alerts

**Phase 2+:**
- 📅 Self-audit cron каждые 6ч (проверка 5 layers intact) — не реализовано, отложено
- 📅 Anomaly detection X trades/min — Phase 2+
- 📅 Loss alerts daily_pnl < -1% → telegram/SMS — Phase 2+
- 📅 DD alerts drawdown > 3% → critical — Phase 2+
- 📅 Failed trades spike (5+ rejected/10мин) — Phase 2+
- 📅 Token usage anomaly > N API/min — Phase 2+
- 📅 Unusual hours (вне MOEX) → CRITICAL — Phase 2+

#### Level 4.1 — Monitoring profile (Prometheus + Grafana) LAN exposure (issue #55)

**Surface:**
- Grafana binds host port 3300 via `network_mode: host` (compose workaround
  for the broken bridge-NAT on the .107 Docker daemon — see
  `docker-compose.yaml:190-194`).
- Prometheus binds host port 9090 via standard bridge port-mapping.
- Both containers are deployed by `scripts/deploy_monitoring.sh` and live
  on the .107 host's LAN.

**Threats:**
1. **Anonymous Grafana access** — any LAN peer that reaches `:3300` would
   see every provisioned dashboard (`alphard_heartbeats_total`,
   `alphard_open_positions`, etc.) without authentication. Status:
   **mitigated** in issue #55 — `GF_AUTH_ANONYMOUS_ENABLED` removed and
   forbidden by the `ops-policy` CI guard. `/api/search` returns 401
   without auth.
2. **Literal admin password in git** — the original `deploy_monitoring.sh`
   hardcoded `GF_SECURITY_ADMIN_PASSWORD=alphard` (committed 2026-08-19).
   Status: **mitigated** in issue #55 — the script now sources
   `GRAFANA_ADMIN_PASSWORD` from `$ALPHARD_ENV_FILE` (default `./.env`)
   and refuses to run if the variable is missing or set to the historical
   literal. Note that the literal is **still in git history**; the
   password must be rotated on the live Grafana instance (owner action,
   tracked in issue #55 acceptance criteria).
3. **Cross-stack secret drift** — pre-fix `scripts/deploy_monitoring.sh`
   and `docker-compose.yaml` had different Grafana security postures
   (anonymous+literal vs. .env+auth-only). Status: **resolved** — both
   now require .env-sourced authentication. No more drift.

**Defenses:**
- `scripts/deploy_monitoring.sh` reads `$GRAFANA_ADMIN_PASSWORD` from
  `$ALPHARD_ENV_FILE` (default `./.env`) and refuses to run if missing
  or set to the historical literal `alphard`.
- `.github/workflows/ci.yml` `ops-policy` job fails the build if
  `scripts/` contains a literal `GF_SECURITY_ADMIN_PASSWORD=<value>` or
  `GF_AUTH_ANONYMOUS_ENABLED=true`.
- `docs/SECURITY.md` (this file) records the threat model so future
  maintainers know which knobs are forbidden and why.

**Operator actions (out of band for code PRs):**
- **Rotate the live Grafana admin password on .107.** The historical
  literal `alphard` is permanently in the public git history; rotation
  is the only way to invalidate it.
- **Restrict host:3300 + host:9090 at the LAN firewall.** The current
  setup exposes Prometheus query API and Grafana UI to anyone on the
  same subnet. A firewall rule limiting access to the Hermes host's
  IP (`192.168.1.103`) and an operator workstation is recommended for
  defense in depth, but is not enforced in code.

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
| 11 | Anomaly detection alerts | Prometheus + AlertManager | TODO Phase 3 |
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
