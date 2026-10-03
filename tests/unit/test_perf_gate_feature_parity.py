"""The Python-vs-Rust feature parity gate must compare what Rust computes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# tests/benchmark is not a package; E402 below is unavoidable after this path insert.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmark"))

import perf_regression_gate as gate  # noqa: E402

from hft_platform.feature.engine import FeatureEngine  # noqa: E402


def test_python_vs_rust_parity_rate_is_zero_on_shared_feature_prefix():
    if FeatureEngine(kernel_backend="rust").kernel_backend() != "rust":
        pytest.skip("rust feature kernel not available in this environment")

    rate = gate.bench_feature_engine_python_vs_rust_parity_mismatch_rate(n=300)

    assert rate == 0.0
