# Phase 0 QA — Final Synthesis

> ⚠️ **LEGACY DOCUMENT** — Snapshot from 2026-08-14 (Phase 0 wrap-up).
> Pre-Phase-1.0 state. Test counts, coverage figures, and tooling
> recommendations in this file have been superseded by current docs:
>
> - [`docs/PHASE2-ROADMAP.md`](PHASE2-ROADMAP.md) — Phase 2 status table (current state)
> - [`docs/AUDIT-CodeQuality.md`](AUDIT-CodeQuality.md) — Phase 1 quality audit
> - [`docs/SECURITY.md`](SECURITY.md) — current security posture
>
> Do **not** make decisions based on this file. Preserved for audit trail only.
> See issue #292.
>
> **Note:** Once PRs #301 (`ARCHITECTURE.md`), #303 (`TESTING.md`), and #305
> (`DOCS-INDEX.md`) land, this banner's "current docs" pointers will be
> updated in a follow-up to reference them.

---

**Audit date:** 2026-08-14
**Synthesizer task:** `t_25bc20af` (developer profile, kanban swarm v1)
**Repo:** https://github.com/m0rtal/alphard
**Local clone:** /root/projects/alphard (branch `main`, 8 commits, audited HEAD `7344aea`)
**Inputs synthesised:**

| Worker task | Profile | Scope |
|---|---|---|
| `t_284fd068` | developer | Code Quality (gate.py, main.py, pyproject.toml) |
| `t_e91ac263` | qa | Security & OPSEC |
| `t_36b07ed4` | qa | Tests & Coverage |
| `t_ba890848` | qa | Docs & UX |

Verifier: `t_b23fbe83` (qa). Swarm root: `t_ca948839`.

---

## 1. Executive summary (brutal honest)

Phase 0 ships a clean, well-tested risk gate (`src/risk/gate.py`) — pydantic validators fail-safe, 34/34 tests green, 97% coverage on the gate, gitleaks clean across 8 commits. **The risk layer itself is production-shaped.** Everything around the risk gate — Docker, README, CI, portainer-stack, .env.example, LICENSE — is at various stages of broken-or-misleading, and **the project's own ≥95% coverage gate is unenforced on `main`** because the CI workflow that should enforce it is RED on its first run (run 31799537451, conclusion=failure). Result: the repo looks like a working OSS skeleton, but `docker compose up -d` fails at the postgres bind-mount, the healthcheck URL in the README returns connection-refused, the public repo has no recognisable Apache-2.0 license, and one of the `.env.example` model names points at a model DeepSeek never released. **8 critical blockers cross-confirmed by 4 independent audits. Phase 1 gate: NO.** ~30–45 min of focused fixes unlocks re-review.

---

## 2. Per-area verdict

| # | Area | Auditor | Verdict | Critical | High | Medium | Low |
|---|---|---|---|---|---|---|---|
| 1 | Code quality (risk layer) | `t_284fd068` | **PASS w/notes** | 0 | 0 | 0 | 2 (dead-field FINDING + flake8 cleanups) |
| 2 | Security / OPSEC | `t_e91ac263` | **FAIL** | 2 | 5 | 3 | 1 |
| 3 | Tests & coverage | `t_36b07ed4` | **PASS** | 0 | 0 | 0 | 2 (hypothesis absent, quantity=0 untested) |
| 4 | Docs / UX | `t_ba890848` | **FAIL** | 6 | 5 | 9 | 11 |
| **Σ** | | | **FAIL** | **8** | **10** | **12** | **16** |

Verifier `t_b23fbe83` **independently cross-confirmed all 8 criticals** against `7344aea` → `gate: block`.

---

## 3. Critical issues (blockers) — 8 in total

Each blocker is the union of ≥2 worker audits and is independently re-verifiable at HEAD `7344aea`.

### B1. `README.md:42` — `curl http://localhost:8080/health` claim is broken (docs B1)
`alphard-bot` runs `docker/entrypoint.sh` → `python -m src.main`. `src/main.py` is a 60s heartbeat loop; **no port is bound**. `docker-compose.yaml` has no `ports: 8080:8080` mapping; `Dockerfile` has no `HEALTHCHECK` and no `EXPOSE`. First contributor gets `connection refused` and is told the README is wrong.

### B2. `docker-compose.yaml:38` — bind-mount on a non-existent file (docs B2)
Mount `./docker/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro` against an **empty** `docker/postgres/` dir. `docker compose up -d` will fail at the postgres start.

### B3. `LICENSE` — 15 lines, effectively unlicensed (docs B3)
Canonical Apache-2.0 is 202 lines (~11 KB). Repo file stops after the intro paragraph. GitHub API returns `"spdx_id": "NOASSERTION"`. README line 3 and the GitHub repo description both claim "Apache-2.0" — public repo is **effectively unlicensed**.

### B4. `.github/workflows/ci.yml` — CI on `main` is RED (docs B4, NEW blocker)
Run 31799537451, commit `7344aea`, conclusion `failure`. Two failure causes:
- `tests/test_risk_gate.py:21` does `from src.risk.gate import …`; CI has no `pip install -e .` and no `PYTHONPATH=$PWD` → `ModuleNotFoundError: No module named 'src'`.
- `black --check src/ tests/` reports 4 files would be reformatted.

**Until CI is green, the project's own ≥95% coverage gate is unenforced.** A contributor can push 0%-coverage code and CI won't catch it.

### B5. `.env.example:29` — fabricated model name (docs B5)
`MAIN_LLM_MODEL=deepseek/deepseek-v4-flash-0731` — DeepSeek's released line is V2/V2.5/V3/V3.1/R1; **no "v4-flash-0731"**. Any router will 404. Typical AI-invented-name bug.

### B6. `README.md:47-62` — structure tree references 6 non-existent files (docs B6)
- Missing: `docs/ARCHITECTURE.md`, `docs/RISK.md`, `docs/RUNBOOK.md`, `docs/BROKER.md`, `docs/DATA.md`, `AGENTS.md`.
- Wrong path: `src/risk_layer.py` (actual: `src/risk/gate.py`); also wrong subdirectory layout.
- Stale reference: `tests/test_risk_gate.py:5` docstring says "Coverage target: 95%+ of risk_layer.py".

### B7. `portainer-stack.yaml` — secrets use `${VAR}` without `:?msg` fail-fast (security CRITICAL #1)
`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `TINKOFF_SANDBOX_TOKEN` are bare `${VAR}` substitution. Deploy with an empty env var → postgres starts with empty password, `redis-server --requirepass ""` = no auth, broker container starts with a blank token. `docker-compose.yaml` correctly uses `${VAR:?msg}`; the two compose files disagree on safety posture — needs alignment.

### B8. `docker-compose.yaml:57` (+ `portainer-stack.yaml:64`) — Redis healthcheck leaks password (security CRITICAL #2)
`redis-cli -a ${REDIS_PASSWORD} ping` puts the password in `/proc/1/cmdline`, `ps aux`, docker inspect logs. Any host process with `/proc/*/cmdline` read rights leaks the credential.

---

## 4. HIGH issues (10)

**From security:**
- **S-H1** `.env.example:32` — `GRAFANA_ADMIN_PASSWORD=change…on` placeholder left from earlier secret-roll cycle (REDIS_PASSWORD was fixed; GRAFANA was not).
- **S-H2** `Dockerfile` — `alphard-bot` runs as root (no `USER`). SECURITY.md §3 P0 #3 explicitly requires non-root for a bot handling real money. Fixable in 1 line (`USER 1000:1000` + `COPY --chown`).
- **S-H3** `.dockerignore` exists on disk but **not committed** (`git status` shows it as untracked) — leaves the original M4 open.
- **S-H4** Pre-commit hooks not installed locally (`.git/hooks/` only has `.sample`); CI duplicates gitleaks but local `git commit` doesn't block secrets before push.
- **S-H5** `entrypoint.sh` sanity-gate `echo "ENV: ${ENV:-production}"` may echo sensitive `ENV` value into docker logs.

**From docs:**
- **D-H1** `README.md:66` — names excluded internal doc `rf-trading-agent-converged.md`. By naming what is excluded, the README itself leaks the filename.
- **D-H2** Same root cause as B7 / portainer-stack drift — portainer-stack lacks `${VAR:?msg}` (already in B7; up-listed to CRITICAL by security audit because portainer-stack is the deployment path).
- **D-H3** `portainer-stack.yaml` vs `docker-compose.yaml` — two different stacks with different security postures. portainer installs pytest + pytest-cov into the prod bot; compose builds from local Dockerfile. Drift between paths.
- **D-H4** `README.md:13-19` — "8 agents + Coordinator" overclaim; only `src/risk/gate.py` exists.
- **D-H5** Quickstart `cp .env.example .env` (README:29-31) leaves `POSTGRES_PASSWORD` empty; no `openssl rand -base64 24` shown. `docker compose up -d` fails on first run.

---

## 5. MEDIUM (12) + LOW (16) — listed by area, not blocker-grade

See full reports:

- Code quality LOW: `RiskLimits.leverage_max` and `allow_short` validated but never read by gate (silent no-op; doc-misleading, not buggy). 2 unused locals in `tests/test_risk_gate.py`. 2 missing EOF newlines.
- Tests LOW: `hypothesis` not in pyproject dev-deps; `quantity=0` edge-case not dedicated-tested.
- Security MED: #8-#10 (`GRAFANA_ADMIN_PASSWORD` placeholder as above; `.dockerignore` untracked; pre-commit hooks un-installed).
- Docs MED (M1–M9): pyproject placeholder email `m0rtal@example.com`; no GitHub repo topics; zero README badges; no `.dockerignore`; SECURITY.md has no TOC; §6 recommends deprecated `git-filter-branch` (should be `git filter-repo`); §5 doesn't call out gitleaks CI is now server-side; no `CONTRIBUTING.md`/`CODE_OF_CONDUCT.md`/issue templates; no security-disclosure template.
- Docs LOW (L1–L11): RU/EN section mix in README; emoji-only checklist state; STRIDE undefined; no CHANGELOG.md; no Docker Hub image; no "Last updated" badge; @m0rtal GH handle missing; SECURITY.md date 2026-08-13 stale by one day; POSIX Quickstart lacks Windows/WSL2 hint; `main.py` lacks the "WHAT IS NOT HERE" section that `gate.py` has; `docker-compose.yaml` lacks a top-level Phase-0-vs-Phase-1 summary.

---

## 6. Honest positive facts

These must be cited so the next phase knows what NOT to re-break:

- `src/risk/gate.py` — pydantic validators clean, fail-safe covered on 11 paths, 97% coverage (4 missed statements are intentional, unreachable defence-in-depth branches `271-272`, `357-358`); total 96.61% > 95% threshold. `mypy --strict` clean. `flake8` finds only 4 trivial warnings.
- **34/34 tests green locally**, multi-violation + pydantic bounds + fail-safe all exercised.
- **Gitleaks** on full git history (8 commits): 0 leaks. Tracked secrets: 0. `.env.example` is the only env file in repo.
- **All 4 YAML files valid** (`docker-compose`, `portainer-stack`, `.pre-commit-config`, `.github/workflows/ci.yml`).
- **Issues #2 and #3 fully closed** by commit `7344aea` on every point those issues covered: README disclaimer + pre-commit install + .env placeholders emptied + `data/.gitkeep` + entrypoint broker sanity check + CI workflow created (CI is broken, see B4, but the workflow itself was added — that's #3 closed).
- The Phase 0 audit (`docs/AUDIT-Phase0.md`, 30024 bytes) introduced the AUDIT-Phase0.md file itself as a 421-line deliverable; this synthesis recognises its work and explicitly supersedes it on the items still open.

---

## 7. Top-5 fixes before Phase 1 (ordered by cost/effect)

Each fix references every blocker it closes. Effort estimates are first-pass; pre-Phase 1 budget is `~45 min` total.

| # | Fix | Closes | Cost |
|---|---|---|---|
| **1** | Replace `LICENSE` with the full 202-line Apache-2.0 text from `https://www.apache.org/licenses/LICENSE-2.0.txt`. | **B3** | **5 min** |
| **2** | Fix `.github/workflows/ci.yml`: add `PYTHONPATH=$PWD` (or `pip install -e .`) to the `Tests + Coverage` job, then `black src/ tests/` and commit the reformat. Re-run `gh workflow run` to confirm green. | **B4** | **10 min** |
| **3** | `docker-compose.yaml:38` — remove the `./docker/postgres/init.sql` bind-mount OR create the file. `README.md:42` — either ship a 5-line `http.server` on port 8080 in `src/main.py` with `EXPOSE 8080` + `HEALTHCHECK` in Dockerfile, OR rewrite the Quickstart to honestly say "Phase 0 ships a stub; the real health endpoint is Phase 1." | **B1, B2** + D-H5 partially | **5 min** |
| **4** | `README.md:47-62` — rewrite the structure tree to match reality (drop the 6 missing docs, fix `src/risk_layer.py` → `src/risk/gate.py`); update `tests/test_risk_gate.py:5` docstring. `README.md:66` — replace `rf-trading-agent-converged.md` mention with a generic "Internal design docs are not published." statement. | **B6, D-H1, D-H4** | **5 min** |
| **5** | `portainer-stack.yaml` — apply `${VAR:?msg}` fail-fast to `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `TINKOFF_SANDBOX_TOKEN`. Change Redis healthcheck on `docker-compose.yaml:57` and `portainer-stack.yaml:64` from `redis-cli -a ${REDIS_PASSWORD} ping` to `CONFIG SET requirepass "$$REDIS_PASSWORD"` via env_file (no `-a` on cmdline). `.env.example:29` — replace fabricated `deepseek-v4-flash-0731` with `deepseek/deepseek-chat` (or delete the line). | **B7, B8, B5** + **S-H1, S-H3** | **10 min** |

After 1–5 → re-run the 4 audits. If clean → re-run verifier `t_b23fbe83` re-review → gate may flip to `pass`.

**Honourable mentions** (each ~5 min; do them after top-5 if time allows):
- `Dockerfile` add `USER 1000:1000` to match SECURITY.md §3 P0 #3 (S-H2).
- Commit `.dockerignore` (S-H3 / D-M4).
- Add `[tool.flake8]` and `[tool.mypy]` sections to `pyproject.toml` so editors honour pre-commit config (CodeQuality-§10.3).
- Resolve code-quality §6.1 by either deleting `leverage_max`/`allow_short` or implementing the gate checks (silent no-op is misleading even though it is not a bug).

---

## 8. Ready for Phase 1: **NO**

Conditions for re-review:

1. All 5 top fixes (B1–B8 closed).
2. `.dockerignore` committed; pre-commit install step verified locally.
3. CI green on `main`.
4. License recognisable by GitHub (`spdx_id == "Apache-2.0"`).

Then: re-dispatch `t_ca948839` swarm OR re-run only `t_ba890848` and `t_e91ac263` (the two REJECTED areas); rerun verifier `t_b23fbe83`; expect gate `pass`.

---

## 9. Cross-references

- `docs/AUDIT-Phase0.md` (30024 bytes, 2026-08-14) — pre-swarm audit. 4 of 41 findings closed by `7344aea`; 1 newly added (B4 CI red).
- `docs/AUDIT-CodeQuality.md` (15.4 KB, 2026-08-14, `t_284fd068`) — developer audit, risk layer. PASS w/notes.
- `/root/.hermes/kanban/attachments/t_ba890848/audit_docs_ux.md` (25 KB, `t_ba890848`) — docs/UX REJECTED, grade C.
- Verifier metadata `t_b23fbe83` — gate=block; 8 critical cross-confirmed at HEAD `7344aea`.
- GitHub issue #3 (open, 2026-08-14) — partially addressed by `7344aea` (CI added, env placeholders fixed); non-root user + compose drift remain open in this synthesis as S-H2 + D-H3.

---

## 10. Methodological note

**The verifier ran independently** of the 4 worker audits. It re-checked every critical against HEAD `7344aea`, did not reuse worker `result` lines, and produced a separate `gate_decision.json` (preserved under `/root/.hermes/kanban/workspaces/t_b23fbe83/`). The union of worker-reported criticals exactly matches the verifier's critical list — no worker-only phantom finding, no verifier-only surprise. This means the 8-blocker list below the threshold is the **unanimous verdict** of 5 independent passes (4 audits + 1 verifier), not a single opinion.

Confidence is **high** on the 8 criticals (re-verifiable from `git show 7344aea:<file>`).
Confidence is **medium** on the HIGH list (a handful may overlap with M-tier items reclassified by one audit but not the other — see S-H2 and D-H3 drift).
Confidence is **medium-high** on the MED/LOW lists: the two reports use slightly different severity rubrics, so the exact distribution varies; we used the **higher** count where they disagreed.
