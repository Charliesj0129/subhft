"""Profile-unification tests (Stage 2 of the research-workflow consolidation).

Verifies that:
  1. ``vm_ul6`` is accepted as a legacy alias for ``vm_ul6_strict`` and emits a
     ``DeprecationWarning``.
  2. Pipeline overrides loaded from
     ``config/research/profiles/vm_ul6_strict.yaml`` mutate argparse args
     equivalently to the legacy in-code dict.
  3. ``hft alpha promote-batch`` exits nonzero when ``--profile`` is missing
     (D9 closure).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from hft_platform.alpha._validation_profile import load_profile
from research.pipeline import (
    _apply_validation_profile,
    _resolve_profile_token,
    build_parser,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE_YAML = REPO_ROOT / "config" / "research" / "profiles" / "vm_ul6_strict.yaml"


def _baseline_args() -> argparse.Namespace:
    """Build an argparse Namespace populated with argparse defaults."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--alpha-id",
            "dummy",
            "--owner",
            "tester",
            "--data",
            "/tmp/none",
        ]
    )
    return args


def test_vm_ul6_is_legacy_alias_with_deprecation_warning() -> None:
    notes: list[str] = []
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        stem = _resolve_profile_token("vm_ul6", notes=notes)
    assert stem == "vm_ul6_strict"
    assert any(issubclass(w.category, DeprecationWarning) for w in captured)
    assert any("legacy alias" in n for n in notes)


def test_vm_ul6_strict_is_canonical() -> None:
    notes: list[str] = []
    stem = _resolve_profile_token("vm_ul6_strict", notes=notes)
    assert stem == "vm_ul6_strict"
    assert notes == []


def test_standard_returns_none() -> None:
    notes: list[str] = []
    assert _resolve_profile_token("standard", notes=notes) is None


def test_pipeline_overrides_applied_from_yaml() -> None:
    """vm_ul6_strict YAML must mutate argparse defaults to the strict targets."""
    args = _baseline_args()
    args.validation_profile = "vm_ul6_strict"
    notes: list[str] = []
    profile = _apply_validation_profile(args, strict_mode=True, notes=notes)
    assert profile is not None
    assert profile.name == "vm_ul6_strict"

    # Spot-check several keys we know flipped.
    assert args.latency_profile_id == "sim_stress_v2026-02-26"
    assert args.local_decision_pipeline_latency_us == 1000
    assert args.min_sharpe_oos_gate_d == 1.8
    assert args.max_abs_drawdown_gate_d == 0.10
    assert args.max_correlation_gate_d == 0.5
    assert args.data_ul == 6
    assert args.enforce_rust_benchmark_gate is True
    assert "source" in args.required_data_provenance_fields


def test_vm_ul6_alias_loads_same_yaml_as_canonical() -> None:
    args_alias = _baseline_args()
    args_alias.validation_profile = "vm_ul6"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        notes_a: list[str] = []
        profile_a = _apply_validation_profile(args_alias, strict_mode=True, notes=notes_a)

    args_canon = _baseline_args()
    args_canon.validation_profile = "vm_ul6_strict"
    notes_b: list[str] = []
    profile_b = _apply_validation_profile(args_canon, strict_mode=True, notes=notes_b)

    assert profile_a is not None and profile_b is not None
    assert profile_a.name == profile_b.name == "vm_ul6_strict"

    # Every overridden arg must match across alias and canonical.
    for key in profile_b.pipeline_overrides:
        if hasattr(args_alias, key):
            assert getattr(args_alias, key) == getattr(args_canon, key), (
                f"alias drift on key {key}"
            )


def test_yaml_load_exposes_pipeline_overrides() -> None:
    """Sanity-check: load_profile populates the new pipeline_overrides field."""
    profile = load_profile(PROFILE_YAML)
    assert profile.is_strict is True
    assert profile.pipeline_overrides, "pipeline_overrides must be populated"
    assert profile.pipeline_baseline_defaults, "pipeline_baseline_defaults must be populated"
    # The Stage-2 drift resolution: stricter Sharpe wins.
    assert profile.pipeline_overrides["min_sharpe_oos_gate_d"] == 1.8
    assert profile.thresholds["taker"]["sharpe_oos_min"] == 1.8


def test_promote_batch_requires_profile() -> None:
    """`hft alpha promote-batch` without --profile must exit nonzero (D9)."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hft_platform",
            "alpha",
            "promote-batch",
            "--alpha-ids",
            "nonexistent_alpha",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0, (
        f"promote-batch without --profile should fail, got rc={proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "--profile" in proc.stderr or "--profile" in proc.stdout


@pytest.mark.parametrize("token", ["vm_ul6", "vm_ul6_strict"])
def test_promote_batch_accepts_profile(token: str) -> None:
    """With --profile set, promote-batch must at least pass arg-validation."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hft_platform",
            "alpha",
            "promote-batch",
            "--profile",
            token,
            "--alpha-ids",
            "nonexistent_alpha_xyz",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    # rc=2 is acceptable here ("no approved alphas") but the failure mode must
    # NOT be the "--profile required" early-exit.
    combined = proc.stdout + proc.stderr
    assert "--profile is required" not in combined
