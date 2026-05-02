from research.alphas.r30_rfsv_vol_timing.impl import R30RfsvVolTimingAlpha


def test_update_returns_float() -> None:
    # Minimum AlphaProtocol contract: update() with no args must not raise
    # and must return a numeric value (signal is float for ranking only).
    alpha = R30RfsvVolTimingAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    alpha = R30RfsvVolTimingAlpha()
    a = alpha.update()
    b = alpha.update()
    assert a == b
