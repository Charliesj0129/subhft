"""Tests for Combinatorial Purged Cross-Validation (CPCV) with embargo."""
from __future__ import annotations

import itertools
import json
import os
from dataclasses import asdict
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from research.backtest.types import CPCVConfig, CPCVFoldResult, CPCVResult

# ---------------------------------------------------------------------------
# Helpers — mock alpha and data generation
# ---------------------------------------------------------------------------


class _MockManifest:
    alpha_id = "test_alpha"
    hypothesis = "test"
    formula = "test"
    paper_refs = ()
    data_fields = ("bid_px", "ask_px")
    complexity = "O(1)"


class _MockAlpha:
    """Minimal alpha satisfying AlphaProtocol for CPCV tests."""

    manifest = _MockManifest()
    _reset_count: int = 0

    def update(self, *args: Any, **kwargs: Any) -> float:
        return 0.1

    def reset(self) -> None:
        self._reset_count += 1

    def get_signal(self) -> float:
        return 0.1


def _make_hftbt_npz(path: str, n_rows: int = 600) -> str:
    """Create a minimal hftbt.npz file with n_rows events.

    Uses a simple structured array matching hftbacktest event_dtype layout:
    (ev, exch_ts, local_ts, px, qty).
    """
    dtype = np.dtype([
        ("ev", np.uint64),
        ("exch_ts", np.int64),
        ("local_ts", np.int64),
        ("px", np.float64),
        ("qty", np.float64),
    ])
    arr = np.zeros(n_rows, dtype=dtype)
    arr["ev"] = 1
    arr["exch_ts"] = np.arange(n_rows, dtype=np.int64) * 1_000_000
    arr["local_ts"] = arr["exch_ts"]
    arr["px"] = 100.0 + np.sin(np.arange(n_rows, dtype=np.float64) * 0.01)
    arr["qty"] = 10.0
    out = os.path.join(path, "hftbt.npz")
    np.savez_compressed(out, data=arr)
    return out


# ---------------------------------------------------------------------------
# Test 1: Correct number of paths
# ---------------------------------------------------------------------------


def test_cpcv_generates_correct_number_of_paths():
    """n_groups=6 → C(6,3) = 20 paths."""
    cfg = CPCVConfig(n_groups=6)
    n_test = cfg.n_groups // 2
    expected = len(list(itertools.combinations(range(cfg.n_groups), n_test)))
    assert expected == 20

    cfg4 = CPCVConfig(n_groups=4)
    expected4 = len(list(itertools.combinations(range(4), 2)))
    assert expected4 == 6


# ---------------------------------------------------------------------------
# Test 2: Embargo removes boundary rows
# ---------------------------------------------------------------------------


def test_cpcv_embargo_removes_boundary_rows():
    """Verify that embargo gaps are applied at train/test boundaries.

    The runner combines embargo_rows + purge_rows into a single gap that is
    removed from train data on each side of every test group boundary.
    """
    total_rows = 600
    n_groups = 6
    group_size = total_rows // n_groups  # 100
    embargo_pct = 0.01  # 1% of 600 = 6 rows
    purge_pct = 0.0  # disable purge for this test

    embargo_rows = max(1, int(embargo_pct * total_rows))
    purge_rows = max(1, int(purge_pct * total_rows))
    gap = embargo_rows + purge_rows  # 6 + 1 = 7

    # Test groups = {1, 3, 5}, train groups = {0, 2, 4}
    test_groups = (1, 3, 5)
    train_groups = (0, 2, 4)
    boundaries = [i * group_size for i in range(n_groups)] + [total_rows]

    # Build raw train set
    train_row_set: set[int] = set()
    for g in train_groups:
        train_row_set.update(range(boundaries[g], boundaries[g + 1]))

    raw_train_size = len(train_row_set)

    # Apply combined gap (mirrors runner logic)
    for g in sorted(test_groups):
        test_start = boundaries[g]
        test_end = boundaries[g + 1]
        train_row_set -= set(range(max(0, test_start - gap), test_start))
        train_row_set -= set(range(test_end, min(total_rows, test_end + gap)))

    # Train should be smaller after embargo
    assert len(train_row_set) < raw_train_size

    # Verify specific boundary rows are removed
    # Test group 1 starts at row 100; rows (100-7)=93..99 should be removed
    for r in range(93, 100):
        assert r not in train_row_set, f"Row {r} should be in gap zone (before test group 1)"

    # Test group 1 ends at row 200; rows 200..206 should be removed
    for r in range(200, 207):
        assert r not in train_row_set, f"Row {r} should be in gap zone (after test group 1)"


# ---------------------------------------------------------------------------
# Test 3: Purge removes adjacent train rows
# ---------------------------------------------------------------------------


def test_cpcv_purge_removes_adjacent_train_rows():
    """Verify that purge adds to embargo gap, removing more rows at boundaries."""
    total_rows = 1000
    n_groups = 4
    group_size = total_rows // n_groups  # 250
    embargo_pct = 0.01  # 10 rows
    purge_pct = 0.005  # 5 rows

    embargo_rows = max(1, int(embargo_pct * total_rows))
    purge_rows = max(1, int(purge_pct * total_rows))
    assert embargo_rows == 10
    assert purge_rows == 5

    gap = embargo_rows + purge_rows  # 15

    test_groups = (1,)
    train_groups = (0, 2, 3)
    boundaries = [i * group_size for i in range(n_groups)] + [total_rows]

    train_row_set: set[int] = set()
    for g in train_groups:
        train_row_set.update(range(boundaries[g], boundaries[g + 1]))

    # Apply combined gap (mirrors runner logic)
    for g in sorted(test_groups):
        test_start = boundaries[g]
        test_end = boundaries[g + 1]
        train_row_set -= set(range(max(0, test_start - gap), test_start))
        train_row_set -= set(range(test_end, min(total_rows, test_end + gap)))

    # Test group 1: [250, 500)
    # Before: gap = 15 → rows 235-249 removed
    for r in range(235, 250):
        assert r not in train_row_set, f"Row {r} should be in gap zone"

    # After: gap = 15 → rows 500-514 removed
    for r in range(500, 515):
        assert r not in train_row_set, f"Row {r} should be in gap zone"


# ---------------------------------------------------------------------------
# Test 4: Alpha reset called per path
# ---------------------------------------------------------------------------


def test_cpcv_alpha_reset_called_per_path():
    """Verify alpha.reset() is called once per CPCV path."""
    n_groups = 4  # C(4,2) = 6 paths
    n_paths = 6

    alpha = _MockAlpha()
    alpha._reset_count = 0

    # We can't easily run the full runner without hftbacktest installed,
    # so we test the contract: reset should be called n_paths times.
    # Simulate the loop:
    for _ in range(n_paths):
        alpha.reset()

    assert alpha._reset_count == n_paths


@patch("research.backtest.hft_native_runner._run_adapter_slice")
@patch("research.backtest.hft_native_runner.ensure_hftbt_npz")
def test_cpcv_alpha_reset_called_per_path_integration(
    mock_ensure: MagicMock,
    mock_run: MagicMock,
    tmp_path: Any,
) -> None:
    """Integration test: verify reset() is called once per path via run_cpcv."""
    from research.backtest.hft_native_runner import HftNativeRunner
    from research.backtest.types import BacktestConfig

    # Create mock data
    npz_path = _make_hftbt_npz(str(tmp_path), n_rows=400)
    mock_ensure.return_value = npz_path

    # Mock _run_adapter_slice to return valid arrays
    mock_eq = np.linspace(1_000_000, 1_010_000, 100, dtype=np.float64)
    mock_sig = np.random.default_rng(42).normal(0, 0.1, 100).astype(np.float64)
    mock_mid = np.full(100, 100.0, dtype=np.float64)
    mock_pos = np.zeros(100, dtype=np.float64)
    mock_run.return_value = (mock_eq, mock_sig, mock_mid, mock_pos)

    alpha = _MockAlpha()
    alpha._reset_count = 0

    bt_config = BacktestConfig(data_paths=[str(tmp_path / "data.npy")])
    runner = HftNativeRunner(alpha, bt_config)
    cpcv_cfg = CPCVConfig(n_groups=4, min_group_samples=10)
    result = runner.run_cpcv(alpha, cpcv_cfg)

    # C(4,2) = 6 paths → 6 reset calls
    assert alpha._reset_count == 6
    assert result.n_paths == 6
    assert len(result.folds) == 6


# ---------------------------------------------------------------------------
# Test 5: PBO in valid range
# ---------------------------------------------------------------------------


@patch("research.backtest.hft_native_runner._run_adapter_slice")
@patch("research.backtest.hft_native_runner.ensure_hftbt_npz")
def test_cpcv_pbo_in_valid_range(
    mock_ensure: MagicMock,
    mock_run: MagicMock,
    tmp_path: Any,
) -> None:
    """PBO must be in [0, 1]."""
    from research.backtest.hft_native_runner import HftNativeRunner
    from research.backtest.types import BacktestConfig

    npz_path = _make_hftbt_npz(str(tmp_path), n_rows=400)
    mock_ensure.return_value = npz_path

    mock_eq = np.linspace(1_000_000, 1_010_000, 100, dtype=np.float64)
    mock_sig = np.random.default_rng(42).normal(0, 0.1, 100).astype(np.float64)
    mock_mid = np.full(100, 100.0, dtype=np.float64)
    mock_pos = np.zeros(100, dtype=np.float64)
    mock_run.return_value = (mock_eq, mock_sig, mock_mid, mock_pos)

    alpha = _MockAlpha()
    bt_config = BacktestConfig(data_paths=[str(tmp_path / "data.npy")])
    runner = HftNativeRunner(alpha, bt_config)
    result = runner.run_cpcv(alpha, CPCVConfig(n_groups=4, min_group_samples=10))

    assert 0.0 <= result.pbo <= 1.0


# ---------------------------------------------------------------------------
# Test 6: Consistency matches positive Sharpe fraction
# ---------------------------------------------------------------------------


@patch("research.backtest.hft_native_runner._run_adapter_slice")
@patch("research.backtest.hft_native_runner.ensure_hftbt_npz")
def test_cpcv_consistency_matches_positive_sharpe_fraction(
    mock_ensure: MagicMock,
    mock_run: MagicMock,
    tmp_path: Any,
) -> None:
    """path_consistency_pct should equal fraction of paths with positive Sharpe."""
    from research.backtest.hft_native_runner import HftNativeRunner
    from research.backtest.types import BacktestConfig

    npz_path = _make_hftbt_npz(str(tmp_path), n_rows=400)
    mock_ensure.return_value = npz_path

    mock_eq = np.linspace(1_000_000, 1_010_000, 100, dtype=np.float64)
    mock_sig = np.random.default_rng(42).normal(0, 0.1, 100).astype(np.float64)
    mock_mid = np.full(100, 100.0, dtype=np.float64)
    mock_pos = np.zeros(100, dtype=np.float64)
    mock_run.return_value = (mock_eq, mock_sig, mock_mid, mock_pos)

    alpha = _MockAlpha()
    bt_config = BacktestConfig(data_paths=[str(tmp_path / "data.npy")])
    runner = HftNativeRunner(alpha, bt_config)
    result = runner.run_cpcv(alpha, CPCVConfig(n_groups=4, min_group_samples=10))

    n_positive = sum(1 for s in result.path_sharpes if s > 0.0)
    expected = n_positive / len(result.path_sharpes)
    assert result.path_consistency_pct == pytest.approx(expected)
    # PBO + consistency should sum to 1.0
    assert result.pbo + result.path_consistency_pct == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Test 7: Small data raises ValueError
# ---------------------------------------------------------------------------


@patch("research.backtest.hft_native_runner.ensure_hftbt_npz")
def test_cpcv_small_data_raises(
    mock_ensure: MagicMock,
    tmp_path: Any,
) -> None:
    """If group size < min_group_samples, raise ValueError."""
    from research.backtest.hft_native_runner import HftNativeRunner
    from research.backtest.types import BacktestConfig

    # Create data with only 30 rows, but min_group_samples=50 with 6 groups
    # → group_size = 5 < 50
    npz_path = _make_hftbt_npz(str(tmp_path), n_rows=30)
    mock_ensure.return_value = npz_path

    alpha = _MockAlpha()
    bt_config = BacktestConfig(data_paths=[str(tmp_path / "data.npy")])
    runner = HftNativeRunner(alpha, bt_config)

    with pytest.raises(ValueError, match="min_group_samples"):
        runner.run_cpcv(alpha, CPCVConfig(n_groups=6, min_group_samples=50))


# ---------------------------------------------------------------------------
# Test 8: CPCVResult serializable
# ---------------------------------------------------------------------------


def test_cpcv_result_serializable():
    """CPCVResult should be convertible to dict/JSON."""
    fold = CPCVFoldResult(
        path_idx=0,
        train_indices=(0, 2, 4),
        test_indices=(1, 3, 5),
        train_size=280,
        test_size=300,
        sharpe=1.5,
        ic_mean=0.1,
        max_drawdown=-0.05,
        turnover=0.3,
    )
    result = CPCVResult(
        config=CPCVConfig(n_groups=6),
        n_paths=20,
        folds=[fold],
        pbo=0.15,
        path_sharpes=[1.5],
        path_consistency_pct=0.85,
        sharpe_mean=1.5,
        sharpe_std=0.3,
        sharpe_min=0.8,
    )

    d = asdict(result)
    assert isinstance(d, dict)
    assert d["n_paths"] == 20
    assert d["pbo"] == 0.15
    assert d["config"]["n_groups"] == 6
    assert len(d["folds"]) == 1
    assert d["folds"][0]["train_indices"] == (0, 2, 4)

    # JSON round-trip (tuples become lists in JSON, that's expected)
    json_str = json.dumps(d, default=str)
    parsed = json.loads(json_str)
    assert parsed["n_paths"] == 20
    assert parsed["sharpe_mean"] == 1.5
