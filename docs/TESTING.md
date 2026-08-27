# Testing

> Тестовая стратегия Alphard: как гонять тесты, как мокать Tinkoff SDK / Postgres,
> какие тесты скипаются и почему, как coverage gate держится на ≥95%.
> Документ для тех, кто пишет новые тесты, разбирает упавший CI или
> добавляет защитную ветку в `pragma: no cover`.

---

## 1. Где живут тесты и как их гонять

```text
alphard/
├── tests/                       # pytest, единая точка входа
│   ├── conftest.py              # общие фикстуры (autouse): reset MOEX cache,
│   │                            #   isolate ALPHARD_PEAK_STORE_DIR
│   ├── test_<module>.py         # юнит-тесты на src/<module>.py
│   ├── test_*_integration.py    # интеграционные (skipif ALPHARD_PG_DSN / TINKOFF_*_TOKEN)
│   └── test_*_live.py           # live smoke (sandbox token + Postgres)
├── pyproject.toml               # pytest config + coverage gate (single source of truth)
├── pytest.data.ini              # override: gate src/data отдельно (≥75%, не глобальный 95)
└── .github/workflows/ci.yml     # CI: pytest + black + flake8 + mypy + gitleaks
```

### Базовые команды

```bash
# Полный прогон + coverage gate (как в CI)
pytest

# Verbose, без coverage (быстрее на dev loop)
pytest -v --no-cov

# Один файл / один тест
pytest tests/test_coordinator.py::TestCoordinator::test_foo -v

# Только упавшие / только по маркеру
pytest --lf                     # last-failed
pytest -m "not integration"      # (если введём маркеры; см. §5)

# HTML coverage report
pytest --cov=src --cov-report=html
open htmlcov/index.html
```

Все настройки (`addopts`, `testpaths`, coverage source/omit/exclude_lines)
живут в `pyproject.toml` `[tool.pytest.ini_options]` и
`[tool.coverage.run|report]` — не дублируйте флаги в CI-скриптах, иначе
gate начнёт расходиться между локалом и CI. Исторически это уже
приводило к регрессу: после PR #255 `--cov-fail-under=94` уехал в
`pyproject.toml`, а CI гонял `94` ещё до того, как `pyproject.toml`
вернули на `95` (issue #257).

---

## 2. Coverage gate (≥95%)

### Что покрываем

| Слой | Что включено | Что исключено |
|---|---|---|
| `src/` (всё, что импортируется) | runtime модули | `src/main.py` (Phase 0 entrypoint — покрыт интеграционным smoke, не юнитами), `*/__init__.py`, `*/tests/*` |
| `src.data.pg_store` | да, через `test_pg_store_integration.py` | нет (раньше исключали — отменили, см. §6) |
| `src.data.quality/*` | да | `pragma: no cover` ветки (см. §4) |

Гейт стоит на `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "-ra --strict-markers --strict-config --cov=src --cov-report=term-missing --cov-fail-under=95"
```

Если новый код роняет coverage ниже 95%, **сначала добавьте тесты**;
`# pragma: no cover` — последнее средство, и только если ветка реально
defensive (см. §4).

### Phase 1.1 sub-gate для Data Agent

`pytest.data.ini` — это **override** для прогонов только Data-слоя:

```bash
pytest tests/test_data_loader.py \
      --cov-config=pytest.data.ini \
      --cov=src.data \
      --cov-fail-under=75
```

Используйте его, когда правите `src/data/*` и хотите убедиться, что
ваш PR не сломал Data-покрытие, не дожидаясь полного CI-прогона.
`pytest-cov` без `--cov-config` подхватит `pyproject.toml` (где
`source = ["src"]`), и gate будет мерить всё, а не только Data.

---

## 3. Стратегии мокинга

Alphard мокает **три категории внешних поверхностей**:
Tinkoff Invest gRPC SDK, Postgres (`psycopg`) и shell-скрипты.

### 3.1 Tinkoff Invest SDK (`t_tech.invest.Client`)

SDK генерирует protobuf-классы, не имеет полноценных type stubs и активно
использует `Any`, поэтому юнит-тесты строят **реалистичный `MagicMock`**
с поверхностью, повторяющей реальные поля.

Канонический шаблон — `tests/test_broker_connector.py::_make_mock_tinkoff_client`:

```python
from unittest.mock import MagicMock

def _make_mock_tinkoff_client(cash=Decimal("1000000"), positions=None, ...):
    pos_mocks = []
    for p in positions or []:
        avg_price = p.get("avg_price", Decimal("0"))
        avg_price_q = MagicMock()
        avg_price_q.units = int(avg_price)
        avg_price_q.nano = int((avg_price - int(avg_price)) * Decimal("1000000000"))
        pos_mock = MagicMock()
        pos_mock.ticker = p["ticker"]
        pos_mock.quantity = p.get("quantity", Decimal("1"))
        pos_mock.average_position_price = avg_price_q
        pos_mock.average_buy_price = avg_price_q
        pos_mocks.append(pos_mock)
    portfolio_mock = MagicMock()
    portfolio_mock.positions = pos_mocks
    total_q = MagicMock()
    total_q.units = int(cash)
    total_q.nano = int((cash - int(cash)) * Decimal("1000000000"))
    portfolio_mock.total_amount_currencies = total_q

    client = MagicMock()
    client.users.get_accounts.return_value.accounts = [MagicMock(id="SB1")]
    client.operations.get_portfolio.return_value = portfolio_mock
    # ... last_prices, find_instrument, post_order stubs
    return client
```

**Правила:**

1. **Decimal через `units` + `nano`** — SDK возвращает деньги как
   protobuf `Quotation`, не `Decimal`. Mock должен повторять это
   (`avg_price_q.units = 100; avg_price_q.nano = 500_000_000` →
   `Decimal("100.5")`).
2. **`MagicMock(spec=[...])` для узких поверхностей** — когда
   нужно показать, что у объекта есть только одно поле, а остальные
   атрибуты не должны существовать (fail-fast на тестах контракта).
   Примеры: `test_fallback_loader.py::247`
   (`MagicMock(spec=["iter_ohlcv"])` — нет `iter_corporate_actions`).
3. **Не мокать весь `Client`** как один объект без отдельных полей —
   тест теряет реализм и падает на каждом новом методе SDK. Лучше
   собрать поверхность руками через helper.
4. **Fail-safe path покрывать явно** — `place_order` намеренно
   raises `BrokerError("no quote")` если ticker нет в `last_prices`.
   Helper генерирует то же исключение, чтобы тест видел реальный
   fail-safe, а не тихий mock-pass.

### 3.2 Postgres (`psycopg`)

Два режима:

| Режим | Когда | Файл |
|---|---|---|
| Skip | нет `ALPHARD_PG_DSN` | `pytestmark = pytest.mark.skipif(not os.environ.get("ALPHARD_PG_DSN"), reason=...)` |
| Run | `ALPHARD_PG_DSN` задан | `tests/test_audit_integration.py`, `tests/test_pg_store_integration.py` |

CI (`.github/workflows/ci.yml`) поднимает `postgres:16` сервис-контейнер
и выставляет DSN через env — интеграционные тесты гоняются на каждом push.

Локально:

```bash
export ALPHARD_PG_DSN="host=192.168.48.3 port=5432 dbname=alphard \
  user=alphard password=***"
pytest tests/test_audit_integration.py tests/test_pg_store_integration.py -v
```

**Mock для юнитов** — `MagicMock()` с `cursor.execute`, `connection.commit`,
`connection.rollback`. Не использовать `spec=psycopg.connection.Connection`,
если production-код обращается к полям, которых нет в публичной
поверхности psycopg (например, `_dsn`); `spec` ломает тест преждевременно.
См. `src/data/quality/audit.py:178` — `try/except ImportError` для psycopg
помечен `pragma: no cover`, потому что psycopg в CI — **hard dependency**.

### 3.3 Shell-скрипты (`entrypoint.sh`, `init_postgres.sh`)

Скрипты **не запускаются** в pytest. Вместо этого тесты читают их
через `Path(__file__).resolve().parent.parent / "scripts" / "<name>.sh"`
и парсят содержимое (regex/shlex). Канонический пример —
`tests/test_init_postgres_sh.py`:

```python
def test_no_zero_zero_line(self) -> None:
    body = (Path(__file__).resolve().parent.parent / "scripts" / "init_postgres.sh").read_text()
    no_comments = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    assert "0.0.0.0/0 trust" not in no_comments, (
        "init_postgres.sh must NOT prepend a 0.0.0.0/0 trust rule; "
        "it should be scoped to the internal subnet per the 2026-08-18 audit."
    )
```

Это guard против регрессии, а не функциональный тест. Если скрипт
ломается в рантайме, smoke-тест делается отдельно через
`bash -n <script>` (пример: `tests/test_init_postgres_sh.py::test_syntax_check`).

Новый шаблон для source-loop тестов (issue #295/#298): extract loop в
Python через `shlex.split`, исполни в `subprocess.run(["bash", "-c", ...])`
с очищенным env, проверь `stdout` на маркер sourced файла. См.
`tests/test_entrypoint_source_loop.py`.

---

## 4. Defensive branches и `pragma: no cover`

Coverage gate ≥95% — это контракт. Иногда defensive-ветка существует,
но недостижима в production (например, `except ValueError` на
exhaustive enum-match). Тогда мы помечаем её `# pragma: no cover`.

**Правило:** каждый `# pragma: no cover` сопровождается комментарием,
**почему** эта ветка unreachable в practice. Без комментария — review
зарежет.

Текущие `pragma: no cover` (на 2026-08-27):

| Файл | Строка | Причина |
|---|---|---|
| `src/data/quality/cross_source.py:396` | message construction в defensive NaN-poison ветке `_log_returns` | путь достижим только при corrupted state |
| `src/data/quality/cross_source.py:453` | `continue` при `any close` | defensive; pytest не строит такой фикстуры |
| `src/data/quality/severity.py:76` | `except ValueError` exhaustiveness guard | enum exhaustive; raise — это fail-fast |
| `src/data/quality/audit.py:178` | `try` для импорта psycopg | hard dep в CI |
| `src/data/quality/audit.py:180` | `except ImportError` | hard dep в CI |

Историческая ошибка (issue #258): `# pragma: no cover` ссылался на
`tests/test_pg_store_integration.py` как evidence, но тот файл **не
импортирует** `PostgresAuditLog`. Это давало ложное покрытие. Реальное
покрытие `PostgresAuditLog` теперь даёт `tests/test_audit_integration.py`
(3 теста: roundtrip, commit-on-close, `make_default_audit_log` с DSN),
а `pragma: no cover` на `_cursor.execute` и `finally: self._conn.close()`
сняты.

**Если вы добавляете новый `# pragma: no cover`:**

1. Напишите комментарий с конкретной причиной.
2. Поднимите вопрос в PR: можно ли написать тест, который всё-таки
   пройдёт по этой ветке (например, corrupted fixture, monkeypatch
   возвращающий `None`).
3. Если тест невозможен — добавьте ссылку на внешнюю причину
   (env-only, race condition которую не воспроизвести в pytest).

---

## 5. Skip policy

Текущая таксономия (на 2026-08-27):

| Skip | Trigger | Файл |
|---|---|---|
| `pytest.mark.skipif(not _real_token() or not os.environ.get("ALPHARD_PG_DSN"))` | live Coordinator smoke | `tests/test_coordinator_live.py:26` (pytestmark на module) |
| `pytest.mark.skipif(shutil.which("docker") is None)` | bash syntax check | `tests/test_init_postgres_sh.py:87` |
| `pytest.mark.skip(reason="...")` внутри теста | conditional, не fixture-level | `tests/test_quality_integration.py` |

Все skips **обязаны** иметь `reason=`. `--strict-markers` в `addopts`
зарежет маркер без описания.

**Skip-when-host-state** (issue #298) — отдельный паттерн: тест
проверяет **состояние хоста**, а не returncode. Пример:

```python
def test_root_env_picked_when_no_env_file_override(self, extracted_loop: str) -> None:
    root_env = Path("/root/.env")
    try:
        root_env.exists()
    except PermissionError:
        pytest.skip("/root/.env is not stat-able by this test runner")
    if not root_env.exists():
        pytest.skip("/root/.env not bind-mounted on this host (CI runner without compose)")
    # ...
```

Этот паттерн **предпочтительнее** runtime-скіпа по `sourced is None`,
когда тест зависит от инфраструктуры (compose bind-mount, secrets
volume, etc.). Runtime-skip маскирует регрессии: тест может
проходить, ничего не проверяя, а потом fail-fast в production.

---

## 6. Тестовые фикстуры (`conftest.py`)

`tests/conftest.py` содержит **autouse** фикстуры, которые
применяются к каждому тесту в suite:

| Фикстура | Что делает | Зачем |
|---|---|---|
| `_reset_moex_cache_per_test` | `apply_corporate_actions._MOEX_CACHE.clear()` до и после теста | issue #140: module-level cache иначе течёт между тестами |
| `_isolate_alphard_peak_store_dir` | `monkeypatch.setenv("ALPHARD_PEAK_STORE_DIR", tmpdir)` | issue #220: TinkoffAccount пишет `peak_equity_<acc>.json`; без изоляции следующий тест на следующий день ловит `BrokerError("Untrusted daily-P&L basis ... calendar mismatch")` |

**Правило:** если вы пишете тест, который пишет на диск или
держит module-level state — добавьте autouse-фикстуру в `conftest.py`
**вместо** ручного `try/finally` в каждом тесте. Это гарантирует, что
будущие тесты на тот же модуль автоматически получат изоляцию.

---

## 7. CI gates

`Tests + Coverage` job в `.github/workflows/ci.yml`:

1. Запускает `postgres:16` service container.
2. Устанавливает deps + `-e .`.
3. Прогоняет `pytest --cov=src --cov-report=term-missing` (без
   `--cov-fail-under` — gate приходит из `pyproject.toml`).

Другие jobs, которые должны быть зелёными перед merge:

| Job | Что проверяет |
|---|---|
| `Lint + Format` | flake8, black |
| `Ops policy` | нет literal Grafana password, нет anonymous auth |
| `SCA (pip-audit)` | known CVEs в зависимостях |
| `Secrets scan (gitleaks)` | нет секретов в diff |
| `Grafana secrets guard` | дополнительный gate на grafana-секреты |
| `Build + push` | docker image собирается |

Если **любой** job красный — PR не мёрджится. Ветка `main` защищена
branch protection.

---

## 8. Anti-patterns (что не делать)

1. **Не отключать coverage gate** комментарием `# pragma: no cover`
   на production-логике. Это для defensive-веток, не для lazy-тестов.
2. **Не мокать `t_tech.invest.Client` как один `MagicMock()` без полей**
   — тест теряет реализм, ломается на каждом новом SDK-методе.
3. **Не гонять `pytest -k "skip"` для подсчёта skipped** — skips
   приходят из `pytest.mark.skipif`, а не из имени теста.
4. **Не добавлять `print()` в тестах** — используйте
   `pytest -s` для отладки или `caplog` для проверки логов.
5. **Не использовать `time.sleep()`** для ожидания асинхронной
   логики — мокайте или используйте `asyncio.sleep` mock.
6. **Не оставлять реальные секреты** в тестовых фикстурах даже
   через `.env` — gitleaks зарежет, но это лишний round-trip.
7. **Не править `pyproject.toml` coverage gate** (`95 → 94`) для
   проноса вашего PR — это уже случалось (PR #255, issue #257).
   Лучше добавить тест.

---

## 9. Куда добавлять новые тесты

| Если вы правите | Добавьте тесты в | Имя файла |
|---|---|---|
| `src/risk/gate.py` | `tests/` | `test_<feature>.py` |
| `src/broker/tinkoff_account.py` | `tests/` | используйте `_make_mock_tinkoff_client` |
| `src/data/quality/*.py` | `tests/` | наследуйте `MagicMock(spec=[...])` паттерн |
| `scripts/<script>.sh` | `tests/` | парсите regex/shlex (см. §3.3) |
| `docker/entrypoint.sh` | `tests/` | extract loop → bash subprocess.run |
| `docker-compose.yaml` | `tests/test_compose_structure.py` | yaml-load + ast-проверки |
| `.env.example` или schema | `tests/test_env_schema*.py` | roundtrip через pydantic |

Если новый модуль требует Postgres — откройте `tests/test_*_integration.py`
и добавьте `pytestmark = pytest.mark.skipif(not os.environ.get("ALPHARD_PG_DSN"))`.

---

## 10. Полезные однострочники

```bash
# Только упавшие
pytest --lf

# Без coverage, verbose
pytest -v --no-cov

# Тесты по конкретному файлу без coverage gate
pytest tests/test_x.py --no-cov -v

# Покрытие конкретного модуля
pytest --cov=src.data --cov-report=term-missing

# Проверить, что flake8/black/mypy чистые
flake8 src/ tests/
black --check src/ tests/
mypy src/

# Все gates одной командой (как pre-commit)
pytest && flake8 src/ tests/ && black --check src/ tests/ && mypy src/

# Проверить, что secrets не утекли
pre-commit run --all-files
```

---

## См. также

- `CONTRIBUTING.md` — общие правила участия (fork → branch → PR).
- `docs/SECURITY.md` — threat model и runbook для security-релевантных PR.
- `docs/RUNBOOK.md` — incident response.
- `docs/decisions/0006-position-sizing.md`, `0007-rebalance-scheduler.md` —
  ADR-паттерн для архитектурных решений (не путать с test decisions).
- `pyproject.toml` — единственный источник правды для coverage gate.
- `.github/workflows/ci.yml` — полный список CI jobs.