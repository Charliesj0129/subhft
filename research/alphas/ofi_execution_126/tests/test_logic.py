from research.alphas.ofi_execution_126.impl import OfiExecution126Alpha


def test_manifest_alpha_id() -> None:
    alpha = OfiExecution126Alpha()
    assert alpha.manifest.alpha_id == "ofi_execution_126"
