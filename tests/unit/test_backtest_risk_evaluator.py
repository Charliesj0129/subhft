"""TDD tests for BacktestRiskEvaluator.

Tests are written FIRST (red phase) to drive implementation.
"""

from __future__ import annotations

import yaml

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side


def _make_intent(
    *,
    price: int = 220_000_000,
    qty: int = 1,
    symbol: str = "TMFD6",
    strategy_id: str = "TEST_STRAT",
    intent_type: IntentType = IntentType.NEW,
    side: Side = Side.BUY,
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
    )


def _write_risk_yaml(tmp_path, overrides: dict | None = None) -> str:
    """Write a minimal risk YAML and return path string."""
    cfg: dict = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "max_price_cap_futures": 50000.0,
            "max_price_cap_TMFD6": 50000.0,
            "max_notional": 500_000_000,
            "per_symbol_max_notional": 5_000_000_000,
            "max_position_lots": 3,
            "max_daily_loss": 50_000_000,
        }
    }
    if overrides:
        cfg["global_defaults"].update(overrides)
    p = tmp_path / "risk.yaml"
    p.write_text(yaml.dump(cfg))
    return str(p)


def _position_provider_zero(symbol: str, strategy_id: str) -> int:
    """Always returns 0 (empty book)."""
    return 0


class TestBacktestRiskEvaluatorApprove:
    def test_approve_valid_intent(self, tmp_path):
        """All validators pass for a well-formed NEW intent → approved."""
        from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator

        config_path = _write_risk_yaml(tmp_path)
        cfg = BacktestRiskConfig(config_path=config_path)
        evaluator = BacktestRiskEvaluator(cfg, position_provider=_position_provider_zero)

        intent = _make_intent(price=220_000_000, qty=1)
        decision = evaluator.evaluate(intent)

        assert decision.approved is True
        assert decision.reason_code == "OK"
        assert evaluator.rejection_count == 0

    def test_cancel_intent_always_approved(self, tmp_path):
        """CANCEL intent bypasses all validators and is always approved."""
        from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator

        config_path = _write_risk_yaml(tmp_path)
        cfg = BacktestRiskConfig(config_path=config_path)
        evaluator = BacktestRiskEvaluator(cfg, position_provider=_position_provider_zero)

        cancel_intent = _make_intent(intent_type=IntentType.CANCEL)
        decision = evaluator.evaluate(cancel_intent)

        assert decision.approved is True
        assert evaluator.rejection_count == 0


class TestBacktestRiskEvaluatorReject:
    def test_reject_float_price(self, tmp_path):
        """Float price on intent → FLOAT_PRICE rejection before validator chain."""
        from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator

        config_path = _write_risk_yaml(tmp_path)
        cfg = BacktestRiskConfig(config_path=config_path)
        evaluator = BacktestRiskEvaluator(cfg, position_provider=_position_provider_zero)

        intent = _make_intent(price=220_000_000, qty=1)
        object.__setattr__(intent, "price", 22000.5)

        decision = evaluator.evaluate(intent)

        assert decision.approved is False
        assert decision.reason_code == "FLOAT_PRICE"
        assert evaluator.rejection_count == 1

    def test_reject_position_limit(self, tmp_path):
        """position_provider returns qty at limit → POSITION_LIMIT_EXCEEDED."""
        from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator

        config_path = _write_risk_yaml(tmp_path, {"max_position_lots": 2})
        cfg = BacktestRiskConfig(config_path=config_path)

        def position_at_limit(symbol: str, strategy_id: str) -> int:
            return 2

        evaluator = BacktestRiskEvaluator(cfg, position_provider=position_at_limit)
        intent = _make_intent(price=220_000_000, qty=1, side=Side.BUY)
        decision = evaluator.evaluate(intent)

        assert decision.approved is False
        assert "POSITION_LIMIT_EXCEEDED" in decision.reason_code
        assert evaluator.rejection_count == 1


class TestBacktestRiskEvaluatorDisabled:
    def test_disabled_always_approves(self, tmp_path):
        """enabled=False → all intents approved regardless of content."""
        from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator

        config_path = _write_risk_yaml(tmp_path)
        cfg = BacktestRiskConfig(enabled=False, config_path=config_path)
        evaluator = BacktestRiskEvaluator(cfg, position_provider=_position_provider_zero)

        intent = _make_intent(price=220_000_000, qty=1)
        object.__setattr__(intent, "price", 99999.99)

        decision = evaluator.evaluate(intent)

        assert decision.approved is True
        assert evaluator.rejection_count == 0


class TestBacktestRiskEvaluatorStats:
    def test_rejection_breakdown_accumulates(self, tmp_path):
        """Multiple rejections of same code accumulate correctly in breakdown dict."""
        from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator

        config_path = _write_risk_yaml(tmp_path)
        cfg = BacktestRiskConfig(config_path=config_path)
        evaluator = BacktestRiskEvaluator(cfg, position_provider=_position_provider_zero)

        for _ in range(3):
            intent = _make_intent()
            object.__setattr__(intent, "price", 22000.5)
            evaluator.evaluate(intent)

        assert evaluator.rejection_count == 3
        assert evaluator.rejection_breakdown["FLOAT_PRICE"] == 3


class TestBacktestRiskEvaluatorConfig:
    def test_selective_validators(self, tmp_path):
        """With position_limit=False, position violations are approved."""
        from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator

        config_path = _write_risk_yaml(tmp_path, {"max_position_lots": 2})
        cfg = BacktestRiskConfig(
            position_limit=False,
            config_path=config_path,
        )

        def position_at_limit(symbol: str, strategy_id: str) -> int:
            return 10

        evaluator = BacktestRiskEvaluator(cfg, position_provider=position_at_limit)
        intent = _make_intent(price=220_000_000, qty=1, side=Side.BUY)
        decision = evaluator.evaluate(intent)

        assert decision.approved is True

    def test_missing_config_file_uses_empty_defaults(self, tmp_path):
        """Non-existent config path → empty config dict, evaluator still works."""
        from hft_platform.backtest.risk_evaluator import BacktestRiskConfig, BacktestRiskEvaluator

        cfg = BacktestRiskConfig(
            config_path=str(tmp_path / "nonexistent_risk.yaml"),
        )
        evaluator = BacktestRiskEvaluator(cfg, position_provider=_position_provider_zero)

        intent = _make_intent(price=1_000_000, qty=1)
        decision = evaluator.evaluate(intent)

        assert isinstance(decision.approved, bool)
