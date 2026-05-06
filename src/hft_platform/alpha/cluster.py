"""Slice-D Task 7: hierarchical clustering on alpha correlation matrix.

Single-linkage agglomerative clustering on ``1 - |corr|`` distance, cut at
``1 - threshold`` (defaults to 0.3 for ρ=0.7). Wraps
``pool.compute_pool_matrix`` to obtain the correlation payload, then assigns
each alpha to a cluster with deterministic, lex-stable cluster ids.

Determinism contract (Codex §10 H mitigation):
  * ``alpha_ids`` are sorted lexicographically before clustering, so the
    correlation matrix is reordered consistently regardless of input
    order from upstream callers.
  * ``cluster_id`` for multi-alpha clusters is ``cluster_<rank>`` where
    rank is the 0-indexed position of the cluster's lex-min alpha among
    all clusters also sorted by lex-min alpha.
  * Singleton clusters use ``singleton_<alpha_id>``; their
    ``max_intra_cluster_corr`` is ``0.0`` (no peer).

Sidecar artifact (when ``write_artifact=True``):
  * Path: ``research/alphas/_cluster_assignments.json`` (gitignored).
  * Top-level dict, key = ``f"{threshold}:{metric}:{sha256(base_dir)}:{corpus_hash}"``.
  * ``corpus_hash = sha256(json.dumps(sorted(alpha_ids)))`` — same alpha
    set ⇒ same key ⇒ idempotent overwrite within a (threshold, metric, corpus).
  * Atomic write via ``.tmp`` + ``os.replace``; merges with any existing
    keys so concurrent thresholds/metrics can coexist in the same file.

Module is offline-only (``alpha/`` permitted to use ``float`` per
``.agent/rules/25-architecture-governance.md`` §11). No CK / no live data.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from hft_platform.alpha.pool import compute_pool_matrix

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class EmptyCorpusError(RuntimeError):
    """Raised when ``compute_pool_matrix`` returns no alpha signals."""


@dataclass(frozen=True, slots=True)
class ClusterAssignment:
    """One alpha's cluster placement.

    Fields:
        alpha_id: The alpha being assigned.
        cluster_id: ``"cluster_<rank>"`` for shared clusters,
            ``"singleton_<alpha_id>"`` for solo alphas.
        cluster_size: Number of alphas in this cluster (>= 1).
        max_intra_cluster_corr: Max ``|corr|`` between any pair within
            the cluster. ``0.0`` for singletons (no peer to correlate with).
    """

    alpha_id: str
    cluster_id: str
    cluster_size: int
    max_intra_cluster_corr: float


#: Default sidecar artifact location. Tests monkeypatch this attribute to
#: redirect writes into ``tmp_path``.
_ARTIFACT_PATH: Path = Path("research/alphas/_cluster_assignments.json")


def cluster_alphas(
    *,
    base_dir: str = "research/experiments",
    threshold: float = 0.7,
    metric: str = "pearson",
    write_artifact: bool = False,
) -> list[ClusterAssignment]:
    """Cluster all alphas with experiment runs under ``base_dir``.

    Args:
        base_dir: Path passed through to ``compute_pool_matrix``.
        threshold: Absolute correlation cutoff. Pairs with
            ``|corr| >= threshold`` collapse into the same cluster.
        metric: ``"pearson"`` (default) or ``"spearman"``; selects which
            correlation matrix from the payload to use.
        write_artifact: When True, persists results to
            ``_ARTIFACT_PATH`` keyed by
            ``f"{threshold}:{metric}:{sha256(base_dir)}:{corpus_hash}"``.

    Returns:
        List of ``ClusterAssignment`` records, one per alpha. Order
        follows the lex-sorted alpha id list.

    Raises:
        EmptyCorpusError: If ``compute_pool_matrix`` returns an empty
            ``alpha_ids`` list (no signals available).
    """
    payload = compute_pool_matrix(base_dir=base_dir)
    if not payload.get("alpha_ids"):
        raise EmptyCorpusError(f"compute_pool_matrix returned no alpha_ids for base_dir={base_dir!r}")

    assignments = _cluster_from_payload(payload, threshold=threshold, metric=metric)

    if write_artifact:
        _persist_artifact(
            assignments,
            base_dir=base_dir,
            threshold=threshold,
            metric=metric,
            alpha_ids=list(payload["alpha_ids"]),
        )

    return assignments


# ---------------------------------------------------------------------------
# Pure helper — testable without monkeypatching ``compute_pool_matrix``.
# ---------------------------------------------------------------------------


def _cluster_from_payload(
    matrix_payload: dict[str, Any],
    *,
    threshold: float,
    metric: str,
) -> list[ClusterAssignment]:
    """Cluster from a pre-computed correlation payload.

    Pre-sorts ``alpha_ids`` lexicographically and reorders the correlation
    matrix rows/columns to match. This is the determinism mitigation.
    """
    raw_ids = list(matrix_payload.get("alpha_ids", []))
    if not raw_ids:
        return []

    matrix_key = "spearman_matrix" if metric == "spearman" else "pearson_matrix"
    raw_matrix = matrix_payload.get(matrix_key) or matrix_payload.get("matrix", [])
    corr = np.asarray(raw_matrix, dtype=np.float64)

    # Determinism: lex-sort alpha_ids and reorder matrix accordingly.
    # Build a position map (O(n)) instead of repeated ``list.index`` (O(n²)).
    sorted_ids = sorted(raw_ids)
    raw_id_pos = {aid: i for i, aid in enumerate(raw_ids)}
    perm = [raw_id_pos[aid] for aid in sorted_ids]

    # Singleton corpus — skip linkage entirely.
    if len(sorted_ids) == 1:
        return [
            ClusterAssignment(
                alpha_id=sorted_ids[0],
                cluster_id=f"singleton_{sorted_ids[0]}",
                cluster_size=1,
                max_intra_cluster_corr=0.0,
            )
        ]

    if corr.ndim != 2 or corr.shape[0] != corr.shape[1] or corr.shape[0] != len(raw_ids):
        # Degenerate / mismatched matrix — emit all singletons defensively.
        # Warn so downstream alerting (Slice-D audit log + Prom) can fire.
        logger.warning(
            "cluster_degenerate_matrix",
            n_alphas=len(sorted_ids),
            matrix_shape=tuple(corr.shape) if corr.ndim else (),
            metric=metric,
        )
        return [
            ClusterAssignment(
                alpha_id=aid,
                cluster_id=f"singleton_{aid}",
                cluster_size=1,
                max_intra_cluster_corr=0.0,
            )
            for aid in sorted_ids
        ]

    corr = corr[np.ix_(perm, perm)]
    abs_corr = np.abs(corr)
    # Distance matrix; clip to [0, 1] to absorb numerical noise.
    dist = np.clip(1.0 - abs_corr, 0.0, 1.0)
    np.fill_diagonal(dist, 0.0)
    # squareform requires perfectly symmetric input.
    dist = (dist + dist.T) / 2.0

    condensed = squareform(dist, checks=False)
    linkage_matrix = linkage(condensed, method="single")
    cut = max(0.0, 1.0 - float(threshold))
    labels = fcluster(linkage_matrix, t=cut, criterion="distance")

    # Group alphas by integer label (from fcluster).
    groups: dict[int, list[str]] = {}
    for idx, label in enumerate(labels):
        groups.setdefault(int(label), []).append(sorted_ids[idx])

    # Determine cluster_id naming:
    #   * size == 1: ``singleton_<alpha_id>``
    #   * size  > 1: rank multi-clusters by their lex-min alpha and emit
    #                ``cluster_<rank>`` for each member.
    multi_groups = sorted(
        (g for g in groups.values() if len(g) > 1),
        key=lambda g: min(g),
    )
    cluster_id_by_alpha: dict[str, str] = {}
    cluster_size_by_alpha: dict[str, int] = {}
    for rank, members in enumerate(multi_groups):
        cluster_id = f"cluster_{rank}"
        for aid in members:
            cluster_id_by_alpha[aid] = cluster_id
            cluster_size_by_alpha[aid] = len(members)

    # Compute max intra-cluster |corr| per multi-cluster.
    max_corr_by_alpha: dict[str, float] = {}
    id_to_perm_idx = {aid: i for i, aid in enumerate(sorted_ids)}
    for members in multi_groups:
        member_idx = [id_to_perm_idx[aid] for aid in members]
        sub = abs_corr[np.ix_(member_idx, member_idx)]
        # Mask diagonal (self-correlation = 1.0).
        m = sub.copy()
        np.fill_diagonal(m, 0.0)
        max_corr = float(np.max(m)) if m.size else 0.0
        for aid in members:
            max_corr_by_alpha[aid] = max_corr

    # Emit one ClusterAssignment per alpha, in lex-sorted order.
    out: list[ClusterAssignment] = []
    for aid in sorted_ids:
        if aid in cluster_id_by_alpha:
            out.append(
                ClusterAssignment(
                    alpha_id=aid,
                    cluster_id=cluster_id_by_alpha[aid],
                    cluster_size=cluster_size_by_alpha[aid],
                    max_intra_cluster_corr=max_corr_by_alpha.get(aid, 0.0),
                )
            )
        else:
            out.append(
                ClusterAssignment(
                    alpha_id=aid,
                    cluster_id=f"singleton_{aid}",
                    cluster_size=1,
                    max_intra_cluster_corr=0.0,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Sidecar artifact persistence
# ---------------------------------------------------------------------------


def _persist_artifact(
    assignments: list[ClusterAssignment],
    *,
    base_dir: str,
    threshold: float,
    metric: str,
    alpha_ids: list[str],
) -> None:
    """Atomic merge-write into ``_ARTIFACT_PATH``."""
    path = _ARTIFACT_PATH
    base_dir_hash = hashlib.sha256(base_dir.encode("utf-8")).hexdigest()
    corpus_hash = hashlib.sha256(json.dumps(sorted(alpha_ids)).encode("utf-8")).hexdigest()
    key = f"{threshold}:{metric}:{base_dir_hash}:{corpus_hash}"

    payload: list[dict[str, Any]] = [asdict(a) for a in assignments]

    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if not isinstance(existing, dict):
                logger.warning(
                    "cluster_artifact_unexpected_shape",
                    path=str(path),
                    type=type(existing).__name__,
                )
                existing = {}
        except (OSError, ValueError) as exc:
            logger.warning(
                "cluster_artifact_unreadable",
                path=str(path),
                error=str(exc),
            )
            existing = {}

    existing[key] = payload

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(existing, indent=2, sort_keys=True))
    os.replace(tmp_path, path)
    logger.info(
        "cluster_artifact_written",
        path=str(path),
        key=key,
        n_alphas=len(assignments),
    )
