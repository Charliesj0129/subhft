from __future__ import annotations

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


class HfExecSopDemoAlpha:
    def __init__(self) -> None:
        self._signal = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="hf_exec_sop_demo",
            hypothesis="Depth imbalance combined with queue pressure predicts short-horizon direction, while wide spread should dampen conviction.",
            formula="alpha_t = (0.7 * depth_imbalance_ppm_t / 1e6 + 0.3 * queue_imbalance_t) / (1 + spread_scaled_t / 1e6)",
            paper_refs=("128",),
            data_fields=("depth_imbalance_ppm", "l1_bid_qty", "l1_ask_qty", "spread_scaled", "mid_price_x2"),
            complexity="O(1)",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.ENSEMBLE,
            rust_module=None,
            latency_profile=None,
            roles_used=("planner", "code-reviewer"),
            skills_used=("iterative-retrieval", "validation-gate"),
            feature_set_version="lob_shared_v1",
        )

    def update(self, *args, **kwargs) -> float:
        depth_ppm = float(kwargs.get("depth_imbalance_ppm", 0.0))
        bid_qty = float(kwargs.get("l1_bid_qty", kwargs.get("bid_qty", 0.0)))
        ask_qty = float(kwargs.get("l1_ask_qty", kwargs.get("ask_qty", 0.0)))
        spread_scaled = float(kwargs.get("spread_scaled", 0.0))

        total_qty = bid_qty + ask_qty
        queue_imbalance = ((bid_qty - ask_qty) / total_qty) if total_qty > 0.0 else 0.0
        depth_term = depth_ppm / 1_000_000.0
        spread_penalty = 1.0 + max(0.0, spread_scaled) / 1_000_000.0

        self._signal = (0.7 * depth_term + 0.3 * queue_imbalance) / spread_penalty
        return self._signal

    def reset(self) -> None:
        self._signal = 0.0

    def get_signal(self) -> float:
        return self._signal

    def update_batch(self, data) -> np.ndarray:
        arr = np.asarray(data)
        if arr.size == 0:
            return np.zeros(0, dtype=np.float64)

        if not arr.dtype.names:
            values = np.asarray(arr, dtype=np.float64).reshape(-1)
            self._signal = float(values[-1]) if values.size else 0.0
            return values

        def _field(name: str, fallback: float = 0.0) -> np.ndarray:
            if name in arr.dtype.names:
                return np.asarray(arr[name], dtype=np.float64).reshape(-1)
            return np.full(arr.shape[0], fallback, dtype=np.float64)

        depth_ppm = _field("depth_imbalance_ppm")
        bid_qty = _field("l1_bid_qty")
        ask_qty = _field("l1_ask_qty")
        spread_scaled = _field("spread_scaled")

        total_qty = bid_qty + ask_qty
        queue_imbalance = np.divide(
            bid_qty - ask_qty,
            total_qty,
            out=np.zeros_like(total_qty),
            where=total_qty > 0.0,
        )
        depth_term = depth_ppm / 1_000_000.0
        spread_penalty = 1.0 + np.maximum(spread_scaled, 0.0) / 1_000_000.0
        out = (0.7 * depth_term + 0.3 * queue_imbalance) / spread_penalty

        self._signal = float(out[-1]) if out.size else 0.0
        return out.astype(np.float64, copy=False)


ALPHA_CLASS = HfExecSopDemoAlpha
