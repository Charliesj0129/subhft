from hft_platform.core.pricing import FixedPriceScaleProvider, PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.feed_adapter.normalizer import SymbolMetadata


def test_price_codec_fixed_scale():
    codec = PriceCodec(FixedPriceScaleProvider(scale=100))

    assert codec.scale("AAA", 1.23) == 123
    assert codec.descale("AAA", 123) == 1.23


def test_price_codec_symbol_metadata(tmp_path, monkeypatch):
    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols_cfg))

    metadata = SymbolMetadata(str(symbols_cfg))
    codec = PriceCodec(SymbolMetadataPriceScaleProvider(metadata))

    assert codec.scale("AAA", 1.23) == 123
    assert codec.descale("AAA", 123) == 1.23
