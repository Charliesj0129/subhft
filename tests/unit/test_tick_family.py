from __future__ import annotations

import numpy as np
import pytest

from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.tick import TICK_SLOPE_LAGS, build_tick_family_features


def _inputs(count: int = 12) -> dict[str, np.ndarray]:
    index = np.arange(count, dtype=np.float64)
    buy = 30.0 + (index % 5) * 2.0
    sell = 25.0 + (index % 3) * 3.0
    unknown = 5.0 + (index % 2)
    return {
        "trade_tick_count": buy + sell + unknown,
        "quote_update_count": 200.0 + index * 4.0,
        "buy_tick_count": buy,
        "sell_tick_count": sell,
        "buy_tick_volume": buy * 2.0,
        "sell_tick_volume": sell * 3.0,
    }


def _reset_mask(count: int = 12, period: int = 6) -> np.ndarray:
    mask: np.ndarray = np.zeros(count, dtype=np.bool_)
    mask[::period] = True
    return mask


def _features(count: int = 12, period: int = 6) -> dict[str, np.ndarray]:
    return build_tick_family_features(**_inputs(count), reset_mask=_reset_mask(count, period))


def test_tick_features_are_float64_arrays_matching_input_length() -> None:
    features = _features()
    assert features
    for name, values in features.items():
        assert values.dtype == np.float64, name
        assert values.shape == (12,), name


def test_tick_feature_names_are_dsl_compilable_and_carry_transform_tokens() -> None:
    for name in _features():
        compiled = compile_expression(name)
        assert compiled.variables == (name,)


def test_tick_buy_sell_imbalance_equals_signed_count_over_trade_count() -> None:
    inputs = _inputs()
    features = build_tick_family_features(**inputs, reset_mask=_reset_mask())
    expected = (inputs["buy_tick_count"] - inputs["sell_tick_count"]) / inputs["trade_tick_count"]
    np.testing.assert_allclose(features["tick_buy_sell_imbalance"], expected)


def test_tick_imbalance_features_stay_within_unit_interval() -> None:
    features = _features()
    for name in ("tick_buy_sell_imbalance", "tick_buy_sell_volume_ratio"):
        values = features[name]
        assert np.all(np.isfinite(values))
        assert np.all(np.abs(values) <= 1.0)


def test_tick_intensity_ratio_equals_trades_over_quote_updates() -> None:
    inputs = _inputs()
    features = build_tick_family_features(**inputs, reset_mask=_reset_mask())
    np.testing.assert_allclose(
        features["tick_intensity_ratio"],
        inputs["trade_tick_count"] / inputs["quote_update_count"],
    )


def test_tick_deltas_are_nan_before_the_lag_is_available_within_a_segment() -> None:
    features = _features(count=12, period=6)
    for lag in TICK_SLOPE_LAGS:
        delta = features[f"tick_intensity_ratio_delta{lag}"]
        for segment_start in (0, 6):
            assert np.all(np.isnan(delta[segment_start : segment_start + lag]))
            assert np.all(np.isfinite(delta[segment_start + lag : segment_start + 6]))


def test_tick_delta_never_differences_across_a_reset_boundary() -> None:
    count = 12
    inputs = _inputs(count)
    inputs["quote_update_count"] = np.where(np.arange(count) < 6, 10.0, 5000.0)
    features = build_tick_family_features(**inputs, reset_mask=_reset_mask(count, 6))
    base = features["tick_intensity_ratio"]
    delta = features["tick_intensity_ratio_delta1"]
    assert np.isnan(delta[6])
    np.testing.assert_allclose(delta[7], base[7] - base[6])


def test_tick_guarded_divide_yields_nan_on_zero_quote_updates() -> None:
    count = 4
    inputs = _inputs(count)
    inputs["quote_update_count"] = np.zeros(count)
    features = build_tick_family_features(**inputs, reset_mask=_reset_mask(count, count))
    assert np.all(np.isnan(features["tick_intensity_ratio"]))


def test_tick_guarded_divide_yields_nan_on_zero_aggressor_volume() -> None:
    count = 4
    inputs = _inputs(count)
    inputs["buy_tick_volume"] = np.zeros(count)
    inputs["sell_tick_volume"] = np.zeros(count)
    features = build_tick_family_features(**inputs, reset_mask=_reset_mask(count, count))
    assert np.all(np.isnan(features["tick_buy_sell_volume_ratio"]))


def test_tick_rejects_mismatched_input_lengths() -> None:
    inputs = _inputs(6)
    inputs["sell_tick_count"] = inputs["sell_tick_count"][:-1]
    with pytest.raises(ValueError, match="identical lengths"):
        build_tick_family_features(**inputs, reset_mask=_reset_mask(6, 6))


def test_tick_rejects_reset_mask_of_the_wrong_length() -> None:
    with pytest.raises(ValueError, match="reset_mask length"):
        build_tick_family_features(**_inputs(6), reset_mask=_reset_mask(5, 5))


@pytest.mark.parametrize("lags", [(), (0,), (-2, 1)])
def test_tick_rejects_non_positive_lags(lags: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        build_tick_family_features(**_inputs(6), reset_mask=_reset_mask(6, 6), lags=lags)
