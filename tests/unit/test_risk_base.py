"""Tests for hft_platform.risk.base — RiskManager ABC and StormGuard."""

import pytest

from hft_platform.risk.base import RiskManager, StormGuard

# ---------------------------------------------------------------------------
# RiskManager ABC
# ---------------------------------------------------------------------------


class TestRiskManagerABC:
    """RiskManager is abstract and cannot be instantiated directly."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            RiskManager()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_check_order(self):
        class Incomplete(RiskManager):
            def on_fill(self, fill):
                pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_on_fill(self):
        class Incomplete(RiskManager):
            def check_order(self, order):
                return True

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_concrete_subclass_works(self):
        class ConcreteRisk(RiskManager):
            def check_order(self, order):
                return order is not None

            def on_fill(self, fill):
                self.last_fill = fill

        rm = ConcreteRisk()
        assert rm.check_order({"price": 100})
        assert not rm.check_order(None)

        rm.on_fill({"price": 100, "qty": 5})
        assert rm.last_fill == {"price": 100, "qty": 5}


# ---------------------------------------------------------------------------
# StormGuard
# ---------------------------------------------------------------------------


class TestStormGuard:
    """StormGuard is a simple circuit-breaker base class."""

    def test_initial_state_not_triggered(self):
        sg = StormGuard()
        assert not sg.triggered

    def test_check_returns_true_when_not_triggered(self):
        sg = StormGuard()
        assert sg.check() is True

    def test_check_returns_false_when_triggered(self):
        sg = StormGuard()
        sg.triggered = True
        assert sg.check() is False

    def test_can_reset_after_trigger(self):
        sg = StormGuard()
        sg.triggered = True
        assert sg.check() is False

        sg.triggered = False
        assert sg.check() is True

    def test_multiple_instances_independent(self):
        sg1 = StormGuard()
        sg2 = StormGuard()
        sg1.triggered = True

        assert sg1.check() is False
        assert sg2.check() is True
