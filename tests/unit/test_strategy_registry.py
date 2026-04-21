import logging
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


def test_disabled_scaffold_with_missing_module_logs_info_not_error(tmp_path, monkeypatch):
    """Bug #34: enabled=false config entries pointing at not-yet-merged
    modules (scaffolds for upcoming strategies) must log INFO, not ERROR.
    Real failures (other Exception types) keep the ERROR level."""
    cfg = tmp_path / "strategies.yaml"
    cfg.write_text(
        "\n".join(
            [
                "strategies:",
                "  - id: SCAFFOLD_X",
                "    module: hft_platform.strategies.does_not_exist_yet",
                "    class: PlaceholderStrategy",
                "    enabled: false",
            ]
        )
        + "\n"
    )

    captured: list[tuple[str, str, dict]] = []

    class _SpyLogger:
        def info(self, event, **kwargs):
            captured.append(("info", event, kwargs))

        def warning(self, event, **kwargs):
            captured.append(("warning", event, kwargs))

        def error(self, event, **kwargs):
            captured.append(("error", event, kwargs))

    import hft_platform.strategy.registry as registry_mod

    monkeypatch.setattr(registry_mod, "logger", _SpyLogger())

    reg = StrategyRegistry(str(cfg))
    strategies = reg.instantiate()

    assert strategies == []
    assert any(level == "info" and event == "strategy_scaffold_placeholder_skipped" for level, event, _ in captured)
    assert not any(level == "error" for level, _, _ in captured)


def test_enabled_scaffold_with_missing_module_logs_warning(tmp_path, monkeypatch):
    """Bug #34: enabled=true scaffold whose module is missing is a real
    deployment problem — log at WARNING level (still recoverable, but
    needs operator attention)."""
    cfg = tmp_path / "strategies.yaml"
    cfg.write_text(
        "\n".join(
            [
                "strategies:",
                "  - id: ENABLED_SCAFFOLD",
                "    module: hft_platform.strategies.does_not_exist_yet",
                "    class: PlaceholderStrategy",
                "    enabled: true",
            ]
        )
        + "\n"
    )

    captured: list[tuple[str, str, dict]] = []

    class _SpyLogger:
        def info(self, event, **kwargs):
            captured.append(("info", event, kwargs))

        def warning(self, event, **kwargs):
            captured.append(("warning", event, kwargs))

        def error(self, event, **kwargs):
            captured.append(("error", event, kwargs))

    import hft_platform.strategy.registry as registry_mod

    monkeypatch.setattr(registry_mod, "logger", _SpyLogger())

    reg = StrategyRegistry(str(cfg))
    strategies = reg.instantiate()

    assert strategies == []
    assert any(
        level == "warning" and event == "strategy_scaffold_missing_but_enabled" for level, event, _ in captured
    )
    assert not any(level == "error" for level, _, _ in captured)
