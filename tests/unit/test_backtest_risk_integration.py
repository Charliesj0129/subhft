"""Integration tests for risk evaluator wired into backtest dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.backtest._hbt_utils import dispatch_strategy
from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator
from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderIntent,
    RiskDecision,
    Side,
)


def _make_intent(price: int = 1_000_000, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
    )


def _make_adapter(risk_evaluator=None):
    adapter = MagicMock()
    adapter._risk_evaluator = risk_evaluator
    adapter.dispatch_feature_events = False
    adapter.strategy.handle_event.return_value = [_make_intent()]
    return adapter


class TestDispatchStrategyRiskGate:
    def test_no_risk_config_backward_compatible(self):
        adapter = _make_adapter(risk_evaluator=None)
        dispatch_strategy(adapter, MagicMock(), None)
        adapter.execute_intent.assert_called_once()

    def test_approved_intent_submitted(self):
        evaluator = MagicMock()
        evaluator.evaluate.return_value = RiskDecision(True, _make_intent())
        adapter = _make_adapter(risk_evaluator=evaluator)
        dispatch_strategy(adapter, MagicMock(), None)
        adapter.execute_intent.assert_called_once()

    def test_rejected_intent_not_submitted(self):
        evaluator = MagicMock()
        evaluator.evaluate.return_value = RiskDecision(False, _make_intent(), "POSITION_LIMIT")
        adapter = _make_adapter(risk_evaluator=evaluator)
        dispatch_strategy(adapter, MagicMock(), None)
        adapter.execute_intent.assert_not_called()
        adapter._record_rejection.assert_called_once()

    def test_mixed_intents_partial_execution(self):
        intent_ok = _make_intent(price=100_000)
        intent_bad = _make_intent(price=999_999_999)
        evaluator = MagicMock()
        evaluator.evaluate.side_effect = [
            RiskDecision(True, intent_ok),
            RiskDecision(False, intent_bad, "PRICE_BAND"),
        ]
        adapter = _make_adapter(risk_evaluator=evaluator)
        adapter.strategy.handle_event.return_value = [intent_ok, intent_bad]
        dispatch_strategy(adapter, MagicMock(), None)
        assert adapter.execute_intent.call_count == 1
        assert adapter._record_rejection.call_count == 1

    def test_position_provider_reflects_fills(self, tmp_path):
        cfg_file = tmp_path / "risk.yaml"
        cfg_file.write_text(
            "global_defaults:\n  max_position_lots: 3\n  max_price_cap: 99999\n  max_notional: 999999999\n"
        )
        positions = {"2330": 3}
        config = BacktestRiskConfig(config_path=str(cfg_file))
        evaluator = BacktestRiskEvaluator(
            config,
            position_provider=lambda sym, sid: positions.get(sym, 0),
        )
        intent = _make_intent(qty=1)
        decision = evaluator.evaluate(intent)
        assert decision.approved is False


class TestRunResultRejectionData:
    def test_run_result_includes_rejection_fields(self):
        from hft_platform.backtest.runner import HftBacktestRunResult

        result = HftBacktestRunResult(
            run_id="test",
            config_hash="abc",
            symbol="2330",
            strategy_name="demo",
            data_path="/tmp/test.npz",
            pnl=0.0,
            equity_points=0,
            used_synthetic_equity=False,
            report_path=None,
            risk_rejection_count=5,
            risk_rejection_breakdown={"POSITION_LIMIT": 3, "PRICE_BAND": 2},
        )
        assert result.risk_rejection_count == 5
        assert result.risk_rejection_breakdown["POSITION_LIMIT"] == 3

    def test_run_result_defaults_to_zero_rejections(self):
        from hft_platform.backtest.runner import HftBacktestRunResult

        result = HftBacktestRunResult(
            run_id="test",
            config_hash="abc",
            symbol="2330",
            strategy_name="demo",
            data_path="/tmp/test.npz",
            pnl=0.0,
            equity_points=0,
            used_synthetic_equity=False,
            report_path=None,
        )
        assert result.risk_rejection_count == 0
        assert result.risk_rejection_breakdown == {}
