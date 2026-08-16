"""Compatibility exports for the canonical platform DataUL contract.

The definitions live in ``hft_platform.contracts.data_ul``. Research code keeps
importing them from here so existing call sites are unaffected, but there is now
exactly one place where a UL tier's required fields are declared.
"""

from hft_platform.contracts.data_ul import (
    UL_REQUIRED_FIELDS,
    DataUL,
    audit_claimed_ul,
    coerce_data_ul,
    infer_data_ul,
    required_fields_for_ul,
    validate_meta_ul,
)

__all__ = [
    "DataUL",
    "UL_REQUIRED_FIELDS",
    "audit_claimed_ul",
    "coerce_data_ul",
    "infer_data_ul",
    "required_fields_for_ul",
    "validate_meta_ul",
]
