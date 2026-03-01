from __future__ import annotations

from research.alphas.ofi_mc.impl import OFIMCAlpha
from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


class OfiCycleDemoAlpha(OFIMCAlpha):
    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="ofi_cycle_demo",
            hypothesis=(
                "Order-flow pressure at the top of book contains short-horizon directional information. "
                "Scale OFI by market-capacity proxy to keep signal stable across sessions."
            ),
            formula="OFI_t = BidFlow_t - AskFlow_t; signal = cumulative(OFI)/market_cap",
            paper_refs=("120",),
            data_fields=("bid_px", "bid_qty", "ask_px", "ask_qty", "trade_vol", "current_mid"),
            complexity="O(1)",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.ENSEMBLE,
            rust_module="alpha_ofi",
            latency_profile="shioaji_sim_p95_v2026-02-28",
            roles_used=("planner", "code-reviewer"),
            skills_used=("iterative-retrieval", "validation-gate"),
            feature_set_version="lob_shared_v1",
        )

    def update(self, *args, **kwargs) -> float:
        # Keep scaffold anti-leak test compatibility while still supporting OFI inputs.
        if not args and not kwargs:
            return self.get_signal()
        return float(super().update(*args, **kwargs))


ALPHA_CLASS = OfiCycleDemoAlpha
