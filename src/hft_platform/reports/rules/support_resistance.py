"""Support and resistance price-level rules SR-01 through SR-06.

All price arguments are ScaledPrice (int x10_000) per the Precision Law.
All functions return list[PriceLevel] and are pure (no side effects).
"""

from __future__ import annotations

from collections import defaultdict

from hft_platform.reports.models import Bar5m, LargeTrade, PriceLevel, SessionData

__all__ = [
    "find_large_trade_levels",
    "find_double_bottoms_tops",
    "find_round_numbers",
    "find_session_extremes",
    "find_volume_at_price",
    "find_failed_breakouts",
]

PLATFORM_SCALE: int = 10_000


def _fmt(price: int) -> str:
    """Format a ScaledPrice as a human-readable point value with commas."""
    return f"{price // PLATFORM_SCALE:,}"


# ---------------------------------------------------------------------------
# SR-01
# ---------------------------------------------------------------------------


def find_large_trade_levels(
    trades: list[LargeTrade],
    min_volume: int = 20,
) -> list[PriceLevel]:
    """SR-01 — Map each large trade to a support/resistance PriceLevel.

    Args:
        trades: List of LargeTrade events.
        min_volume: Minimum volume threshold; trades below this are ignored.

    Returns:
        One PriceLevel per qualifying trade.
    """
    levels: list[PriceLevel] = []
    for trade in trades:
        if trade.volume < min_volume:
            continue
        if trade.direction == "buy":
            reason = "支撐"
        elif trade.direction == "sell":
            reason = "壓力"
        else:
            reason = "關鍵"
        strength = min(1.0, trade.volume / 50.0)
        levels.append(PriceLevel(price=trade.price, strength=strength, reason=reason))
    return levels


# ---------------------------------------------------------------------------
# SR-02
# ---------------------------------------------------------------------------


def find_double_bottoms_tops(
    bars: list[Bar5m],
    tolerance: int = 50_000,
) -> list[PriceLevel]:
    """SR-02 — Detect double-bottom and double-top candlestick patterns.

    Args:
        bars: Ordered list of 5-minute bars (oldest first).
        tolerance: Maximum price difference (ScaledPrice units) for the two
            lows/highs to be considered equal.  Default is ±5 pts.

    Returns:
        PriceLevel entries with strength 0.9 for each confirmed pattern.
    """
    if len(bars) < 3:
        return []

    levels: list[PriceLevel] = []
    n = len(bars)

    # Double bottoms: find pair (i, j) where j >= i + 2,
    # lows within tolerance, and a higher low exists strictly between them.
    for i in range(n - 2):
        for j in range(i + 2, n):
            low_i = bars[i].low
            low_j = bars[j].low
            if abs(low_i - low_j) > tolerance:
                continue
            # There must be at least one bar between i and j with a higher low
            middle_lows = [bars[k].low for k in range(i + 1, j)]
            if not middle_lows:
                continue
            higher_middle = any(ml > max(low_i, low_j) for ml in middle_lows)
            if not higher_middle:
                continue
            avg_price = (low_i + low_j) // 2
            levels.append(
                PriceLevel(
                    price=avg_price,
                    strength=0.9,
                    reason=f"雙底 {_fmt(avg_price)}",
                )
            )

    # Double tops: find pair (i, j) where j >= i + 2,
    # highs within tolerance, and a lower high exists strictly between them.
    for i in range(n - 2):
        for j in range(i + 2, n):
            high_i = bars[i].high
            high_j = bars[j].high
            if abs(high_i - high_j) > tolerance:
                continue
            middle_highs = [bars[k].high for k in range(i + 1, j)]
            if not middle_highs:
                continue
            lower_middle = any(mh < min(high_i, high_j) for mh in middle_highs)
            if not lower_middle:
                continue
            avg_price = (high_i + high_j) // 2
            levels.append(
                PriceLevel(
                    price=avg_price,
                    strength=0.9,
                    reason=f"雙頂 {_fmt(avg_price)}",
                )
            )

    return levels


# ---------------------------------------------------------------------------
# SR-03
# ---------------------------------------------------------------------------


def find_round_numbers(low: int, high: int) -> list[PriceLevel]:
    """SR-03 — Identify psychologically significant round-number price levels.

    Multiples of 1000 pts (importance 3), 500 pts (importance 2), and
    100 pts (importance 1) within [low, high] are returned.  Where a price
    qualifies for multiple tiers only the highest-importance entry is kept.

    Args:
        low: Lower bound of the price range (ScaledPrice).
        high: Upper bound of the price range (ScaledPrice).

    Returns:
        Deduplicated PriceLevel list sorted ascending by price.
    """
    # Tier definitions: (step_points, importance)
    tiers: list[tuple[int, int]] = [
        (1000, 3),
        (500, 2),
        (100, 1),
    ]

    # price → best importance found so far
    best: dict[int, int] = {}

    for step_pts, importance in tiers:
        step_scaled = step_pts * PLATFORM_SCALE
        # First multiple of step_scaled that is >= low
        start = ((low + step_scaled - 1) // step_scaled) * step_scaled
        price = start
        while price <= high:
            if price not in best or importance > best[price]:
                best[price] = importance
            price += step_scaled

    levels: list[PriceLevel] = [
        PriceLevel(
            price=p,
            strength=importance / 3.0,
            reason=f"整數關卡 {_fmt(p)}",
        )
        for p, importance in sorted(best.items())
    ]
    return levels


# ---------------------------------------------------------------------------
# SR-04
# ---------------------------------------------------------------------------


def find_session_extremes(sd: SessionData) -> list[PriceLevel]:
    """SR-04 — Return the session high and low as PriceLevels.

    Args:
        sd: Full session snapshot.

    Returns:
        Two PriceLevel entries: session high (index 0) and session low (index 1).
    """
    high_level = PriceLevel(
        price=sd.high,
        strength=0.5,
        reason=f"{sd.session}盤高點 {_fmt(sd.high)}",
    )
    low_level = PriceLevel(
        price=sd.low,
        strength=0.5,
        reason=f"{sd.session}盤低點 {_fmt(sd.low)}",
    )
    return [high_level, low_level]


# ---------------------------------------------------------------------------
# SR-05
# ---------------------------------------------------------------------------


def find_volume_at_price(
    bars: list[Bar5m],
    bucket_size: int = 500_000,
    top_n: int = 3,
) -> list[PriceLevel]:
    """SR-05 — Volume-at-price (VAP) analysis bucketed by mid-price.

    Each bar is assigned to the bucket whose lower edge is the largest
    multiple of *bucket_size* that is <= the bar's mid-price.  Volume is
    accumulated per bucket and the top-N buckets are returned.

    Args:
        bars: Ordered list of 5-minute bars.
        bucket_size: Width of each price bucket in ScaledPrice units.
        top_n: Number of top-volume buckets to return.

    Returns:
        Up to *top_n* PriceLevel entries, sorted descending by volume.
    """
    if not bars:
        return []

    vol_by_bucket: dict[int, int] = defaultdict(int)
    for bar in bars:
        mid = (bar.high + bar.low) // 2
        bucket = (mid // bucket_size) * bucket_size
        vol_by_bucket[bucket] += bar.volume

    total_vol = sum(vol_by_bucket.values())
    if total_vol == 0:
        return []

    sorted_buckets = sorted(vol_by_bucket.items(), key=lambda kv: kv[1], reverse=True)
    result: list[PriceLevel] = []
    for bucket_price, vol in sorted_buckets[:top_n]:
        strength = min(1.0, vol / total_vol * 2.0)
        result.append(
            PriceLevel(
                price=bucket_price,
                strength=strength,
                reason=f"成交量集中 {_fmt(bucket_price)}",
            )
        )
    return result


# ---------------------------------------------------------------------------
# SR-06
# ---------------------------------------------------------------------------


def find_failed_breakouts(
    bars: list[Bar5m],
    large_trades: list[LargeTrade],
    min_reversal_pts: int = 500_000,
) -> list[PriceLevel]:
    """SR-06 — Detect failed-breakout patterns confirmed by large trades.

    A failed high breakout occurs when:
      * bar[i+1] makes a new high vs bar[i]
      * bar[i+2].close < bar[i+1].open - min_reversal_pts
      * A large sell trade exists within the tolerance band around bar[i+1].high

    A failed low breakout (false breakdown) is the mirror image.

    Args:
        bars: Ordered list of 5-minute bars (oldest first).
        large_trades: All large trades for the session.
        min_reversal_pts: Minimum price reversal in ScaledPrice units.

    Returns:
        PriceLevel entries at the failed breakout price.
    """
    if len(bars) < 3:
        return []

    # Build trade index by price (±tolerance) for fast lookup
    trade_tolerance = 100 * PLATFORM_SCALE  # 100 pts

    def _has_trade_near(target_price: int, direction: str) -> bool:
        for t in large_trades:
            if abs(t.price - target_price) <= trade_tolerance and t.direction == direction:
                return True
        return False

    levels: list[PriceLevel] = []

    for i in range(len(bars) - 2):
        prev, curr, nxt = bars[i], bars[i + 1], bars[i + 2]

        # Failed high breakout → resistance
        if curr.high > prev.high:
            reversal_close = curr.open - min_reversal_pts
            if nxt.close < reversal_close and _has_trade_near(curr.high, "sell"):
                levels.append(
                    PriceLevel(
                        price=curr.high,
                        strength=0.8,
                        reason=f"假突破壓力 {_fmt(curr.high)}",
                    )
                )

        # Failed low breakout → support
        if curr.low < prev.low:
            reversal_close = curr.open + min_reversal_pts
            if nxt.close > reversal_close and _has_trade_near(curr.low, "buy"):
                levels.append(
                    PriceLevel(
                        price=curr.low,
                        strength=0.8,
                        reason=f"假突破支撐 {_fmt(curr.low)}",
                    )
                )

    return levels
