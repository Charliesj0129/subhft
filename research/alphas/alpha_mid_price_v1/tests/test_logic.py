from research.alphas.alpha_mid_price_v1.impl import AlphaMidPriceV1Alpha


def test_manifest_alpha_id() -> None:
    alpha = AlphaMidPriceV1Alpha()
    assert alpha.manifest.alpha_id == "alpha_mid_price_v1"
