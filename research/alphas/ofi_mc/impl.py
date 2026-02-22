from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


@dataclass
class OFIFactorResult:
    ofi_raw: float
    ofi_vol_norm: float
    ofi_mc_norm: float
    mid_price_change: float


class OFIMCFactor:
    """Order Flow Imbalance (OFI) with volume and market-cap normalization."""

    def __init__(self, market_cap: float = 1e9, shares_outstanding: float = 14332.0):
        self.market_cap = market_cap
        self.shares = shares_outstanding
        self.prev_bid_px: float | None = None
        self.prev_bid_qty: float | None = None
        self.prev_ask_px: float | None = None
        self.prev_ask_qty: float | None = None
        self.cumulative_ofi = 0.0
        self.cumulative_vol = 0.0
        self._signal = 0.0
        self.history: list[OFIFactorResult] = []

    def reset(self) -> None:
        self.prev_bid_px = None
        self.prev_bid_qty = None
        self.prev_ask_px = None
        self.prev_ask_qty = None
        self.cumulative_ofi = 0.0
        self.cumulative_vol = 0.0
        self._signal = 0.0
        self.history = []

    def compute(
        self,
        bid_px: float,
        bid_qty: float,
        ask_px: float,
        ask_qty: float,
        trade_vol: float,
        current_mid: float,
    ) -> Optional[OFIFactorResult]:
        del current_mid  # Reserved for future mid-price-based diagnostics.
        if self.prev_bid_px is None:
            self.prev_bid_px = bid_px
            self.prev_bid_qty = bid_qty
            self.prev_ask_px = ask_px
            self.prev_ask_qty = ask_qty
            return None

        if bid_px > self.prev_bid_px:
            b_flow = bid_qty
        elif bid_px == self.prev_bid_px:
            b_flow = bid_qty - (self.prev_bid_qty or 0.0)
        else:
            b_flow = -(self.prev_bid_qty or 0.0)

        if ask_px > self.prev_ask_px:
            a_flow = -(self.prev_ask_qty or 0.0)
        elif ask_px == self.prev_ask_px:
            a_flow = ask_qty - (self.prev_ask_qty or 0.0)
        else:
            a_flow = ask_qty

        ofi_t = b_flow - a_flow
        self.cumulative_ofi += ofi_t
        self.cumulative_vol += trade_vol

        self.prev_bid_px = bid_px
        self.prev_bid_qty = bid_qty
        self.prev_ask_px = ask_px
        self.prev_ask_qty = ask_qty

        vol_norm_factor = self.cumulative_vol if self.cumulative_vol > 0 else 1.0
        ofi_vol = self.cumulative_ofi / vol_norm_factor
        ofi_mc = self.cumulative_ofi / self.market_cap
        self._signal = ofi_mc

        result = OFIFactorResult(
            ofi_raw=self.cumulative_ofi,
            ofi_vol_norm=ofi_vol,
            ofi_mc_norm=ofi_mc,
            mid_price_change=0.0,
        )
        self.history.append(result)
        return result

    def get_signal(self) -> float:
        return self._signal


class OFIMCAlpha(OFIMCFactor):
    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="ofi_mc",
            hypothesis="Order-flow pressure normalized by market capacity has predictive power.",
            formula="OFI_t = BidFlow_t - AskFlow_t; signal = cumulative(OFI)/market_cap",
            paper_refs=("018",),
            data_fields=("bid_px", "bid_qty", "ask_px", "ask_qty", "trade_vol", "current_mid"),
            complexity="O(1)",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.TIER_2,
            rust_module="alpha_ofi",
        )

    def update(self, *args, **kwargs) -> float:
        if args:
            if len(args) < 6:
                raise ValueError("update() expects 6 positional args: bid_px, bid_qty, ask_px, ask_qty, trade_vol, current_mid")
            bid_px, bid_qty, ask_px, ask_qty, trade_vol, current_mid = args[:6]
        else:
            bid_px = kwargs["bid_px"]
            bid_qty = kwargs["bid_qty"]
            ask_px = kwargs["ask_px"]
            ask_qty = kwargs["ask_qty"]
            trade_vol = kwargs.get("trade_vol", 0.0)
            current_mid = kwargs.get("current_mid", 0.0)
        self.compute(
            bid_px=float(bid_px),
            bid_qty=float(bid_qty),
            ask_px=float(ask_px),
            ask_qty=float(ask_qty),
            trade_vol=float(trade_vol),
            current_mid=float(current_mid),
        )
        return self.get_signal()


ALPHA_CLASS = OFIMCAlpha

__all__ = ["OFIFactorResult", "OFIMCFactor", "OFIMCAlpha", "ALPHA_CLASS"]
