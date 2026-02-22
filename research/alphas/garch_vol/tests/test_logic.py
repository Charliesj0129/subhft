import os
import sys

sys.path.append(os.getcwd())

from research.alphas.garch_vol.impl import GARCHVolAlpha


def test_manifest_alpha_id() -> None:
    alpha = GARCHVolAlpha()
    assert alpha.manifest.alpha_id == "garch_vol"
