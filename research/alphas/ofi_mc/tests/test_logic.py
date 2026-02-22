import os
import sys

sys.path.append(os.getcwd())

from research.alphas.ofi_mc.impl import OFIMCAlpha


def test_manifest_alpha_id() -> None:
    alpha = OFIMCAlpha()
    assert alpha.manifest.alpha_id == "ofi_mc"
