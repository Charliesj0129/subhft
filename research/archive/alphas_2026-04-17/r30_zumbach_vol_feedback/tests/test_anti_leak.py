from research.alphas.r30_zumbach_vol_feedback.impl import R30ZumbachVolFeedbackAlpha


def test_update_returns_float() -> None:
    # Minimum AlphaProtocol contract: update() with no args must not raise
    # and must return a numeric value (signal is float for ranking only).
    alpha = R30ZumbachVolFeedbackAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    alpha = R30ZumbachVolFeedbackAlpha()
    a = alpha.update()
    b = alpha.update()
    assert a == b
