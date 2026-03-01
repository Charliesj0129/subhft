from __future__ import annotations

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


class AlphaMidPriceV1Alpha:
    def __init__(self) -> None:
        self._signal = 0.0
        self._alpha_ema = 0.5

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="alpha_mid_price_v1",
            hypothesis="Short-horizon microstructure imbalance can predict near-term mid-price direction after controlling for spread and depth.",
            formula="alpha_t = (depth_imbalance_ppm_t / 1e6) - 0.5 * (spread_scaled_t / max(mid_price_x2_t, 1))",
            paper_refs=(),
            data_fields=("spread_scaled", "depth_imbalance_ppm", "l1_bid_qty", "l1_ask_qty", "mid_price_x2"),
            complexity="O(1)",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.ENSEMBLE,
            rust_module=None,
            latency_profile=None,
            roles_used=("planner", "code-reviewer"),
            skills_used=("iterative-retrieval", "validation-gate"),
            feature_set_version="lob_shared_v1",
        )

    def update(
        self,
        spread_scaled: int = 0,
        depth_imbalance_ppm: int = 0,
        l1_bid_qty: int = 0,
        l1_ask_qty: int = 0,
        mid_price_x2: int = 0,
        *args,
        **kwargs,
    ) -> float:
        spread = int(kwargs.get("spread_scaled", spread_scaled))
        depth_ppm = int(kwargs.get("depth_imbalance_ppm", depth_imbalance_ppm))
        bid_q = float(kwargs.get("l1_bid_qty", l1_bid_qty))
        ask_q = float(kwargs.get("l1_ask_qty", l1_ask_qty))
        mid_x2 = int(kwargs.get("mid_price_x2", mid_price_x2))

        if depth_ppm == 0:
            total_q = bid_q + ask_q
            if total_q > 0.0:
                depth_norm = (bid_q - ask_q) / total_q
            else:
                depth_norm = 0.0
        else:
            depth_norm = max(-1.0, min(1.0, float(depth_ppm) / 1_000_000.0))

        denom = float(max(mid_x2, 1))
        spread_penalty = float(spread) / denom
        raw_signal = depth_norm - (0.5 * spread_penalty)
        self._signal = (self._alpha_ema * raw_signal) + ((1.0 - self._alpha_ema) * self._signal)
        return self._signal

    def reset(self) -> None:
        self._signal = 0.0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = AlphaMidPriceV1Alpha
