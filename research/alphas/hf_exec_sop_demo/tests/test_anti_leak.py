from research.alphas.hf_exec_sop_demo.impl import HfExecSopDemoAlpha


def test_update_returns_float() -> None:
    # Minimum AlphaProtocol contract: update() with no args must not raise
    # and must return a numeric value (signal is float for ranking only).
    alpha = HfExecSopDemoAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    alpha = HfExecSopDemoAlpha()
    a = alpha.update()
    b = alpha.update()
    assert a == b
