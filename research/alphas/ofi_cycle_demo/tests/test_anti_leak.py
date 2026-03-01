import os
import sys

sys.path.append(os.getcwd())

from research.alphas.ofi_cycle_demo.impl import OfiCycleDemoAlpha


def test_update_returns_float() -> None:
    # Minimum AlphaProtocol contract: update() with no args must not raise
    # and must return a numeric value (signal is float for ranking only).
    alpha = OfiCycleDemoAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    alpha = OfiCycleDemoAlpha()
    a = alpha.update()
    b = alpha.update()
    assert a == b
