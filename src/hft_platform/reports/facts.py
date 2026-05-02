"""Layer 1 — FactExtractor: pure data extraction from SessionData.

Six extractors produce structured facts consumed by the Layer 2 reasoner.
All price fields remain ScaledPrice (int x10000) — no conversion.
"""

from __future__ import annotations

import math
from datetime import datetime, time
from typing import TYPE_CHECKING

import structlog

from hft_platform.reports.models import (
    ChipCluster,
    ChipFacts,
    CrossDayFacts,
    DaySnapshot,
    FactReport,
    FlowBar,
    FlowFacts,
    PriceLevel,
    SegmentFact,
    SessionData,
    StructureFacts,
    VolatilityFacts,
)
from hft_platform.reports.rules.informed_flow import find_large_trade_clusters
from hft_platform.reports.rules.support_resistance import (
    find_double_bottoms_tops,
    find_failed_breakouts,
    find_round_numbers,
    find_session_extremes,
    find_volume_at_price,
)

if TYPE_CHECKING:
    from hft_platform.reports.models import Bar5m, LargeTrade

__all__ = [
    "extract_time_segments",
    "extract_chip_facts",
    "extract_flow_facts",
    "extract_structure_facts",
    "extract_volatility_facts",
    "extract_cross_day_facts",
    "extract_all",
]

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Time-segment definitions
# ---------------------------------------------------------------------------

_DAY_SEGMENTS: list[tuple[str, time, time]] = [
    ("pre_open", time(7, 0), time(8, 45)),
    ("opening", time(8, 45), time(9, 30)),
    ("midday", time(9, 30), time(12, 0)),
    ("closing", time(12, 0), time(13, 45)),
]

_NIGHT_SEGMENTS: list[tuple[str, time, time]] = [
    ("opening", time(15, 0), time(15, 45)),
    ("midday_1", time(15, 45), time(23, 59, 59)),
    ("midday_2", time(0, 0), time(3, 0)),
    ("closing", time(3, 0), time(5, 0)),
]

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_ts(ts_str: str) -> datetime:
    """Parse a timestamp string, tolerating optional fractional seconds."""
    # ClickHouse may return '.000' milliseconds; strip before parsing
    clean = ts_str.split(".")[0] if "." in ts_str else ts_str
    return datetime.strptime(clean, _TS_FMT)


def _time_of(ts_str: str) -> time:
    """Extract just the time component from a timestamp string."""
    return _parse_ts(ts_str).time()


def _in_range(t: time, start: time, end: time) -> bool:
    """Check if *t* falls within [start, end) (supports overnight wrap)."""
    if start <= end:
        return start <= t < end
    # Overnight: start > end  (e.g., 15:45 → 03:00)
    return t >= start or t < end


def _is_night_session(sd: SessionData) -> bool:
    """Heuristic: session string contains 'night' or first bar is after 14:00."""
    if "night" in sd.session.lower():
        return True
    if sd.flow_5m:
        t = _time_of(sd.flow_5m[0].ts)
        return t >= time(14, 0) or t < time(6, 0)
    return False


def _segment_label_range(name: str, segments: list[tuple[str, time, time]]) -> str:
    """Build a HH:MM-HH:MM display string for a segment name."""
    for seg_name, start, end in segments:
        if seg_name == name:
            return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    return ""


def _classify_dominant(ud_ratio: float) -> str:
    if ud_ratio > 1.15:
        return "bull"
    if ud_ratio < 0.85:
        return "bear"
    return "neutral"


def _classify_time_day(t: time) -> str | None:
    """Classify a time into a day-session segment."""
    for seg_name, start, end in _DAY_SEGMENTS:
        if _in_range(t, start, end):
            return seg_name
    return None


def _classify_time_night(t: time) -> str | None:
    """Classify a time into a night-session segment."""
    if _in_range(t, time(15, 0), time(15, 45)):
        return "opening"
    if _in_range(t, time(15, 45), time(3, 0)):
        return "midday"
    if _in_range(t, time(3, 0), time(5, 0)):
        return "closing"
    return None


_DAY_TIME_RANGES: dict[str, str] = {
    "pre_open": "07:00-08:45",
    "opening": "08:45-09:30",
    "midday": "09:30-12:00",
    "closing": "12:00-13:45",
}

_NIGHT_TIME_RANGES: dict[str, str] = {
    "opening": "15:00-15:45",
    "midday": "15:45-03:00",
    "closing": "03:00-05:00",
}


def _build_segment_fact(
    name: str,
    bars: list[FlowBar],
    trades: list[LargeTrade],
    bars5: list[Bar5m],
    session_total: int,
    time_ranges: dict[str, str],
) -> SegmentFact:
    """Compute a single SegmentFact from its constituent data."""
    total_up = sum(b.uptick_vol for b in bars)
    total_dn = sum(b.downtick_vol for b in bars)
    ud = total_up / total_dn if total_dn > 0 else (float("inf") if total_up > 0 else 1.0)

    net_flow = sum(b.net_flow for b in bars)
    volume = sum(b.total_vol for b in bars)
    vol_pct = volume / session_total if session_total > 0 else 0.0

    large_buy = sum(1 for t in trades if t.direction == "buy")
    large_sell = sum(1 for t in trades if t.direction == "sell")

    seg_high = max((b.high for b in bars5), default=0)
    seg_low = min((b.low for b in bars5), default=0)

    return SegmentFact(
        name=name,
        time_range=time_ranges.get(name, ""),
        ud_ratio=ud,
        net_flow=net_flow,
        volume=volume,
        volume_pct=vol_pct,
        large_buy_count=large_buy,
        large_sell_count=large_sell,
        high=seg_high,
        low=seg_low,
        dominant_side=_classify_dominant(ud),
    )


# ---------------------------------------------------------------------------
# 1. extract_time_segments
# ---------------------------------------------------------------------------


def extract_time_segments(sd: SessionData) -> list[SegmentFact]:
    """Classify each FlowBar into time segments and compute per-segment facts."""
    is_night = _is_night_session(sd)

    if is_night:
        segment_names = ["opening", "midday", "closing"]
        classify = _classify_time_night
        time_ranges = _NIGHT_TIME_RANGES
    else:
        segment_names = ["pre_open", "opening", "midday", "closing"]
        classify = _classify_time_day
        time_ranges = _DAY_TIME_RANGES

    # Bucket items into segments
    seg_bars: dict[str, list[FlowBar]] = {name: [] for name in segment_names}
    seg_trades: dict[str, list[LargeTrade]] = {name: [] for name in segment_names}
    seg_bars_5m: dict[str, list[Bar5m]] = {name: [] for name in segment_names}

    for bar in sd.flow_5m:
        seg = classify(_time_of(bar.ts))
        if seg is not None:
            seg_bars[seg].append(bar)

    for trade in sd.large_trades:
        seg = classify(_time_of(trade.ts))
        if seg is not None:
            seg_trades[seg].append(trade)

    for bar5 in sd.bars_5m:
        seg = classify(_time_of(bar5.ts))
        if seg is not None:
            seg_bars_5m[seg].append(bar5)

    session_total = sum(b.total_vol for b in sd.flow_5m)

    return [
        _build_segment_fact(
            name,
            seg_bars[name],
            seg_trades[name],
            seg_bars_5m[name],
            session_total,
            time_ranges,
        )
        for name in segment_names
    ]


# ---------------------------------------------------------------------------
# 2. extract_chip_facts
# ---------------------------------------------------------------------------


def extract_chip_facts(sd: SessionData) -> ChipFacts:
    """Extract chip structure from large trades and volume-at-price analysis."""
    trades = sd.large_trades

    # Use existing cluster function with tolerance=50000
    raw_clusters = find_large_trade_clusters(trades, price_tolerance=50_000)

    # Augment clusters with timestamps and buy/sell breakdown
    clusters: list[ChipCluster] = []
    for center_price, _total_vol in raw_clusters:
        # Find trades belonging to this cluster (within tolerance)
        cluster_trades = [t for t in trades if abs(t.price - center_price) <= 50_000]
        if not cluster_trades:
            continue

        buy_vol = sum(t.volume for t in cluster_trades if t.direction == "buy")
        sell_vol = sum(t.volume for t in cluster_trades if t.direction == "sell")

        prices = [t.price for t in cluster_trades]
        price_lo = min(prices)
        price_hi = max(prices)

        # Parse timestamps for time range
        timestamps = [t.ts for t in cluster_trades]
        first_ts = min(timestamps)
        last_ts = max(timestamps)

        first_t = _parse_ts(first_ts).strftime("%H:%M")
        last_t = _parse_ts(last_ts).strftime("%H:%M")
        time_range = f"{first_t}-{last_t}"

        dominant = "buy" if buy_vol > sell_vol else "sell" if sell_vol > buy_vol else "neutral"

        clusters.append(
            ChipCluster(
                price_center=center_price,
                price_range=(price_lo, price_hi),
                buy_volume=buy_vol,
                sell_volume=sell_vol,
                trade_count=len(cluster_trades),
                dominant_side=dominant,
                first_ts=first_ts,
                last_ts=last_ts,
                time_range=time_range,
            )
        )

    # Total buy/sell from ALL large trades
    total_buy = sum(t.volume for t in trades if t.direction == "buy")
    total_sell = sum(t.volume for t in trades if t.direction == "sell")
    total = total_buy + total_sell
    net_ratio = total_buy / total if total > 0 else 0.5

    # VAP peaks from 5m bars
    vap_peaks = find_volume_at_price(sd.bars_5m)

    # Buy/sell zones from clusters
    buy_zone: tuple[int, int] | None = None
    sell_zone: tuple[int, int] | None = None
    for c in clusters:
        if c.dominant_side == "buy":
            if buy_zone is None:
                buy_zone = c.price_range
            else:
                buy_zone = (
                    min(buy_zone[0], c.price_range[0]),
                    max(buy_zone[1], c.price_range[1]),
                )
        elif c.dominant_side == "sell":
            if sell_zone is None:
                sell_zone = c.price_range
            else:
                sell_zone = (
                    min(sell_zone[0], c.price_range[0]),
                    max(sell_zone[1], c.price_range[1]),
                )

    return ChipFacts(
        clusters=clusters,
        vap_peaks=vap_peaks,
        buy_zone=buy_zone,
        sell_zone=sell_zone,
        total_buy_volume=total_buy,
        total_sell_volume=total_sell,
        net_ratio=net_ratio,
    )


# ---------------------------------------------------------------------------
# 3. extract_flow_facts
# ---------------------------------------------------------------------------

_DUMMY_FLOW_BAR = FlowBar(
    ts="1970-01-01 00:00:00",
    ticks=0,
    total_vol=0,
    uptick_vol=0,
    downtick_vol=0,
    flat_vol=0,
    ud_ratio=1.0,
    net_flow=0,
)


def extract_flow_facts(sd: SessionData) -> FlowFacts:
    """Extract session-level order flow facts."""
    bars = sd.flow_5m

    if not bars:
        return FlowFacts(
            session_ud=1.0,
            session_net_flow=0,
            strongest_buy_bar=_DUMMY_FLOW_BAR,
            strongest_sell_bar=_DUMMY_FLOW_BAR,
            sustained_runs=[],
            volume_spikes=[],
            eod_ud=1.0,
            eod_drift=0.0,
        )

    # Session U/D ratio
    total_up = sum(b.uptick_vol for b in bars)
    total_dn = sum(b.downtick_vol for b in bars)
    session_ud = total_up / total_dn if total_dn > 0 else (float("inf") if total_up > 0 else 1.0)

    session_net_flow = sum(b.net_flow for b in bars)

    # Strongest buy/sell bars
    strongest_buy = max(bars, key=lambda b: b.ud_ratio)
    strongest_sell = min(bars, key=lambda b: b.ud_ratio)

    # Sustained runs: consecutive bars with ud_ratio > 1.3 (bull) or < 0.7 (bear)
    sustained_runs: list[tuple[str, int, str]] = []
    run_side: str | None = None
    run_count = 0
    run_start_ts = ""

    last_run_end_ts = ""

    def _flush_run() -> None:
        nonlocal run_side, run_count, run_start_ts
        if run_side is not None and run_count >= 4:
            start_t = _parse_ts(run_start_ts).strftime("%H:%M")
            end_t = _parse_ts(last_run_end_ts).strftime("%H:%M") if last_run_end_ts else start_t
            sustained_runs.append((run_side, run_count, f"{start_t}-{end_t}"))
        run_side = None
        run_count = 0

    for bar in bars:
        last_run_end_ts = bar.ts
        if bar.ud_ratio > 1.3:
            cur_side = "bull"
        elif bar.ud_ratio < 0.7:
            cur_side = "bear"
        else:
            _flush_run()
            continue

        if cur_side == run_side:
            run_count += 1
        else:
            _flush_run()
            run_side = cur_side
            run_count = 1
            run_start_ts = bar.ts

    # Final flush
    _flush_run()

    # Volume spikes: bars where total_vol > 2.0 * mean_vol
    mean_vol = sum(b.total_vol for b in bars) / len(bars)
    threshold = 2.0 * mean_vol
    volume_spikes: list[tuple[FlowBar, float]] = []
    if mean_vol > 0:
        for bar in bars:
            if bar.total_vol > threshold:
                volume_spikes.append((bar, bar.total_vol / mean_vol))

    # End-of-day U/D
    eod_bars = bars[-6:] if len(bars) >= 6 else bars
    eod_up = sum(b.uptick_vol for b in eod_bars)
    eod_dn = sum(b.downtick_vol for b in eod_bars)
    eod_ud = eod_up / eod_dn if eod_dn > 0 else (float("inf") if eod_up > 0 else 1.0)

    # Handle inf in drift calculation
    if session_ud == float("inf") or eod_ud == float("inf"):
        eod_drift = 0.0
    else:
        eod_drift = eod_ud - session_ud

    return FlowFacts(
        session_ud=session_ud,
        session_net_flow=session_net_flow,
        strongest_buy_bar=strongest_buy,
        strongest_sell_bar=strongest_sell,
        sustained_runs=sustained_runs,
        volume_spikes=volume_spikes,
        eod_ud=eod_ud,
        eod_drift=eod_drift,
    )


# ---------------------------------------------------------------------------
# 4. extract_structure_facts
# ---------------------------------------------------------------------------


def extract_structure_facts(sd: SessionData) -> StructureFacts:
    """Extract price structure facts using existing rule functions."""
    if not sd.bars_5m:
        high_level = PriceLevel(price=sd.high, strength=0.5, reason="session high")
        low_level = PriceLevel(price=sd.low, strength=0.5, reason="session low")
        return StructureFacts(
            double_bottoms=[],
            double_tops=[],
            failed_breakouts=[],
            round_numbers=find_round_numbers(sd.low, sd.high) if sd.high > sd.low else [],
            session_high=high_level,
            session_low=low_level,
        )

    # find_double_bottoms_tops returns a single flat list (bottoms + tops)
    all_levels = find_double_bottoms_tops(sd.bars_5m)
    double_bottoms = [lv for lv in all_levels if "底" in lv.reason]
    double_tops = [lv for lv in all_levels if "頂" in lv.reason]

    failed_breakouts = find_failed_breakouts(sd.bars_5m, sd.large_trades)
    round_numbers = find_round_numbers(sd.low, sd.high)

    # Session extremes returns [high_level, low_level]
    extremes = find_session_extremes(sd)
    session_high = extremes[0]
    session_low = extremes[1]

    return StructureFacts(
        double_bottoms=double_bottoms,
        double_tops=double_tops,
        failed_breakouts=failed_breakouts,
        round_numbers=round_numbers,
        session_high=session_high,
        session_low=session_low,
    )


# ---------------------------------------------------------------------------
# 5. extract_volatility_facts
# ---------------------------------------------------------------------------


def extract_volatility_facts(sd: SessionData) -> VolatilityFacts:
    """Compute Wilder ATR and range metrics from 5m bars."""
    bars = sd.bars_5m

    if not bars:
        return VolatilityFacts(
            atr_5m=0,
            session_range=0,
            range_atr_ratio=0.0,
            atr_session=0,
        )

    # True range calculation
    tr_values: list[int] = []
    for idx in range(1, len(bars)):
        prev_close = bars[idx - 1].close
        curr = bars[idx]
        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev_close),
            abs(curr.low - prev_close),
        )
        tr_values.append(tr)

    if not tr_values:
        # Only one bar — use its range
        single_range = bars[0].high - bars[0].low
        session_range = sd.high - sd.low
        return VolatilityFacts(
            atr_5m=single_range,
            session_range=session_range,
            range_atr_ratio=session_range / single_range if single_range > 0 else 0.0,
            atr_session=single_range,
        )

    # EMA with period = len(bars) as the smoothing factor
    period = len(bars)
    alpha = 2.0 / (period + 1)
    ema = float(tr_values[0])
    for tr in tr_values[1:]:
        ema = alpha * tr + (1.0 - alpha) * ema

    atr_5m = int(round(ema))
    atr_session = int(round(ema * math.sqrt(len(bars))))
    session_range = sd.high - sd.low
    range_atr_ratio = session_range / atr_session if atr_session > 0 else 0.0

    return VolatilityFacts(
        atr_5m=atr_5m,
        session_range=session_range,
        range_atr_ratio=range_atr_ratio,
        atr_session=atr_session,
    )


# ---------------------------------------------------------------------------
# 6. extract_cross_day_facts
# ---------------------------------------------------------------------------


def extract_cross_day_facts(
    sd: SessionData,
    prev_days: list[DaySnapshot],
) -> CrossDayFacts:
    """Compute cross-day comparison facts."""
    if not prev_days:
        return CrossDayFacts(
            prev_days=prev_days,
            volume_change_pct=0.0,
            price_position="inside_range",
            trend_direction="sideways",
            flow_reversal=False,
        )

    prev = prev_days[0]

    # Volume change
    vol_change = (sd.volume - prev.volume) / prev.volume * 100.0 if prev.volume > 0 else 0.0

    # Price position relative to previous day range
    if sd.close > prev.high:
        price_position = "above_prev_high"
    elif sd.close < prev.low:
        price_position = "below_prev_low"
    else:
        price_position = "inside_range"

    # Trend direction from prev_days closes
    if len(prev_days) >= 2:
        closes = [d.close for d in prev_days]
        # prev_days[0] is most recent previous day
        if all(closes[i] >= closes[i + 1] for i in range(len(closes) - 1)):
            trend_direction = "up"
        elif all(closes[i] <= closes[i + 1] for i in range(len(closes) - 1)):
            trend_direction = "down"
        else:
            trend_direction = "sideways"
    else:
        trend_direction = "sideways"

    # Flow reversal: today bearish + prev bullish, or vice versa
    # Compute today's session ud
    total_up = sum(b.uptick_vol for b in sd.flow_5m)
    total_dn = sum(b.downtick_vol for b in sd.flow_5m)
    today_ud = total_up / total_dn if total_dn > 0 else 1.0

    flow_reversal = (today_ud < 0.95 and prev.ud_ratio > 1.05) or (today_ud > 1.05 and prev.ud_ratio < 0.95)

    return CrossDayFacts(
        prev_days=prev_days,
        volume_change_pct=vol_change,
        price_position=price_position,
        trend_direction=trend_direction,
        flow_reversal=flow_reversal,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def extract_all(
    sd: SessionData,
    *,
    prev_days: list[DaySnapshot] | None = None,
) -> FactReport:
    """Run all six extractors and return a complete FactReport."""
    if prev_days is None:
        prev_days = []

    log.info(
        "fact_extraction_start",
        symbol=sd.symbol,
        date=sd.date,
        session=sd.session,
        bar_count=len(sd.bars_5m),
        flow_count=len(sd.flow_5m),
        trade_count=len(sd.large_trades),
    )

    segments = extract_time_segments(sd)
    chips = extract_chip_facts(sd)
    flow = extract_flow_facts(sd)
    structure = extract_structure_facts(sd)
    volatility = extract_volatility_facts(sd)
    cross_day = extract_cross_day_facts(sd, prev_days)

    log.info(
        "fact_extraction_done",
        symbol=sd.symbol,
        segments=len(segments),
        clusters=len(chips.clusters),
    )

    return FactReport(
        session_data=sd,
        segments=segments,
        chips=chips,
        flow=flow,
        structure=structure,
        volatility=volatility,
        cross_day=cross_day,
    )
