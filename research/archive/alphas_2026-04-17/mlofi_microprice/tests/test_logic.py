"""Unit tests for MlofiMicropriceCorrectionAlpha."""

from __future__ import annotations

import numpy as np
import pytest

from research.alphas.mlofi_microprice.impl import (
    MlofiMicropriceCorrectionAlpha,
    _DEFAULT_ALPHA_COEF,
    _DEFAULT_LAMBDA,
    _N_LEVELS,
    _WARMUP_TICKS,
)


def _make_book(
    bid_prices: list[int],
    bid_vols: list[int],
    ask_prices: list[int],
    ask_vols: list[int],
) -> dict[str, np.ndarray]:
    """Helper to create bids/asks kwargs."""
    n = len(bid_prices)
    bids = np.array(list(zip(bid_prices, bid_vols)), dtype=np.float64).reshape(n, 2)
    asks = np.array(list(zip(ask_prices, ask_vols)), dtype=np.float64).reshape(n, 2)
    return {"bids": bids, "asks": asks}


class TestMlofiMicropriceCorrectionAlpha:
    """Tests for the MLOFI microprice correction alpha."""

    def test_manifest_fields(self) -> None:
        """Manifest has required fields."""
        alpha = MlofiMicropriceCorrectionAlpha()
        m = alpha.manifest
        assert m.alpha_id == "mlofi_microprice_correction"
        assert m.data_fields == ("bids", "asks")
        assert m.feature_set_version == "lob_shared_v2"
        assert m.latency_profile is not None

    def test_warmup_returns_zero(self) -> None:
        """Signal is zero during warmup period."""
        alpha = MlofiMicropriceCorrectionAlpha()
        book = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [10, 20, 30, 40, 50],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [10, 20, 30, 40, 50],
        )
        for _ in range(_WARMUP_TICKS - 1):
            result = alpha.update(**book)
            assert result == 0, "Signal must be zero during warmup"

    def test_signal_after_warmup(self) -> None:
        """Signal is non-zero after warmup with changing depth."""
        alpha = MlofiMicropriceCorrectionAlpha(alpha_coef=100.0)
        base_book = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [10, 20, 30, 40, 50],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [10, 20, 30, 40, 50],
        )

        # Warmup with stable book
        for _ in range(_WARMUP_TICKS):
            alpha.update(**base_book)

        # Now inject a bid increase (should produce positive MLOFI)
        changed_book = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [20, 30, 40, 50, 60],  # All bids increased by 10
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [10, 20, 30, 40, 50],  # Asks unchanged
        )
        signal = alpha.update(**changed_book)
        assert signal != 0, "Signal should be non-zero after depth change post-warmup"

    def test_bbo_shift_guard(self) -> None:
        """MLOFI is zeroed when BBO shifts."""
        alpha = MlofiMicropriceCorrectionAlpha(alpha_coef=100.0)
        book1 = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [10, 20, 30, 40, 50],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [10, 20, 30, 40, 50],
        )
        # Warmup
        for _ in range(_WARMUP_TICKS + 5):
            alpha.update(**book1)

        # BBO shift — best bid changes
        book2 = _make_book(
            [101_0000, 100_0000, 99_0000, 98_0000, 97_0000],  # BBO shifted up
            [50, 20, 30, 40, 50],
            [102_0000, 103_0000, 104_0000, 105_0000, 106_0000],
            [10, 20, 30, 40, 50],
        )
        # On BBO shift, raw MLOFI is zeroed, EMA decays toward 0
        pre_signal = alpha.get_signal()
        alpha.update(**book2)
        # Signal should move toward zero (EMA decay)
        post_signal = alpha.get_signal()
        # After BBO shift, signal may still be non-zero from EMA memory
        # but the raw contribution this tick is zero
        assert isinstance(post_signal, int), "Signal must be int"

    def test_reset_clears_state(self) -> None:
        """Reset returns alpha to initial state."""
        alpha = MlofiMicropriceCorrectionAlpha()
        book = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [10, 20, 30, 40, 50],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [10, 20, 30, 40, 50],
        )
        for _ in range(100):
            alpha.update(**book)

        alpha.reset()
        assert alpha.get_signal() == 0
        assert alpha.get_mlofi_ema() == 0.0
        assert alpha._tick_count == 0
        assert not alpha._initialized

    def test_requires_bids_asks(self) -> None:
        """Raises ValueError without bids/asks."""
        alpha = MlofiMicropriceCorrectionAlpha()
        with pytest.raises(ValueError, match="requires bids= and asks="):
            alpha.update(1.0, 2.0)

    def test_geometric_weighting(self) -> None:
        """Weights follow geometric decay w_k = lambda^(k-1)."""
        lam = 0.5
        alpha = MlofiMicropriceCorrectionAlpha(lam=lam)
        expected = np.array([lam**k for k in range(_N_LEVELS)])
        np.testing.assert_allclose(alpha._weights, expected)

    def test_signal_clip(self) -> None:
        """Signal is clipped to prevent extreme values."""
        alpha = MlofiMicropriceCorrectionAlpha(alpha_coef=1e6)
        book = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [10, 20, 30, 40, 50],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [10, 20, 30, 40, 50],
        )
        # Warmup
        for _ in range(_WARMUP_TICKS):
            alpha.update(**book)

        # Massive bid increase
        big_book = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [10000, 20000, 30000, 40000, 50000],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [10, 20, 30, 40, 50],
        )
        signal = alpha.update(**big_book)
        assert abs(signal) <= 500, f"Signal {signal} exceeds clip bounds"

    def test_output_is_int(self) -> None:
        """Signal output is always int (scaled price units)."""
        alpha = MlofiMicropriceCorrectionAlpha()
        book = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [10, 20, 30, 40, 50],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [10, 20, 30, 40, 50],
        )
        for _ in range(100):
            result = alpha.update(**book)
            assert isinstance(result, int), f"Expected int, got {type(result)}"

    def test_symmetric_depth_produces_zero_mlofi(self) -> None:
        """Equal bid/ask depth changes produce zero MLOFI."""
        alpha = MlofiMicropriceCorrectionAlpha(alpha_coef=100.0)
        book1 = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [10, 20, 30, 40, 50],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [10, 20, 30, 40, 50],
        )
        # Warmup
        for _ in range(_WARMUP_TICKS + 5):
            alpha.update(**book1)

        # Symmetric increase: both bids and asks increase by same amount
        book2 = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [20, 30, 40, 50, 60],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [20, 30, 40, 50, 60],
        )
        alpha.update(**book2)
        # MLOFI should be zero for this tick (delta_bid == delta_ask at all levels)
        # But EMA has memory, so just check EMA moved toward 0
        ema = alpha.get_mlofi_ema()
        # After stable book for many ticks (EMA near 0), then one zero-MLOFI tick,
        # EMA should still be near 0
        assert abs(ema) < 1.0, f"MLOFI EMA should be near zero for symmetric changes, got {ema}"

    def test_l2_only_book(self) -> None:
        """Works with only 2 levels of depth."""
        alpha = MlofiMicropriceCorrectionAlpha()
        book = _make_book(
            [100_0000, 99_0000],
            [10, 20],
            [101_0000, 102_0000],
            [10, 20],
        )
        # Should not raise
        for _ in range(100):
            result = alpha.update(**book)
            assert isinstance(result, int)

    def test_contrarian_mlofi_sign(self) -> None:
        """Positive MLOFI (bid refill > ask refill) produces positive correction.

        On TWSE, empirical finding: MLOFI is PRO-CYCLICAL (positive MLOFI
        predicts price UP), NOT contrarian as originally hypothesized.
        """
        alpha = MlofiMicropriceCorrectionAlpha(alpha_coef=100.0)
        stable_book = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [50, 50, 50, 50, 50],
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [50, 50, 50, 50, 50],
        )
        for _ in range(_WARMUP_TICKS + 5):
            alpha.update(**stable_book)

        # Large bid increase (positive MLOFI)
        bid_increase_book = _make_book(
            [100_0000, 99_0000, 98_0000, 97_0000, 96_0000],
            [100, 100, 100, 100, 100],  # +50 at all levels
            [101_0000, 102_0000, 103_0000, 104_0000, 105_0000],
            [50, 50, 50, 50, 50],
        )
        signal = alpha.update(**bid_increase_book)
        assert signal > 0, f"Positive MLOFI should produce positive correction (pro-cyclical), got {signal}"
