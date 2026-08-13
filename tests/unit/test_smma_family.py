from __future__ import annotations

import numpy as np

from hft_platform.contracts.alpha import AlphaManifest
from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.smma import (
    FIBONACCI_SMMA_LENGTHS,
    build_smma_family_features,
    evaluate_smma_expression,
    generated_gp_expressions,
    pine_smma,
    smma_lengths_from_expression,
    validate_stationary_signal,
)
from research.combinatorial.smma_alpha_adapter import SMMACompiledAlpha


def _manifest(expression: str) -> AlphaManifest:
    return AlphaManifest(
        alpha_id="smma_test",
        hypothesis="test",
        formula=expression,
        paper_refs=(),
        data_fields=tuple(compile_expression(expression).variables),
        complexity="O(1)",
        instrument="TXFD6",
        cost_profile_refs=("TXFD6",),
    )


def test_pine_smma_seeds_with_sma_then_recurses() -> None:
    actual = pine_smma(np.arange(1.0, 7.0), 3)
    expected = np.asarray([np.nan, np.nan, 2.0, 8.0 / 3.0, 31.0 / 9.0, 116.0 / 27.0])
    np.testing.assert_allclose(actual, expected, equal_nan=True)


def test_pine_smma_length_one_equals_source_and_nan_does_not_advance() -> None:
    source = np.asarray([1.0, np.nan, 3.0])
    np.testing.assert_allclose(pine_smma(source, 1), source, equal_nan=True)


def test_pine_smma_reset_reseeds_after_roll() -> None:
    source = np.arange(1.0, 9.0)
    reset = np.asarray([False, False, False, False, True, False, False, False])
    actual = pine_smma(source, 3, reset_mask=reset)
    assert np.isnan(actual[4])
    assert np.isnan(actual[5])
    assert actual[6] == 6.0
    assert actual[7] == (6.0 * 2.0 + 8.0) / 3.0


def test_smma_family_exposes_stationary_features_without_levels() -> None:
    close = 100.0 + np.sin(np.arange(100) / 5.0) + np.arange(100) * 0.01
    features = build_smma_family_features(
        open_=close - 0.2,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        reset_mask=np.zeros(close.size, dtype=bool),
    )
    assert features
    assert all(not name.endswith("_level") for name in features)
    assert "close_l3_atr14_distance" in features
    assert "hl2_l3_5_atr14_spread" in features
    assert "ohlc4_l3_5_7_retvol14_alignment" in features


def test_fibonacci_lengths_expose_two_minute_family_without_raw_level() -> None:
    close = 100.0 + np.sin(np.arange(160) / 7.0) + np.arange(160) * 0.01
    features = build_smma_family_features(
        open_=close - 0.2,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        reset_mask=np.zeros(close.size, dtype=bool),
        lengths=FIBONACCI_SMMA_LENGTHS,
    )

    assert "close_l1_atr14_distance" in features
    assert "close_l1_2_atr14_spread" in features
    assert "close_l1_2_3_retvol14_alignment" in features
    assert all(not name.endswith("_level") for name in features)
    finite_distance = features["close_l1_atr14_distance"]
    finite_distance = finite_distance[np.isfinite(finite_distance)]
    np.testing.assert_array_equal(finite_distance, np.zeros_like(finite_distance))
    assert smma_lengths_from_expression("add(close_l1_2_atr14_spread, hlc3_l3_5_8_retvol14_separation)") == (
        1,
        2,
        3,
        5,
        8,
    )


def test_generated_gp_expressions_ban_self_correlation_and_repeated_operands() -> None:
    expressions = generated_gp_expressions(("a", "b", "c"), seed=7, limit=50)
    assert expressions
    assert any(expression.startswith("ts_delta(") for expression in expressions)
    assert any(expression.startswith("add(") for expression in expressions)
    assert any(expression.startswith("ts_corr(") for expression in expressions)
    assert all("ts_corr(a, a" not in expression for expression in expressions)
    assert all("ts_corr(b, b" not in expression for expression in expressions)
    assert all("ts_corr(c, c" not in expression for expression in expressions)
    assert all(compile_expression(expression, max_depth=3).max_depth <= 3 for expression in expressions)


def test_constant_signal_is_rejected() -> None:
    valid, reason = validate_stationary_signal(np.zeros(20))
    assert not valid
    assert reason == "constant_signal"


def test_signal_validator_allows_causal_warmup_nan_but_rejects_infinity() -> None:
    valid, reason = validate_stationary_signal(np.asarray([np.nan, np.nan, 1.0, 2.0]))
    assert valid
    assert reason == ""

    valid, reason = validate_stationary_signal(np.asarray([np.nan, 1.0, np.inf]))
    assert not valid
    assert reason == "non_finite_output"


def test_smma_stream_matches_batch_and_checkpoint_resume() -> None:
    expression = "ts_delta(close_l3_atr14_distance, 3)"
    count = 90
    close = 100.0 + np.sin(np.arange(count) / 4.0) + np.arange(count) * 0.03
    open_ = close - 0.1
    high = close + 0.8
    low = close - 0.7
    reset = np.zeros(count, dtype=bool)
    reset[45] = True
    features = build_smma_family_features(
        open_=open_,
        high=high,
        low=low,
        close=close,
        reset_mask=reset,
    )
    expected = evaluate_smma_expression(expression, features, reset)

    streaming = SMMACompiledAlpha(expression, _manifest(expression))
    actual: list[float] = []
    snapshot: dict[str, object] | None = None
    for index in range(count):
        actual.append(
            streaming.update(
                open=open_[index],
                high=high[index],
                low=low[index],
                close=close[index],
                reset=bool(reset[index]),
            )
        )
        if index == 59:
            snapshot = streaming.snapshot()

    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-12)
    assert snapshot is not None
    resumed = SMMACompiledAlpha(expression, _manifest(expression))
    resumed.restore(snapshot)
    resumed_tail = [
        resumed.update(open=open_[index], high=high[index], low=low[index], close=close[index])
        for index in range(60, count)
    ]
    np.testing.assert_allclose(resumed_tail, expected[60:], rtol=1e-12, atol=1e-12)
