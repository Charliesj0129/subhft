import sys
import types

from hft_platform.strategy.base import BaseStrategy
from hft_platform.strategy.registry import StrategyRegistry


class DummyStrategy(BaseStrategy):
    pass


def test_strategy_registry_instantiates(tmp_path, monkeypatch):
    cfg = tmp_path / "strategies.yaml"
    cfg.write_text(
        "\n".join(
            [
                "strategies:",
                "  - id: strat1",
                "    module: dummy_mod",
                "    class: DummyStrategy",
                "    enabled: false",
                "    budget_us: 123",
                "    symbols: ['AAA']",
                "    product_type: FUTURES",
                "    params:",
                "      foo: 1",
            ]
        )
        + "\n"
    )

    dummy_mod = types.ModuleType("dummy_mod")
    dummy_mod.DummyStrategy = DummyStrategy
    monkeypatch.setitem(sys.modules, "dummy_mod", dummy_mod)

    reg = StrategyRegistry(str(cfg))
    strategies = reg.instantiate()

    assert len(strategies) == 1
    strat = strategies[0]
    assert strat.strategy_id == "strat1"
    assert strat.enabled is False
    assert strat.symbols == ["AAA"]
    assert strat.product_type == "FUTURES"
    assert strat.budget_us == 123
    assert strat.config.get("foo") == 1
