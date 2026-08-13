"""Architecture and ABI checks for the feature/event boundary."""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from hft_platform.events import FeatureUpdateEvent
from hft_platform.feature.boundary import TypedFeatureFrameV1, event_to_typed_frame


def _feature_event() -> FeatureUpdateEvent:
    return FeatureUpdateEvent(
        symbol="2330",
        ts=1_000,
        local_ts=1_010,
        seq=7,
        feature_set_id="lob_shared_v1",
        schema_version=1,
        changed_mask=0b11,
        warmup_ready_mask=0b01,
        quality_flags=0,
        feature_ids=("mid_price", "imbalance"),
        values=(100_500, 0.25),
    )


def test_feature_update_event_has_no_feature_layer_conversion_method() -> None:
    assert not hasattr(_feature_event(), "to_typed_frame")


def test_canonical_boundary_preserves_typed_frame_v1_abi() -> None:
    frame = event_to_typed_frame(_feature_event())

    assert tuple(field.name for field in fields(TypedFeatureFrameV1)) == (
        "marker",
        "symbol",
        "seq",
        "source_ts_ns",
        "local_ts_ns",
        "feature_set_id",
        "schema_version",
        "changed_mask",
        "warmup_ready_mask",
        "quality_flags",
        "feature_ids",
        "value_kind_mask",
        "values_i64",
        "values_f64",
    )
    assert frame == TypedFeatureFrameV1(
        marker="feature_update_v1",
        symbol="2330",
        seq=7,
        source_ts_ns=1_000,
        local_ts_ns=1_010,
        feature_set_id="lob_shared_v1",
        schema_version=1,
        changed_mask=0b11,
        warmup_ready_mask=0b01,
        quality_flags=0,
        feature_ids=("mid_price", "imbalance"),
        value_kind_mask=0b10,
        values_i64=(100_500, 0),
        values_f64=(0.0, 0.25),
    )


def test_events_module_has_no_feature_layer_import() -> None:
    source_path = Path(__file__).resolve().parents[2] / "src" / "hft_platform" / "events.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    feature_imports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            feature_imports.extend(alias.name for alias in node.names if alias.name.startswith("hft_platform.feature"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("hft_platform.feature"):
                feature_imports.append(module)

    assert feature_imports == []
