"""
R44: Night Session VWAP Mean-Reversion Alpha

Hypothesis:
    During the TXFD6 night session (15:00-05:00 TWN), prices that deviate
    from session VWAP tend to revert. Negative IC of VWAP deviation vs
    forward returns indicates sell when above VWAP, buy when below.

Evidence (8 nights, 2026-03-19 to 2026-04-02):
    - IC(30min) = -0.28, t = -4.37, 7/8 days negative
    - IC(60min) = -0.29, t = -4.29, 8/8 days negative
    - Survives detrending (identical IC with/without trend removal)
    - Night spread: median 4 pts (same as day), RT cost = 4.7 pts
    - Not monotonically negative => NOT trend contamination

Open risks:
    - 8 nights is small sample. Need 20+ for promotion.
    - Backtest PnL heavily skewed by single date (2026-04-02).
    - VWAP MR is well-known; alpha may decay in live execution.
"""
from __future__ import annotations

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


class NightVwapMrAlpha:
    """Night session VWAP mean-reversion signal."""

    def __init__(self) -> None:
        self._signal = 0.0
        self._cum_vol = 0.0
        self._cum_pv = 0.0  # cumulative price * volume

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="r44_night_vwap_mr",
            hypothesis=(
                "TXFD6 night session prices mean-revert to session VWAP. "
                "Negative IC at 30-60 min horizons (t < -4) across 8 nights."
            ),
            formula="alpha_t = -(close_t - VWAP_t) / vol_t",
            paper_refs=(),
            data_fields=(
                "price_scaled",
                "volume",
            ),
            complexity="O(1)",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.STANDALONE,
            rust_module=None,
            latency_profile=None,
            roles_used=("planner",),
            skills_used=(),
            feature_set_version="lob_shared_v1",
        )

    def update(self, price: float, volume: float) -> float:
        """Update VWAP and compute signal (negative of deviation)."""
        self._cum_vol += volume
        self._cum_pv += price * volume
        if self._cum_vol > 0:
            vwap = self._cum_pv / self._cum_vol
            self._signal = -(price - vwap)
        else:
            self._signal = 0.0
        return self._signal

    def reset(self) -> None:
        self._signal = 0.0
        self._cum_vol = 0.0
        self._cum_pv = 0.0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = NightVwapMrAlpha
