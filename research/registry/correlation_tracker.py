from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from hft_platform.alpha.pool import compute_correlation_payload


class CorrelationTracker:
    def compute_matrix(
        self,
        signals: Mapping[str, Sequence[float]],
        *,
        sample_step: int = 1,
    ) -> dict[str, Any]:
        return compute_correlation_payload(signals=signals, sample_step=sample_step)

    def flag_redundant(
        self,
        payload: Mapping[str, Any],
        *,
        threshold: float = 0.7,
        metric: str = "pearson",
    ) -> list[dict[str, Any]]:
        from hft_platform.alpha.pool import flag_redundant_pairs

        return flag_redundant_pairs(dict(payload), threshold=threshold, metric=metric)

    def save(
        self,
        payload: Mapping[str, Any],
        *,
        path: str = "research/registry/correlation_matrix.parquet",
    ) -> str:
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
        except Exception:
            frame_written = False

        if frame_written:
            return str(out)

        fallback = out.with_suffix(".json")
        fallback.write_text(json.dumps(dict(payload), indent=2, sort_keys=True))
        return str(fallback)
