"""SignalEngine: weighted rule scoring to produce a SignalReport from SessionData.

All price fields use ScaledPrice (int x10000) per the Precision Law.
All functions are pure; _assign_directions returns a new list and never mutates input.
"""
from __future__ import annotations

from hft_platform.reports.models import FlowBar, LargeTrade, PriceLevel, SessionData, SignalReport
from hft_platform.reports.rules.informed_flow import (
    find_large_trade_clusters,
    score_end_of_session_drift,
    score_large_trade_net,
    score_session_ud,
    score_sustained_pressure,
    score_volume_spike,
)
from hft_platform.reports.rules.support_resistance import (
    find_double_bottoms_tops,
    find_failed_breakouts,
    find_large_trade_levels,
    find_round_numbers,
    find_session_extremes,
    find_volume_at_price,
)

__all__ = ["SignalEngine"]

WEIGHTS: dict[str, float] = {
    "IF-01_session_ud": 0.25,
    "IF-02_sustained": 0.15,
    "IF-03_large_net": 0.20,
    "IF-04_cluster": 0.10,
    "IF-05_eod_drift": 0.10,
    "IF-06_vol_spike": 0.05,
    "SR-02_double_pattern": 0.10,
    "SR-06_failed_breakout": 0.05,
}


def _assign_directions(trades: list[LargeTrade], sd: SessionData) -> list[LargeTrade]:
    """Return a new list of LargeTrade with "unknown" directions resolved.

    Trades priced <= session midpoint are classified as "sell";
    trades priced > midpoint are classified as "buy".
    The original trade objects are never mutated.
    """
    midpoint = (sd.high + sd.low) // 2
    result: list[LargeTrade] = []
    for trade in trades:
        if trade.direction != "unknown":
            result.append(trade)
        else:
            direction = "sell" if trade.price <= midpoint else "buy"
            result.append(
                LargeTrade(
                    ts=trade.ts,
                    price=trade.price,
                    volume=trade.volume,
                    direction=direction,
                )
            )
    return result


def _score_cluster(
    clusters: list[tuple[int, int]],
    trades: list[LargeTrade],
) -> float:
    """IF-04 cluster score.

    -0.5 if sell-direction trades near cluster prices,
    +0.5 if buy-direction trades near cluster prices,
    else 0.
    """
    if not clusters:
        return 0.0

    price_tolerance = 30_000  # same default as find_large_trade_clusters
    sell_near = False
    buy_near = False

    for cluster_price, _ in clusters:
        for trade in trades:
            if abs(trade.price - cluster_price) <= price_tolerance:
                if trade.direction == "sell":
                    sell_near = True
                elif trade.direction == "buy":
                    buy_near = True

    if sell_near and not buy_near:
        return -0.5
    if buy_near and not sell_near:
        return 0.5
    return 0.0


def _score_double_pattern(levels: list[PriceLevel]) -> float:
    """SR-02 pattern score: +0.3 if any double bottom/top found, else 0."""
    return 0.3 if levels else 0.0


def _score_failed_breakout(levels: list[PriceLevel]) -> float:
    """SR-06 failed breakout score.

    -0.3 if any level has '壓力' in reason (resistance),
    +0.3 if any level has '支撐' in reason (support),
    else 0.
    """
    for level in levels:
        if "壓力" in level.reason:
            return -0.3
        if "支撐" in level.reason:
            return 0.3
    return 0.0


class SignalEngine:
    """Aggregates IF and SR rule scores into a weighted SignalReport."""

    def analyze(self, sd: SessionData) -> SignalReport:
        """Produce a SignalReport from a SessionData snapshot."""
        # ------------------------------------------------------------------
        # 1. Resolve trade directions
        # ------------------------------------------------------------------
        trades = _assign_directions(sd.large_trades, sd)

        # ------------------------------------------------------------------
        # 2. Compute IF rule scores
        # ------------------------------------------------------------------
        if01 = score_session_ud(sd.flow_5m)
        if02 = score_sustained_pressure(sd.flow_5m)
        if03 = score_large_trade_net(trades)
        clusters = find_large_trade_clusters(trades)
        if04 = _score_cluster(clusters, trades)
        if05 = score_end_of_session_drift(sd.flow_5m)
        if06_score, _ = score_volume_spike(sd.flow_5m)

        # ------------------------------------------------------------------
        # 3. Compute SR rule scores
        # ------------------------------------------------------------------
        double_levels = find_double_bottoms_tops(sd.bars_5m)
        sr02 = _score_double_pattern(double_levels)

        failed_levels = find_failed_breakouts(sd.bars_5m, trades)
        sr06 = _score_failed_breakout(failed_levels)

        # ------------------------------------------------------------------
        # 4. Weighted sum → bias
        # ------------------------------------------------------------------
        rule_scores: dict[str, float] = {
            "IF-01_session_ud": if01,
            "IF-02_sustained": if02,
            "IF-03_large_net": if03,
            "IF-04_cluster": if04,
            "IF-05_eod_drift": if05,
            "IF-06_vol_spike": if06_score,
            "SR-02_double_pattern": sr02,
            "SR-06_failed_breakout": sr06,
        }

        weighted_sum = sum(WEIGHTS[k] * v for k, v in rule_scores.items())

        if weighted_sum < -0.3:
            bias = "bearish"
        elif weighted_sum > 0.3:
            bias = "bullish"
        else:
            bias = "neutral"

        confidence = min(1.0, abs(weighted_sum))

        # ------------------------------------------------------------------
        # 5. Aggregate flow metrics
        # ------------------------------------------------------------------
        total_net_flow = sum(b.net_flow for b in sd.flow_5m)

        if sd.flow_5m:
            ud_ratio_session = sum(b.uptick_vol for b in sd.flow_5m) / max(
                1, sum(b.downtick_vol for b in sd.flow_5m)
            )
            strongest_sell = min(sd.flow_5m, key=lambda b: b.ud_ratio)
            strongest_buy = max(sd.flow_5m, key=lambda b: b.ud_ratio)
        else:
            ud_ratio_session = 1.0
            _dummy = FlowBar(
                ts="", ticks=0, total_vol=0, uptick_vol=0,
                downtick_vol=0, flat_vol=0, ud_ratio=1.0, net_flow=0,
            )
            strongest_sell = _dummy
            strongest_buy = _dummy

        large_buy_volume = sum(t.volume for t in trades if t.direction == "buy")
        large_sell_volume = sum(t.volume for t in trades if t.direction == "sell")
        large_net = large_buy_volume - large_sell_volume

        # ------------------------------------------------------------------
        # 6. Collect S/R levels, split into supports / resistances
        # ------------------------------------------------------------------
        all_levels: list[PriceLevel] = []
        all_levels.extend(find_large_trade_levels(trades))
        all_levels.extend(double_levels)
        all_levels.extend(find_round_numbers(sd.low, sd.high))
        all_levels.extend(find_session_extremes(sd))
        all_levels.extend(find_volume_at_price(sd.bars_5m))
        all_levels.extend(failed_levels)

        close = sd.close
        supports = sorted(
            [lv for lv in all_levels if lv.price <= close],
            key=lambda lv: lv.strength,
            reverse=True,
        )[:3]
        resistances = sorted(
            [lv for lv in all_levels if lv.price > close],
            key=lambda lv: lv.strength,
            reverse=True,
        )[:3]

        # ------------------------------------------------------------------
        # 7. Key large trades (all resolved trades, unchanged order)
        # ------------------------------------------------------------------
        key_large_trades = list(trades)

        return SignalReport(
            session_data=sd,
            total_net_flow=total_net_flow,
            ud_ratio_session=ud_ratio_session,
            strongest_sell=strongest_sell,
            strongest_buy=strongest_buy,
            large_buy_volume=large_buy_volume,
            large_sell_volume=large_sell_volume,
            large_net=large_net,
            key_large_trades=key_large_trades,
            supports=supports,
            resistances=resistances,
            bias=bias,
            bias_confidence=confidence,
            rule_scores=rule_scores,
        )
