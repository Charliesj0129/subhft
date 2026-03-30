"""Unit tests for VolCBS signal generator."""

from __future__ import annotations

import math

import numpy as np
import pytest

from research.alphas.vol_cbs.impl import VolCBS, _WARMUP_TICKS


class TestVolCBS:
    """Tests for the VolCBS signal generator."""

    def test_initial_state(self) -> None:
        vcbs = VolCBS()
        assert not vcbs.warmed_up
        assert vcbs.compute_atr_bps() == 0.0

    def test_no_signal_on_zero_price(self) -> None:
        vcbs = VolCBS()
        result = vcbs.update(0)
        assert result["signal"] == 0

    def test_warmup_period(self) -> None:
        vcbs = VolCBS()
        price = 200000
        # First update sets prev_mid_x2 but doesn't increment tick_count
        vcbs.update(price)
        for i in range(_WARMUP_TICKS - 1):
            vcbs.update(price + i + 1)
        assert not vcbs.warmed_up
        vcbs.update(price + _WARMUP_TICKS + 1)
        assert vcbs.warmed_up

    def test_atr_increases_with_volatility(self) -> None:
        vcbs_calm = VolCBS()
        vcbs_wild = VolCBS()
        base = 200000

        # Calm: small moves
        for i in range(500):
            vcbs_calm.update(base + (i % 3) - 1)

        # Wild: large moves
        for i in range(500):
            vcbs_wild.update(base + (i % 2) * 100 - 50)

        assert vcbs_wild.compute_atr_bps() > vcbs_calm.compute_atr_bps()

    def test_threshold_scales_with_atr(self) -> None:
        vcbs = VolCBS(k_entry=3.0)
        base = 200000
        for i in range(500):
            vcbs.update(base + (i % 2) * 10 - 5)

        atr_bps = vcbs.compute_atr_bps()
        threshold_bps = vcbs.compute_threshold_bps()
        assert abs(threshold_bps - 3.0 * atr_bps) < 0.01

    def test_stop_scales_with_atr(self) -> None:
        vcbs = VolCBS(s_stop=1.5)
        base = 200000
        for i in range(500):
            vcbs.update(base + (i % 2) * 10 - 5)

        atr_bps = vcbs.compute_atr_bps()
        stop_bps = vcbs.compute_stop_bps()
        assert abs(stop_bps - 1.5 * atr_bps) < 0.01

    def test_vol_regime_classification(self) -> None:
        vcbs = VolCBS()
        base = 200000
        for i in range(500):
            result = vcbs.update(base + (i % 2) * 10 - 5)

        # After warmup, should have a regime
        assert result["vol_regime"] in ("low", "medium", "high", "unknown")

    def test_position_size_mult_bounded(self) -> None:
        vcbs = VolCBS(max_leverage=2.0, target_vol_annual=0.15)
        base = 200000
        for i in range(500):
            result = vcbs.update(base + (i % 2) * 10 - 5)

        mult = result["position_size_mult"]
        assert isinstance(mult, float)
        assert mult <= 2.0

    def test_reset_clears_state(self) -> None:
        vcbs = VolCBS()
        base = 200000
        for i in range(500):
            vcbs.update(base + i)

        assert vcbs.warmed_up
        vcbs.reset()
        assert not vcbs.warmed_up
        assert vcbs.compute_atr_bps() == 0.0

    def test_manifest_exists(self) -> None:
        vcbs = VolCBS()
        m = vcbs.manifest
        assert m.alpha_id == "vol_cbs"
        assert "2511.08571" in m.paper_refs

    def test_atr_bps_positive_after_warmup(self) -> None:
        vcbs = VolCBS()
        base = 200000
        for i in range(500):
            vcbs.update(base + (i % 10) * 2)

        atr = vcbs.compute_atr_bps()
        assert atr > 0.0

    def test_different_k_changes_threshold(self) -> None:
        vcbs_low = VolCBS(k_entry=2.0)
        vcbs_high = VolCBS(k_entry=5.0)
        base = 200000

        for i in range(500):
            p = base + (i % 2) * 10 - 5
            vcbs_low.update(p)
            vcbs_high.update(p)

        assert vcbs_high.compute_threshold_bps() > vcbs_low.compute_threshold_bps()
