"""Round 44: strict profile loads force_flat_residual threshold + blocking.

This anchor test ensures the YAML edits in ``vm_ul6_strict.yaml`` stay
in sync with the gate registered in Round 43.  Without this anchor a
silent YAML drift (e.g. removed threshold, blocking list misspelling)
would degrade the gate to advisory-only on strict runs.
"""

from __future__ import annotations

import pytest

from hft_platform.alpha._sub_gates import (
    ensure_builtin_sub_gates_registered,
    get_registered_sub_gates,
)
from hft_platform.alpha._sub_gates.force_flat_residual import (
    ForceFlatResidualGate,
)
from hft_platform.alpha._validation_profile import load_profile

STRICT_PATH = "config/research/profiles/vm_ul6_strict.yaml"


@pytest.fixture(scope="module")
def strict_profile():
    return load_profile(STRICT_PATH)


class TestForceFlatResidualInStrictProfile:
    def test_force_flat_residual_in_blocking_list(self, strict_profile) -> None:
        assert "force_flat_residual" in strict_profile.blocking_sub_gates

    def test_maker_threshold_present_and_strict(self, strict_profile) -> None:
        maker_th = strict_profile.thresholds_for(strategy_type="maker")
        assert "force_flat_trip_share_max_pct" in maker_th
        assert float(maker_th["force_flat_trip_share_max_pct"]) == pytest.approx(30.0)

    def test_taker_threshold_present_and_strict(self, strict_profile) -> None:
        taker_th = strict_profile.thresholds_for(strategy_type="taker")
        assert "force_flat_trip_share_max_pct" in taker_th
        assert float(taker_th["force_flat_trip_share_max_pct"]) == pytest.approx(30.0)

    def test_thresholds_match_goal_floor(self, strict_profile) -> None:
        # 限制 §3: net edge > 10 pts/trade cannot be relaxed; the
        # force-flat cap is part of that floor's defence so it must
        # not exceed 50 %.  This guards future drift.
        for st in ("maker", "taker"):
            th = strict_profile.thresholds_for(strategy_type=st)
            assert th["force_flat_trip_share_max_pct"] <= 50.0, st

    def test_gate_consumes_strict_thresholds(self, strict_profile) -> None:
        # End-to-end: load threshold from YAML, hand it to the gate,
        # confirm a 40 % force-flat candidate FAILs under strict.
        ensure_builtin_sub_gates_registered()
        gate = ForceFlatResidualGate()
        taker_th = strict_profile.thresholds_for(strategy_type="taker")

        class _R:
            trade_pnl = [1.0] * 10
            abs_residual_qty = 4  # 40 % share
            mark_method = "force_flat_last_mid"

        out = gate.evaluate(_R(), config=None, thresholds=taker_th)
        assert out.passed is False
        assert out.metrics["force_flat_trip_share_pct"] == pytest.approx(40.0)

    def test_gate_passes_under_strict_for_clean_run(self, strict_profile) -> None:
        # Mirror of the above: a candidate with no force-flat residual
        # passes under the same strict threshold.
        gate = ForceFlatResidualGate()
        taker_th = strict_profile.thresholds_for(strategy_type="taker")

        class _R:
            trade_pnl = [1.0] * 10
            abs_residual_qty = 0
            mark_method = "no_residual"

        out = gate.evaluate(_R(), config=None, thresholds=taker_th)
        assert out.passed is True

    def test_registry_registration_anchored_by_profile_load(self) -> None:
        # load_profile validates blocking_sub_gates against the registry;
        # a successful load here is itself evidence the gate is registered.
        load_profile(STRICT_PATH)
        names = {g.name for g in get_registered_sub_gates()}
        assert "force_flat_residual" in names
