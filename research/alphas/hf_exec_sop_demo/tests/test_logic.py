from research.alphas.hf_exec_sop_demo.impl import HfExecSopDemoAlpha


def test_manifest_alpha_id() -> None:
    alpha = HfExecSopDemoAlpha()
    assert alpha.manifest.alpha_id == "hf_exec_sop_demo"
