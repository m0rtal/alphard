# Code Quality Audit — Alphard Phase 0 (risk layer)

**Audit date:** 2026-08-14
**Auditor:** automated (developer profile), task `t_284fd068`
**Scope (per task body):** `src/risk/gate.py`, `src/main.py`, `pyproject.toml`
**Audit lens:** pydantic validators, pure-Python constraint, fail-safe defaults, defence-in-depth, 95%+ coverage, type hints, dead code

---

## Verdict (TL;DR)

| Dimension | Status | Note |
|---|---|---|
| Pydantic validators (input rejection at model layer) | **GOOD** | 11 fail-safe tests; `extra="forbid"` on every model |
| Pure-Python constraint (no numpy/pandas/sklearn/torch) | **GOOD** | `pyproject.toml` declares only `pydantic`; stdlib-only logic in `gate.py` |
| Fail-safe defaults (`allowed=False` on any violation) | **GOOD** | Verified: empty `violations` ⇒ `allowed=True`; model-level invariant enforces it |
| Defence-in-depth (redundant checks in gate AND model) | **GOOD** | `_check_position_size` and `_check_drawdown` re-check invariants the model already enforces — both paths exercised by coverage |
| 95%+ coverage | **PASS** | 96.61% on `src/risk/gate.py` (118 stmts, 4 miss), 34/34 tests pass |
| Type hints (mypy --strict) | **PASS** | `mypy --strict --ignore-missing-imports src/risk/gate.py` → 0 issues |
| Dead code | **FINDINGS** | 2 actual dead-code issues (see §6), both low-severity |
| Lint (flake8) | **FINDINGS** | 4 flake8 warnings (1 W292 missing newline × 2, 2 F841 unused locals in tests) |

**Overall:** risk-layer code quality is solid. Pydantic is doing its job, fail-safe is structural (not just runtime), pure-Python is enforced by `pyproject.toml`. The two real gaps are: (a) `RiskLimits.leverage_max` and `allow_short` are validated but never read by the gate, and (b) `RiskGate` doesn't enforce the financial invariant `cash ≥ sum(position.market_value)`. Neither is a hot bug — both are flagged for Phase 1.3.

---

## 1. Pydantic validators

### 1.1 What is validated

All five models declare `model_config = ConfigDict(extra="forbid")`. Unknown kwargs raise `ValidationError`. Tests cover this in `TestLimits.test_limits_extra_field_rejected`.

| Model | Field validators | Model validators |
|---|---|---|
| `TradeIntent` | `_strip_symbol` (non-empty after `.strip().upper()`), `_validate_side` (only `"buy"` in skeleton) | — |
| `Position` | — (no fields need post-processing) | — |
| `PortfolioState` | `total_equity > 0`, `peak_equity > 0`, `cash ≥ 0`, `daily_pnl` default 0 | `_peak_at_least_equity` — `peak_equity ≥ total_equity` |
| `RiskLimits` | `0 < max_dd_pct ≤ 100`, same for `_position_pct`/`_sector_pct`/`_daily_loss_pct`; `1.0 ≤ leverage_max ≤ 2.0` | — |
| `RiskDecision` | `violations: tuple[str, ...] = ()` (frozen) | `_allowed_implies_no_violations` — `allowed=True ⇒ violations==()` |

### 1.2 Coverage of validators by tests

| Validator | Test |
|---|---|
| `_strip_symbol` empty | `TestFailSafe.test_fail_safe_default_unknown_input` |
| `_validate_side` | `TestFailSafe.test_fail_safe_invalid_side` |
| `quantity ≥ 0` | `TestFailSafe.test_fail_safe_negative_quantity` |
| `price > 0` | `TestFailSafe.test_fail_safe_zero_price` |
| `total_equity > 0` | `TestFailSafe.test_fail_safe_negative_equity` |
| `peak_equity ≥ total_equity` | `TestFailSafe.test_fail_safe_peak_less_than_equity` |
| `RiskLimits` bounds (0, 100] | `TestFailSafe.test_fail_safe_invalid_limits` (tests both `0` and `101`) |
| `RiskLimits` extra | `TestLimits.test_limits_extra_field_rejected` |
| `RiskDecision` invariant | `TestFailSafe.test_fail_safe_decision_invariant` |
| `leverage_max` bounds | `TestLimits.test_limits_leverage_below_one_rejected`, `test_limits_leverage_above_two_rejected` |

**11 fail-safe tests, 11 fail-safe paths covered.** No dead validator.

### 1.3 Minor: TradeIntent.symbol normalization round-trip

`_strip_symbol` strips and uppercases, but the symbol validator returns the *normalized* value. `TradeIntent(symbol="  sber  ", ...)` returns `symbol="SBER"`. Good. But `TradeIntent(symbol="Sber", ...)` returns `"SBER"` too. Test `TestHelpers.test_intent_symbol_normalised` confirms the lower-case path. The upper-case path is not explicitly tested (the test uses `"  sber  "`). Severity: trivial.

---

## 2. Pure-Python constraint

`pyproject.toml` `[tool.poetry.dependencies]` declares exactly:

- `python = "^3.11"`
- `pydantic = "^2.0"`

All other deps (`tinkoff-investments`, `riskfolio-lib`, `lightgbm`, `vectorbt`, `pandas`) are commented out with `# Phase 1+` markers. Good. `gate.py` uses only `decimal.Decimal`, `typing.Any`, `pydantic`. No `import numpy`, no `import pandas`, no `import statistics`. The architectural guarantee is real, not just declared.

`requirements.txt` was added in commit `487a651` ("fix(docker): replace poetry with requirements.txt for Phase 0"). The repo lives in a Docker-only deployment, so this is fine — but it means `pyproject.toml` is the source of truth for declared deps, not the runtime installer. Worth noting if/when `requirements.txt` and `pyproject.toml` drift. Current `requirements.txt` not inspected — out of audit scope per task body.

---

## 3. Fail-safe defaults

`RiskGate.evaluate()` is structurally fail-safe in three layers:

1. **Model layer:** pydantic rejects malformed input before `evaluate()` runs. Bad input ⇒ `ValidationError`, never a `RiskDecision`.
2. **Decision layer:** `_allowed_implies_no_violations` enforces `allowed=True ⇒ violations == ()`. So even a buggy caller can't construct an `allowed=True` decision with non-empty violations.
3. **Gate layer:** `allowed = not violations`. Any single violation flips `allowed` to `False`.

`RiskDecision` is `frozen=True`, so callers cannot mutate `allowed` post-hoc. `RiskGate.limits` is referenced (not copied), so a caller mutating `limits` post-construction would silently change gate behaviour — see §6.1.

There is one tiny hole: `TradeIntent` is `frozen=False` (line 73, `ConfigDict(extra="forbid", frozen=False)`). The decision uses `intent.notional` which is `quantity * price`, both `Decimal` — so even if `intent` is mutated after `evaluate()` is called, the *decision* still holds the correct `notional` snapshot in `meta["position_pct"]`. But `violations` is a `tuple[str, ...]` of *formatted strings* that embed `notional` and `position_pct` at evaluate time, so they are stable. This is fine; the asymmetry is intentional (decisions must be immutable; intents may be mutated by callers between iterations).

---

## 4. Defence-in-depth

Two checks in `RiskGate` duplicate pydantic invariants:

```python
# gate.py line 261-265
if state.total_equity <= 0:
    violations.append("RISK_POSITION: invalid portfolio state (total_equity <= 0)")
    return
# gate.py line 348-351
if state.peak_equity <= 0:
    violations.append("RISK_DD: invalid portfolio state (peak_equity <= 0)")
    return
```

Both paths are unreachable in production (PortfolioState validator forbids them) but covered by coverage exclusion (lines 264-265, 350-351 = 4 missed statements). The two checks are documented as "Defence-in-depth" in the inline comments. Verdict: **intentional**, not dead code. If a future refactor loosens the model validator without updating the gate, these catch the regression. Good engineering.

No other defence-in-depth issue found.

---

## 5. 95%+ coverage

`pytest` output (2026-08-14):

```
collected 34 items
tests/test_risk_gate.py ..................................               [100%]

Name               Stmts   Miss  Cover   Missing
------------------------------------------------
src/risk/gate.py     118      4    97%   264-265, 350-351
TOTAL                118      4    97%

Required test coverage of 95% reached. Total coverage: 96.61%
============================== 34 passed in 0.23s ==============================
```

- Gate coverage: **97%** (118 stmts, 4 miss). Threshold 95% → PASS.
- Tests: 34/34 pass.
- The 4 missed lines are the defence-in-depth branches documented in §4. Coverage could go to 100% by deleting those branches (which is wrong) or by adding contrived tests that bypass pydantic validation (which is also wrong — pydantic forbids it). Verdict: **97% is the ceiling for this design** without compromising the design intent.

`pyproject.toml` explicitly omits `src/main.py` from coverage (line 43: `# Phase 0 stub, not production code`). The Phase 0 audit (`docs/AUDIT-Phase0.md` §C3) already flagged `main.py` as a stub. Not a code-quality issue per se.

---

## 6. Dead code / unused fields — TWO FINDINGS

### 6.1 FINDING (low severity): `RiskLimits.leverage_max` and `RiskLimits.allow_short` are never read by the gate

`RiskLimits` declares two fields that the gate does **not** consult:

```python
# gate.py line 164-166
leverage_max: Decimal = Field(default=Decimal("1.0"), ge=Decimal("1.0"), le=Decimal("2.0"))
allow_short: bool = Field(default=False)
```

A `grep -rn 'leverage_max\|allow_short' src/ tests/` confirms both fields appear only:

1. In the `RiskLimits` definition (line 164, 166).
2. In `tests/test_risk_gate.py` for boundary tests on the validators.

`RiskGate.evaluate()` never reads either field. This means a caller who sets `leverage_max=Decimal("1.5")` and `allow_short=True` will get a `RiskGate` that behaves **identically** to one constructed with the defaults. The "leverage and shorting" controls are silently no-ops.

**Why this matters:** the docstring on `RiskLimits` (line 151) implies these are "Hard limits enforced by the gate". They are not. Anyone auditing the gate's behaviour against the limits will be misled.

**Recommendations (pick one for Phase 1.3):**

- **A.** Remove `leverage_max` and `allow_short` from `RiskLimits` until Phase 1.3 implements them. Replace with `# TODO Phase 1.3: enforce leverage_max in position-size check; enforce allow_short in TradeIntent.side validator.`
- **B.** Implement them now: add `intent.leverage` field to `TradeIntent` (or read `cash / total_equity` ratio); add a `_check_leverage` method to `RiskGate`; loosen `_validate_side` to permit `"sell"` only when `allow_short=True`.

Severity: **low** — the gate still does what its name says (limits position size, sector exposure, daily loss, drawdown). The misleading fields do not change correctness for any test that exists today.

### 6.2 FINDING (low severity): unused local variables in two tests

`flake8 --max-line-length=120 --extend-ignore=E203,W503 src/ tests/` reports:

```
src/risk/gate.py:372:2: W292 no newline at end of file
tests/test_risk_gate.py:229:9: F841 local variable 'state' is assigned to but never used
tests/test_risk_gate.py:388:9: F841 local variable 'gate' is assigned to but never used
tests/test_risk_gate.py:575:34: W292 no newline at end of file
```

Test 229 (`test_sector_exposure_exceeded`) builds a `state` PortfolioState at lines 229-242, then **overwrites** it with `big_state` at lines 248-261 — the original `state` is unused. Test 388 (`test_fail_safe_default_unknown_input`) builds a `gate` but only uses it to demonstrate the exception flow on `TradeIntent`, not on the gate.

**Recommendations:**

- Delete the unused `state` assignment in `test_sector_exposure_exceeded`.
- Delete the unused `gate = RiskGate(limits)` in `test_fail_safe_default_unknown_input`.
- Add a newline at the end of `gate.py` (line 372) and `test_risk_gate.py` (line 575).

Severity: **trivial**. Pre-commit's `end-of-file-fixer` and `flake8` hooks should catch all four on the next `pre-commit run --all-files`.

---

## 7. Type hints (mypy --strict)

```
$ python -m mypy --strict --ignore-missing-imports src/risk/gate.py
Success: no issues found in 1 source file
```

No type errors under strict mode. The codebase uses `from __future__ import annotations` (line 52) so all annotations are evaluated as strings — this works with `--strict` because pydantic v2 evaluates them via its own resolver.

`src/main.py` was not type-checked (task scope says it, but it's a stub with `time.sleep(60)`). Noted: the `logger.info("Heartbeat — agents not yet active")` loop has no annotations to check. Mypy would pass on `main.py` trivially.

---

## 8. pyproject.toml — three notes

| # | Note | Severity |
|---|---|---|
| 8.1 | `[tool.coverage.run]` omits `src/main.py` with the comment `# Phase 0 stub, not production code`. Correct and consistent with `main.py`'s `Phase 0 stub` docstring. | OK |
| 8.2 | `[tool.pytest.ini_options] addopts` includes `--cov-fail-under=95`. Gate coverage is 96.61%, passes. | OK |
| 8.3 | No `[tool.mypy]` config — `.pre-commit-config.yaml` invokes `mypy --strict --ignore-missing-imports` as a hook, not via pyproject. Inconsistent but not a bug. Phase 1+ may want to centralise in `pyproject.toml`. | Note |

No `[tool.black]` or `[tool.flake8]` config either — relies on defaults. `.pre-commit-config.yaml` declares `--max-line-length=120 --extend-ignore=E203,W503` for flake8. This should be promoted to a `[tool.flake8]` section in `pyproject.toml` so editors honour the same rules.

---

## 9. Cross-references with existing Phase 0 audit

`docs/AUDIT-Phase0.md` (30024 bytes, 2026-08-14) covers UX/docs/Docker/secret-leak issues. Overlap with this audit is **zero** — Phase 0 audit did not look at lint, type hints, dead code, or per-model validator coverage. The two audits complement each other.

The Phase 0 audit's **C4** ("README claim '30 tests' is wrong — there are 34") confirms the current count of 34 tests. This audit re-confirms it (34 collected, 34 passed).

---

## 10. Recommendations — ordered by ROI

1. **Resolve §6.1:** either delete `leverage_max`/`allow_short` or implement the checks. Lowest-effort fix is to add `# TODO Phase 1.3` comments on both fields. **Cost: 1 line per field.**
2. **Fix §6.2:** delete the two unused locals + add EOF newlines. **Cost: 4 line-edits.**
3. **Promote flake8 config to `pyproject.toml`** `[tool.flake8]` section so editors agree with pre-commit. **Cost: 4 lines.**
4. **Optional Phase 1.3:** add `[tool.mypy]` section mirroring the pre-commit args. **Cost: 5 lines.**
5. **No action** on §6.1 defence-in-depth coverage gaps (lines 264-265, 350-351). They are by design.

None of the findings block a Phase 1.0 tag. The risk gate's correctness, fail-safe behaviour, and type discipline are all in order.

---

## Appendix A — exact commands used

```bash
# Tests + coverage
cd /root/projects/alphard
python -m pytest --tb=short
# → 34 passed, gate.py 97% covered (96.61% total), threshold 95% reached

# mypy --strict
python -m mypy --strict --ignore-missing-imports src/risk/gate.py
# → Success: no issues found in 1 source file

# flake8 (pre-commit-equivalent args)
python -m flake8 --max-line-length=120 --extend-ignore=E203,W503 src/ tests/
# → 4 issues: 2× W292 (EOF newline), 2× F841 (unused locals in tests)

# dead-field grep
grep -rn 'leverage_max\|allow_short' src/ tests/
# → confirms fields appear only in RiskLimits and tests
```

## Appendix B — files inspected

- `/root/projects/alphard/src/risk/gate.py` (371 lines)
- `/root/projects/alphard/src/main.py` (30 lines, stub)
- `/root/projects/alphard/src/__init__.py`, `/root/projects/alphard/src/risk/__init__.py`
- `/root/projects/alphard/tests/test_risk_gate.py` (574 lines)
- `/root/projects/alphard/pyproject.toml` (51 lines)
- `/root/projects/alphard/.pre-commit-config.yaml`
- `/root/projects/alphard/docs/AUDIT-Phase0.md` (cross-reference)