"""Tests for the Slice-D path-b extension of `AlphaManifest`.

The schema gains exactly two intrinsic, write-once fields:
  * ``dsl_formula``    — DSL text round-trippable by the Slice-D parser/compiler.
  * ``parent_alpha_id`` — provenance pointer to the predecessor alpha (if any).

``kill_reason`` and ``cluster_id`` are deliberately *not* on the manifest
(see plan §5): the former lives on ``audit.alpha_kill_ledger`` rows because it
is a per-run outcome; the latter lives on the sidecar
``research/alphas/_cluster_assignments.json`` because clusters are
recomputed every cluster run.
"""

from __future__ import annotations

from research.registry.schemas import AlphaManifest, AlphaStatus


def _base_kwargs() -> dict[str, object]:
    return {
        "alpha_id": "test_alpha",
        "hypothesis": "test hypothesis",
        "formula": "x = 1",
        "paper_refs": ("1234.5678",),
        "data_fields": ("feature[0]",),
        "complexity": "O(1)",
        "status": AlphaStatus.DRAFT,
    }


def test_alpha_manifest_round_trip_with_new_fields() -> None:
    manifest = AlphaManifest(
        **_base_kwargs(),
        dsl_formula="d1_pe_entropy * d2_queue_survival * d3_mfg_inventory",
        parent_alpha_id="r47_maker",
    )
    payload = manifest.to_dict()
    assert payload["dsl_formula"] == "d1_pe_entropy * d2_queue_survival * d3_mfg_inventory"
    assert payload["parent_alpha_id"] == "r47_maker"

    revived = AlphaManifest.from_dict(payload)
    assert revived == manifest
    assert revived.dsl_formula == manifest.dsl_formula
    assert revived.parent_alpha_id == manifest.parent_alpha_id


def test_legacy_manifest_loads_without_new_fields() -> None:
    """Pre-Slice-D manifests must still load with both new fields defaulted to None."""
    legacy_payload: dict[str, object] = {
        "alpha_id": "legacy_alpha",
        "hypothesis": "h",
        "formula": "f",
        "paper_refs": [],
        "data_fields": [],
        "complexity": "O(1)",
        "status": "DRAFT",
    }
    manifest = AlphaManifest.from_dict(legacy_payload)
    assert manifest.dsl_formula is None
    assert manifest.parent_alpha_id is None

    payload = manifest.to_dict()
    assert payload["dsl_formula"] is None
    assert payload["parent_alpha_id"] is None


def test_round_trip_preserves_none_for_unset_new_fields() -> None:
    manifest = AlphaManifest(**_base_kwargs())
    assert manifest.dsl_formula is None
    assert manifest.parent_alpha_id is None

    revived = AlphaManifest.from_dict(manifest.to_dict())
    assert revived == manifest
