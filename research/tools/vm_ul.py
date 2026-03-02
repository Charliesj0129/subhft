from __future__ import annotations

from collections.abc import Mapping
from enum import IntEnum
from typing import Any


class DataUL(IntEnum):
    UL1 = 1
    UL2 = 2
    UL3 = 3
    UL4 = 4
    UL5 = 5
    UL6 = 6


_TIER_INCREMENTAL: dict[DataUL, frozenset[str]] = {
    DataUL.UL2: frozenset({"dataset_id", "source_type", "schema_version", "rows", "fields"}),
    DataUL.UL3: frozenset({"rng_seed", "generator_script", "generator_version", "parameters"}),
    DataUL.UL4: frozenset({"regimes_covered"}),
    DataUL.UL5: frozenset({"data_fingerprint", "lineage"}),
    DataUL.UL6: frozenset({"peer_reviewed", "validated_by", "approved_at"}),
}


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def coerce_data_ul(value: Any, default: DataUL = DataUL.UL2) -> DataUL:
    if isinstance(value, DataUL):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        token = value.strip().upper()
        if token.startswith("UL"):
            token = token[2:]
        value = token
    try:
        ul = DataUL(int(value))
    except (TypeError, ValueError):
        return default
    return ul


def required_fields_for_ul(min_ul: DataUL) -> frozenset[str]:
    required: set[str] = set()
    for tier in DataUL:
        if tier.value < DataUL.UL2.value:
            continue
        if tier <= min_ul:
            required.update(_TIER_INCREMENTAL.get(tier, frozenset()))
    return frozenset(required)


def validate_meta_ul(meta: Mapping[str, Any], min_ul: DataUL) -> tuple[bool, list[str]]:
    required = required_fields_for_ul(min_ul)
    missing = [field for field in sorted(required) if _is_missing(meta.get(field))]
    return (len(missing) == 0, missing)


def infer_data_ul(meta: Mapping[str, Any]) -> DataUL:
    achieved = DataUL.UL1
    for tier in (DataUL.UL2, DataUL.UL3, DataUL.UL4, DataUL.UL5, DataUL.UL6):
        ok, _missing = validate_meta_ul(meta, tier)
        if not ok:
            break
        achieved = tier
    return achieved


UL_REQUIRED_FIELDS: dict[DataUL, frozenset[str]] = {
    tier: required_fields_for_ul(tier) for tier in DataUL
}


__all__ = [
    "DataUL",
    "UL_REQUIRED_FIELDS",
    "coerce_data_ul",
    "infer_data_ul",
    "required_fields_for_ul",
    "validate_meta_ul",
]

