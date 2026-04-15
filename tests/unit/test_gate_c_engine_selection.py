"""Tests for Gate C engine selection based on manifest strategy_type."""
import pytest

from research.backtest.maker_engine import MakerEngine
from research.backtest.taker_engine import TakerEngine


def test_select_maker_engine():
    """When strategy_type=maker, MakerEngine is selected."""
    from research.backtest.cost_models import load_cost_profile
    from research.backtest.fill_models import QueueDepletionFill

    manifest = {"strategy_type": "maker", "instrument": "TMFD6"}
    cost = load_cost_profile(manifest["instrument"])
    fill = QueueDepletionFill(queue_fraction=0.5)
    engine = MakerEngine(fill_model=fill, cost_model=cost)
    assert engine.engine_type == "maker"


def test_select_taker_engine():
    """When strategy_type=taker, TakerEngine is selected."""
    manifest = {"strategy_type": "taker", "instrument": "TXFD6"}
    engine = TakerEngine()
    assert engine.engine_type == "taker"


def test_missing_strategy_type_raises():
    """manifest without strategy_type should raise."""
    manifest = {"instrument": "TMFD6"}
    with pytest.raises(KeyError):
        _ = manifest["strategy_type"]
