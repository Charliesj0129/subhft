"""Informed-flow scoring rules IF-01 through IF-06.

All functions are pure (no side effects) and return scores in [-1.0, +1.0]
unless noted otherwise.  Inputs use the platform's ScaledPrice convention
(int x10000) for all price fields.
"""

from __future__ import annotations

from hft_platform.reports.models import FlowBar, LargeTrade

__all__ = [
    "score_session_ud",
    "score_sustained_pressure",
    "score_large_trade_net",
    "find_large_trade_clusters",
    "score_end_of_session_drift",
    "score_volume_spike",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _ud_ratio_from_bars(bars: list[FlowBar]) -> float:
    """Return sum(uptick_vol) / sum(downtick_vol) for a list of bars.

    Returns 1.0 when both totals are zero to represent neutral.
    """
    total_up = sum(b.uptick_vol for b in bars)
    total_dn = sum(b.downtick_vol for b in bars)
    if total_dn == 0:
        return float("inf") if total_up > 0 else 1.0
    return total_up / total_dn


def _ratio_to_score(ratio: float) -> float:
    """Linear map: ratio 0.9 → -1.0, 1.0 → 0.0, 1.1 → +1.0 (clamped)."""
    if ratio == float("inf"):
        return 1.0
    # slope = 1.0 / 0.1 = 10  so score = (ratio - 1.0) * 10
    return _clamp((ratio - 1.0) * 10.0)


# ---------------------------------------------------------------------------
# IF-01  score_session_ud
# ---------------------------------------------------------------------------


def score_session_ud(bars: list[FlowBar]) -> float:
    """IF-01: Session-wide up/down volume ratio score.

    Sums uptick_vol and downtick_vol across all bars, computes
    ratio = total_up / total_dn, then maps linearly:
        0.9 → -1.0,  1.0 → 0.0,  1.1 → +1.0  (clamped to [-1, 1]).

    Returns 0.0 for an empty bar list.
    """
    if not bars:
        return 0.0
    return _ratio_to_score(_ud_ratio_from_bars(bars))


# ---------------------------------------------------------------------------
# IF-02  score_sustained_pressure
# ---------------------------------------------------------------------------


def score_sustained_pressure(bars: list[FlowBar]) -> float:
    """IF-02: Maximum consecutive run of strongly directional bars.

    A bar is *bearish* when ud_ratio < 0.7, *bullish* when ud_ratio > 1.3.
    The longest consecutive run is found for each direction.  A run of ≥4
    bars scores ±(count/4), capped at ±1.0.  No qualifying run → 0.0.
    """
    if not bars:
        return 0.0

    max_bull = 0
    max_bear = 0
    cur_bull = 0
    cur_bear = 0

    for bar in bars:
        if bar.ud_ratio > 1.3:
            cur_bull += 1
            cur_bear = 0
        elif bar.ud_ratio < 0.7:
            cur_bear += 1
            cur_bull = 0
        else:
            cur_bull = 0
            cur_bear = 0
        max_bull = max(max_bull, cur_bull)
        max_bear = max(max_bear, cur_bear)

    if max_bull >= 4:
        return _clamp(max_bull / 4.0)
    if max_bear >= 4:
        return _clamp(-(max_bear / 4.0))
    return 0.0


# ---------------------------------------------------------------------------
# IF-03  score_large_trade_net
# ---------------------------------------------------------------------------


def score_large_trade_net(trades: list[LargeTrade]) -> float:
    """IF-03: Net directional volume of large trades.

    buy_vol = sum(volume) for direction=="buy"
    sell_vol = sum(volume) for direction=="sell"
    "unknown" direction is ignored.

    Returns net / (buy + sell) clamped to [-1, 1].
    Returns 0.0 when there are no known-direction trades.
    """
    if not trades:
        return 0.0

    buy_vol = sum(t.volume for t in trades if t.direction == "buy")
    sell_vol = sum(t.volume for t in trades if t.direction == "sell")
    total = buy_vol + sell_vol
    if total == 0:
        return 0.0
    return _clamp((buy_vol - sell_vol) / total)


# ---------------------------------------------------------------------------
# IF-04  find_large_trade_clusters
# ---------------------------------------------------------------------------


def find_large_trade_clusters(
    trades: list[LargeTrade],
    price_tolerance: int = 30_000,
    time_window_s: float = 60.0,  # reserved for future timestamp-aware logic
) -> list[tuple[int, int]]:
    """IF-04: Identify clusters of ≥3 large trades within price_tolerance.

    Groups trades by price proximity only (timestamp parsing is deferred).
    Two trades are "close" when abs(price_a - price_b) <= price_tolerance.

    This uses a greedy single-linkage approach: for each unassigned trade,
    form a group of all trades whose price lies within ±price_tolerance of
    that trade's price.  Only groups with ≥3 members are returned.

    Returns a list of (representative_price, total_volume) tuples, one per
    qualifying cluster.
    """
    if len(trades) < 3:
        return []

    # Sort by price to make proximity checks efficient.
    sorted_trades = sorted(trades, key=lambda t: t.price)
    used = [False] * len(sorted_trades)
    clusters: list[tuple[int, int]] = []

    for i, anchor in enumerate(sorted_trades):
        if used[i]:
            continue
        group = [anchor]
        for j in range(i + 1, len(sorted_trades)):
            if sorted_trades[j].price - anchor.price > price_tolerance:
                break
            group.append(sorted_trades[j])

        if len(group) >= 3:
            for k in range(i, i + len(group)):
                used[k] = True
            total_vol = sum(t.volume for t in group)
            clusters.append((anchor.price, total_vol))

    return clusters


# ---------------------------------------------------------------------------
# IF-05  score_end_of_session_drift
# ---------------------------------------------------------------------------


def score_end_of_session_drift(bars: list[FlowBar]) -> float:
    """IF-05: End-of-session U/D drift vs full session.

    Compares the U/D ratio of the last 6 bars (≈30 min) against the full
    session U/D ratio.

    drift = eod_ud - session_ud
    abs(drift) < 0.2 → 0.0
    otherwise → clamp(drift * 3.0, -1, 1)

    Requires ≥8 bars; returns 0.0 otherwise.
    """
    if len(bars) < 8:
        return 0.0

    eod_bars = bars[-6:]
    session_ud = _ud_ratio_from_bars(bars)
    eod_ud = _ud_ratio_from_bars(eod_bars)

    # Guard against inf comparisons
    if session_ud == float("inf") or eod_ud == float("inf"):
        if session_ud == eod_ud:
            return 0.0
        return 1.0 if eod_ud == float("inf") else -1.0

    drift = eod_ud - session_ud
    if abs(drift) < 0.2:
        return 0.0
    return _clamp(drift * 3.0)


# ---------------------------------------------------------------------------
# IF-06  score_volume_spike
# ---------------------------------------------------------------------------


def score_volume_spike(
    bars: list[FlowBar],
) -> tuple[float, list[FlowBar]]:
    """IF-06: Detect volume spikes and score their directional bias.

    A *spike bar* is any bar whose total_vol > 2× the session mean volume.

    Score = mean over spike bars of (net_flow / total_vol * 5.0), clamped
    to [-1, 1].  If there are no spike bars, score is 0.0.

    Returns (score, list_of_spike_bars).
    """
    if not bars:
        return 0.0, []

    mean_vol = sum(b.total_vol for b in bars) / len(bars)
    threshold = 2.0 * mean_vol

    spike_bars = [b for b in bars if b.total_vol > threshold]
    if not spike_bars:
        return 0.0, []

    total_score = 0.0
    for bar in spike_bars:
        if bar.total_vol == 0:
            continue
        raw = bar.net_flow / bar.total_vol * 5.0
        total_score += _clamp(raw)

    score = _clamp(total_score / len(spike_bars))
    return score, spike_bars
