"""Tests for sub-gate registry."""

from __future__ import annotations

import pytest

from hft_platform.alpha._sub_gates.registry import (
    SubGate,
    SubGateResult,
    clear_registry,
    get_registered_sub_gates,
    register_sub_gate,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Isolate each test from registry state."""
    clear_registry()
    yield
    clear_registry()


def test_sub_gate_result_frozen():
    r = SubGateResult(name="x", passed=True, metrics={"a": 1.0}, details="ok")
    with pytest.raises((AttributeError, TypeError)):
        r.passed = False


def test_sub_gate_result_defaults():
    r = SubGateResult(name="x", passed=True)
    assert r.metrics == {}
    assert r.details == ""


def test_register_and_retrieve():
    class MyGate:
        name = "my_gate"
        applies_to = {"maker"}

        def evaluate(self, result, config, thresholds):
            return SubGateResult(name=self.name, passed=True)

    register_sub_gate(MyGate())
    gates = get_registered_sub_gates()
    assert len(gates) == 1
    assert gates[0].name == "my_gate"


def test_registry_preserves_insertion_order():
    class A:
        name = "a"
        applies_to = {"maker"}

        def evaluate(self, r, c, t):
            return SubGateResult("a", True)

    class B:
        name = "b"
        applies_to = {"taker"}

        def evaluate(self, r, c, t):
            return SubGateResult("b", True)

    register_sub_gate(A())
    register_sub_gate(B())
    gates = get_registered_sub_gates()
    assert [g.name for g in gates] == ["a", "b"]


def test_clear_registry_removes_all():
    class G:
        name = "g"
        applies_to = {"maker"}

        def evaluate(self, r, c, t):
            return SubGateResult("g", True)

    register_sub_gate(G())
    assert len(get_registered_sub_gates()) == 1
    clear_registry()
    assert get_registered_sub_gates() == []


def test_get_registered_returns_copy():
    """Modifying returned list should not affect registry."""

    class G:
        name = "g"
        applies_to = {"maker"}

        def evaluate(self, r, c, t):
            return SubGateResult("g", True)

    register_sub_gate(G())
    result = get_registered_sub_gates()
    result.clear()
    # Registry should still have the gate
    assert len(get_registered_sub_gates()) == 1


def test_protocol_runtime_check():
    """SubGate Protocol works with isinstance at runtime."""

    class ProperGate:
        name = "p"
        applies_to = {"maker"}

        def evaluate(self, r, c, t):
            return SubGateResult("p", True)

    assert isinstance(ProperGate(), SubGate)


def test_protocol_runtime_check_missing_attr():
    class IncompleteGate:
        name = "i"
        # missing applies_to and evaluate

    # Runtime-checkable Protocol checks for presence of attributes
    # (methods + annotations). Without all of them, isinstance returns False.
    assert not isinstance(IncompleteGate(), SubGate)
