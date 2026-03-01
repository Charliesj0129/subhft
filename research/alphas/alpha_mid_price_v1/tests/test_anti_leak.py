from research.alphas.alpha_mid_price_v1.impl import AlphaMidPriceV1Alpha


def test_update_returns_float() -> None:
    # Minimum AlphaProtocol contract: update() with no args must not raise
    # and must return a numeric value (signal is float for ranking only).
    alpha = AlphaMidPriceV1Alpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    alpha = AlphaMidPriceV1Alpha()
    a = alpha.update()
    b = alpha.update()
    assert a == b
