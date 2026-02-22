import os
import sys

sys.path.append(os.getcwd())

from research.alphas.kl_regime.impl import KLRegimeAlpha


def test_update_is_deterministic() -> None:
    returns = [0.0, 0.01, -0.005, 0.002, -0.003, 0.001]
    alpha_a = KLRegimeAlpha(window_recent=2, window_ref=2, n_bins=3)
    alpha_b = KLRegimeAlpha(window_recent=2, window_ref=2, n_bins=3)
    out_a = [alpha_a.update(current_return=r) for r in returns]
    out_b = [alpha_b.update(current_return=r) for r in returns]
    assert out_a == out_b
