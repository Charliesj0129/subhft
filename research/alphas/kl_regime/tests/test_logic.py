import os
import sys

sys.path.append(os.getcwd())

from research.alphas.kl_regime.impl import KLRegimeAlpha


def test_manifest_alpha_id() -> None:
    alpha = KLRegimeAlpha()
    assert alpha.manifest.alpha_id == "kl_regime"
