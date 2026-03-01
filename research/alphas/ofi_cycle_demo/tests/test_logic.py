import os
import sys

sys.path.append(os.getcwd())

from research.alphas.ofi_cycle_demo.impl import OfiCycleDemoAlpha


def test_manifest_alpha_id() -> None:
    alpha = OfiCycleDemoAlpha()
    assert alpha.manifest.alpha_id == "ofi_cycle_demo"
