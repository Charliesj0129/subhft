"""Unit tests for the NaN/Inf guard in FeatureEngine (_safe_int_round helper)."""
from __future__ import annotations

from hft_platform.feature.engine import _safe_int_round


def test_safe_int_round_normal() -> None:
    assert _safe_int_round(3.7) == 4


def test_safe_int_round_nan() -> None:
    assert _safe_int_round(float("nan")) == 0


def test_safe_int_round_inf() -> None:
    assert _safe_int_round(float("inf")) == 0


def test_safe_int_round_neg_inf() -> None:
    assert _safe_int_round(float("-inf")) == 0
