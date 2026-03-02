from research.alphas.ofi_execution_126.impl import OfiExecution126Alpha


def test_update_returns_float() -> None:
    # Minimum AlphaProtocol contract: update() with no args must not raise
    # and must return a numeric value (signal is float for ranking only).
    alpha = OfiExecution126Alpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    alpha = OfiExecution126Alpha()
    a = alpha.update()
    b = alpha.update()
    assert a == b
