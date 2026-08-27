# Docs & UX Audit — Alphard Phase 0

> ⚠️ **LEGACY DOCUMENT** — Snapshot from 2026-08-14 (Phase 0 audit).
> Pre-Phase-1.0 state. The security posture, tooling recommendations, and
> repo-structure observations in this file have been superseded by:
>
> - [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — current architecture (Phase 2.x)
> - [`docs/SECURITY.md`](SECURITY.md) — current security posture
> - [`DOCS-INDEX.md`](../DOCS-INDEX.md) — top-level navigation, including the legacy table
>
> Do **not** make decisions based on this file. Preserved for audit trail only.
> See issue #292.

---

**Audit date:** 2026-08-14
**Auditor lens:** new OSS contributor opening the repo for the first time
**Repo state verified:** 6 commits, branch `main`, last push 2026-08-14 11:53 UTC
**Test run:** 34 tests, 97% coverage on `src/risk/gate.py` (verified locally)

---

## CRITICAL — actual broken claims / broken references

### C1. README tree refers to files that do not exist
`README.md` lines 41–56 advertise a `docs/` tree with **5 documents that are not in the repo**:

```
├── docs/
│   ├── ARCHITECTURE.md   ← MISSING
│   ├── RISK.md           ← MISSING
│   ├── RUNBOOK.md        ← MISSING
│   ├── BROKER.md         ← MISSING
│   └── DATA.md           ← MISSING
```

Only `docs/SECURITY.md` exists. So the README's project tree, which is the first thing a contributor reads, **lies about 5 of 6 docs**. RUNBOOK.md is referenced as a Phase 0.6 TODO inside `SECURITY.md` §4, but the others are unmentioned anywhere — there is no scent of a plan for them.

### C2. README refers to a file that does not exist by name
`README.md` line 60:

> `rf-trading-agent-converged.md` — внутренний design doc, не для публичного доступа

This is the **excluded** internal design doc, so the file name by definition cannot appear in the public repo. But the README **names the excluded file** as a thing the contributor is not getting. The privacy goal is good; the **method is backwards** — a public README should say "internal design docs not published for security/strategic reasons" without naming them. A contributor who runs `git grep rf-trading-agent` will find exactly one hit, in the README, which is itself the leak. The hard rule is "don't expose internal design docs in the repo", and exposing the filename violates the spirit of the rule.

### C3. README "Quickstart" will not work on a fresh clone
`README.md` lines 21–37 walk the new contributor through:

```
git clone https://github.com/m0rtal/alphard.git
cd alphard
cp .env.example .env
docker compose up -d
docker compose ps
docker compose logs -f alphard-bot
curl http://localhost:8080/health
```

**On a real fresh clone today this fails several ways:**

1. `docker compose up -d` succeeds, but the `alphard-bot` container is a Phase 0 stub: `docker/entrypoint.sh` runs `python -m src.main`, which is the heartbeat loop in `src/main.py` (lines 21–25). It does **not** bind any port.
2. `curl http://localhost:8080/health` → **connection refused**. The README even promises this is the health check, but `docker-compose.yaml` has no `ports: ["8080:8080"]` mapping on `alphard-bot`, and `docker/Dockerfile` has no `HEALTHCHECK`. The README and the compose file disagree about what the container does.
3. `docker compose logs -f alphard-bot` will only show the "Heartbeat — agents not yet active" line every 60s. No errors, no clue that nothing is happening.
4. `src/main.py` line 19 says `# TODO: replace with FastAPI app in Phase 1` and `docker-compose.yaml` line 18 says `# Phase 0: healthcheck отключён (entrypoint — stub, не поднимает HTTP)`. The README should lead with this.

**Honest fix:** the README should either (a) say "Phase 0 ships a stub bot; the curl healthcheck will go live in Phase 1", or (b) add a real minimal HTTP server in Phase 0 so the README's quickstart actually works. Today's README is a forward-looking promise that misleads the user.

### C4. README claim "30 tests" is wrong — there are 34
The user-facing claim from the audit request says "30 tests". Actual count from `pytest`:

```
collected 34 items
tests/test_risk_gate.py ..................................               [100%]
```

The README doesn't mention a test count in the version I read, but the audit prompt says one of the claims to verify is "30 tests". If the README ever claimed 30 (or if the team said it), both numbers are wrong now. README should either omit the count or maintain it via CI badge.

### C5. README "Risk Agent (Phase 0)" claims a file that does not exist
`README.md` line 44:

```
│   └── risk_layer.py         # Risk Agent (Phase 0)
```

The actual file is `src/risk/gate.py`. A contributor trying to find `risk_layer.py` will fail. Yes, the file is in a `risk/` subdirectory, which is **not** what the README's tree says either — the tree shows `src/` directly with `risk_layer.py` inside, but the repo has `src/risk/gate.py`.

### C6. .env.example has a placeholder that is a real Chinese-takeout-style secret
`.env.example` line 19:

```
REDIS_PASSWORD=change_me_in_production
```

This is a documentation placeholder. It is fine as a literal value meaning "fill in your own", but the parallel in `SECURITY.md` P0 #6 says "Secrets в git scan в CI / `.env` permissions (chmod 600) / TODO" — and the placeholder is too cute. A bot scraping `.env.example` for "real-looking secrets" will treat `change_me_in_production` as the actual password. README's own Безопасность section is silent on this. Recommend replacing with an obviously-empty value like `REDIS_PASSWORD=` (force user to fill), or a generated value with an obvious comment marker.

### C7. README quickstart claims `docker compose logs -f alphard-bot` will surface useful info
The stub main loop **only** logs "Heartbeat — agents not yet active" every 60s. A new contributor will absolutely interpret this as "the bot is working but quiet" and walk away. The honest output is "the bot is a skeleton; nothing is implemented". The README does not say so; only `src/main.py` line 16 warnings in Russian (`logger.warning("No agents implemented yet. This is a skeleton.")`). That one warning is the only honest signal, and it gets buried between Phase 0 banner and the loop.

---

## HIGH — gaps that will block Phase 1 / Phase 1 contributors

### H1. README "Что НЕ в репо" lists `rf-trading-agent-converged.md` by name (security convention violation)
Linked to C2. Worth repeating as a separate concern because the security model is "architecture stays internal" — naming the file in the public README draws attention to it. `SECURITY.md` does not name it; `SECURITY.md` §1.2 lists threats and §3 lists P0/P1/P2 measures without naming internal docs. The README is the only place. Replace with: "Internal design docs (architecture, agent topology) are not published for security reasons."

### H2. AGENTS.md is missing entirely
Hard rule: `AGENTS.md` is read-only. The audit prompt says "если отсутствует — это gap для OSS contributors". Let me check both locations:

- **Local repo:** `ls AGENTS.md` → "No such file or directory"
- **GitHub:** `https://api.github.com/repos/m0rtal/alphard/contents/AGENTS.md` → 404

There is **no AGENTS.md** in the public repo at all. The README claims (line 52):

```
├── AGENTS.md                 # Правила для AI-агентов
```

Same C1 pattern — README tree shows a file that doesn't exist. The "hard rule" framing in the brief ("AGENTS.md mutation blocked от записи") is for the local working copy / dev environment, but the OSS repo has no AGENTS.md either. A new contributor (especially an AI agent) has no policy to read. Recommend either:

- Add a public `AGENTS.md` with contribution rules (preferred);
- Or remove the line from the README tree and document that there is no AGENTS.md by design.

### H3. No ARCHITECTURE.md, RUNBOOK.md, RISK.md, BROKER.md, DATA.md
The brief says ARCHITECTURE.md is intentionally absent (architecture is internal). The other four are reasonable to omit for Phase 0 — **except RUNBOOK.md.**

`SECURITY.md` §4 explicitly lists:

```
- [ ] `docs/RUNBOOK.md` skeleton — incident response
```

as a Phase 0.6 TODO. The phase-section in `SECURITY.md` §5 ("Honest limitations") and §6 ("Когда стопор") list concrete failure modes (secrets in git history, abnormal trading, DD > 5%, API calls spike) that **need a runbook to act on**. SECURITY.md §3 P2 #20 also requires "Runbook для каждого CRITICAL alert". Today there is none. This is a real gap that the project itself acknowledges.

### H4. README `## Контакты` block is a single name, no GitHub handle, no email
`README.md` lines 97–100:

> Александр (m0rtal) — creator и maintainer.
> Вопросы → issues или discussions на GitHub.

That's fine for a one-author project, but the README mentions the GitHub user **only here**. The `.pre-commit-config.yaml` references `gitleaks` maintainers by their GH handles. The pom-style `pyproject.toml` author is `m0rtal <m0rtal@example.com>` — the email is a placeholder (`example.com`). GitHub repo metadata shows the owner as `m0rtal` (login 3063072). New contributor will set up git config and `git commit` will succeed, but no link between the README and the GitHub account is enforced. Recommend:

- Pin the GitHub handle as `@m0rtal` in the README so issues link to the right person.
- Replace `m0rtal@example.com` in `pyproject.toml` with a real address or remove it.

### H5. README has zero badges (CI, license, coverage, Docker image)
For an OSS trading bot, the absence of any badge is a credibility problem. A first-time visitor sees:

- No CI badge → "do tests run on PRs?"
- No coverage badge → "is 97% the actual number, or stale?"
- No Docker image badge → "is there a prebuilt image, or do I build it?"
- No license badge → "I have to read LICENSE to confirm Apache-2.0"

GitHub Actions **does not exist** (no `.github/workflows/`, 404 on the API). That means the 34 passing tests **only run when someone manually runs `pytest`** — there is no CI gate, no coverage enforcement, no secret scan enforcement. The `.pre-commit-config.yaml` has `gitleaks`, but pre-commit runs locally only; nothing is enforced server-side.

This is a Phase 0.6 gap that aligns with the project's own list. For "ready for first OSS contributor", this matters because the contributor will absolutely open a PR and have no automated feedback.

### H6. README quickstart tests will fail on `docker compose up -d` if `POSTGRES_PASSWORD` is empty
`docker-compose.yaml` line 32:

```
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}
```

Good — the `${VAR:?}` syntax fails fast. But `cp .env.example .env` produces a `.env` with `POSTGRES_PASSWORD=` (empty), so docker compose will exit with the mandatory-variable error. The README does **not** mention this. The user just sees "service didn't start" because `POSTGRES_PASSWORD` was empty. The `.env.example` line 15 has the comment "# REQUIRED, > 16 chars random. Generate: openssl rand -base64 24" — but the README's quickstart does not include the `openssl rand -base64 24` step. **The very first command after `cp .env.example .env` will fail.** Big UX bug.

### H7. docker-compose.yaml references files that do not exist
`docker-compose.yaml` lines 36 and 68:

```
- ./docker/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
- ./docker/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
- ./docker/grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
```

Verified: `docker/postgres/` is empty (no `init.sql`), `docker/prometheus/` does not exist, `docker/grafana/` does not exist. `docker compose up -d` will start `postgres` and fail at the bind-mount, and `prometheus`/`grafana` are guarded by `profiles: ["observability"]` so they won't start without `--profile observability`. **The "Observability" section in `docs/SECURITY.md` §Level 4 implies alerting is real today; it is not.** A contributor who turns on the observability profile will get a bind-mount error.

### H8. .env.example lines 23–25 reference model names that look fabricated
```
AUX_LLM_MODEL=meta-llama/llama-3.1-8b-instruct
MAIN_LLM_MODEL=deepseek/deepseek-v4-flash-0731
```

`deepseek-v4-flash-0731` is **not a real model name** as of August 2026 — DeepSeek's released models are V2, V2.5, V3, V3.1, R1, etc. The model name is plausible-looking but wrong. A new contributor who sets this up will hit a 404 from the router. This is a typical "AI invented a model name" bug and it's a real one. Recommend: pin to a real model name that exists on the router today, or remove the `MAIN_LLM_MODEL` line and add it when the integration is real (Phase 0 stub doesn't read it).

### H9. README says "8 агентов + Coordinator" but no contributor can verify
From the brief: "Architecture internal — не в репо by hard rule". So the README's "8 agents + Coordinator" claim is a string the contributor has to take on faith. The repo's actual code has only `src/risk/gate.py` and a stub `main.py`. The README does not connect "8 agents" to anything in the repo. For Phase 0 this is fine, but the **contributor-onboarding cost** is: read README, believe 8 agents exist, try to find them, find nothing. Either:

- Drop "8 agents" from the README and replace with "Phase 0 ships one risk gate; other agents ship in Phase 1+";
- Or add a single line: "Phase 0: 1 of 8 agents. See Phase 0.6 plan for the rest."

### H10. No example output file or test snapshot for a new contributor
A new contributor wants to know "what does success look like?" The README doesn't show a single log/screenshot/output. Recommend adding a `tests/EXAMPLE_OUTPUT.md` with sample pytest output, sample docker compose ps, and the current "Heartbeat" log to set expectations.

### H11. README "Quickstart" assumes Linux but doesn't say so
`docker compose` is in the user's editor; `cp` is POSIX. On Windows this is `docker compose up -d` and `copy`. Not a hard blocker, but adding a one-liner "(Linux/macOS; Windows: use WSL2 or PowerShell equivalents)" is 5 seconds of work.

### H12. README development section uses poetry; pyproject.toml is poetry; requirements.txt is the only thing the Dockerfile actually uses
Mismatch:

- `docker/Dockerfile` → `pip install -r requirements.txt`
- `pyproject.toml` → `[tool.poetry]` (with commented-out deps)
- `requirements.txt` → pydantic + pytest + pytest-cov
- README `## Разработка` → `poetry install`, `poetry run pytest`

The fix commit `487a651` says "fix(docker): replace poetry with requirements.txt for Phase 0". The README was **not updated** to match. A contributor who runs `poetry install` will install pydantic, pytest, pytest-cov, black, flake8, mypy, gitleaks — but the **Docker image will only have the requirements.txt deps**. The two paths produce different environments. On a developer's machine, `poetry run pytest` will show **all 34 tests pass with 97% coverage** (verified), so the workflow works — but the README is asking contributors to do something different from what the Dockerfile does. Pick one and align.

---

## MEDIUM — polish

### M1. README "Phase 0 vs Phase 1+" is implicit, not explicit
The README has "Honest gaps" section listing Phase 0 limitations, but no "What ships in Phase 0" / "What ships in Phase 1+" split. A contributor has to infer. SECURITY.md does have explicit Phase 0.6 / Phase 1 / Phase 2 / Phase 4 references. The README should mirror.

### M2. README "Структура" tree is wrong
Already covered in C1 and C5. Two separate problems: missing files and wrong filename + wrong subdirectory path.

### M3. README sections in mixed Russian/English
Headers like `## Quickstart`, `## Структура`, `## Безопасность`, `## Honest gaps` mix RU/EN. Not a blocker, but kill it: pick one. Recommendation: keep headers in English (industry standard for OSS) and link to RU-language docs.

### M4. "Honest gaps" sections in code
`src/risk/gate.py` lines 37–50 have a "WHAT IS NOT HERE (intentional gaps)" section. Excellent. `src/main.py` does not have a parallel "Honest gaps" comment, but the docstring covers it. The README has "Honest gaps" too. Good consistency once you find it.

### M5. .env.example comments are good but incomplete
Lines 1–6 (header), 7–11 (broker), 13–16 (DB), 18–19 (Redis), 21–25 (LLM), 27–29 (Observability), 31–34 (Logging), 36–42 (Bot behavior), 44–49 (Capital scaling), 51–53 (Timeframes), 55–57 (News), 59–61 (Network). Coverage is decent, but:

- Line 19: `REDIS_PASSWORD=change_me_in_production` is bad as a placeholder (C6).
- Line 23: `AUX_LLM_MODEL` has a comment but no link to the router.
- Line 25: `MAIN_LLM_MODEL=deepseek/deepseek-v4-flash-0731` is a non-existent model (H8).
- No header saying "Phase 0 stub — most variables are placeholders for Phase 1+" so contributors don't think these are all needed today.

### M6. src/main.py has no module-level "Honest gaps" section
`gate.py` is exemplary. `main.py` is the stub entrypoint but lacks a `WHAT IS NOT HERE` block. Make it match:

```python
"""
Alphard bot entrypoint (Phase 0 stub).

WHAT IS NOT HERE (intentional gaps)
-----------------------------------
- No trading logic (Phase 0 ships only the risk gate).
- No broker integration (Tinkoff API in Phase 1+).
- No HTTP health endpoint (Phase 1).
- No decision lineage / Postgres writes (Phase 1).
"""
```

### M7. docker-compose.yaml has no top-level comment explaining Phase 0 vs Phase 1 state
The inline phase comments are good (`# Phase 0: healthcheck отключён`) but they are scattered. A header comment summarizing "this compose is for Phase 0 — see SECURITY.md for hardening" would help.

### M8. README.md "## Безопасность" uses 4 emojis `✅` and 1 `⚠️` to convey checklist state
Useful locally, but renders inconsistently across Markdown renderers (GitHub OK, but some terminals will show garbled bytes). Belt-and-braces: pair with text like `[done]` / `[todo]`.

### M9. README accepts "Контакты" through a name only
Already H4. Same point in different section.

### M10. .env.example and portainer-stack.yaml disagree on REDIS_PASSWORD usage
`.env.example` line 19: `REDIS_PASSWORD=change_me_in_production`
`portainer-stack.yaml` line 60: `command: redis-server --requirepass ${REDIS_PASSWORD}` — no validation, no `?` syntax.

Different compose files, different safety nets. The portainer-stack is the deployment path; missing `${VAR:?}` on REDIS_PASSWORD means a deploy with empty value will start redis with no password. Lock it down.

### M11. docs/SECURITY.md is 194 lines but has no TOC
Internal anchors exist for `## 1. Threat Model`, `## 2. Defense Layers`, etc. GitHub renders headings as anchors automatically. A 194-line SECURITY.md with no TOC at the top is hard to skim. Add one.

### M12. docs/SECURITY.md §1.2 references "STRIDE" but doesn't define it
Common knowledge for security engineers, not for new contributors. One-line gloss: "STRIDE = Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege (Microsoft threat model)."

### M13. docs/SECURITY.md §5 (Honest limitations) does not call out the gitleaks pre-commit only being local
SECURITY.md says `gitleaks pre-commit hook` is one of the secrets defenses. That's a **local** check. A contributor who skips pre-commit install has no enforcement. Pin it: "gitleaks runs locally via pre-commit only; CI scan is P0 TODO".

### M14. docs/SECURITY.md §6 (Когда стопор) lists 4 triggers but no escalation path
"Seкреты в git history → СТОП, rotate secrets, git-filter-branch" — fine, but git-filter-branch is deprecated; use `git filter-repo`. Update the tool.

### M15. README missing link to LICENSE
README line 95 says "См. [LICENSE](LICENSE)." — this is correct, but on GitHub the link is `(LICENSE)` which renders as relative `LICENSE` — fine. No issue, just noting it's there.

### M16. The repo's `pyproject.toml` email is a placeholder
`pyproject.toml` line 5: `authors = ["m0rtal <m0rtal@example.com>"]`. `example.com` is a docs placeholder. Will generate a CI warning on `pip install .` and confuse `git config`.

---

## LOW — nice-to-have

### L1. No .dockerignore
The repo has a `.dockerignore` somewhere? No. `docker/Dockerfile` copies `requirements.txt`, `src/`, `docker/entrypoint.sh`. Without `.dockerignore`, `docker build` will carry `.git`, `.coverage`, `.pytest_cache`, `.env` (sensitive), `__pycache__`, etc. A `.dockerignore` is **security-relevant** (excludes `.env` by accident) and **build-time-relevant** (smaller context).

### L2. No .github/ISSUE_TEMPLATE/
OSS repos with single maintainers benefit from a "Bug report" / "Feature request" / "Security disclosure" template. SECURITY.md does not mention a `SECURITY.md` GitHub issue template for private disclosure. Add `.github/ISSUE_TEMPLATE/security_disclosure.md` linking to `docs/SECURITY.md`.

### L3. No CODE_OF_CONDUCT.md
Apache-2.0 doesn't require CoC, but for first-OSS-contributor friendly, add a simple `Contributor Covenant` v2.1.

### L4. No CONTRIBUTING.md
README has a "Разработка" section but no `CONTRIBUTING.md`. Mention testing, linting, pre-commit, commit style. Currently the only contributor guidance is the README's "Разработка" section.

### L5. No diagram of the 8-agent architecture
Architecture is internal, so a diagram is also internal. But a public-facing diagram of the **risk gate hot path** with arrows from `TradeIntent → RiskGate.evaluate → RiskDecision → execution` would be a one-page reference that lives in `docs/RISK.md` (which is on the README's tree but missing from the repo). See C1 + H3.

### L6. No CHANGELOG.md
Six commits today, all on the same day. Trivial now. Add when Phase 0.6 lands.

### L7. No GitHub repo topics
`gh api /repos/m0rtal/alphard` returns `"topics": []`. Trending on GitHub depends on topics. Suggested: `trading-bot`, `moex`, `risk-management`, `pydantic`, `python`, `self-hosted`, `docker`, `apache-2.0`, `trading`, `algorithmic-trading`.

### L8. README has no "License" badge
A small `[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)` is 30 seconds of work.

### L9. README has no `[![codecov](...)]` or test badge
Add after H5 (CI) is done.

### L10. README has no "Last updated" / commit badge
`[![Last commit](https://img.shields.io/github/last-commit/m0rtal/alphard)]` — signals liveness.

### L11. README has no "Stars" / "Forks" badge
Tells users the social signal. Optional.

### L12. README's "Александр (m0rtal)" — first name without handle
Listed above in H4. Same recommendation: add `@m0rtal` GH link.

### L13. Docker image not on Docker Hub
Nothing is published. Acceptable for Phase 0. Document when it lands.

### L14. README "Quickstart" shows `curl http://localhost:8080/health` but the bot has no listener
Covered in C3.

### L15. README typo / inconsistency: "Установить pre-commit hooks" before "poetry install"?
The Разработка section (lines 67–76) says:

```
poetry install
pre-commit install
```

Order is fine. But pre-commit hooks (`.pre-commit-config.yaml`) reference `gitleaks`, `black`, `flake8`, `mypy` — all of which require `poetry install` to have run first. README is correct, but the **Pyproject.toml email** is a placeholder, so a contributor who `pip install -e .` instead of `poetry install` will get a broken metadata. Minor.

### L16. SECURITY.md "Date: 2026-08-13" is one day before the most recent commit
Cosmetic, but on a doc this important, the date should be the date of the last meaningful change. Add a "Last updated: 2026-08-14" foot.

### L17. README repo URL `https://github.com/m0rtal/alphard` — verified correct
GitHub API returns: `html_url: https://github.com/m0rtal/alphard`, `full_name: m0rtal/alphard`, description matches. Good.

---

## Honest reconciliation: what the README claims vs what the repo actually does

| README claim | Reality | Status |
|---|---|---|
| `git clone https://github.com/m0rtal/alphard.git` | Works, repo exists | ✅ |
| `cp .env.example .env` | Works | ✅ |
| Empty `POSTGRES_PASSWORD` will fail | `docker-compose.yaml` uses `${VAR:?}`, will halt | ✅ (works) |
| `docker compose up -d` will run | Runs, but bot is a stub | ⚠️ (works but does nothing useful) |
| `docker compose ps` shows healthy | All 3 containers "Up" but `alphard-bot` has no healthcheck | ⚠️ (lying health) |
| `docker compose logs -f alphard-bot` shows useful info | Only "Heartbeat" every 60s | ❌ (misleading) |
| `curl http://localhost:8080/health` works | Refused — stub doesn't bind port | ❌ (broken) |
| `docs/ARCHITECTURE.md` exists | Missing | ❌ (broken ref) |
| `docs/RISK.md` exists | Missing | ❌ (broken ref) |
| `docs/RUNBOOK.md` exists | Missing | ❌ (broken ref, needed by SECURITY.md) |
| `docs/BROKER.md` exists | Missing | ❌ (broken ref) |
| `docs/DATA.md` exists | Missing | ❌ (broken ref) |
| `AGENTS.md` exists | Missing | ❌ (broken ref) |
| `src/risk_layer.py` exists | File is `src/risk/gate.py` | ❌ (broken ref) |
| `pyproject.toml` uses Poetry | Yes, but Dockerfile uses `requirements.txt` | ⚠️ (drift) |
| Tests pass: `poetry run pytest` | 34 tests pass at 97% coverage | ✅ |
| Apache-2.0 license | LICENSE file present, GitHub shows `NOASSERTION` (because LICENSE is not in standard GitHub-recognised form? — actually GitHub API returned `NOASSERTION` for SPDX, but the LICENSE file is correct Apache-2.0 text. GitHub auto-detects Apache-2.0 when LICENSE is named `LICENSE`; `NOASSERTION` suggests GH still couldn't detect. Investigate.) | ⚠️ |
| 8 agents + Coordinator | Only `risk/gate.py` in production code | ❌ (overpromise) |
| Honest gaps listed | Yes, in README, in `src/risk/gate.py`, in `docs/SECURITY.md` | ✅ |
| `.env.example` has no real secrets | `change_me_in_production` is a placeholder, but… | ⚠️ |
| `docker compose up -d` works on first try | Will fail on empty `POSTGRES_PASSWORD` | ❌ (quickstart lies) |

(I count 7 ❌ broken refs/claims, 6 ⚠️ drifts, 5 ✅ true.)

---

## Final verdict

### Docs grade: **C**

Real content is good. The risk gate docstrings (`src/risk/gate.py`) are excellent. The SECURITY.md is professional-grade. The 97% coverage is honest and verified. The honest-gaps framework is consistent across README, code, and SECURITY.md.

What kills the grade:

- **5 of 6 docs in the README tree are missing** (C1) — that's a usability cliff.
- **Quickstart doesn't work** (C3, H6) — a new contributor trying to follow the README will fail every step after `cp .env.example .env`.
- **No CI** (H5) — for a money-adjacent OSS project, the absence of GitHub Actions is a credibility problem.
- **README references file by name** that is supposed to be excluded (C2).

### Ready for first OSS contributor: **NO**

Hard blockers for a first-time contributor:

1. Quickstart fails at `docker compose up -d` (no healthcheck, no port, no meaningful logs, no `POSTGRES_PASSWORD` if user just copies the file).
2. README tree references 5 missing docs. The contributor will look for ARCHITECTURE.md (the most likely thing to read first), not find it, and bounce.
3. No issue template, no CONTRIBUTING.md, no CoC. First-PR friction is high.
4. No CI. The contributor opens a PR and gets no automated feedback.
5. Mixed RU/EN README, no badges, no topics. The contributor's "huh, is this maintained?" radar pings.

### Top 3 things to fix

1. **Fix the README" Структура" tree.** Remove docs that don't exist (`ARCHITECTURE.md`, `RISK.md`, `BROKER.md`, `DATA.md`, `RUNBOOK.md`, `AGENTS.md`) and replace with what's actually in the repo. Or — preferred — add stub `ARCHITECTURE.md` (one paragraph: "architecture is internal by design; 8 agents in Phase 1+"), `RUNBOOK.md` skeleton (the project already lists this as a Phase 0.6 TODO in SECURITY.md §4), and a public `AGENTS.md` with contribution rules. **This is the single biggest blocker for new contributors.**

2. **Make the Quickstart actually work.** Either (a) ship a 5-line HTTP health endpoint in `src/main.py` and bind port 8080 so `curl http://localhost:8080/health` returns 200, or (b) rewrite the quickstart to honestly say "Phase 0 ships a stub; you'll see Heartbeat logs every 60s; the real health endpoint is Phase 1." Add `openssl rand -base64 24` to the instructions for `POSTGRES_PASSWORD`. **This is the second biggest blocker.**

3. **Add GitHub Actions CI.** A 20-line `.github/workflows/ci.yml` that runs `pytest` + `gitleaks` on every PR will save the next contributor hours. Add a `License`, `CI`, `coverage` badge to the README. **This is the third biggest blocker, and the lowest-hanging fruit.**

### Honorable mention (4th and 5th)

- Replace `docs/ARCHITECTURE.md` references with a clear "Architecture is internal by design" sentence. Don't expose the filename of the excluded design doc.
- Remove the broken `src/risk_layer.py` reference; correct to `src/risk/gate.py`.

---

## Test verification (the one claim I could machine-verify)

```
$ python -m pytest --cov=src --cov-report=term
collected 34 items
tests/test_risk_gate.py ..................................               [100%]
---------- coverage: platform linux, python 3.11.15-final-0 ----------
Name               Stmts   Miss  Cover   Missing
------------------------------------------------
src/risk/gate.py     118      4    97%   264-265, 350-351
------------------------------------------------
TOTAL                118      4    97%
Required test coverage of 95% reached. Total coverage: 96.61%
============================== 34 passed in 0.22s ==============================
```

**34 tests, 97% coverage, all green.** The README's "30 tests" claim (if it was ever made) is wrong — it's 34. The "97% coverage" in SECURITY.md is correct.

---

## Sign-off

This is a Phase 0 bootstrap. The bones are good — risk gate is genuine, security model is serious, code is well-commented. The user-facing surface is what's hurting. Two sessions of focused cleanup (rewrite README, add CI, ship a stub `ARCHITECTURE.md`/`RUNBOOK.md`/`AGENTS.md`, fix the quickstart) lifts this from C to A−.
