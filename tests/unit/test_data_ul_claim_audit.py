"""A declared data-UL must be checkable against the metadata that carries it.

The mining sidecars wrote ``data_ul: 5`` unconditionally while carrying none of
the UL3–UL5 provenance fields ``research/tools/vm_ul.py`` requires. Nothing
downstream could tell, because the number is what gets read — the claim and the
evidence for it were never compared.
"""

from __future__ import annotations

from research.tools.vm_ul import DataUL, audit_claimed_ul, required_fields_for_ul


def _ul5_complete_meta() -> dict[str, object]:
    return {
        "dataset_id": "smma_taifex_20260401_20260731",
        "source_type": "real",
        "schema_version": 2,
        "rows": 1000,
        "fields": ["trading_day", "close"],
        "rng_seed": 42,
        "generator_script": "research/combinatorial/smma_dataset.py",
        "generator_version": "abc123",
        "parameters": {"symbols": ["TXF"]},
        "regimes_covered": ["trend", "chop"],
        "data_fingerprint": "def456",
        "lineage": {"source": "hft.market_data"},
    }


def test_audit_reports_no_gap_when_every_required_field_is_present() -> None:
    audit = audit_claimed_ul(_ul5_complete_meta(), DataUL.UL5)

    assert audit["data_ul_claimed"] == 5
    assert audit["data_ul_inferred"] == 5
    assert audit["data_ul_unmet_fields"] == []


def test_audit_names_the_exact_fields_a_claim_cannot_support() -> None:
    meta = _ul5_complete_meta()
    del meta["rng_seed"]
    del meta["regimes_covered"]

    audit = audit_claimed_ul(meta, DataUL.UL5)

    assert audit["data_ul_claimed"] == 5
    assert audit["data_ul_inferred"] == 2
    assert audit["data_ul_unmet_fields"] == ["regimes_covered", "rng_seed"]


def test_audit_treats_a_blank_field_as_absent() -> None:
    """An empty string satisfies ``in meta`` but proves nothing."""
    meta = _ul5_complete_meta()
    meta["lineage"] = {}
    meta["generator_script"] = "   "

    audit = audit_claimed_ul(meta, DataUL.UL5)

    assert audit["data_ul_unmet_fields"] == ["generator_script", "lineage"]


def test_ul5_requires_the_provenance_fields_the_mining_sidecars_now_write() -> None:
    """Pin the contract the sidecar payloads are built against."""
    required = required_fields_for_ul(DataUL.UL5)

    assert {"generator_script", "generator_version", "parameters", "lineage", "data_fingerprint"} <= required
