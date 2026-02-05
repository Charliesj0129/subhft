import os
import time
from pathlib import Path

import yaml

from hft_platform.feed_adapter.normalizer import SymbolMetadata


def _write_config(path: Path, symbols: list[dict]) -> None:
    data = {"symbols": symbols}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_symbol_metadata_load_and_lookups(tmp_path: Path) -> None:
    config = tmp_path / "symbols.yaml"
    _write_config(
        config,
        [
            {
                "code": "AAA",
                "tags": "TW50,Large",
                "price_scale": 100,
                "exchange": "TSE",
            },
            {
                "code": "BBB",
                "tags": ["FUT", "Index"],
                "tick_size": 0.5,
                "exchange": "FUT",
                "product_type": "future",
            },
        ],
    )

    meta = SymbolMetadata(config_path=str(config))

    assert meta.symbols_for_tags(["tw50"]) == {"AAA"}
    assert meta.symbols_for_tags(["fut", "index"]) == {"BBB"}
    assert meta.price_scale("AAA") == 100
    assert meta.price_scale("BBB") == 2
    assert meta.exchange("BBB") == "FUT"
    assert meta.product_type("AAA") == "stock"
    assert meta.product_type("BBB") == "future"


def test_symbol_metadata_reload_if_changed(tmp_path: Path) -> None:
    config = tmp_path / "symbols.yaml"
    _write_config(config, [{"code": "AAA", "tags": "tw50"}])

    meta = SymbolMetadata(config_path=str(config))
    assert meta.symbols_for_tags(["tw50"]) == {"AAA"}
    assert meta.reload_if_changed() is False

    time.sleep(0.01)
    _write_config(config, [{"code": "CCC", "tags": "new"}])
    os.utime(config, None)

    assert meta.reload_if_changed() is True
    assert meta.symbols_for_tags(["tw50"]) == set()
    assert meta.symbols_for_tags(["new"]) == {"CCC"}
