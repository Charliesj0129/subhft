"""Compatibility exports for the canonical platform DataUL contract."""

from hft_platform.contracts.data_ul import (
    UL_REQUIRED_FIELDS,
    DataUL,
    coerce_data_ul,
    infer_data_ul,
    required_fields_for_ul,
    validate_meta_ul,
)

__all__ = [
    "DataUL",
    "UL_REQUIRED_FIELDS",
    "coerce_data_ul",
    "infer_data_ul",
    "required_fields_for_ul",
    "validate_meta_ul",
]
