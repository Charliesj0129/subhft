"""Slice-D Task 7: hierarchical clustering unit tests.

Covers the contract spelled out in plan §7 T7:
  * dataclass shape (frozen + slots, exact 4 fields).
  * ``EmptyCorpusError`` on empty corpus from ``compute_pool_matrix``.
  * Singleton clusters get ``cluster_id='singleton_<alpha_id>'`` with
    ``max_intra_cluster_corr=0.0``.
  * Multi-alpha clusters get ``cluster_id='cluster_<rank>'`` where rank is
    0-indexed and ordered by each cluster's lex-min alpha.
  * Single-linkage agglomerative on ``1 - |corr|`` distance — handles
    negative correlation by clustering on absolute value.
  * Determinism contract: 100 reruns produce identical assignment lists
    (Codex §10 H mitigation).
  * Sidecar artifact: ``research/alphas/_cluster_assignments.json``
    written only when ``write_artifact=True`` and merges with existing
    keys (does not clobber).
  * Metric switch: ``metric='spearman'`` reads ``spearman_matrix``.

Module is offline-only (``alpha/`` permitted to use ``float`` per
``.agent/rules/25-architecture-governance.md`` §11). No CK / no live data.
"""
from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hft_platform.alpha import cluster
from hft_platform.alpha.cluster import (
    ClusterAssignment,
    EmptyCorpusError,
    cluster_alphas,
)

# ---------------------------------------------------------------------------
# Helpers — synthetic matrix payloads (mirror ``pool.compute_pool_matrix``).
# ---------------------------------------------------------------------------


def _make_payload(alpha_ids: list[str], pearson: list[list[float]]) -> dict[str, Any]:
    """Build a synthetic ``compute_pool_matrix``-shaped payload."""
    arr = np.asarray(pearson, dtype=np.float64)
    return {
        "alpha_ids": list(alpha_ids),
        "matrix": arr.tolist(),
        "pearson_matrix": arr.tolist(),
        "spearman_matrix": arr.tolist(),
        "sample_length": 256,
    }


def _eye_corr(n: int) -> list[list[float]]:
    return np.eye(n, dtype=np.float64).tolist()


# ---------------------------------------------------------------------------
# Test 1 — dataclass shape
# ---------------------------------------------------------------------------


def test_cluster_assignment_dataclass_frozen_slots() -> None:
    assert is_dataclass(ClusterAssignment)
    assert ClusterAssignment.__dataclass_params__.frozen is True
    # slots=True implies __slots__ on the class (Python 3.10+).
    assert hasattr(ClusterAssignment, "__slots__")

    field_names = {f.name for f in fields(ClusterAssignment)}
    assert field_names == {
        "alpha_id",
        "cluster_id",
        "cluster_size",
        "max_intra_cluster_corr",
    }


# ---------------------------------------------------------------------------
# Test 2 — empty corpus raises EmptyCorpusError
# ---------------------------------------------------------------------------


def test_empty_corpus_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``compute_pool_matrix`` returning empty alpha_ids → EmptyCorpusError."""

    def _empty_payload(**_kwargs: Any) -> dict[str, Any]:
        return {
            "alpha_ids": [],
            "matrix": [],
            "pearson_matrix": [],
            "spearman_matrix": [],
            "sample_length": 0,
        }

    monkeypatch.setattr(cluster, "compute_pool_matrix", _empty_payload)
    assert issubclass(EmptyCorpusError, RuntimeError)
    with pytest.raises(EmptyCorpusError):
        cluster_alphas()


# ---------------------------------------------------------------------------
# Test 3 — single alpha → singleton
# ---------------------------------------------------------------------------


def test_singleton_cluster_id() -> None:
    payload = _make_payload(["alpha_solo"], _eye_corr(1))
    out = cluster._cluster_from_payload(payload, threshold=0.7, metric="pearson")
    assert len(out) == 1
    only = out[0]
    assert only.alpha_id == "alpha_solo"
    assert only.cluster_id == "singleton_alpha_solo"
    assert only.cluster_size == 1
    assert only.max_intra_cluster_corr == 0.0


# ---------------------------------------------------------------------------
# Test 4 — two uncorrelated alphas → two singletons
# ---------------------------------------------------------------------------


def test_two_uncorrelated_alphas_two_singletons() -> None:
    payload = _make_payload(["alpha_a", "alpha_b"], _eye_corr(2))
    out = cluster._cluster_from_payload(payload, threshold=0.7, metric="pearson")
    assert len(out) == 2
    by_id = {a.alpha_id: a for a in out}
    assert by_id["alpha_a"].cluster_id == "singleton_alpha_a"
    assert by_id["alpha_b"].cluster_id == "singleton_alpha_b"
    for entry in out:
        assert entry.cluster_size == 1
        assert entry.max_intra_cluster_corr == 0.0


# ---------------------------------------------------------------------------
# Test 5 — two correlated alphas → one cluster
# ---------------------------------------------------------------------------


def test_two_correlated_alphas_one_cluster() -> None:
    payload = _make_payload(
        ["alpha_a", "alpha_b"],
        [[1.0, 0.9], [0.9, 1.0]],
    )
    out = cluster._cluster_from_payload(payload, threshold=0.7, metric="pearson")
    assert len(out) == 2
    cluster_ids = {a.cluster_id for a in out}
    assert cluster_ids == {"cluster_0"}
    for entry in out:
        assert entry.cluster_size == 2
        assert entry.max_intra_cluster_corr == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Test 6 — negative correlation still clusters (uses |corr|)
# ---------------------------------------------------------------------------


def test_negative_correlation_clusters() -> None:
    payload = _make_payload(
        ["alpha_a", "alpha_b"],
        [[1.0, -0.9], [-0.9, 1.0]],
    )
    out = cluster._cluster_from_payload(payload, threshold=0.7, metric="pearson")
    assert {a.cluster_id for a in out} == {"cluster_0"}
    for entry in out:
        assert entry.cluster_size == 2
        assert entry.max_intra_cluster_corr == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Test 7 — lex-min naming inside a single cluster
# ---------------------------------------------------------------------------


def test_lexmin_cluster_naming() -> None:
    """3 fully-correlated alphas; lex-min is alpha_a; all share cluster_0."""
    payload = _make_payload(
        ["alpha_z", "alpha_b", "alpha_a"],
        [
            [1.0, 0.95, 0.92],
            [0.95, 1.0, 0.93],
            [0.92, 0.93, 1.0],
        ],
    )
    out = cluster._cluster_from_payload(payload, threshold=0.7, metric="pearson")
    cluster_ids = {a.cluster_id for a in out}
    assert cluster_ids == {"cluster_0"}
    for entry in out:
        assert entry.cluster_size == 3


# ---------------------------------------------------------------------------
# Test 8 — multiple clusters ranked by lex-min alpha
# ---------------------------------------------------------------------------


def test_multiple_clusters_lex_ranked() -> None:
    """A+B correlated, C+D correlated, A<C lex → A+B=cluster_0, C+D=cluster_1."""
    payload = _make_payload(
        ["alpha_a", "alpha_b", "alpha_c", "alpha_d"],
        [
            [1.0, 0.9, 0.0, 0.0],
            [0.9, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.85],
            [0.0, 0.0, 0.85, 1.0],
        ],
    )
    out = cluster._cluster_from_payload(payload, threshold=0.7, metric="pearson")
    assert len(out) == 4
    by_id = {a.alpha_id: a for a in out}
    assert by_id["alpha_a"].cluster_id == "cluster_0"
    assert by_id["alpha_b"].cluster_id == "cluster_0"
    assert by_id["alpha_c"].cluster_id == "cluster_1"
    assert by_id["alpha_d"].cluster_id == "cluster_1"
    assert by_id["alpha_a"].max_intra_cluster_corr == pytest.approx(0.9)
    assert by_id["alpha_c"].max_intra_cluster_corr == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# Test 9 — determinism: 100 reruns produce identical lists
# ---------------------------------------------------------------------------


def test_determinism_100_reruns() -> None:
    """Codex §10 H mitigation — sort alpha_ids before clustering."""
    # Deliberate non-sorted alpha_ids on input.
    payload = _make_payload(
        ["alpha_z", "alpha_m", "alpha_a", "alpha_b"],
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.85, 0.0],
            [0.0, 0.85, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
    )
    baseline = cluster._cluster_from_payload(payload, threshold=0.7, metric="pearson")
    for _ in range(100):
        rerun = cluster._cluster_from_payload(payload, threshold=0.7, metric="pearson")
        assert rerun == baseline


# ---------------------------------------------------------------------------
# Test 10 — metric switch reads spearman_matrix
# ---------------------------------------------------------------------------


def test_metric_spearman_uses_spearman_matrix() -> None:
    """When metric='spearman', spearman_matrix governs (not pearson)."""
    # Pearson says uncorrelated, Spearman says correlated → spearman wins.
    payload: dict[str, Any] = {
        "alpha_ids": ["alpha_a", "alpha_b"],
        "matrix": _eye_corr(2),
        "pearson_matrix": _eye_corr(2),
        "spearman_matrix": [[1.0, 0.9], [0.9, 1.0]],
        "sample_length": 256,
    }
    out = cluster._cluster_from_payload(payload, threshold=0.7, metric="spearman")
    assert {a.cluster_id for a in out} == {"cluster_0"}


# ---------------------------------------------------------------------------
# Test 11 — write_artifact creates the sidecar file
# ---------------------------------------------------------------------------


def test_write_artifact_creates_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``write_artifact=True`` persists results to the sidecar JSON."""
    payload = _make_payload(
        ["alpha_a", "alpha_b"],
        [[1.0, 0.9], [0.9, 1.0]],
    )

    def _stub_payload(**_kwargs: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(cluster, "compute_pool_matrix", _stub_payload)
    # Redirect the artifact path into tmp_path.
    artifact_path = tmp_path / "_cluster_assignments.json"
    monkeypatch.setattr(cluster, "_ARTIFACT_PATH", artifact_path)

    out = cluster_alphas(
        base_dir="research/experiments",
        threshold=0.7,
        metric="pearson",
        write_artifact=True,
    )
    assert artifact_path.exists()
    data = json.loads(artifact_path.read_text())
    assert isinstance(data, dict)
    assert len(data) == 1
    # The single key should encode threshold/metric/base_dir hash/corpus hash.
    only_key = next(iter(data))
    assert only_key.startswith("0.7:pearson:")
    # Persisted payload contains 2 records matching the 2 alphas.
    assert len(data[only_key]) == len(out)
    assert {rec["alpha_id"] for rec in data[only_key]} == {"alpha_a", "alpha_b"}


# ---------------------------------------------------------------------------
# Test 12 — write_artifact merges with existing keys (does not clobber)
# ---------------------------------------------------------------------------


def test_write_artifact_merges_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing unrelated key in the file must survive a new write."""
    payload = _make_payload(
        ["alpha_a", "alpha_b"],
        [[1.0, 0.9], [0.9, 1.0]],
    )

    def _stub_payload(**_kwargs: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(cluster, "compute_pool_matrix", _stub_payload)
    artifact_path = tmp_path / "_cluster_assignments.json"
    monkeypatch.setattr(cluster, "_ARTIFACT_PATH", artifact_path)

    # Pre-existing payload from a different threshold/metric run.
    pre_existing_key = "0.5:spearman:deadbeef:cafef00d"
    pre_existing_value: list[dict[str, Any]] = [
        {
            "alpha_id": "alpha_legacy",
            "cluster_id": "singleton_alpha_legacy",
            "cluster_size": 1,
            "max_intra_cluster_corr": 0.0,
        }
    ]
    artifact_path.write_text(json.dumps({pre_existing_key: pre_existing_value}))

    cluster_alphas(
        base_dir="research/experiments",
        threshold=0.7,
        metric="pearson",
        write_artifact=True,
    )
    data = json.loads(artifact_path.read_text())
    assert pre_existing_key in data, "existing key was clobbered"
    assert data[pre_existing_key] == pre_existing_value
    # And the new key was added.
    new_keys = [k for k in data if k != pre_existing_key]
    assert len(new_keys) == 1
    assert new_keys[0].startswith("0.7:pearson:")
