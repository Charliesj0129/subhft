"""Maker-aware cost view (``taifex_maker_qhat_v1``) — approved additive extension.

Pure functions over flip events produced by the taker cost proxy; no
ClickHouse, no I/O beyond the caller-supplied :class:`QHatTable`.

Model: a maker pays commission+tax regardless of fill; with probability
``p_fill`` (calibrated q_hat) the quote fills in queue (no spread paid),
otherwise the maker crosses the spread (taker degenerate case)::

    p_fill_i = QHatTable.lookup(q_hat_symbol, hour_utc(ts_i), near_side_L1_qty_i)
    maker_required_move_pts = 2*(comm+tax pts/side) + (1 - mean_p_fill) * median_spread_pts
    maker_cost_survival_score = gross_pts_per_flip / maker_required_move_pts

By construction ``maker_required_move_pts <= taker required_move_pts`` (equal
at ``p_fill = 0``) and ``>= 2*(comm+tax)`` (the zero-spread lower bound), so
at an equal threshold this view can never kill a candidate the taker gate
passed — it exists to mark candidates dead under ANY execution and to surface
the maker-rescuable pool. It never relaxes the frozen taifex_v1 taker gate.

Hour convention: UTC epoch-modulo, EXACTLY matching
``research/backtest/calibrate_queue_fill.py::_hour_of_day`` — the q_hat table
is keyed that way; "correcting" to Taipei hours would silently swap day/night
liquidity regimes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from research.backtest.q_hat_table import QHatTable

MAKER_COST_ASSUMPTION_VERSION = "taifex_maker_qhat_v1"


def hour_of_day_utc(ts_ns: int) -> int:
    """Epoch-modulo UTC hour (0-23); MUST match calibrate_queue_fill._hour_of_day."""
    return int((ts_ns // 1_000_000_000) // 3600 % 24)


@dataclass(frozen=True)
class MakerCostResult:
    maker_fill_prob_mean: float
    maker_required_move_threshold_pts: float
    maker_cost_survival_score: float
    maker_cost_assumption_version: str = MAKER_COST_ASSUMPTION_VERSION
    n_flip_events: int = 0


def fill_probs_for_flips(
    flip_ts_ns: np.ndarray,
    near_side_l1_qty: np.ndarray,
    q_hat: QHatTable,
    q_hat_symbol: str,
) -> np.ndarray:
    """Per-flip calibrated fill probability via (symbol, hour, depth) lookup."""
    n = int(flip_ts_ns.size)
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        depth = int(near_side_l1_qty[i]) if np.isfinite(near_side_l1_qty[i]) else 0
        out[i] = q_hat.lookup(q_hat_symbol, hour_of_day_utc(int(flip_ts_ns[i])), depth)
    return out


def compute_maker_cost(
    *,
    flip_ts_ns: np.ndarray,
    near_side_l1_qty: np.ndarray,
    gross_pts_per_flip: float,
    median_spread_pts: float,
    cost_per_side_pts: float,
    q_hat: QHatTable,
    q_hat_symbol: str,
) -> MakerCostResult:
    """Maker-aware required move + survival score over pooled flip events.

    With zero flip events ``mean_p_fill = 0`` so the required move degenerates
    to the taker formula (fail-closed: no optimism without evidence).
    """
    p_fill = fill_probs_for_flips(flip_ts_ns, near_side_l1_qty, q_hat, q_hat_symbol)
    mean_p = float(np.mean(p_fill)) if p_fill.size else 0.0
    required = 2.0 * cost_per_side_pts + (1.0 - mean_p) * median_spread_pts
    score = gross_pts_per_flip / required if required > 0.0 else 0.0
    return MakerCostResult(
        maker_fill_prob_mean=mean_p,
        maker_required_move_threshold_pts=required,
        maker_cost_survival_score=score,
        n_flip_events=int(p_fill.size),
    )


__all__ = [
    "MAKER_COST_ASSUMPTION_VERSION",
    "MakerCostResult",
    "compute_maker_cost",
    "fill_probs_for_flips",
    "hour_of_day_utc",
]
