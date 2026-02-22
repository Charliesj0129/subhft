"""Registry helper for computing and persisting alpha correlation matrices.

``CorrelationTracker`` delegates computation to ``hft_platform.alpha.pool``
and persists the result to Parquet (preferred) or JSON (fallback).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from structlog import get_logger

from hft_platform.alpha.pool import compute_correlation_payload

logger = get_logger("registry.correlation_tracker")


class CorrelationTracker:
    """Compute, flag, and persist correlation matrices for the alpha pool."""

    def compute_matrix(
        self,
        signals: Mapping[str, Any],
        *,
        sample_step: int = 1,
    ) -> dict[str, Any]:
        """Return a correlation payload dict (Pearson + Spearman) for *signals*.

        Args:
            signals:     alpha_id → signal sequence/array mapping.
            sample_step: downsample factor applied before computing correlations.
        """
        return compute_correlation_payload(signals=signals, sample_step=sample_step)

    def flag_redundant(
        self,
        payload: Mapping[str, Any],
        *,
        threshold: float = 0.7,
        metric: str = "pearson",
    ) -> list[dict[str, Any]]:
        """Return pairs whose absolute correlation exceeds *threshold*.

        Args:
            payload:   result of :meth:`compute_matrix`.
            threshold: absolute-correlation cutoff (default 0.7).
            metric:    ``"pearson"`` or ``"spearman"``.
        """
        from hft_platform.alpha.pool import flag_redundant_pairs

        return flag_redundant_pairs(dict(payload), threshold=threshold, metric=metric)

    def save(
        self,
        payload: Mapping[str, Any],
        *,
        path: str = "research/registry/correlation_matrix.parquet",
    ) -> str:
        """Persist *payload* to *path* (Parquet) with a JSON fallback.

        Returns the path that was actually written (Parquet or ``.json``).
        """
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)

        frame_written = False
        try:
            import pandas as pd

            alpha_ids = list(payload.get("alpha_ids", []))
            matrix = np.asarray(payload.get("pearson_matrix") or payload.get("matrix", []), dtype=np.float64)
            if matrix.ndim == 2 and matrix.shape[0] == matrix.shape[1] and len(alpha_ids) == matrix.shape[0]:
                frame = pd.DataFrame(matrix, index=alpha_ids, columns=alpha_ids)
                frame.to_parquet(out)
                frame_written = True
        except (ImportError, OSError, ValueError) as exc:
            logger.warning(
                "correlation_tracker.save: Parquet write failed, falling back to JSON",
                path=str(out),
                error=str(exc),
            )
            frame_written = False

        if frame_written:
            return str(out)

        fallback = out.with_suffix(".json")
        fallback.write_text(json.dumps(dict(payload), indent=2, sort_keys=True))
        return str(fallback)
