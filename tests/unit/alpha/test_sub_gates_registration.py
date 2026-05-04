"""Verify all 7 new sub-gates are auto-registered."""
from __future__ import annotations

from hft_platform.alpha._sub_gates import (
    clear_registry,
    ensure_builtin_sub_gates_registered,
    get_registered_sub_gates,
)


_NEW_GATE_NAMES = {
    "min_sample_size",
    "single_day_dominance",
    "loo_day_sensitivity",
    "outlier_trade_removal",
    "day_bootstrap_ci",
    "stationary_block_bootstrap",
    "deflated_sharpe_maker",
}


def test_all_new_gates_registered_after_clear_and_reset() -> None:
    clear_registry()
    ensure_builtin_sub_gates_registered()
    names = {g.name for g in get_registered_sub_gates()}
    missing = _NEW_GATE_NAMES - names
    assert not missing, f"missing gates: {missing}"


def test_registration_is_idempotent() -> None:
    ensure_builtin_sub_gates_registered()
    before = [g.name for g in get_registered_sub_gates()]
    ensure_builtin_sub_gates_registered()
    after = [g.name for g in get_registered_sub_gates()]
    assert before == after
