"""Tests for WU6: research/tools/feature_screener.py"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from research.tools.feature_screener import (
    _load_data,
    screen_features,
    screen_interactions,
)

# Minimum observations the screener requires before computing metrics.
_MIN_OBS = 50


def _make_feature_array(
    n_rows: int = 200,
    n_cols: int = 5,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Return a plain float64 2-D array suitable for screen_features."""
    if rng is None:
        rng = np.random.default_rng(0)
    return rng.standard_normal((n_rows, n_cols)).astype(np.float64)


# ---------------------------------------------------------------------------
# screen_features
# ---------------------------------------------------------------------------


def test_screen_features_returns_ranked_list(tmp_path: Path) -> None:
    """screen_features should return a non-empty list of dicts with expected keys."""
    rng = np.random.default_rng(42)
    arr = _make_feature_array(300, 5, rng)
    npy_path = tmp_path / "features.npy"
    np.save(str(npy_path), arr)

    results = screen_features([str(npy_path)])

    assert isinstance(results, list)
    assert len(results) > 0

    required_keys = {"feature_id", "ic", "sharpe", "turnover", "score", "n_obs", "latency_profile"}
    for row in results:
        assert required_keys.issubset(row.keys()), f"Missing keys in row: {row.keys()}"

    # Results should be sorted: finite-score entries first, then NaN scores.
    finite_entries = [r for r in results if np.isfinite(r["score"])]
    nan_entries = [r for r in results if not np.isfinite(r["score"])]
    # All finite entries come before NaN entries.
    for fi in finite_entries:
        idx_fi = results.index(fi)
        for ni in nan_entries:
            idx_ni = results.index(ni)
            assert idx_fi < idx_ni, "Finite-score results should precede NaN-score results"

    # Finite-score entries should be sorted by score descending.
    scores = [r["score"] for r in finite_entries]
    assert scores == sorted(scores, reverse=True), "Finite scores should be sorted descending"


def test_screen_features_latency_profile_propagated(tmp_path: Path) -> None:
    """latency_profile kwarg should appear verbatim in every result row."""
    arr = _make_feature_array(200, 3)
    npy_path = tmp_path / "f.npy"
    np.save(str(npy_path), arr)

    custom_profile = "my_custom_profile_v1"
    results = screen_features([str(npy_path)], latency_profile=custom_profile)

    assert len(results) > 0
    for row in results:
        assert row["latency_profile"] == custom_profile


def test_screen_features_handles_empty_array(tmp_path: Path) -> None:
    """An array with fewer rows than _MIN_OBS should produce NaN metrics, not crash."""
    arr = np.zeros((10, 4), dtype=np.float64)  # 10 rows — below _MIN_OBS
    npy_path = tmp_path / "tiny.npy"
    np.save(str(npy_path), arr)

    results = screen_features([str(npy_path)])

    # Should still return a list (possibly with NaN scores) rather than raising.
    assert isinstance(results, list)
    for row in results:
        assert "score" in row


def test_screen_features_handles_missing_file(tmp_path: Path) -> None:
    """screen_features should raise an informative error when no valid file can be loaded."""
    missing = str(tmp_path / "does_not_exist.npy")

    with pytest.raises((FileNotFoundError, RuntimeError)):
        screen_features([missing])


def test_screen_features_stacks_multiple_files(tmp_path: Path) -> None:
    """screen_features accepts multiple paths and stacks them."""
    rng = np.random.default_rng(1)
    arr1 = _make_feature_array(150, 4, rng)
    arr2 = _make_feature_array(150, 4, rng)

    p1 = tmp_path / "part1.npy"
    p2 = tmp_path / "part2.npy"
    np.save(str(p1), arr1)
    np.save(str(p2), arr2)

    results = screen_features([str(p1), str(p2)])

    # Combined should have more observations than single file.
    n_obs_combined = max(r["n_obs"] for r in results if np.isfinite(r["n_obs"]))
    results_single = screen_features([str(p1)])
    n_obs_single = max(r["n_obs"] for r in results_single if np.isfinite(r["n_obs"]))
    assert n_obs_combined >= n_obs_single


# ---------------------------------------------------------------------------
# _load_data
# ---------------------------------------------------------------------------


def test_load_data_npy_plain(tmp_path: Path) -> None:
    arr = np.arange(20, dtype=np.float64).reshape(4, 5)
    p = tmp_path / "plain.npy"
    np.save(str(p), arr)
    loaded = _load_data(str(p))
    assert loaded.shape == (4, 5)
    np.testing.assert_array_almost_equal(loaded, arr)


def test_load_data_npy_1d_becomes_column(tmp_path: Path) -> None:
    arr = np.arange(10, dtype=np.float64)
    p = tmp_path / "vec.npy"
    np.save(str(p), arr)
    loaded = _load_data(str(p))
    assert loaded.ndim == 2
    assert loaded.shape == (10, 1)


def test_load_data_structured_npy(tmp_path: Path) -> None:
    dt = np.dtype([("a", "f8"), ("b", "f8")])
    arr = np.zeros(5, dtype=dt)
    arr["a"] = np.arange(5)
    arr["b"] = np.arange(5) * 2
    p = tmp_path / "struct.npy"
    np.save(str(p), arr)
    loaded = _load_data(str(p))
    assert loaded.shape == (5, 2)


def test_load_data_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _load_data(str(tmp_path / "ghost.npy"))


def test_load_data_unsupported_extension_raises(tmp_path: Path) -> None:
    p = tmp_path / "data.csv"
    p.write_text("1,2,3\n4,5,6\n")
    with pytest.raises(ValueError, match="Unsupported extension"):
        _load_data(str(p))


# ---------------------------------------------------------------------------
# screen_interactions
# ---------------------------------------------------------------------------


def test_screen_interactions_returns_pairs(tmp_path: Path) -> None:
    """screen_interactions returns dicts with feature_a, feature_b, interaction, ic, n_obs."""
    rng = np.random.default_rng(99)
    # Need at least 2 distinct features that have finite IC to form pairs.
    arr = _make_feature_array(300, 6, rng)
    p = tmp_path / "data.npy"
    np.save(str(p), arr)

    results = screen_interactions([str(p)], top_k=3)

    # May be empty if no features pass finite-IC filter, but if non-empty, keys are correct.
    required_keys = {"feature_a", "feature_b", "interaction", "ic", "n_obs"}
    for row in results:
        assert required_keys.issubset(row.keys()), f"Missing keys: {set(row.keys())}"
        assert row["interaction"] in ("product", "ratio")


def test_screen_interactions_requires_at_least_two_features(tmp_path: Path) -> None:
    """With only 1 column all features may be the same, yielding no pairs."""
    arr = np.ones((200, 1), dtype=np.float64)
    p = tmp_path / "single.npy"
    np.save(str(p), arr)

    # Should not raise — should return empty list or a list (graceful handling).
    try:
        results = screen_interactions([str(p)], top_k=5)
        assert isinstance(results, list)
    except RuntimeError:
        pass  # RuntimeError("No valid data") is also acceptable


def test_screen_interactions_sorted_by_abs_ic(tmp_path: Path) -> None:
    """Results should be sorted by |IC| descending, finite first."""
    rng = np.random.default_rng(7)
    arr = _make_feature_array(400, 8, rng)
    p = tmp_path / "data.npy"
    np.save(str(p), arr)

    results = screen_interactions([str(p)], top_k=4)

    finite_results = [r for r in results if np.isfinite(r["ic"])]
    abs_ics = [abs(r["ic"]) for r in finite_results]
    assert abs_ics == sorted(abs_ics, reverse=True)
