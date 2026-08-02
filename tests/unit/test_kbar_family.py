from __future__ import annotations

import numpy as np
import pytest

from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.kbar import KBAR_DELTA_LAGS, KBAR_FEATURE_HISTORY_BARS, build_kbar_family_features


def _inputs(count: int = 12) -> dict[str, np.ndarray]:
    index = np.arange(count, dtype=np.float64)
    close = 100.0 + np.sin(index / 3.0) + index * 0.25
    return {
        "open_": close - 0.4 + (index % 3) * 0.1,
        "high": close + 0.9 + (index % 2) * 0.2,
        "low": close - 1.1 - (index % 4) * 0.05,
        "close": close,
        "volume": 500.0 + (index % 7) * 40.0,
    }


def _reset_mask(count: int = 12, period: int = 6) -> np.ndarray:
    mask: np.ndarray = np.zeros(count, dtype=np.bool_)
    mask[::period] = True
    return mask


def _features(count: int = 12, period: int = 6) -> dict[str, np.ndarray]:
    return build_kbar_family_features(**_inputs(count), reset_mask=_reset_mask(count, period))


def test_kbar_features_are_float64_arrays_matching_input_length() -> None:
    features = _features()
    assert features
    for name, values in features.items():
        assert values.dtype == np.float64, name
        assert values.shape == (12,), name


def test_kbar_feature_history_metadata_covers_every_exported_feature() -> None:
    assert set(KBAR_FEATURE_HISTORY_BARS) == set(_features())


def test_kbar_feature_names_are_dsl_compilable_and_survive_the_raw_price_ban() -> None:
    for name in _features():
        compiled = compile_expression(name)
        assert compiled.variables == (name,)


def test_kbar_open_close_return_equals_body_over_open() -> None:
    inputs = _inputs()
    features = build_kbar_family_features(**inputs, reset_mask=_reset_mask())
    expected = (inputs["close"] - inputs["open_"]) / inputs["open_"]
    np.testing.assert_allclose(features["kbar_open_close_return"], expected)


def test_kbar_body_and_wick_ratios_partition_the_bar_range() -> None:
    features = _features()
    total = np.abs(features["kbar_body_ratio"]) + features["kbar_upper_wick_ratio"] + features["kbar_lower_wick_ratio"]
    np.testing.assert_allclose(total, np.ones_like(total))


def test_kbar_wick_ratios_are_non_negative_and_bounded_by_one() -> None:
    features = _features()
    for name in ("kbar_upper_wick_ratio", "kbar_lower_wick_ratio"):
        values = features[name]
        assert np.all(np.isfinite(values)), name
        assert np.all(values >= 0.0), name
        assert np.all(values <= 1.0), name


def test_kbar_close_location_ratio_is_minus_one_at_the_low_and_plus_one_at_the_high() -> None:
    count = 3
    features = build_kbar_family_features(
        open_=np.array([10.0, 10.0, 10.0]),
        high=np.array([12.0, 12.0, 12.0]),
        low=np.array([8.0, 8.0, 8.0]),
        close=np.array([8.0, 10.0, 12.0]),
        volume=np.ones(count),
        reset_mask=_reset_mask(count, count),
    )
    np.testing.assert_allclose(features["kbar_close_location_ratio"], [-1.0, 0.0, 1.0])


def test_kbar_range_ratio_is_the_bar_range_normalised_by_close() -> None:
    inputs = _inputs()
    features = build_kbar_family_features(**inputs, reset_mask=_reset_mask())
    expected = (inputs["high"] - inputs["low"]) / inputs["close"]
    np.testing.assert_allclose(features["kbar_range_ratio"], expected)


def test_kbar_volume_change_ratio_stays_within_the_unit_interval() -> None:
    features = _features()
    values = features["kbar_volume_change_ratio"]
    finite = values[np.isfinite(values)]
    assert finite.size == values.size - 2  # one NaN per reset segment start
    assert np.all(np.abs(finite) <= 1.0)


def test_kbar_gap_return_is_nan_at_every_reset_and_uses_the_previous_in_segment_close() -> None:
    inputs = _inputs()
    features = build_kbar_family_features(**inputs, reset_mask=_reset_mask())
    gap = features["kbar_gap_return"]
    assert np.isnan(gap[0])
    assert np.isnan(gap[6])
    expected = (inputs["open_"][7] - inputs["close"][6]) / inputs["close"][6]
    np.testing.assert_allclose(gap[7], expected)


def test_kbar_gap_return_never_reaches_across_a_reset_boundary() -> None:
    count = 12
    inputs = _inputs(count)
    inputs["close"] = np.where(np.arange(count) < 6, 100.0, 5000.0)
    inputs["open_"] = inputs["close"] - 1.0
    inputs["high"] = inputs["close"] + 2.0
    inputs["low"] = inputs["close"] - 3.0
    features = build_kbar_family_features(**inputs, reset_mask=_reset_mask(count, 6))
    # A 50x contract-roll jump lands exactly on the reset; it must not become a signal.
    assert np.isnan(features["kbar_gap_return"][6])
    np.testing.assert_allclose(features["kbar_gap_return"][7], (5000.0 - 1.0 - 5000.0) / 5000.0)


def test_kbar_deltas_are_nan_before_the_lag_is_available_within_a_segment() -> None:
    features = _features(count=12, period=6)
    for lag in KBAR_DELTA_LAGS:
        delta = features[f"kbar_body_ratio_delta{lag}"]
        for segment_start in (0, 6):
            assert np.all(np.isnan(delta[segment_start : segment_start + lag]))
            assert np.all(np.isfinite(delta[segment_start + lag : segment_start + 6]))


def test_kbar_delta_never_differences_across_a_reset_boundary() -> None:
    features = _features(count=12, period=6)
    base = features["kbar_range_ratio"]
    delta = features["kbar_range_ratio_delta1"]
    assert np.isnan(delta[6])
    np.testing.assert_allclose(delta[7], base[7] - base[6])


def test_kbar_guarded_divide_yields_nan_instead_of_raising_on_a_zero_range_bar() -> None:
    count = 4
    flat = np.full(count, 100.0)
    features = build_kbar_family_features(
        open_=flat,
        high=flat,
        low=flat,
        close=flat,
        volume=np.zeros(count),
        reset_mask=_reset_mask(count, count),
    )
    assert np.all(np.isnan(features["kbar_body_ratio"]))
    assert np.all(np.isnan(features["kbar_upper_wick_ratio"]))
    assert np.all(np.isnan(features["kbar_close_location_ratio"]))
    assert np.all(np.isnan(features["kbar_volume_change_ratio"]))


def test_kbar_rejects_mismatched_input_lengths() -> None:
    inputs = _inputs(6)
    inputs["volume"] = inputs["volume"][:-1]
    with pytest.raises(ValueError, match="identical lengths"):
        build_kbar_family_features(**inputs, reset_mask=_reset_mask(6, 6))


def test_kbar_rejects_reset_mask_of_the_wrong_length() -> None:
    with pytest.raises(ValueError, match="reset_mask length"):
        build_kbar_family_features(**_inputs(6), reset_mask=_reset_mask(5, 5))


@pytest.mark.parametrize("lags", [(), (0,), (-1, 3)])
def test_kbar_rejects_non_positive_lags(lags: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        build_kbar_family_features(**_inputs(6), reset_mask=_reset_mask(6, 6), lags=lags)
