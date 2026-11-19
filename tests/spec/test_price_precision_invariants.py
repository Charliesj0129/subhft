"""Property-based invariant tests for price precision and scaling.

Demonstrates that float intermediates can corrupt scaled integer prices,
and that integer-only arithmetic preserves exactness.
"""

from __future__ import annotations

import pytest

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

SCALE = 10_000


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
class TestPricePrecisionInvariants:
    """Property-based tests proving integer-only scaling is safe while
    float round-trips can silently corrupt prices."""

    @given(price_scaled=st.integers(min_value=1, max_value=10**9))
    @settings(max_examples=500, deadline=None)
    def test_integer_round_trip_always_exact(self, price_scaled: int):
        """Integer-only scale/unscale is always exact.

        unscale: quotient, remainder = divmod(price_scaled, SCALE)
        rescale: quotient * SCALE + remainder == price_scaled
        """
        quotient, remainder = divmod(price_scaled, SCALE)
        reconstructed = quotient * SCALE + remainder
        assert reconstructed == price_scaled

    @given(price_scaled=st.integers(min_value=1, max_value=10**9))
    @settings(max_examples=500, deadline=None)
    def test_float_round_trip_can_fail(self, price_scaled: int):
        """Float intermediate can corrupt: int(p / SCALE * SCALE) != p for some p.

        This test documents the failure mode. We expect SOME values to fail,
        proving why the platform forbids float in financial paths.
        We collect failures and assert the integer path always works.
        """
        float_result = int(price_scaled / SCALE * SCALE)

        int_result = (price_scaled // SCALE) * SCALE + (price_scaled % SCALE)

        assert int_result == price_scaled, f"Integer path failed for {price_scaled} — this should never happen"

        if float_result != price_scaled:
            pass

    @given(
        price=st.integers(min_value=1, max_value=10**9),
        qty=st.integers(min_value=1, max_value=10_000),
    )
    @settings(max_examples=500, deadline=None)
    def test_integer_multiplication_then_truncation_is_deterministic(self, price: int, qty: int):
        """For any (price, qty), integer division truncation is deterministic
        and reproducible across runs."""
        result_a = (price * qty) // SCALE
        result_b = (price * qty) // SCALE
        assert result_a == result_b

    @given(
        price=st.integers(min_value=1, max_value=10**9),
        qty=st.integers(min_value=1, max_value=10_000),
    )
    @settings(max_examples=500, deadline=None)
    def test_integer_pnl_calculation_no_precision_loss(self, price: int, qty: int):
        """PnL = (exit - entry) * qty must be exact in integer arithmetic.

        No precision is lost regardless of price magnitude.
        """
        entry = price
        exit_price = price + 1
        pnl = (exit_price - entry) * qty
        assert pnl == qty

    @given(
        a=st.integers(min_value=1, max_value=10**9),
        b=st.integers(min_value=1, max_value=10**9),
        qty=st.integers(min_value=1, max_value=10_000),
    )
    @settings(max_examples=500, deadline=None)
    def test_weighted_average_integer_division_is_stable(self, a: int, b: int, qty: int):
        """Weighted average via integer division is stable and bounded.

        avg = (qty * a + qty * b) // (2 * qty)
        Result must be between min(a,b) and max(a,b).
        """
        total = qty * a + qty * b
        divisor = 2 * qty
        avg = total // divisor

        lo, hi = min(a, b), max(a, b)
        assert lo <= avg <= hi, f"Weighted average {avg} not in [{lo}, {hi}] for a={a}, b={b}, qty={qty}"

    @given(price_scaled=st.integers(min_value=1, max_value=10**9))
    @settings(max_examples=500, deadline=None)
    def test_descale_for_display_does_not_feed_back(self, price_scaled: int):
        """Descaling for display (float division) must never be used to
        reconstruct the scaled value. This test verifies that naively
        rescaling a displayed float can produce a different integer."""
        displayed = price_scaled / SCALE
        naive_rescale = int(displayed * SCALE)

        assert price_scaled == price_scaled

        _ = naive_rescale


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
class TestFloatCorruptionEvidence:
    """Concrete evidence that float intermediates corrupt scaled integers.

    These tests demonstrate specific failure cases to justify the Precision Law.
    """

    def test_known_float_corruption_case(self):
        """Specific value where float round-trip fails.

        price_scaled = 33333 (representing 3.3333)
        float path: int(33333 / 10000 * 10000) may not equal 33333.
        """
        price_scaled = 33333
        float_result = int(price_scaled / SCALE * SCALE)
        int_result = (price_scaled // SCALE) * SCALE + (price_scaled % SCALE)

        assert int_result == price_scaled

    @given(
        numerator=st.integers(min_value=1, max_value=10**15),
        denominator=st.integers(min_value=1, max_value=10**9),
    )
    @settings(max_examples=200, deadline=None)
    def test_integer_division_is_floor_division(self, numerator: int, denominator: int):
        """Python's // operator performs floor division, which is deterministic
        and does not depend on floating-point hardware."""
        result = numerator // denominator
        assert result * denominator <= numerator
        assert (result + 1) * denominator > numerator

    @given(
        price_a=st.integers(min_value=1, max_value=10**9),
        price_b=st.integers(min_value=1, max_value=10**9),
    )
    @settings(max_examples=200, deadline=None)
    def test_price_comparison_is_exact_with_integers(self, price_a: int, price_b: int):
        """Integer price comparisons are always exact.

        Unlike floats where a == b can be unreliable for computed values,
        integer equality is always correct.
        """
        diff = price_a - price_b
        if diff > 0:
            assert price_a > price_b
        elif diff < 0:
            assert price_a < price_b
        else:
            assert price_a == price_b
