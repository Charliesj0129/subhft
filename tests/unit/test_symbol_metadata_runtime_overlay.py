"""Metadata must describe the universe that is actually subscribed.

A universe roll replaces contracts while the engine runs, but
``config/symbols.yaml`` stays the operator's file and does not move. Without an
overlay, a contract subscribed at 06:00 would be priced from a file written a
month earlier: ``price_scale`` falls back to ``DEFAULT_SCALE`` and the tag index
still points at the settled month.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hft_platform.feed_adapter.normalizer import SymbolMetadata


def _write(path: Path, symbols: list[dict[str, Any]]) -> None:
    path.write_text(yaml.safe_dump({"symbols": symbols}), encoding="utf-8")


def _september(**extra: Any) -> dict[str, Any]:
    entry = {
        "code": "TMFI6",
        "exchange": "FUT",
        "product_type": "future",
        "tags": ["futures", "front_month", "tmf"],
        "tick_size": 1,
        "price_scale": 10000,
        "point_value": 10,
    }
    entry.update(extra)
    return entry


def _october(**extra: Any) -> dict[str, Any]:
    entry = dict(_september())
    entry["code"] = "TMFJ6"
    entry.update(extra)
    return entry


def test_rolled_in_contract_is_priced_by_its_own_metadata(tmp_path: Path) -> None:
    path = tmp_path / "symbols.yaml"
    _write(path, [_september()])
    metadata = SymbolMetadata(str(path))
    assert metadata.meta.get("TMFJ6") is None

    metadata.apply_runtime_overlay([_october(price_scale=100)])

    assert metadata.price_scale("TMFJ6") == 100


def test_overlay_survives_a_reload_of_the_canonical_file(tmp_path: Path) -> None:
    """The operator's file changing must not silently drop the live universe."""
    path = tmp_path / "symbols.yaml"
    _write(path, [_september()])
    metadata = SymbolMetadata(str(path))
    metadata.apply_runtime_overlay([_october()])

    _write(path, [_september(name="edited by an operator")])
    metadata.reload()

    assert "TMFJ6" in metadata.meta


def test_overlay_replaces_stale_tags_for_a_code(tmp_path: Path) -> None:
    path = tmp_path / "symbols.yaml"
    _write(path, [_october(tags=["futures", "next_month", "tmf"])])
    metadata = SymbolMetadata(str(path))
    assert "TMFJ6" in metadata.symbols_for_tags(["next_month"])

    metadata.apply_runtime_overlay([_october(tags=["futures", "front_month", "tmf"])])

    assert "TMFJ6" in metadata.symbols_for_tags(["front_month"])
    assert "TMFJ6" not in metadata.symbols_for_tags(["next_month"])


def test_a_contract_that_left_the_universe_keeps_its_metadata(tmp_path: Path) -> None:
    """A settled contract can still be held in a position and must stay priceable."""
    path = tmp_path / "symbols.yaml"
    _write(path, [_september()])
    metadata = SymbolMetadata(str(path))

    metadata.apply_runtime_overlay([_october()])

    assert metadata.meta["TMFI6"]["price_scale"] == 10000


def test_metadata_with_no_overlay_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "symbols.yaml"
    _write(path, [_september()])
    metadata = SymbolMetadata(str(path))

    metadata.reload()

    assert set(metadata.meta) == {"TMFI6"}
    assert metadata.symbols_for_tags(["front_month"]) == {"TMFI6"}
