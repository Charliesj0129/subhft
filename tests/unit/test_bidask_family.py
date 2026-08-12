from __future__ import annotations

import numpy as np
import pytest

from research.combinatorial.bidask import BIDASK_SLOPE_LAGS, build_bidask_family_features
from research.combinatorial.expression_lang import compile_expression


def _inputs(count: int = 12) -> dict[str, np.ndarray]:
    mid = 100.0 + np.arange(count, dtype=np.float64) * 0.25
    half_spread = 0.1 + (np.arange(count, dtype=np.float64) % 3) * 0.05
    return {
        "bid_open": mid - half_spread,
        "ask_open": mid + half_spread,
        "bid_qty_open": 10.0 + np.arange(count, dtype=np.float64),
        "ask_qty_open": 12.0 + (np.arange(count, dtype=np.float64) % 4),
        "bid_close": mid - half_spread + 0.02,
        "ask_close": mid + half_spread + 0.02,
        "bid_qty_close": 8.0 + (np.arange(count, dtype=np.float64) % 5),
        "ask_qty_close": 9.0 + (np.arange(count, dtype=np.float64) % 2),
    }


def _reset_mask(count: int = 12, period: int = 6) -> np.ndarray:
    mask: np.ndarray = np.zeros(count, dtype=np.bool_)
    mask[::period] = True
    return mask


def _features(count: int = 12, period: int = 6) -> dict[str, np.ndarray]:
    return build_bidask_family_features(**_inputs(count), reset_mask=_reset_mask(count, period))


def test_bidask_features_are_float64_arrays_matching_input_length() -> None:
    features = _features()
    assert features
    for name, values in features.items():
        assert values.dtype == np.float64, name
        assert values.shape == (12,), name


def test_bidask_feature_names_are_dsl_compilable_and_carry_transform_tokens() -> None:
    for name in _features():
        compiled = compile_expression(name)
        assert compiled.variables == (name,)


def test_bidask_spread_fraction_equals_spread_over_mid() -> None:
    inputs = _inputs()
    features = build_bidask_family_features(**inputs, reset_mask=_reset_mask())
    expected = (inputs["ask_open"] - inputs["bid_open"]) / ((inputs["ask_open"] + inputs["bid_open"]) / 2.0)
    np.testing.assert_allclose(features["bidask_spread_frac_open"], expected)


def test_bidask_qty_imbalance_stays_within_unit_interval() -> None:
    features = _features()
    for name in ("bidask_qty_imbalance_open", "bidask_qty_imbalance_close"):
        values = features[name]
        finite = values[np.isfinite(values)]
        assert finite.size == values.size
        assert np.all(np.abs(finite) <= 1.0)


def test_bidask_open_close_delta_matches_close_minus_open() -> None:
    features = _features()
    np.testing.assert_allclose(
        features["bidask_spread_frac_delta_open_close"],
        features["bidask_spread_frac_close"] - features["bidask_spread_frac_open"],
    )
    np.testing.assert_allclose(
        features["bidask_qty_imbalance_delta_open_close"],
        features["bidask_qty_imbalance_close"] - features["bidask_qty_imbalance_open"],
    )


def test_bidask_slopes_are_nan_before_the_lag_is_available_within_a_segment() -> None:
    features = _features(count=12, period=6)
    for lag in BIDASK_SLOPE_LAGS:
        slope = features[f"bidask_spread_frac_open_slope{lag}"]
        for segment_start in (0, 6):
            assert np.all(np.isnan(slope[segment_start : segment_start + lag]))
            assert np.all(np.isfinite(slope[segment_start + lag : segment_start + 6]))


def test_bidask_slope_never_differences_across_a_reset_boundary() -> None:
    count = 12
    inputs = _inputs(count)
    inputs["bid_qty_open"] = np.where(np.arange(count) < 6, 1.0, 1000.0)
    features = build_bidask_family_features(**inputs, reset_mask=_reset_mask(count, 6))
    base = features["bidask_qty_ratio_open"]
    slope = features["bidask_qty_ratio_open_slope1"]
    assert np.isnan(slope[6])
    np.testing.assert_allclose(slope[7], base[7] - base[6])


def test_bidask_guarded_divide_yields_nan_instead_of_raising_on_zero_denominator() -> None:
    count = 4
    inputs = _inputs(count)
    inputs["ask_qty_open"] = np.zeros(count)
    inputs["bid_qty_open"] = np.zeros(count)
    features = build_bidask_family_features(**inputs, reset_mask=_reset_mask(count, count))
    assert np.all(np.isnan(features["bidask_qty_ratio_open"]))
    assert np.all(np.isnan(features["bidask_qty_imbalance_open"]))


def test_bidask_rejects_mismatched_input_lengths() -> None:
    inputs = _inputs(6)
    inputs["ask_close"] = inputs["ask_close"][:-1]
    with pytest.raises(ValueError, match="identical lengths"):
        build_bidask_family_features(**inputs, reset_mask=_reset_mask(6, 6))


def test_bidask_rejects_reset_mask_of_the_wrong_length() -> None:
    with pytest.raises(ValueError, match="reset_mask length"):
        build_bidask_family_features(**_inputs(6), reset_mask=_reset_mask(5, 5))


@pytest.mark.parametrize("lags", [(), (0,), (-1, 3)])
def test_bidask_rejects_non_positive_lags(lags: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        build_bidask_family_features(**_inputs(6), reset_mask=_reset_mask(6, 6), lags=lags)
