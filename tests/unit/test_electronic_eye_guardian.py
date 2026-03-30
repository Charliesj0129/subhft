"""Tests for Guardian state machine in Electronic Eye strategy."""
from hft_platform.strategies.electronic_eye import EyeState, Guardian


class TestGuardianInitialState:
    def test_initial_state_is_init(self):
        g = Guardian()
        assert g.state == EyeState.INIT


class TestGuardianActivate:
    def test_activate_transitions_init_to_quoting(self):
        g = Guardian()
        g.activate()
        assert g.state == EyeState.QUOTING


class TestGuardianUtilization:
    def test_on_utilization_high_transitions_quoting_to_narrow(self):
        g = Guardian(warn_utilization_pct=80.0)
        g.activate()  # INIT -> QUOTING
        g.on_utilization(85.0)
        assert g.state == EyeState.NARROW

    def test_on_utilization_low_transitions_narrow_to_quoting(self):
        g = Guardian(warn_utilization_pct=80.0)
        g.activate()  # INIT -> QUOTING
        g.on_utilization(85.0)  # QUOTING -> NARROW
        assert g.state == EyeState.NARROW
        g.on_utilization(50.0)  # NARROW -> QUOTING
        assert g.state == EyeState.QUOTING


class TestGuardianGreeksRejection:
    def test_on_greeks_rejection_transitions_to_restrict(self):
        g = Guardian()
        g.activate()  # INIT -> QUOTING
        g.on_greeks_rejection(reason="delta_exceeded")
        assert g.state == EyeState.RESTRICT


class TestGuardianStressResult:
    def test_on_stress_result_false_transitions_to_restrict(self):
        g = Guardian()
        g.activate()  # INIT -> QUOTING
        g.on_stress_result(within_limits=False, worst_pnl=-600_000)
        assert g.state == EyeState.RESTRICT


class TestGuardianHalt:
    def test_on_halt_transitions_to_halt_from_quoting(self):
        g = Guardian()
        g.activate()  # INIT -> QUOTING
        g.on_halt()
        assert g.state == EyeState.HALT

    def test_on_halt_transitions_to_halt_from_narrow(self):
        g = Guardian(warn_utilization_pct=80.0)
        g.activate()  # INIT -> QUOTING
        g.on_utilization(85.0)  # -> NARROW
        g.on_halt()
        assert g.state == EyeState.HALT

    def test_on_halt_transitions_to_halt_from_restrict(self):
        g = Guardian()
        g.activate()  # INIT -> QUOTING
        g.on_greeks_rejection(reason="vega_exceeded")
        g.on_halt()
        assert g.state == EyeState.HALT


class TestGuardianRestrictClear:
    def test_restrict_clears_to_quoting_when_stress_and_util_both_ok(self):
        g = Guardian(warn_utilization_pct=80.0)
        g.activate()  # INIT -> QUOTING
        g.on_stress_result(within_limits=False, worst_pnl=-600_000)  # -> RESTRICT
        assert g.state == EyeState.RESTRICT
        g.on_utilization(50.0)  # util OK
        g.on_stress_result(within_limits=True, worst_pnl=-100_000)  # stress OK -> QUOTING
        assert g.state == EyeState.QUOTING


class TestGuardianAllowsNewQuotes:
    def test_allows_new_quotes_true_when_quoting(self):
        g = Guardian()
        g.activate()  # INIT -> QUOTING
        assert g.allows_new_quotes() is True

    def test_allows_new_quotes_true_when_narrow(self):
        g = Guardian(warn_utilization_pct=80.0)
        g.activate()  # INIT -> QUOTING
        g.on_utilization(85.0)  # -> NARROW
        assert g.allows_new_quotes() is True

    def test_allows_new_quotes_false_when_restrict(self):
        g = Guardian()
        g.activate()  # INIT -> QUOTING
        g.on_greeks_rejection(reason="delta_exceeded")  # -> RESTRICT
        assert g.allows_new_quotes() is False

    def test_allows_new_quotes_false_when_halt(self):
        g = Guardian()
        g.activate()  # INIT -> QUOTING
        g.on_halt()  # -> HALT
        assert g.allows_new_quotes() is False


class TestGuardianShouldFlatten:
    def test_should_flatten_true_only_in_halt(self):
        g = Guardian()
        g.activate()  # INIT -> QUOTING
        assert g.should_flatten() is False
        g.on_halt()  # -> HALT
        assert g.should_flatten() is True
