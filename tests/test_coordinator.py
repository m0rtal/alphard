"""Tests for the Coordinator stub (Phase 1.5) — mocked pipeline paths."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.coordinator import (
    Coordinator,
    CoordinatorConfig,
    CoordinatorSide,
    PipelineResult,
    PipelineStage,
)

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def _limits() -> MagicMock:
    return MagicMock(
        max_dd_pct=Decimal("10"),
        max_position_pct=Decimal("10"),
        max_sector_pct=Decimal("30"),
        max_daily_loss_pct=Decimal("3"),
    )


def _config(**overrides: object) -> CoordinatorConfig:
    """Build CoordinatorConfig; kwargs override defaults."""
    kwargs: dict[str, object] = {
        "ticker": "SBER",
        "side": CoordinatorSide.BUY,
        "quantity": Decimal("1"),
        "limit_price": Decimal("100"),
        "risk_limits": _limits(),
        "portfolio_equity": Decimal("1000000"),
        "portfolio_cash": Decimal("1000000"),
        "portfolio_peak": Decimal("1000000"),
        "live_trading": False,
        "store_dsn": None,
    }
    kwargs.update(overrides)
    return CoordinatorConfig(**kwargs)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# Config dataclass
# -----------------------------------------------------------------------------


class TestCoordinatorConfig:
    def test_config_is_immutable(self) -> None:
        cfg = _config()
        with pytest.raises(Exception):
            cfg.ticker = "GAZP"  # type: ignore[misc]

    def test_config_holds_all_required_fields(self) -> None:
        cfg = _config()
        assert cfg.ticker == "SBER"
        assert cfg.side == CoordinatorSide.BUY
        assert cfg.quantity == Decimal("1")
        assert cfg.live_trading is False

    # Issue #238 — defense-in-depth ticker normalisation at construction.
    # Sister of #234/#236 (per-function normalisation in AdvProvider /
    # _ticker_to_figi). The audit-log writer was the ONE path that
    # persisted the raw ticker, breaking the grep-by-ticker invariant
    # for the canonical compliance trail.
    def test_config_normalises_lowercase_ticker(self) -> None:
        """Lowercase operator input → UPPERCASE canonical form."""
        cfg = _config(ticker="sber")
        assert cfg.ticker == "SBER"

    def test_config_normalises_whitespace_around_ticker(self) -> None:
        """Surrounding whitespace is stripped during normalisation."""
        cfg = _config(ticker="  gaZp\n")
        assert cfg.ticker == "GAZP"

    def test_config_rejects_empty_ticker_after_normalisation(self) -> None:
        """Whitespace-only ticker is rejected at construction."""
        with pytest.raises(ValueError, match="must be non-empty"):
            _config(ticker="   ")


# -----------------------------------------------------------------------------
# Coordinator.run_once() — full mocked paths
# -----------------------------------------------------------------------------


def _bar() -> MagicMock:
    """Minimal OHLCVRow stand-in."""
    today = date.today()
    bar = MagicMock()
    bar.ts = today - timedelta(days=1)
    bar.open = Decimal("100")
    bar.high = Decimal("101")
    bar.low = Decimal("99")
    bar.close = Decimal("100.5")
    bar.volume = Decimal("1000")
    return bar


class TestCoordinatorFullPipeline:
    """Mock the entire pipeline to test internal coordination."""

    def test_fetch_failure_short_circuits_with_FETCH_ERROR(self) -> None:
        """FETCH fails → pipeline returns immediately with FETCH_ERROR violation."""
        with patch.object(Coordinator, "_fetch", side_effect=RuntimeError("Tinkoff unreachable")):
            result = Coordinator(_config()).run_once()
        assert result.stages_completed == ()
        assert result.bars_loaded == 0
        assert "FETCH_ERROR" in result.risk_violations
        assert result.broker_status is None

    def test_fetch_returns_empty_bars_skips_validation(self) -> None:
        """Empty fetch → VALIDATE returns False → SKIPPED stage."""
        with patch.object(Coordinator, "_fetch", return_value=[]):
            result = Coordinator(_config()).run_once()
        assert result.bars_loaded == 0

    def test_validate_critical_returns_skipped(self) -> None:
        """VALIDATE returns False → SKIPPED, no risk check, no broker call."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=False),
        ):
            result = Coordinator(_config()).run_once()
        assert PipelineStage.SKIPPED in result.stages_completed
        assert "VALIDATE_CRITICAL" in result.risk_violations
        assert result.risk_allowed is False
        assert result.broker_status is None

    def test_validate_raises_blocks_pipeline(self) -> None:
        """Issue #15 (C.1): if _validate raises, the pipeline must NOT
        proceed to RISK or EXECUTE — unvalidated data must never reach
        the risk gate or the broker. We now block with VALIDATE_EXCEPTION
        and risk_allowed=False."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", side_effect=RuntimeError("gate fail")),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="REJECTED_LIVE_TRADING_FALSE"),
        ):
            result = Coordinator(_config()).run_once()
        assert PipelineStage.RISK not in result.stages_completed
        assert PipelineStage.EXECUTE not in result.stages_completed
        assert result.risk_allowed is False
        assert "VALIDATE_EXCEPTION" in result.risk_violations
        assert result.broker_status is None

    def test_risk_check_raises_blocks_broker_even_when_live_trading_true(
        self,
    ) -> None:
        """Issue #15 (C.2): if _risk_check raises, the broker must NOT be
        called even when LIVE_TRADING=true. The historical test
        ``test_risk_check_exception_does_not_block_execute`` enshrined
        the opposite behaviour; that test is replaced by this one."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(
                Coordinator,
                "_risk_check",
                side_effect=RuntimeError("risk engine down"),
            ),
            patch.object(Coordinator, "_execute", return_value="FILLED") as exec_mock,
        ):
            result = Coordinator(_config(live_trading=True)).run_once()
        assert PipelineStage.RISK not in result.stages_completed
        assert PipelineStage.EXECUTE not in result.stages_completed
        assert "RISK_EXCEPTION" in result.risk_violations
        assert result.risk_allowed is False
        assert result.broker_status is None
        # Critical: _execute must NOT have been called.
        exec_mock.assert_not_called()

    def test_toctou_window_blocks_when_too_slow(self) -> None:
        """Issue #15 (C.3): if the gap between RISK and EXECUTE exceeds
        ``toctou_max_seconds``, the trade is blocked even when both
        stages individually succeed. We patch ``time.monotonic`` to
        simulate a slow downstream caller."""
        cfg = _config(live_trading=True, toctou_max_seconds=0.050)

        # First call (in RISK) returns t=100.0, second call (in TOCTOU
        # guard) returns t=100.5 → 0.5s elapsed, > 50ms window.
        monotonic_values = iter([100.0, 100.5])
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="FILLED") as exec_mock,
            patch("src.coordinator.time.monotonic", side_effect=lambda: next(monotonic_values)),
        ):
            result = Coordinator(cfg).run_once()
        assert PipelineStage.RISK in result.stages_completed
        assert PipelineStage.EXECUTE not in result.stages_completed
        assert "TOCTOU_STATE_STALE" in result.risk_violations
        assert result.risk_allowed is False
        assert result.broker_status is None
        exec_mock.assert_not_called()

    def test_toctou_window_passes_when_fast_enough(self) -> None:
        """Issue #15 (C.3) positive case: the gap is well within the
        window, so the trade proceeds normally."""
        cfg = _config(live_trading=True, toctou_max_seconds=1.0)
        monotonic_values = iter([100.0, 100.001])
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="FILLED"),
            patch("src.coordinator.time.monotonic", side_effect=lambda: next(monotonic_values)),
        ):
            result = Coordinator(cfg).run_once()
        assert result.broker_status == "FILLED"
        assert PipelineStage.EXECUTE in result.stages_completed

    def test_risk_allowed_live_trading_false_blocks_at_broker(self) -> None:
        """Risk passes but LIVE_TRADING=false → broker_status REJECTED_LIVE_TRADING_FALSE."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
        ):
            result = Coordinator(_config(live_trading=False)).run_once()
        assert result.risk_allowed is True
        assert result.broker_status == "REJECTED_LIVE_TRADING_FALSE"

    def test_risk_denied_blocks_broker_live_trading_true(self) -> None:
        """Risk fails AND LIVE_TRADING=true → still blocks at risk gate."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(False, ("RISK_DD: 12% > 10%",))),
            patch.object(Coordinator, "_execute", return_value="REJECTED_RISK_GATE"),
        ):
            result = Coordinator(_config(live_trading=True)).run_once()
        assert result.risk_allowed is False
        assert "RISK_DD" in result.risk_violations[0]
        assert result.broker_status == "REJECTED_RISK_GATE"

    def test_risk_allowed_live_trading_true_calls_execute(self) -> None:
        """Risk passes AND LIVE_TRADING=true → _execute called, result returned."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="FILLED"),
        ):
            result = Coordinator(_config(live_trading=True)).run_once()
        assert result.broker_status == "FILLED"

    def test_broker_raises_returns_error_status(self) -> None:
        """If _execute raises (broker unavailable), main loop catches and returns ERROR:..."""
        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(Coordinator, "_execute", return_value="ERROR:RuntimeError"),
        ):
            result = Coordinator(_config(live_trading=True)).run_once()
        assert result.broker_status == "ERROR:RuntimeError"


# -----------------------------------------------------------------------------
# Coordinator._audit()
# -----------------------------------------------------------------------------


class TestCoordinatorAudit:
    def test_audit_skips_when_no_dsn(self) -> None:
        """No store_dsn → audit returns None silently."""
        coord = Coordinator(_config(store_dsn=None))
        result = coord._audit(
            stages=[PipelineStage.FETCH, PipelineStage.DONE],
            bars_loaded=10,
            risk_allowed=False,
            risk_violations=(),
            broker_status=None,
        )
        assert result is None

    def test_audit_persists_to_postgres(self) -> None:
        """Audit inserts JSONB row, returns id."""
        from unittest.mock import MagicMock

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (42,)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value.__enter__.return_value = mock_conn

        with patch.dict("sys.modules", {"psycopg": mock_psycopg}):
            coord = Coordinator(_config(store_dsn="postgresql://fake"))
            result = coord._audit(
                stages=[PipelineStage.FETCH, PipelineStage.DONE],
                bars_loaded=10,
                risk_allowed=True,
                risk_violations=(),
                broker_status="FILLED",
            )
        assert result == 42
        mock_psycopg.connect.assert_called_once()

    # Issue #238 — the audit-log payload must use the canonical
    # UPPERCASE ticker even if the operator constructed the config
    # with a lowercase string. This is the audit-log grep invariant
    # that the rest of the system enforces (#234, #236).
    def test_audit_persists_normalised_ticker_when_config_built_lowercase(self) -> None:
        import json

        from unittest.mock import MagicMock

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (7,)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value.__enter__.return_value = mock_conn

        with patch.dict("sys.modules", {"psycopg": mock_psycopg}):
            coord = Coordinator(_config(store_dsn="postgresql://fake", ticker="sber"))
            coord._audit(
                stages=[PipelineStage.FETCH, PipelineStage.DONE],
                bars_loaded=1,
                risk_allowed=True,
                risk_violations=(),
                broker_status="FILLED",
            )

        # The INSERT must carry ticker="SBER", never "sber".
        exec_call = mock_cursor.execute.call_args
        insert_args = exec_call.args[1]  # (ticker, payload)
        persisted_ticker = insert_args[0]
        persisted_payload = json.loads(insert_args[1])
        assert persisted_ticker == "SBER"
        assert persisted_payload["ticker"] == "SBER"


# -----------------------------------------------------------------------------
# Coordinator._fetch()
# -----------------------------------------------------------------------------


class TestCoordinatorFetch:
    def test_fetch_calls_loader_with_short_window(self) -> None:
        """_fetch uses 2-day window to minimise API call payload."""
        with patch("src.data.tinkoff_loader.TinkoffInvestDataLoader") as mock_loader:
            mock_instance = MagicMock()
            mock_instance.fetch_ohlcv.return_value = [_bar()]
            mock_loader.return_value = mock_instance

            coord = Coordinator(_config())
            bars = coord._fetch()

        assert len(bars) == 1
        # Verify fetch_ohlcv was called with start/end dates derived from
        # CoordinatorConfig.fetch_lookback_days (issue #26: previously this
        # was hardcoded to timedelta(days=2), which left only ~3 bars
        # reaching VALIDATE. The test now verifies that the Coordinator
        # delegates to the configured lookback rather than overriding it.)
        call_args = mock_instance.fetch_ohlcv.call_args
        args = call_args.args  # positional (ticker, start, end)
        assert args[2] - args[1] == timedelta(days=1825)  # default 5*365


# -----------------------------------------------------------------------------
# Coordinator._validate()
# -----------------------------------------------------------------------------


class TestCoordinatorValidate:
    def test_validate_empty_bars_returns_false(self) -> None:
        """No bars → validation fails (skips pipeline)."""
        coord = Coordinator(_config())
        assert coord._validate([]) is False

    def test_validate_quality_gate_exception_propagates(self) -> None:
        """Issue #15 (C.1): _validate no longer swallows exceptions. The
        historical behaviour was `return True` on gate crash (fail-open,
        dangerous). The new behaviour is to propagate the exception so
        run_once() can convert it into a fail-safe VALIDATE_EXCEPTION
        block."""
        with patch(
            "src.data.quality.ingestion_gate.check_ingestion",
            side_effect=RuntimeError("gate crash"),
        ):
            coord = Coordinator(_config())
            with pytest.raises(RuntimeError, match="gate crash"):
                coord._validate([_bar()])


# -----------------------------------------------------------------------------
# Coordinator._risk_check()
# -----------------------------------------------------------------------------


class TestCoordinatorRiskCheck:
    def test_risk_check_returns_decision(self) -> None:
        """Risk check returns (allowed, violations) tuple from gate."""
        with patch("src.risk.gate.RiskGate") as mock_gate_class:
            mock_gate = MagicMock()
            mock_gate.evaluate.return_value = MagicMock(allowed=True, violations=())
            mock_gate_class.return_value = mock_gate

            coord = Coordinator(_config())
            allowed, violations = coord._risk_check()

        assert allowed is True
        assert violations == ()

    def test_risk_check_zero_limit_price_returns_structured_violation(self) -> None:
        """Issue #211: ``limit_price=0`` (market-order-style input) must NOT
        raise a pydantic ``ValidationError`` from inside ``TradeIntent``
        construction. Pre-fix, the exception escaped into
        ``run_once()``'s broad ``except Exception`` and surfaced as
        ``RISK_EXCEPTION`` — masking a config error as a gate crash.
        The fix pre-validates ``limit_price > 0`` and returns a
        structured ``RISK_LIMIT_PRICE_INVALID`` violation instead, so
        the gate is never even constructed and the audit log carries
        a precise cause.
        """
        # gate must NOT be called at all on this path.
        with patch("src.risk.gate.RiskGate") as mock_gate_class:
            mock_gate = MagicMock()
            mock_gate_class.return_value = mock_gate

            coord = Coordinator(_config(limit_price=Decimal("0")))
            allowed, violations = coord._risk_check()

        assert allowed is False
        assert len(violations) == 1
        assert "RISK_LIMIT_PRICE_INVALID" in violations[0]
        assert "limit_price" in violations[0]
        # Gate was never asked to evaluate — fail-safe: the validation
        # error is local to the Coordinator layer, so the gate can't
        # have been invoked.
        mock_gate.evaluate.assert_not_called()

    def test_risk_check_negative_limit_price_returns_structured_violation(self) -> None:
        """Issue #211: same path as zero price — negative limit_price
        also fails ``TradeIntent.price gt=0``. We pre-validate at the
        Coordinator layer so the gate stays in a clean state."""
        with patch("src.risk.gate.RiskGate") as mock_gate_class:
            mock_gate = MagicMock()
            mock_gate_class.return_value = mock_gate

            coord = Coordinator(_config(limit_price=Decimal("-5")))
            allowed, violations = coord._risk_check()

        assert allowed is False
        assert "RISK_LIMIT_PRICE_INVALID" in violations[0]
        mock_gate.evaluate.assert_not_called()

    def test_risk_check_zero_price_does_not_raise_validation_error(self) -> None:
        """Issue #211 (regression guard): ``_risk_check`` must NEVER
        raise a pydantic ``ValidationError`` regardless of
        ``limit_price`` value. Pre-fix, ``Decimal('0')`` propagated
        out of ``TradeIntent(...)`` and was caught by ``run_once()``'s
        blanket ``except Exception`` — silent masking of a
        programmer / config error as a runtime risk failure.
        """
        coord = Coordinator(_config(limit_price=Decimal("0")))
        # The call must return normally, not raise.
        result = coord._risk_check()
        assert isinstance(result, tuple)
        assert len(result) == 2
        allowed, violations = result
        assert allowed is False
        assert isinstance(violations, tuple)

    def test_run_once_zero_limit_price_records_RISK_LIMIT_PRICE_INVALID(
        self,
    ) -> None:
        """Issue #211 (end-to-end): the full pipeline, with
        ``limit_price=0``, must short-circuit at the RISK stage with
        ``RISK_LIMIT_PRICE_INVALID`` (not the generic
        ``RISK_EXCEPTION``), and the broker must NOT be called with
        ``risk_allowed=True``. This is the regression guard for the
        original symptom: a market-order CoordinatorConfig leaking
        through every layer until it crashed at TradeIntent
        construction.
        """
        with patch.object(Coordinator, "_fetch", return_value=[_bar()]):
            with patch.object(Coordinator, "_validate", return_value=True):
                with patch.object(Coordinator, "_execute") as mock_execute:
                    result = Coordinator(_config(limit_price=Decimal("0"), live_trading=True)).run_once()

        assert result.risk_allowed is False
        assert len(result.risk_violations) == 1
        assert "RISK_LIMIT_PRICE_INVALID" in result.risk_violations[0]
        # The pipeline reached ``_execute`` with ``risk_allowed=False``
        # — that's the short-circuit. ``_execute`` itself is the
        # layer that refuses the broker call when ``risk_allowed``
        # is False, so the assertion below is the regression guard
        # against accidentally passing ``risk_allowed=True`` through
        # to the broker. Without the fix, ``run_once`` would never
        # reach ``_execute`` (it would crash at TradeIntent
        # construction with ValidationError, surfacing as
        # ``RISK_EXCEPTION``).
        mock_execute.assert_called_once_with(False)
        # The pipeline recorded the normal stages (FETCH/VALIDATE/RISK/
        # EXECUTE/AUDIT/DONE) — the refusal happened *inside* the
        # risk stage via a structured violation code, not as an
        # exception that would have produced SKIPPED. This is the
        # whole point of the fix: we no longer fail-open with a
        # masked ValidationError; we surface a precise refusal.
        assert result.stages_completed == (
            PipelineStage.FETCH,
            PipelineStage.VALIDATE,
            PipelineStage.RISK,
            PipelineStage.EXECUTE,
            PipelineStage.AUDIT,
            PipelineStage.DONE,
        )


# -----------------------------------------------------------------------------
# Coordinator.run_once() — risk-gate exception path (issue #15)
# -----------------------------------------------------------------------------
#
# The historical test ``test_risk_check_exception_does_not_block_execute``
# (formerly in TestCoordinatorRiskException) ENshrined the FAIL-OPEN
# behaviour: it asserted that the broker was still consulted even when
# _risk_check raised. That test is REMOVED in issue #15 — the new
# contract is that _risk_check exceptions BLOCK the broker. The new
# behaviour is verified by
# ``test_risk_check_raises_blocks_broker_even_when_live_trading_true``
# in TestCoordinatorFullPipeline above.
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Coordinator._validate() — severity-branching (lines 267-270)
# -----------------------------------------------------------------------------


class TestCoordinatorValidateSeverity:
    def test_validate_worst_severity_none_returns_true(self) -> None:
        """check_ingestion returns report with worst_severity() == None → True."""
        mock_report = MagicMock()
        mock_report.worst_severity.return_value = None
        with patch(
            "src.data.quality.ingestion_gate.check_ingestion",
            return_value=mock_report,
        ):
            coord = Coordinator(_config())
            assert coord._validate([_bar()]) is True

    def test_validate_worst_severity_critical_returns_false(self) -> None:
        """Worst severity CRITICAL → False (block pipeline)."""
        from src.data.quality.severity import Severity

        mock_report = MagicMock()
        mock_report.worst_severity.return_value = Severity.CRITICAL
        with patch(
            "src.data.quality.ingestion_gate.check_ingestion",
            return_value=mock_report,
        ):
            coord = Coordinator(_config())
            assert coord._validate([_bar()]) is False

    def test_validate_worst_severity_high_returns_false(self) -> None:
        """Worst severity HIGH → False (block pipeline)."""
        from src.data.quality.severity import Severity

        mock_report = MagicMock()
        mock_report.worst_severity.return_value = Severity.HIGH
        with patch(
            "src.data.quality.ingestion_gate.check_ingestion",
            return_value=mock_report,
        ):
            coord = Coordinator(_config())
            assert coord._validate([_bar()]) is False


# -----------------------------------------------------------------------------
# Coordinator._validate() — multi-source dedup (issue #102)
# -----------------------------------------------------------------------------


class TestCoordinatorValidateMultiSourceDedup:
    """Issue #102: Phase 2.6 step 2 multi-source schema lets one
    (ticker, ts) have multiple OHLCVRow entries — one per source tag.
    The ingestion gate treats every Bar as an independent observation,
    so feeding the gate 2× the bar list would inflate the row count,
    double-count volume, and let a ticker with only 126 unique dates
    squeak past min_history_rows=252. _validate must dedup by ts.
    """

    def _bar_for_ts(self, ts: date, volume: int = 1000) -> MagicMock:
        bar = MagicMock()
        bar.ts = ts
        bar.open = Decimal("100")
        bar.high = Decimal("101")
        bar.low = Decimal("99")
        bar.close = Decimal("100.5")
        bar.volume = Decimal(volume)
        return bar

    def test_validate_dedup_duplicate_ts_same_source(self) -> None:
        """Same ts appearing twice (e.g. two source='tkf' rows from a
        noisy loader) — _validate must collapse to one Bar per ts.
        """
        seen_bars: list = []
        mock_report = MagicMock()
        mock_report.worst_severity.return_value = None

        def _capture(*args, **kwargs):
            seen_bars.extend(args[1])
            return mock_report

        today = date.today()
        d1 = today - timedelta(days=1)
        d2 = today - timedelta(days=2)
        bars_input = [
            self._bar_for_ts(d1),
            self._bar_for_ts(d1),  # duplicate of d1
            self._bar_for_ts(d2),
            self._bar_for_ts(d2),  # duplicate of d2
            self._bar_for_ts(d2),  # third copy of d2
        ]
        with patch(
            "src.data.quality.ingestion_gate.check_ingestion",
            side_effect=_capture,
        ):
            coord = Coordinator(_config())
            result = coord._validate(bars_input)
        assert result is True
        # 5 input rows → 2 unique dates → 2 Bars handed to the gate.
        assert len(seen_bars) == 2
        seen_keys = {b.primary_key for b in seen_bars}
        assert seen_keys == {d1, d2}

    def test_validate_dedup_multi_source_keeps_first_seen(self) -> None:
        """Multi-source rows for the same ts: query_ohlcv orders by
        (ts, source), so source='tkf' arrives before source='moex'.
        _validate must keep the FIRST occurrence (tkf), not the last.
        """
        seen_bars: list = []
        mock_report = MagicMock()
        mock_report.worst_severity.return_value = None

        def _capture(*args, **kwargs):
            seen_bars.extend(args[1])
            return mock_report

        today = date.today()
        d1 = today - timedelta(days=1)
        # Simulate query_ohlcv ORDER BY ts, source: tkf first, moex second.
        tkf_bar = self._bar_for_ts(d1, volume=1000)
        tkf_bar.source = "tkf"
        moex_bar = self._bar_for_ts(d1, volume=2000)
        moex_bar.source = "moex"

        with patch(
            "src.data.quality.ingestion_gate.check_ingestion",
            side_effect=_capture,
        ):
            coord = Coordinator(_config())
            coord._validate([tkf_bar, moex_bar])
        assert len(seen_bars) == 1
        # First-seen wins → tkf volume, not moex.
        assert int(seen_bars[0].volume) == 1000


# -----------------------------------------------------------------------------
# Coordinator._execute() — risk-denied, success, and broker-error (lines 303-320)
# -----------------------------------------------------------------------------


class TestCoordinatorExecute:
    def test_execute_live_trading_false_returns_refused(self) -> None:
        """LIVE_TRADING=False → refuses before touching the broker."""
        coord = Coordinator(_config(live_trading=False))
        assert coord._execute(risk_allowed=True) == "REJECTED_LIVE_TRADING_FALSE"

    def test_execute_risk_denied_returns_risk_gate_rejected(self) -> None:
        """LIVE_TRADING=True AND risk_allowed=False → REJECTED_RISK_GATE.

        This is the early-return branch at lines 303-304; no broker call
        is made even though the broker would otherwise be reachable.
        """
        coord = Coordinator(_config(live_trading=True))
        with patch("src.broker.tinkoff_account.TinkoffAccount") as mock_account:
            status = coord._execute(risk_allowed=False)
        assert status == "REJECTED_RISK_GATE"
        mock_account.from_env.assert_not_called()

    def test_execute_success_returns_status_value(self) -> None:
        """LIVE_TRADING=True, risk_allowed=True, broker OK → str(status.value).

        The coordinator source calls ``TinkoffAccount.from_env()`` with a
        ``# type: ignore[attr-defined]`` (the real symbol is now a
        module-level function, but the coordinator's lookup is on the
        class). We patch the class attribute to match the actual call site.
        """
        from src.broker.orders import OrderStatus
        from src.broker.tinkoff_account import TinkoffAccount

        mock_account = MagicMock()
        mock_account.place_order.return_value = OrderStatus.FILLED
        with patch.object(TinkoffAccount, "from_env", return_value=mock_account, create=True):
            coord = Coordinator(_config(live_trading=True))
            status = coord._execute(risk_allowed=True)
        assert status == str(OrderStatus.FILLED.value)

    def test_execute_broker_exception_returns_error_status(self) -> None:
        """Broker raises → 'ERROR:<ExceptionType>'."""
        from src.broker.tinkoff_account import TinkoffAccount

        with patch.object(
            TinkoffAccount,
            "from_env",
            side_effect=ConnectionError("Tinkoff API down"),
            create=True,
        ):
            coord = Coordinator(_config(live_trading=True))
            status = coord._execute(risk_allowed=True)
        assert status == "ERROR:ConnectionError"


# -----------------------------------------------------------------------------
# Coordinator._audit() — exception handler (lines 367-369)
# -----------------------------------------------------------------------------


class TestCoordinatorAuditErrors:
    def test_audit_returns_none_when_psycopg_unavailable(self) -> None:
        """If import psycopg / connect / execute fails, audit returns None.

        Simulates the except handler at lines 367-369 by patching the
        psycopg.connect call to raise during execution.
        """
        mock_psycopg = MagicMock()
        mock_psycopg.connect.side_effect = OSError("postgres down")

        with patch.dict("sys.modules", {"psycopg": mock_psycopg}):
            coord = Coordinator(_config(store_dsn="postgresql://fake"))
            result = coord._audit(
                stages=[PipelineStage.FETCH, PipelineStage.DONE],
                bars_loaded=10,
                risk_allowed=True,
                risk_violations=(),
                broker_status="FILLED",
            )
        assert result is None

    def test_audit_returns_none_when_cursor_returns_no_row(self) -> None:
        """INSERT ... RETURNING id but fetchone() returns None → None."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value.__enter__.return_value = mock_conn

        with patch.dict("sys.modules", {"psycopg": mock_psycopg}):
            coord = Coordinator(_config(store_dsn="postgresql://fake"))
            result = coord._audit(
                stages=[PipelineStage.FETCH, PipelineStage.DONE],
                bars_loaded=10,
                risk_allowed=True,
                risk_violations=(),
                broker_status="FILLED",
            )
        assert result is None


# -----------------------------------------------------------------------------
# PipelineResult — decided property and to_dict (lines 130, 135)
# -----------------------------------------------------------------------------


class TestPipelineResult:
    def _make_result(self, **overrides: object) -> PipelineResult:
        kwargs: dict[str, object] = {
            "config": _config(),
            "stages_completed": (PipelineStage.FETCH,),
            "bars_loaded": 1,
            "risk_allowed": False,
            "risk_violations": (),
            "broker_status": None,
            "audit_log_id": None,
        }
        kwargs.update(overrides)
        return PipelineResult(**kwargs)  # type: ignore[arg-type]

    def test_decided_true_when_execute_stage_present(self) -> None:
        result = self._make_result(
            stages_completed=(PipelineStage.FETCH, PipelineStage.EXECUTE),
            broker_status="FILLED",
        )
        assert result.decided is True

    def test_decided_true_when_done_stage_with_real_broker_status(self) -> None:
        """Issue #99: DONE stage alone is no longer sufficient — must also
        carry a non-rejection broker_status."""
        result = self._make_result(
            stages_completed=(PipelineStage.FETCH, PipelineStage.DONE),
            broker_status="FILLED",
        )
        assert result.decided is True

    def test_decided_false_when_done_but_broker_none(self) -> None:
        """Issue #99: DONE appended unconditionally after a blocked run;
        decided must remain False when no broker response was received."""
        result = self._make_result(
            stages_completed=(PipelineStage.FETCH, PipelineStage.DONE),
            broker_status=None,
        )
        assert result.decided is False

    def test_decided_false_when_neither_execute_nor_done(self) -> None:
        result = self._make_result(
            stages_completed=(PipelineStage.FETCH, PipelineStage.SKIPPED),
            broker_status=None,
        )
        assert result.decided is False

    @pytest.mark.parametrize(
        "broker_status",
        [
            "REJECTED_LIVE_TRADING_FALSE",
            "REJECTED_RISK_GATE",
            "ERROR:ChildProcessError",
            "ERROR:ConnectionError",
        ],
    )
    def test_decided_false_for_all_rejection_paths(self, broker_status: str) -> None:
        """Issue #99: every REJECTED_* / ERROR:* broker_status must
        produce decided=False even when the pipeline reached DONE."""
        result = self._make_result(
            stages_completed=(
                PipelineStage.FETCH,
                PipelineStage.RISK,
                PipelineStage.EXECUTE,
                PipelineStage.AUDIT,
                PipelineStage.DONE,
            ),
            broker_status=broker_status,
        )
        assert result.decided is False

    @pytest.mark.parametrize(
        "broker_status",
        ["FILLED", "NEW", "REJECTED_BY_EXCHANGE", "PARTIALLY_FILLED"],
    )
    def test_decided_true_for_real_broker_responses(self, broker_status: str) -> None:
        """Real broker outcomes (FILLED, NEW, exchange-level rejections,
        partial fills) all mean the pipeline produced an actual decision."""
        result = self._make_result(
            stages_completed=(PipelineStage.FETCH, PipelineStage.DONE),
            broker_status=broker_status,
        )
        assert result.decided is True

    def test_to_dict_serializes_all_fields(self) -> None:
        result = self._make_result(
            stages_completed=(PipelineStage.FETCH, PipelineStage.RISK, PipelineStage.DONE),
            bars_loaded=5,
            risk_allowed=True,
            risk_violations=("X",),
            broker_status="FILLED",
            audit_log_id=7,
        )
        d = result.to_dict()
        assert d["ticker"] == "SBER"
        assert d["side"] == "buy"
        assert d["quantity"] == "1"
        assert d["limit_price"] == "100"
        assert d["stages_completed"] == ["fetch", "risk", "done"]
        assert d["bars_loaded"] == 5
        assert d["risk_allowed"] is True
        assert d["risk_violations"] == ["X"]
        assert d["broker_status"] == "FILLED"
        assert d["audit_log_id"] == 7
        assert d["decided"] is True
        assert isinstance(d["timestamp"], str)


# -----------------------------------------------------------------------------
# End-to-end: run_once with live_trading=True and broker success populates audit
# -----------------------------------------------------------------------------


class TestCoordinatorRunOnceHappyPath:
    def test_run_once_full_pipeline_writes_audit(self) -> None:
        """Full happy path: fetch → validate → risk → execute → audit → done.

        Uses live_trading=True to exercise the broker branch (lines 306-317).
        """
        from src.broker.orders import OrderStatus
        from src.broker.tinkoff_account import TinkoffAccount

        mock_account = MagicMock()
        mock_account.place_order.return_value = OrderStatus.FILLED

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (99,)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value.__enter__.return_value = mock_conn

        with (
            patch.object(Coordinator, "_fetch", return_value=[_bar()]),
            patch.object(Coordinator, "_validate", return_value=True),
            patch.object(Coordinator, "_risk_check", return_value=(True, ())),
            patch.object(TinkoffAccount, "from_env", return_value=mock_account, create=True),
            patch.dict("sys.modules", {"psycopg": mock_psycopg}),
        ):
            result = Coordinator(_config(live_trading=True, store_dsn="postgresql://x")).run_once()

        assert result.broker_status == str(OrderStatus.FILLED.value)
        assert result.audit_log_id == 99
        assert PipelineStage.EXECUTE in result.stages_completed
        assert PipelineStage.AUDIT in result.stages_completed
        assert PipelineStage.DONE in result.stages_completed
        assert result.decided is True
