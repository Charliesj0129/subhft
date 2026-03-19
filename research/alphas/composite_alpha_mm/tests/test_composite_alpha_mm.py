"""Tests for composite_alpha_mm research alpha."""

from __future__ import annotations

import numpy as np

from research.alphas.composite_alpha_mm.impl import compute_alpha


class TestComputeAlpha:
    def _make_data(self, n: int = 100, seed: int = 42) -> np.ndarray:
        rng = np.random.RandomState(seed)
        dt = np.dtype(
            [
                ("ofi_l1_ema8", np.float64),
                ("depth_imbalance_ema8_ppm", np.float64),
                ("l1_bid_qty", np.float64),
                ("l1_ask_qty", np.float64),
                ("spread_scaled", np.float64),
                ("mid_price_x2", np.float64),
            ]
        )
        data = np.zeros(n, dtype=dt)
        data["ofi_l1_ema8"] = rng.randn(n) * 100
        data["depth_imbalance_ema8_ppm"] = rng.randn(n) * 5000
        data["l1_bid_qty"] = rng.randint(1, 200, n).astype(np.float64)
        data["l1_ask_qty"] = rng.randint(1, 200, n).astype(np.float64)
        data["spread_scaled"] = rng.randint(1000, 5000, n).astype(np.float64)
        data["mid_price_x2"] = 200_0000 + rng.randint(-10000, 10000, n).astype(
            np.float64
        )
        return data

    def test_output_shape(self) -> None:
        data = self._make_data(100)
        signal = compute_alpha(data)
        assert signal.shape == (100,)
        assert signal.dtype == np.float64

    def test_signal_bounded(self) -> None:
        data = self._make_data(500)
        signal = compute_alpha(data)
        assert np.all(signal >= -3.0)
        assert np.all(signal <= 3.0)

    def test_zero_input(self) -> None:
        dt = np.dtype(
            [
                ("ofi_l1_ema8", np.float64),
                ("depth_imbalance_ema8_ppm", np.float64),
                ("l1_bid_qty", np.float64),
                ("l1_ask_qty", np.float64),
                ("spread_scaled", np.float64),
                ("mid_price_x2", np.float64),
            ]
        )
        data = np.zeros(10, dtype=dt)
        data["l1_bid_qty"] = 1.0
        data["l1_ask_qty"] = 1.0
        signal = compute_alpha(data)
        assert not np.any(np.isnan(signal))
        assert not np.any(np.isinf(signal))

    def test_custom_weights(self) -> None:
        data = self._make_data(50)
        s1 = compute_alpha(data, w_ofi=1.0, w_depth=0.0, w_slope=0.0)
        s2 = compute_alpha(data, w_ofi=0.0, w_depth=1.0, w_slope=0.0)
        # Different weights should produce different signals
        assert not np.allclose(s1, s2)

    def test_deterministic(self) -> None:
        data = self._make_data(50)
        s1 = compute_alpha(data)
        s2 = compute_alpha(data)
        np.testing.assert_array_equal(s1, s2)

    def test_single_row(self) -> None:
        data = self._make_data(1)
        signal = compute_alpha(data)
        assert signal.shape == (1,)
        assert not np.isnan(signal[0])

    def test_unstructured_array_fallback(self) -> None:
        """Should work with plain 2D array (column order fallback)."""
        rng = np.random.RandomState(42)
        data = np.column_stack(
            [
                rng.randn(50) * 100,  # ofi_l1_ema8
                rng.randn(50) * 5000,  # depth_imbalance_ema8_ppm
                rng.randint(1, 200, 50).astype(np.float64),  # bid_qty
                rng.randint(1, 200, 50).astype(np.float64),  # ask_qty
            ]
        )
        signal = compute_alpha(data)
        assert signal.shape == (50,)
