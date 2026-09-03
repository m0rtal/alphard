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
| **Rate-limit secrets** (none — token bucket is in-process after PR #426) | Caching, rate limits | MEDIUM |
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
- 📅 Phase 2+: dedicated bridge, in-process rate-limit channel per shard, outbound allowlist (нет файрвола в compose)

#### Level 2.1 — Postgres pg_hba.conf trust posture (issue #97)

**Surface:**
- `docker/entrypoint.sh` runs `init_schema()` (idempotent, reads
  `src/data/schema.sql`) on every bot startup, BEFORE `auth_probe()`,
  to prepend a `host all all <CIDR> trust` rule to `pg_hba.conf` so the
  bot can authenticate under Docker-internal DNS even when scram
  hashes drift (recovery safety net — see issues #73 and #347). Note:
  the pre-#347 compose `pg-init` sidecar was dropped because its
  single-file bind-mounts rendered as directories on PVE LXC, breaking
  schema application.
- `scripts/init_postgres.sh` mirrors the same rule for off-compose
  recovery runs (LEGACY path — see the script's own docstring).
- The legacy CIDR was `192.168.0.0/16` — ~65k LAN addresses covered,
  including any peer that might accidentally reach `alphard-postgres`
  via port-forwarding or a future bridge misconfig.

**Threats:**
1. **LAN credential theft** — anyone on `.107`'s /16 segment with
   reach to the postgres container could read or write the entire DB
   (`decision_log`, `ohlcv_daily`, `ticker_universe`,
   `corporate_actions`) without a password. Status: **mitigated** in
   issue #97 — default CIDR narrowed to `172.16.0.0/12` (Docker
   bridge range only).
2. **Public exposure via misconfig** — historical `0.0.0.0/0` rule
   (commit ~2026-08-18) would have exposed Postgres to the entire
   internet if the container port were ever host-published. Status:
   **mitigated** — explicit `sed -i '/^host all all 0\.0\.0\.0\/0
   trust$/d'` removes any legacy `0.0.0.0/0` rule before the new
   scoped rule is added.

**Defenses:**
- `docker/entrypoint.sh` (`init_schema()`) and `scripts/init_postgres.sh`
  both default `POSTGRES_TRUST_SUBNET=172.16.0.0/12` (RFC1918 Docker
  bridge range). Operators can override per-deploy via `.env` if their
  bridge subnet differs.
- Both files strip the legacy `192.168.0.0/16` rule on the next
  redeploy — no manual cleanup required.
- The bot's primary auth path is **password** (`POSTGRES_PASSWORD`
  sourced from `.env`, fail-fast at startup). Trust is a recovery
  fallback only; the bot does not depend on it.

**Operator actions (out of band for code PRs):**
- After redeploy, verify with
  `psql -h alphard-postgres -U alphard -d alphard -c "SELECT type,
  auth_method FROM pg_hba_file_rules WHERE address NOT IN ('localhost',
  '::1')"`: no row should have `address='192.168.0.0/16'` and
  `auth_method='trust'`.
- If your `alphard-net` bridge lives outside `172.16.0.0/12`
  (uncommon — Docker defaults to `172.17-32.0.0/16`), set
  `POSTGRES_TRUST_SUBNET` explicitly in `.env`.

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
- ❌ Prometheus metrics endpoint _(replaced by `alphard-web` (PR #394) reader; Prometheus scraper removed, PR #399)_
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

#### Level 4.1 — Monitoring profile (Prometheus + Grafana) — REMOVED, PR #399 (issue #55)

> **Archaeology banner:** the Prometheus + Grafana stack described
> below was **removed** in PR #399 (2026-08-31). `alphard-web`
> (PR #394) on `.107:8081` is the replacement observability surface;
> it requires `ALPHARD_WEB_TOKEN` for `/api/*` endpoints (PR #406 /
> #411). This section is retained as a historical threat-model record
> for the removed stack — see §4.2 for the current `alphard-web`
> threat model. _(All bullet points below reference services removed
> in PR #399; they are pinned here for the regression guard and the
> git-history audit trail.)_

**Surface (historical, removed in PR #399):**
- Grafana bound host port 3300 via `network_mode: host` (compose
  workaround for the broken bridge-NAT on the .107 Docker daemon —
  see `docker-compose.yaml:190-194`). _(Removed, PR #399.)_
- Prometheus bound host port 9090 via standard bridge port-mapping.
  _(Removed, PR #399.)_
- Both containers lived on the .107 host's LAN via the Portainer Stack
  defined in `docker-compose.yaml` (issue #228 / PR #228, 2026-08-25).
  _(Removed, PR #399; the Portainer Stack itself was reorganized
  post-#399 to omit these services.)_

**Threats (historical, removed in PR #399):**
1. **Anonymous Grafana access** _(removed, PR #399)_ — any LAN peer
   that reached `:3300` would see every provisioned dashboard
   (`alphard_heartbeats_total`, `alphard_open_positions`, etc.) without
   authentication. Status: **mitigated** in issue #55 —
   `GF_AUTH_ANONYMOUS_ENABLED` removed and forbidden by the
   `ops-policy` CI guard. `/api/search` returned 401 without auth.
2. **Literal admin password in git** _(removed, PR #399; literal
   remains in git history, reachable via git-filter-repo)_ — the
   original `deploy_monitoring.sh` hardcoded
   `GF_SECURITY_ADMIN_PASSWORD=alphard` (committed 2026-08-19).
   Status: **mitigated** in issue #55 — the script was rewritten to
   source `GRAFANA_ADMIN_PASSWORD` from `$ALPHARD_ENV_FILE` (default
   `./.env`) and refused to run if the variable was missing or set to
   the historical literal. The literal is **still in git history**
   but the script itself is now deleted (B3 cleanup, 2026-08-26)
   because the .107-specific deploy workaround it implemented is no
   longer needed: PR #228 moved observability under the standard
   `docker compose` flow (which configured port mapping correctly
   even on the .107 daemon) and Portainer StackUpdate is the
   production deploy path. The literal in git history is reachable
   by the same git-filter-repo procedure used for other historical
   leaks (tracked in issue #55 acceptance criteria).
3. **Cross-stack secret drift** _(resolved, services removed in
   PR #399)_ — pre-fix `scripts/deploy_monitoring.sh` and
   `docker-compose.yaml` had different Grafana security postures
   (anonymous+literal vs. .env+auth-only). Status: **resolved** —
   both required .env-sourced authentication. No more drift.

**Defenses (historical, removed in PR #399):**
- `scripts/deploy_monitoring.sh` _(removed, PR #399)_ read
  `$GRAFANA_ADMIN_PASSWORD` from `$ALPHARD_ENV_FILE` (default
  `./.env`) and refused to run if missing or set to the historical
  literal `alphard`.
- `.github/workflows/ci.yml` `ops-policy` job fails the build if
  `scripts/` contains a literal `GF_SECURITY_ADMIN_PASSWORD=<value>`
  or `GF_AUTH_ANONYMOUS_ENABLED=true`. _(Regression guard: the gate
  is still live post-#399 to catch any future PR that accidentally
  re-introduces a monitoring script with the historical defaults.)_
- `docs/SECURITY.md` (this file) records the threat model so future
  maintainers know which knobs are forbidden and why.

**Operator actions (out of band for code PRs, historical):**
- **Rotate the live Grafana admin password on .107** _(no longer
  applicable; service removed, PR #399)_. The historical literal
  `alphard` is permanently in the public git history; rotation is
  the only way to invalidate it. _(Action recommended at the time
  of removal; no ongoing operator action required post-#399.)_
- **Restrict host:3300 + host:9090 at the LAN firewall** _(no
  longer applicable; ports no longer bound, PR #399)_. The current
  setup exposes Prometheus query API and Grafana UI to anyone on
  the same subnet. A firewall rule limiting access to the Hermes
  host's IP (`192.168.1.103`) and an operator workstation was
  recommended for defense in depth, but was not enforced in code.
  _(Superseded by §4.2 `alphard-web` threat model — the LAN-exposed
  surface is now `:8081` behind `ALPHARD_WEB_TOKEN`.)_

#### Level 4.2 — `alphard-web` operator UI LAN exposure (PR #394, PR #406, PR #411)

**Surface (current, post-#399):**
- `alphard-web` binds host port `8081` on `.107` and serves the
  operator dashboard (PR #394). HTML root path is auth-open so the
  login prompt can render; `/api/*` endpoints require a valid bearer
  token (PR #406 / #411).

**Threats (current):**
1. **Anonymous `/api/*` access** — any LAN peer that reaches `:8081`
   would see live positions, decision lineage, and audit-log entries
   if the bearer-token gate were bypassed. Status: **mitigated** in
   PR #406 / #411 — every `/api/*` route runs `check_auth()` which
   returns 401 without a valid `Authorization: Bearer <token>`
   header. The HTML root path returns the login prompt, not the
   dashboard; the dashboard is loaded via `/api/dashboard` after
   the JS helper stores the token (PR #414, `src/web/static/index.html`
   `api()` helper).

**Defenses (current):**
- `src/web/server.py:check_auth()` short-circuits `/api/*` requests
  without a valid `ALPHARD_WEB_TOKEN` to a `401` response (PR #406).
- The HTML root path is exempt from `check_auth()` so the login
  prompt can render; once the operator supplies a token, it is
  stored in `sessionStorage` and sent as `Authorization: Bearer <token>`
  on every subsequent fetch (PR #414).
- A 401 from any fetch clears the stored token so a wrong or
  rotated token surfaces a re-prompt instead of silently throwing.
- `tests/test_411_auth_gate_js_wiring.py` pins the wire-up:
  `api()` helper must read from `sessionStorage` and attach the
  header; `test_token_prompt_on_first_load` confirms the prompt
  fires once on first load.

**Operator actions (out of band for code PRs):**
- **Set `ALPHARD_WEB_TOKEN` to a strong random value** in
  `/root/.env` on `.107`. The default value (if any) is rejected
  by `check_auth()`; rotation is the only way to invalidate a
  leaked token.
- **Restrict host:8081 at the LAN firewall.** The current setup
  exposes the `alphard-web` UI to anyone on the same subnet. A
  firewall rule limiting access to the Hermes host's IP
  (`192.168.1.103`) and operator workstations is recommended for
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
| 7 | Redis password > 16 chars (n/a after PR #426) | n/a | REMOVED |
| 8 | NO direct ports to outside | docker-compose ports | ✅ Phase 0 |
| 9 | HTTPS only для Tinkoff | code | TODO |
| 10 | Decision lineage в Postgres | code + schema | TODO Phase 1 |

### P1 (сделать в sandbox phase)

| # | Мера | Где |
|---|---|---|
| 11 | Anomaly detection alerts | `alphard-web` tile-level alerts (PR #394) — _(was: Prometheus + AlertManager, removed PR #399)_ | TODO Phase 3 |
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
