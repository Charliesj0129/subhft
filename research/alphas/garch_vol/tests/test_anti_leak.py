import os
import sys

sys.path.append(os.getcwd())

from research.alphas.garch_vol.impl import GARCHVolAlpha


def test_update_is_deterministic() -> None:
    prices = [100.0, 100.2, 100.1, 100.4, 100.3, 100.5]
    alpha_a = GARCHVolAlpha()
    alpha_b = GARCHVolAlpha()
    out_a = [alpha_a.update(price=p) for p in prices]
    out_b = [alpha_b.update(price=p) for p in prices]
    assert out_a == out_b
