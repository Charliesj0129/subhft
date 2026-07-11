"""Read-only structural diagnostics for ``cd_free_qt_cx_taiwan_v0``.

The module deliberately separates causal event formation from execution and
PnL.  It is research-only and never imports runtime strategy, order, broker,
or risk modules.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Mapping, Sequence

NS_PER_MINUTE = 60_000_000_000
PRICE_SCALE = 1_000_000.0

ELECTRONIC_SYMBOLS = (
    "2301",
    "2303",
    "2308",
    "2317",
    "2327",
    "2330",
    "2345",
    "2354",
    "2357",
    "2379",
    "2382",
    "2395",
    "2408",
    "2409",
    "2412",
    "2454",
    "2474",
    "3008",
    "3034",
    "3045",
    "3711",
    "4904",
    "4938",
)
FINANCIAL_SYMBOLS = (
    "2801",
    "2881",
    "2882",
    "2883",
    "2884",
    "2885",
    "2886",
    "2887",
    "2890",
    "2891",
    "2892",
    "5880",
)
TXF_CONTRACTS = ("TXFB6", "TXFC6", "TXFD6", "TXFE6", "TXFF6")


@dataclass(frozen=True)
class OhlcBar:
    symbol: str
    trade_date: str
    end_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    valid: bool = True
    valid_count: int = 1
    missing_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class CycleSnapshot:
    symbol: str
    layer: str
    cycle_key: str
    start_ns: int
    end_ns: int
    open: float
    high: float
    low: float
    close: float
    high_ts_ns: int
    low_ts_ns: int


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str
    direction: int
    layer: str
    ts_ns: int
    cycle_key: str
    leg: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    predecessor_ids: tuple[str, ...] = ()

    @classmethod
    def make(
        cls,
        kind: str,
        direction: int,
        layer: str,
        ts_ns: int,
        cycle_key: str,
        leg: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        predecessors: Sequence[Event] = (),
    ) -> Event:
        predecessor_ids = tuple(event.event_id for event in predecessors)
        stable_metadata = json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))
        raw = "|".join(
            [
                kind,
                str(direction),
                layer,
                str(ts_ns),
                cycle_key,
                leg,
                stable_metadata,
                *predecessor_ids,
            ]
        )
        return cls(
            event_id=sha256(raw.encode("utf-8")).hexdigest()[:24],
            kind=kind,
            direction=direction,
            layer=layer,
            ts_ns=ts_ns,
            cycle_key=cycle_key,
            leg=leg,
            metadata=dict(metadata or {}),
            predecessor_ids=predecessor_ids,
        )


@dataclass(frozen=True)
class L2Bar:
    end_ns: int
    spread_mean: float | None
    gap_p95: float | None
    depth_mean: float | None
    signed_aggressiveness: float | None


@dataclass
class FvgState:
    direction: int
    zone_low: float
    zone_high: float
    created_ts_ns: int
    cycle_key: str
    tapped: bool = False
    filled: bool = False

    def apply(self, bar: OhlcBar) -> tuple[Event, ...]:
        if bar.end_ns <= self.created_ts_ns or self.filled:
            return ()
        events: list[Event] = []
        if self.direction > 0:
            touched = bar.low <= self.zone_high
            filled = bar.low <= self.zone_low
        else:
            touched = bar.high >= self.zone_low
            filled = bar.high >= self.zone_high
        if touched and not self.tapped:
            self.tapped = True
            events.append(
                Event.make(
                    "fvg_tap",
                    self.direction,
                    "90m",
                    bar.end_ns,
                    self.cycle_key,
                    "TXF",
                    metadata={"zone_low": self.zone_low, "zone_high": self.zone_high},
                )
            )
        if filled and not self.filled:
            self.filled = True
            events.append(
                Event.make(
                    "fvg_fill",
                    self.direction,
                    "90m",
                    bar.end_ns,
                    self.cycle_key,
                    "TXF",
                    metadata={"zone_low": self.zone_low, "zone_high": self.zone_high},
                )
            )
        return tuple(events)


@dataclass(frozen=True)
class CisdArm:
    direction: int
    threshold: float
    extreme: float
    armed_ts_ns: int
    cycle_key: str

    @classmethod
    def from_extreme(
        cls,
        bars: Sequence[OhlcBar],
        *,
        direction: int,
        cycle_key_value: str,
        max_lookback: int = 20,
    ) -> CisdArm | None:
        if not bars or direction not in {-1, 1}:
            return None
        current = bars[-1]
        lower = max(0, len(bars) - 1 - max_lookback)
        for older_idx in range(len(bars) - 2, lower - 1, -1):
            older = bars[older_idx]
            newer = bars[older_idx + 1]
            if direction < 0:
                if older.high > current.high:
                    break
                transition = newer.close > newer.open and older.close <= older.open
                if transition:
                    bullish_opens = [
                        bar.open
                        for bar in bars[older_idx + 1 :]
                        if bar.close > bar.open
                    ]
                    if bullish_opens:
                        return cls(
                            direction=-1,
                            threshold=min(bullish_opens),
                            extreme=current.high,
                            armed_ts_ns=current.end_ns,
                            cycle_key=cycle_key_value,
                        )
            else:
                if older.low < current.low:
                    break
                transition = newer.close < newer.open and older.close >= older.open
                if transition:
                    bearish_opens = [
                        bar.open
                        for bar in bars[older_idx + 1 :]
                        if bar.close < bar.open
                    ]
                    if bearish_opens:
                        return cls(
                            direction=1,
                            threshold=max(bearish_opens),
                            extreme=current.low,
                            armed_ts_ns=current.end_ns,
                            cycle_key=cycle_key_value,
                        )
        return None


def build_equal_weight_basket(
    rows_by_symbol: Mapping[str, Sequence[OhlcBar]],
    *,
    symbols: Sequence[str],
    min_valid: int,
    name: str,
) -> list[OhlcBar]:
    by_timestamp: dict[int, dict[str, OhlcBar]] = {}
    bases: dict[tuple[str, str], float] = {}
    for symbol in symbols:
        for bar in sorted(rows_by_symbol.get(symbol, ()), key=lambda item: item.end_ns):
            key = (symbol, bar.trade_date)
            if key not in bases and bar.open > 0:
                bases[key] = bar.open
            by_timestamp.setdefault(bar.end_ns, {})[symbol] = bar

    result: list[OhlcBar] = []
    for end_ns in sorted(by_timestamp):
        present = by_timestamp[end_ns]
        normalized: list[tuple[float, float, float, float, float]] = []
        trade_date = next(iter(present.values())).trade_date
        for symbol in symbols:
            bar = present.get(symbol)
            base = bases.get((symbol, trade_date))
            if bar is None or base is None or base <= 0:
                continue
            normalized.append(
                (
                    bar.open / base,
                    bar.high / base,
                    bar.low / base,
                    bar.close / base,
                    bar.volume,
                )
            )
        valid_count = len(normalized)
        missing = tuple(symbol for symbol in symbols if symbol not in present)
        if normalized:
            open_, high, low, close, volume = (
                fmean(values) for values in zip(*normalized, strict=True)
            )
        else:
            open_ = high = low = close = volume = float("nan")
        result.append(
            OhlcBar(
                symbol=name,
                trade_date=trade_date,
                end_ns=end_ns,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                valid=valid_count >= min_valid,
                valid_count=valid_count,
                missing_symbols=missing,
            )
        )
    return result


def cycle_key(
    end_ns: int,
    *,
    duration_minutes: int,
    session_open_hour_utc: int = 1,
) -> str:
    end = datetime.fromtimestamp(end_ns / 1_000_000_000, tz=timezone.utc)
    session_open = end.replace(
        hour=session_open_hour_utc,
        minute=0,
        second=0,
        microsecond=0,
    )
    elapsed_ns = int((end - session_open).total_seconds() * 1_000_000_000) - 1
    index = max(0, elapsed_ns // (duration_minutes * NS_PER_MINUTE))
    return f"{end.date().isoformat()}:{duration_minutes}m:{index}"


def detect_sweep(
    current: CycleSnapshot,
    previous: CycleSnapshot,
    *,
    confirmed_ts_ns: int,
) -> list[Event]:
    events: list[Event] = []
    if current.high > previous.high and current.close < previous.high:
        events.append(
            Event.make(
                "sweep", -1, current.layer, confirmed_ts_ns, current.cycle_key, current.symbol
            )
        )
    if current.low < previous.low and current.close > previous.low:
        events.append(
            Event.make(
                "sweep", 1, current.layer, confirmed_ts_ns, current.cycle_key, current.symbol
            )
        )
    return events


def _opposite_progression(primary: float, primary_prev: float, other: float, other_prev: float) -> bool:
    if primary == primary_prev and other == other_prev:
        return False
    return (primary >= primary_prev and other <= other_prev) or (
        primary <= primary_prev and other >= other_prev
    )


def detect_smt(
    current_by_leg: Mapping[str, CycleSnapshot],
    previous_by_leg: Mapping[str, CycleSnapshot],
    *,
    layer: str,
    confirmed_ts_ns: int,
    cycle_key_value: str,
) -> list[Event]:
    txf = current_by_leg.get("TXF")
    txf_prev = previous_by_leg.get("TXF")
    if txf is None or txf_prev is None:
        return []
    events: list[Event] = []
    for leg in ("electronic", "financial"):
        other = current_by_leg.get(leg)
        other_prev = previous_by_leg.get(leg)
        if other is None or other_prev is None:
            continue
        if _opposite_progression(txf.high, txf_prev.high, other.high, other_prev.high):
            events.append(
                Event.make(
                    "smt",
                    -1,
                    layer,
                    confirmed_ts_ns,
                    cycle_key_value,
                    "triad",
                    metadata={"correlated_leg": leg},
                )
            )
        if _opposite_progression(txf.low, txf_prev.low, other.low, other_prev.low):
            events.append(
                Event.make(
                    "smt",
                    1,
                    layer,
                    confirmed_ts_ns,
                    cycle_key_value,
                    "triad",
                    metadata={"correlated_leg": leg},
                )
            )
    return events


def detect_fvg(
    completed_cycles: Sequence[CycleSnapshot],
    *,
    created_ts_ns: int,
) -> FvgState | None:
    if len(completed_cycles) < 3:
        return None
    oldest, middle, newest = completed_cycles[-3:]
    if newest.low > oldest.high and middle.close > oldest.high and middle.close > middle.open:
        return FvgState(
            direction=1,
            zone_low=oldest.high,
            zone_high=newest.low,
            created_ts_ns=created_ts_ns,
            cycle_key=newest.cycle_key,
        )
    if oldest.low > newest.high and middle.close < oldest.low and middle.close < middle.open:
        return FvgState(
            direction=-1,
            zone_low=newest.high,
            zone_high=oldest.low,
            created_ts_ns=created_ts_ns,
            cycle_key=newest.cycle_key,
        )
    return None


def advance_cisd(arm: CisdArm, bar: OhlcBar) -> tuple[Event, ...]:
    if bar.end_ns <= arm.armed_ts_ns:
        return ()
    if arm.direction < 0:
        invalid = bar.high > arm.extreme
        confirmed = bar.close < arm.threshold
    else:
        invalid = bar.low < arm.extreme
        confirmed = bar.close > arm.threshold
    if invalid or not confirmed:
        return ()
    return (
        Event.make(
            "cisd",
            arm.direction,
            "90m",
            bar.end_ns,
            arm.cycle_key,
            "TXF",
            metadata={"threshold": arm.threshold, "extreme": arm.extreme},
        ),
    )


def compose_channels(events: Sequence[Event]) -> list[Event]:
    output: list[Event] = []
    groups: dict[tuple[str, int], list[Event]] = {}
    for event in sorted(events, key=lambda item: (item.ts_ns, item.event_id)):
        groups.setdefault((event.cycle_key, event.direction), []).append(event)

    for (cycle, direction), group in sorted(groups.items()):
        for cisd in (event for event in group if event.kind == "cisd" and event.leg == "TXF"):
            prior = [event for event in group if event.ts_ns <= cisd.ts_ns and event is not cisd]
            txf_sweeps = [event for event in prior if event.kind == "sweep" and event.leg == "TXF"]
            any_sweeps = [event for event in prior if event.kind == "sweep"]
            ssmts = [event for event in prior if event.kind in {"smt", "ssmt"}]
            qualifiers = [
                event
                for event in prior
                if event.kind in {"bsl_ssl_smt", "fvg_tap", "smt", "ssmt"}
            ]
            if txf_sweeps:
                sweep = txf_sweeps[0]
                output.append(
                    Event.make(
                        "baseline_sweep_cisd",
                        direction,
                        "90m",
                        cisd.ts_ns,
                        cycle,
                        "TXF",
                        predecessors=(sweep, cisd),
                    )
                )
            correlated_pairs = [
                (sweep, smt)
                for sweep in any_sweeps
                for smt in ssmts
                if sweep.ts_ns <= smt.ts_ns <= cisd.ts_ns
            ]
            if correlated_pairs:
                sweep, smt = correlated_pairs[0]
                output.append(
                    Event.make(
                        "correlated_channel",
                        direction,
                        "90m",
                        cisd.ts_ns,
                        cycle,
                        "triad",
                        predecessors=(sweep, smt, cisd),
                    )
                )
            main_pairs = [
                (sweep, qualifier)
                for sweep in txf_sweeps
                for qualifier in qualifiers
                if sweep.ts_ns <= qualifier.ts_ns <= cisd.ts_ns
            ]
            if main_pairs:
                sweep, qualifier = main_pairs[0]
                output.append(
                    Event.make(
                        "main_pair_channel",
                        direction,
                        "90m",
                        cisd.ts_ns,
                        cycle,
                        "TXF",
                        predecessors=(sweep, qualifier, cisd),
                    )
                )
    return sorted(output, key=lambda item: (item.ts_ns, item.kind, item.event_id))


def _correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys, strict=True) if isfinite(x) and isfinite(y)]
    if len(pairs) < 2:
        return None
    x_values, y_values = zip(*pairs, strict=True)
    x_mean = fmean(x_values)
    y_mean = fmean(y_values)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    x_var = sum((x - x_mean) ** 2 for x in x_values)
    y_var = sum((y - y_mean) ** 2 for y in y_values)
    if x_var <= 0 or y_var <= 0:
        return None
    return numerator / (x_var * y_var) ** 0.5


def prior_date_lead_lag(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_pairs: int = 30,
) -> list[dict[str, Any]]:
    dates = sorted({str(row["date"]) for row in rows})
    result: list[dict[str, Any]] = []
    for current_date in dates:
        prior = [row for row in rows if str(row["date"]) < current_date]
        relations: dict[str, tuple[list[float], list[float]]] = {
            "electronic_to_txf": ([], []),
            "financial_to_txf": ([], []),
            "txf_to_electronic": ([], []),
            "txf_to_financial": ([], []),
        }
        prior_dates = sorted({str(row["date"]) for row in prior})
        for prior_date in prior_dates:
            day = sorted(
                (row for row in prior if str(row["date"]) == prior_date),
                key=lambda item: int(item["index"]),
            )
            for left, right in zip(day, day[1:], strict=False):
                relations["electronic_to_txf"][0].append(float(left["electronic_return"]))
                relations["electronic_to_txf"][1].append(float(right["txf_return"]))
                relations["financial_to_txf"][0].append(float(left["financial_return"]))
                relations["financial_to_txf"][1].append(float(right["txf_return"]))
                relations["txf_to_electronic"][0].append(float(left["txf_return"]))
                relations["txf_to_electronic"][1].append(float(right["electronic_return"]))
                relations["txf_to_financial"][0].append(float(left["txf_return"]))
                relations["txf_to_financial"][1].append(float(right["financial_return"]))
        correlations = {
            name: _correlation(xs, ys) if len(xs) >= min_pairs else None
            for name, (xs, ys) in relations.items()
        }
        leader = None
        best_abs = -1.0
        for relation in (
            "electronic_to_txf",
            "financial_to_txf",
            "txf_to_electronic",
            "txf_to_financial",
        ):
            value = correlations[relation]
            if value is not None and abs(value) > best_abs:
                best_abs = abs(value)
                leader = relation.split("_to_", maxsplit=1)[0]
        result.append(
            {
                "date": current_date,
                "training_dates": len(prior_dates),
                "pairs": len(relations["electronic_to_txf"][0]),
                "leader": leader,
                "correlations": correlations,
                "policy": "strict_prior_dates_only",
            }
        )
    return result


def attach_l2_attribution(
    events: Sequence[Event],
    l2_by_end_ns: Mapping[int, L2Bar],
) -> list[dict[str, Any]]:
    timestamps = sorted(l2_by_end_ns)
    result: list[dict[str, Any]] = []
    for event in events:
        same = l2_by_end_ns.get(event.ts_ns)
        next_ts = next((ts for ts in timestamps if ts > event.ts_ns), None)
        following = l2_by_end_ns.get(next_ts) if next_ts is not None else None
        outcome: dict[str, Any] | None = None
        if same is not None and following is not None:
            depth_ratio = None
            if same.depth_mean not in {None, 0.0} and following.depth_mean is not None:
                depth_ratio = following.depth_mean / same.depth_mean
            spread_ratio = None
            if same.spread_mean not in {None, 0.0} and following.spread_mean is not None:
                spread_ratio = following.spread_mean / same.spread_mean
            outcome = {
                "end_ns": following.end_ns,
                "depth_refill_ratio": depth_ratio,
                "spread_resiliency_ratio": spread_ratio,
                "usable_as_signal": False,
            }
        result.append(
            {
                "event": asdict(event),
                "same_bar": asdict(same) if same is not None else None,
                "next_bar_outcome_only": outcome,
            }
        )
    return result


def front_contract_for_date(trade_date: str) -> str:
    """Return the preregistered B6-F6 contract for one date.

    The windows are shared with the approved expanded-chain design. Missing
    observations remain missing; this function never selects a fallback.
    """

    if trade_date <= "2026-02-18":
        return "TXFB6"
    if trade_date <= "2026-03-18":
        return "TXFC6"
    if trade_date <= "2026-04-15":
        return "TXFD6"
    if trade_date <= "2026-05-20":
        return "TXFE6"
    return "TXFF6"


def coverage_summary(
    txf_bars: Sequence[OhlcBar],
    electronic_bars: Sequence[OhlcBar],
    financial_bars: Sequence[OhlcBar],
    *,
    expected_slots_per_day: int = 54,
) -> dict[str, Any]:
    txf = {bar.end_ns: bar for bar in txf_bars}
    electronic = {bar.end_ns: bar for bar in electronic_bars}
    financial = {bar.end_ns: bar for bar in financial_bars}
    dates = sorted(
        {bar.trade_date for bar in txf_bars}
        | {bar.trade_date for bar in electronic_bars}
        | {bar.trade_date for bar in financial_bars}
    )
    per_date: dict[str, dict[str, Any]] = {}
    for trade_date in dates:
        timestamps = sorted(
            {bar.end_ns for bar in txf_bars if bar.trade_date == trade_date}
            | {bar.end_ns for bar in electronic_bars if bar.trade_date == trade_date}
            | {bar.end_ns for bar in financial_bars if bar.trade_date == trade_date}
        )
        txf_valid = sum(ts in txf and txf[ts].valid for ts in timestamps)
        electronic_valid = sum(
            ts in electronic and electronic[ts].valid for ts in timestamps
        )
        financial_valid = sum(ts in financial and financial[ts].valid for ts in timestamps)
        triad_valid = sum(
            ts in txf
            and txf[ts].valid
            and ts in electronic
            and electronic[ts].valid
            and ts in financial
            and financial[ts].valid
            for ts in timestamps
        )
        per_date[trade_date] = {
            "expected_bars": expected_slots_per_day,
            "observed_timestamps": len(timestamps),
            "txf_valid_bars": txf_valid,
            "electronic_valid_bars": electronic_valid,
            "financial_valid_bars": financial_valid,
            "triad_valid_bars": triad_valid,
            "triad_valid_fraction": triad_valid / expected_slots_per_day,
            "date_valid": triad_valid == expected_slots_per_day,
        }
    return {
        "expected_slots_per_day": expected_slots_per_day,
        "date_count": len(dates),
        "fully_valid_date_count": sum(row["date_valid"] for row in per_date.values()),
        "dates": per_date,
    }


@dataclass
class _CycleTracker:
    symbol: str
    duration_minutes: int
    current: CycleSnapshot | None = None
    completed: list[CycleSnapshot] = field(default_factory=list)

    @property
    def layer(self) -> str:
        return f"{self.duration_minutes}m"

    def update(self, bar: OhlcBar) -> tuple[bool, CycleSnapshot | None]:
        key = cycle_key(bar.end_ns, duration_minutes=self.duration_minutes)
        before = self.current
        if self.current is None or self.current.cycle_key != key:
            if self.current is not None:
                self.completed.append(self.current)
            self.current = CycleSnapshot(
                symbol=self.symbol,
                layer=self.layer,
                cycle_key=key,
                start_ns=bar.end_ns - 5 * NS_PER_MINUTE,
                end_ns=bar.end_ns,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                high_ts_ns=bar.end_ns,
                low_ts_ns=bar.end_ns,
            )
            return True, before

        current = self.current
        high = max(current.high, bar.high)
        low = min(current.low, bar.low)
        self.current = CycleSnapshot(
            symbol=self.symbol,
            layer=self.layer,
            cycle_key=key,
            start_ns=current.start_ns,
            end_ns=bar.end_ns,
            open=current.open,
            high=high,
            low=low,
            close=bar.close,
            high_ts_ns=bar.end_ns if bar.high >= current.high else current.high_ts_ns,
            low_ts_ns=bar.end_ns if bar.low <= current.low else current.low_ts_ns,
        )
        return False, before


@dataclass
class _LiquidityStructure:
    kind: str
    direction: int
    created_ts_ns: int
    cycle_key: str
    levels: dict[str, float]
    tapped: set[str] = field(default_factory=set)
    divergence_emitted: bool = False


def _liquidity_level(
    completed: Sequence[CycleSnapshot],
    *,
    kind: str,
) -> float | None:
    if len(completed) < 3:
        return None
    oldest, middle, newest = completed[-3:]
    if kind == "bsl" and middle.high >= oldest.high and middle.high > newest.high:
        return middle.high
    if kind == "ssl" and middle.low <= oldest.low and middle.low < newest.low:
        return middle.low
    return None


def _replace_event(
    event: Event,
    *,
    kind: str | None = None,
    layer: str | None = None,
    cycle_key_value: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Event:
    combined_metadata = dict(event.metadata)
    combined_metadata.update(metadata or {})
    return Event.make(
        kind or event.kind,
        event.direction,
        layer or event.layer,
        event.ts_ns,
        cycle_key_value or event.cycle_key,
        event.leg,
        metadata=combined_metadata,
    )


def run_event_engine(  # noqa: C901
    txf_bars: Sequence[OhlcBar],
    electronic_bars: Sequence[OhlcBar],
    financial_bars: Sequence[OhlcBar],
) -> list[Event]:
    """Generate causal, append-only structural events from aligned bars."""

    txf_by_ts = {bar.end_ns: bar for bar in txf_bars if bar.valid}
    electronic_by_ts = {bar.end_ns: bar for bar in electronic_bars if bar.valid}
    financial_by_ts = {bar.end_ns: bar for bar in financial_bars if bar.valid}
    bars_by_leg = {
        "TXF": txf_by_ts,
        "electronic": electronic_by_ts,
        "financial": financial_by_ts,
    }
    trackers = {
        (leg, duration): _CycleTracker(leg, duration)
        for leg in bars_by_leg
        for duration in (10, 30, 90, 270)
    }
    events: list[Event] = []
    event_ids: set[str] = set()
    suppression: set[tuple[Any, ...]] = set()
    fvg_states: list[FvgState] = []
    liquidity_structures: list[_LiquidityStructure] = []
    txf_history: list[OhlcBar] = []
    cisd_arms: dict[int, CisdArm] = {}

    def emit(event: Event, *, key: tuple[Any, ...] | None = None) -> None:
        if key is not None and key in suppression:
            return
        if event.event_id in event_ids:
            return
        if key is not None:
            suppression.add(key)
        event_ids.add(event.event_id)
        events.append(event)

    for ts_ns in sorted(txf_by_ts):
        txf_bar = txf_by_ts[ts_ns]
        current_bars = {
            leg: rows[ts_ns]
            for leg, rows in bars_by_leg.items()
            if ts_ns in rows
        }
        active_cycle = cycle_key(ts_ns, duration_minutes=90)
        update_info: dict[tuple[str, int], tuple[bool, CycleSnapshot | None]] = {}
        for leg, bar in current_bars.items():
            for duration in (10, 30, 90, 270):
                update_info[(leg, duration)] = trackers[(leg, duration)].update(bar)

        triad_valid = len(current_bars) == 3
        for (leg, _duration), tracker in trackers.items():
            if leg not in current_bars or tracker.current is None or not tracker.completed:
                continue
            for event in detect_sweep(
                tracker.current,
                tracker.completed[-1],
                confirmed_ts_ns=ts_ns,
            ):
                emit(
                    event,
                    key=(event.kind, event.direction, event.layer, event.cycle_key, event.leg),
                )

        if triad_valid:
            for duration in (30, 90, 270):
                current = {
                    leg: trackers[(leg, duration)].current
                    for leg in bars_by_leg
                    if trackers[(leg, duration)].current is not None
                }
                previous = {
                    leg: trackers[(leg, duration)].completed[-1]
                    for leg in bars_by_leg
                    if trackers[(leg, duration)].completed
                }
                if len(current) != 3 or len(previous) != 3:
                    continue
                divergences = detect_smt(
                    current,
                    previous,
                    layer=f"{duration}m",
                    confirmed_ts_ns=ts_ns,
                    cycle_key_value=active_cycle,
                )
                for event in divergences:
                    if duration == 90:
                        emit(
                            event,
                            key=(
                                "smt",
                                event.direction,
                                event.layer,
                                active_cycle,
                                event.metadata.get("correlated_leg"),
                            ),
                        )
                    ssmt = _replace_event(
                        event,
                        kind="ssmt",
                        cycle_key_value=active_cycle,
                        metadata={"source_layer": f"{duration}m"},
                    )
                    emit(
                        ssmt,
                        key=(
                            "ssmt",
                            ssmt.direction,
                            ssmt.layer,
                            active_cycle,
                            ssmt.metadata.get("correlated_leg"),
                        ),
                    )

        active_tracker = trackers[("TXF", 90)]
        new_active, before_active = update_info[("TXF", 90)]
        if new_active:
            fvg_states = [
                state
                for state in fvg_states
                if state.cycle_key.split(":", maxsplit=1)[0] == txf_bar.trade_date
            ]
            liquidity_structures = [
                state
                for state in liquidity_structures
                if state.cycle_key.split(":", maxsplit=1)[0] == txf_bar.trade_date
            ]
            if len(active_tracker.completed) >= 3:
                state = detect_fvg(active_tracker.completed, created_ts_ns=ts_ns)
                if state is not None:
                    state.cycle_key = active_cycle
                    fvg_states.append(state)
                    emit(
                        Event.make(
                            "fvg_create",
                            state.direction,
                            "90m",
                            ts_ns,
                            active_cycle,
                            "TXF",
                            metadata={"zone_low": state.zone_low, "zone_high": state.zone_high},
                        )
                    )
            for kind, direction in (("bsl", -1), ("ssl", 1)):
                levels = {
                    leg: level
                    for leg in bars_by_leg
                    if (level := _liquidity_level(trackers[(leg, 90)].completed, kind=kind))
                    is not None
                }
                if len(levels) >= 2:
                    liquidity_structures.append(
                        _LiquidityStructure(kind, direction, ts_ns, active_cycle, levels)
                    )
                    for leg, level in levels.items():
                        emit(
                            Event.make(
                                f"{kind}_create",
                                direction,
                                "90m",
                                ts_ns,
                                active_cycle,
                                leg,
                                metadata={"level": level, "eligible_legs": sorted(levels)},
                            )
                        )
            cisd_arms.clear()

        for state in fvg_states:
            for event in state.apply(txf_bar):
                emit(_replace_event(event, cycle_key_value=active_cycle))

        if triad_valid:
            for structure in liquidity_structures:
                if structure.cycle_key != active_cycle:
                    continue
                for leg, level in structure.levels.items():
                    bar = current_bars[leg]
                    touched = bar.high >= level if structure.kind == "bsl" else bar.low <= level
                    if touched:
                        structure.tapped.add(leg)
                if (
                    structure.tapped
                    and len(structure.tapped) < len(structure.levels)
                    and not structure.divergence_emitted
                ):
                    structure.divergence_emitted = True
                    emit(
                        Event.make(
                            "bsl_ssl_smt",
                            structure.direction,
                            "90m",
                            ts_ns,
                            active_cycle,
                            "triad",
                            metadata={
                                "structure": structure.kind,
                                "tapped_legs": sorted(structure.tapped),
                                "untapped_legs": sorted(set(structure.levels) - structure.tapped),
                            },
                        )
                    )

        txf_history.append(txf_bar)
        for direction, arm in list(cisd_arms.items()):
            if arm.cycle_key != active_cycle:
                del cisd_arms[direction]
                continue
            invalid = (direction < 0 and txf_bar.high > arm.extreme) or (
                direction > 0 and txf_bar.low < arm.extreme
            )
            if invalid:
                del cisd_arms[direction]
                continue
            confirmed = advance_cisd(arm, txf_bar)
            for event in confirmed:
                emit(event, key=("cisd", direction, active_cycle))
                del cisd_arms[direction]

        if active_tracker.current is not None and active_tracker.completed:
            previous = active_tracker.completed[-1]
            new_high = before_active is None or txf_bar.high > before_active.high
            new_low = before_active is None or txf_bar.low < before_active.low
            if new_high and active_tracker.current.high >= previous.high:
                arm = CisdArm.from_extreme(
                    txf_history,
                    direction=-1,
                    cycle_key_value=active_cycle,
                )
                if arm is not None:
                    cisd_arms[-1] = arm
            if new_low and active_tracker.current.low <= previous.low:
                arm = CisdArm.from_extreme(
                    txf_history,
                    direction=1,
                    cycle_key_value=active_cycle,
                )
                if arm is not None:
                    cisd_arms[1] = arm

    channels = compose_channels(events)
    all_events = events + [event for event in channels if event.event_id not in event_ids]
    return sorted(all_events, key=lambda item: (item.ts_ns, item.kind, item.event_id))


def _aligned_return_rows(
    txf_bars: Sequence[OhlcBar],
    electronic_bars: Sequence[OhlcBar],
    financial_bars: Sequence[OhlcBar],
) -> list[dict[str, Any]]:
    txf = {bar.end_ns: bar for bar in txf_bars if bar.valid}
    electronic = {bar.end_ns: bar for bar in electronic_bars if bar.valid}
    financial = {bar.end_ns: bar for bar in financial_bars if bar.valid}
    timestamps = sorted(set(txf) & set(electronic) & set(financial))
    result: list[dict[str, Any]] = []
    previous: dict[str, tuple[float, float, float]] = {}
    index_by_date: dict[str, int] = {}
    for ts_ns in timestamps:
        txf_bar = txf[ts_ns]
        electronic_bar = electronic[ts_ns]
        financial_bar = financial[ts_ns]
        trade_date = txf_bar.trade_date
        current = (txf_bar.close, electronic_bar.close, financial_bar.close)
        prior = previous.get(trade_date)
        previous[trade_date] = current
        if prior is None or any(value <= 0 for value in prior):
            continue
        index = index_by_date.get(trade_date, 0)
        index_by_date[trade_date] = index + 1
        result.append(
            {
                "date": trade_date,
                "index": index,
                "end_ns": ts_ns,
                "txf_return": current[0] / prior[0] - 1.0,
                "electronic_return": current[1] / prior[1] - 1.0,
                "financial_return": current[2] / prior[2] - 1.0,
            }
        )
    return result


def _mean(values: Iterable[float | None]) -> float | None:
    finite = [float(value) for value in values if value is not None and isfinite(float(value))]
    return fmean(finite) if finite else None


def baseline_comparison_summary(
    events: Sequence[Event],
    lead_lag: Sequence[Mapping[str, Any]],
    *,
    aligned_return_rows: int,
) -> dict[str, Any]:
    kinds = ("baseline_sweep_cisd", "correlated_channel", "main_pair_channel")
    grouped = {kind: [event for event in events if event.kind == kind] for kind in kinds}
    keys = {
        kind: {(event.cycle_key, event.direction, event.ts_ns) for event in grouped[kind]}
        for kind in kinds
    }
    baseline_count = len(grouped["baseline_sweep_cisd"])

    def item(kind: str) -> dict[str, Any]:
        count = len(grouped[kind])
        return {
            "events": count,
            "unique_dates": len({event.cycle_key[:10] for event in grouped[kind]}),
            "selectivity_vs_sweep_cisd": count / baseline_count if baseline_count else None,
        }

    return {
        "sweep_cisd": item("baseline_sweep_cisd"),
        "correlated_channel": item("correlated_channel"),
        "main_pair_channel": item("main_pair_channel"),
        "correlated_overlap_with_sweep_cisd": len(
            keys["correlated_channel"] & keys["baseline_sweep_cisd"]
        ),
        "main_pair_overlap_with_sweep_cisd": len(
            keys["main_pair_channel"] & keys["baseline_sweep_cisd"]
        ),
        "lead_lag": {
            "aligned_return_rows": aligned_return_rows,
            "dates_with_prior_estimate": sum(row.get("leader") is not None for row in lead_lag),
        },
        "comparison_scope": "structural counts and overlap only; no execution or PnL",
    }


def build_diagnostic_payload(
    txf_bars: Sequence[OhlcBar],
    electronic_bars: Sequence[OhlcBar],
    financial_bars: Sequence[OhlcBar],
    *,
    l2_by_end_ns: Mapping[int, L2Bar],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    events = run_event_engine(txf_bars, electronic_bars, financial_bars)
    coverage = coverage_summary(txf_bars, electronic_bars, financial_bars)
    return_rows = _aligned_return_rows(txf_bars, electronic_bars, financial_bars)
    lead_lag = prior_date_lead_lag(return_rows)
    attributed = attach_l2_attribution(events, l2_by_end_ns)

    event_counts: dict[str, int] = {}
    direction_counts: dict[str, dict[str, int]] = {}
    layer_counts: dict[str, dict[str, int]] = {}
    for event in events:
        event_counts[event.kind] = event_counts.get(event.kind, 0) + 1
        direction = "bullish" if event.direction > 0 else "bearish"
        direction_counts.setdefault(event.kind, {}).setdefault(direction, 0)
        direction_counts[event.kind][direction] += 1
        layer_counts.setdefault(event.kind, {}).setdefault(event.layer, 0)
        layer_counts[event.kind][event.layer] += 1

    attribution_by_kind: dict[str, dict[str, Any]] = {}
    for row in attributed:
        kind = str(row["event"]["kind"])
        item = attribution_by_kind.setdefault(
            kind,
            {
                "events": 0,
                "same_bar_l2_events": 0,
                "gap_p95": [],
                "signed_aggressiveness": [],
                "depth_refill_ratio_outcome_only": [],
                "spread_resiliency_ratio_outcome_only": [],
            },
        )
        item["events"] += 1
        same = row["same_bar"]
        if same is not None:
            item["same_bar_l2_events"] += 1
            item["gap_p95"].append(same["gap_p95"])
            item["signed_aggressiveness"].append(same["signed_aggressiveness"])
        outcome = row["next_bar_outcome_only"]
        if outcome is not None:
            item["depth_refill_ratio_outcome_only"].append(outcome["depth_refill_ratio"])
            item["spread_resiliency_ratio_outcome_only"].append(
                outcome["spread_resiliency_ratio"]
            )
    for item in attribution_by_kind.values():
        item["mean_gap_p95"] = _mean(item.pop("gap_p95"))
        item["mean_signed_aggressiveness"] = _mean(item.pop("signed_aggressiveness"))
        item["mean_depth_refill_ratio_outcome_only"] = _mean(
            item.pop("depth_refill_ratio_outcome_only")
        )
        item["mean_spread_resiliency_ratio_outcome_only"] = _mean(
            item.pop("spread_resiliency_ratio_outcome_only")
        )
        item["outcome_metrics_usable_as_signal"] = False

    prefix_invariant = True
    if len(txf_bars) >= 2:
        cutoff = txf_bars[len(txf_bars) // 2 - 1].end_ns
        prefix_events = run_event_engine(
            [bar for bar in txf_bars if bar.end_ns <= cutoff],
            [bar for bar in electronic_bars if bar.end_ns <= cutoff],
            [bar for bar in financial_bars if bar.end_ns <= cutoff],
        )
        prefix_invariant = [event.event_id for event in prefix_events] == [
            event.event_id for event in events if event.ts_ns <= cutoff
        ]

    event_rows = [asdict(event) for event in events]
    event_digest = sha256(
        json.dumps(event_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    leader_counts: dict[str, int] = {}
    for row in lead_lag:
        leader = row["leader"] or "unavailable"
        leader_counts[leader] = leader_counts.get(leader, 0) + 1

    return {
        "schema": "research.cd_free_qt_cx_taiwan_v0.feasibility.v1",
        "candidate": "cd_free_qt_cx_taiwan_v0",
        "diagnostic_type": "backfill_evidence_read_only_no_trade",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": dict(provenance),
        "frozen_contract": {
            "signal_interval": "5m confirmed close",
            "active_cycle": "90m",
            "upper_cycle": "270m",
            "lower_cycle": "30m",
            "mini_cycle": "10m",
            "electronic_constituents": list(ELECTRONIC_SYMBOLS),
            "financial_constituents": list(FINANCIAL_SYMBOLS),
            "electronic_min_valid": 19,
            "financial_min_valid": 10,
            "front_month_windows": {
                "TXFB6": "through 2026-02-18",
                "TXFC6": "2026-02-19 through 2026-03-18",
                "TXFD6": "2026-03-19 through 2026-04-15",
                "TXFE6": "2026-04-16 through 2026-05-20",
                "TXFF6": "2026-05-21 onward",
            },
        },
        "coverage": coverage,
        "event_scorecard": {
            "total_events": len(events),
            "counts": dict(sorted(event_counts.items())),
            "direction_counts": dict(sorted(direction_counts.items())),
            "layer_counts": dict(sorted(layer_counts.items())),
            "event_digest_sha256": event_digest,
            "prefix_invariant": prefix_invariant,
        },
        "baseline_comparison_counts": baseline_comparison_summary(
            events,
            lead_lag,
            aligned_return_rows=len(return_rows),
        ),
        "lead_lag": {
            "policy": "lag-one correlations estimated from strictly prior dates only",
            "leader_counts": dict(sorted(leader_counts.items())),
            "daily_estimates": lead_lag,
        },
        "l2_attribution": {
            "same_bar_is_causal_context": True,
            "next_bar_is_outcome_only": True,
            "by_event_kind": dict(sorted(attribution_by_kind.items())),
        },
        "events": event_rows,
        "event_l2_attribution": attributed,
        "trading_metrics_computed": False,
        "cost_model_applied": False,
        "oos_claim_made": False,
        "ready_for_paper": False,
        "production_behavior_changed": False,
        "risk_behavior_changed": False,
        "broker_behavior_changed": False,
        "position_sizing_changed": False,
        "session_or_force_flat_changed": False,
        "cost_model_changed": False,
    }


def _run_clickhouse_query(sql: str, *, timeout_seconds: int = 300) -> list[dict[str, Any]]:
    normalized = sql.lstrip().upper()
    if not normalized.startswith(("SELECT", "WITH")):
        raise ValueError("diagnostic ClickHouse adapter accepts read-only SELECT/WITH queries only")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "clickhouse",
            "clickhouse-client",
            "--readonly=1",
            "--max_memory_usage=5000000000",
            "--max_threads=8",
            "--query",
            sql,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ClickHouse read failed: {result.stderr.strip()}")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def _quoted_symbols(symbols: Sequence[str]) -> str:
    return ",".join(f"'{symbol}'" for symbol in symbols)


def _five_minute_bar_query(date_from: str, date_to: str) -> str:
    symbols = ELECTRONIC_SYMBOLS + FINANCIAL_SYMBOLS + TXF_CONTRACTS
    local_ts = "fromUnixTimestamp64Nano(exch_ts,'Asia/Taipei')"
    bucket = f"toStartOfInterval({local_ts}, INTERVAL 5 MINUTE)"
    end_ns = f"toUnixTimestamp({bucket}) * 1000000000 + 300000000000"
    return f"""
SELECT
    symbol,
    toString(toDate({local_ts})) AS trade_date,
    {end_ns} AS end_ns,
    argMin(price_scaled, exch_ts) AS open_scaled,
    max(price_scaled) AS high_scaled,
    min(price_scaled) AS low_scaled,
    argMax(price_scaled, exch_ts) AS close_scaled,
    sum(volume) AS volume,
    count() AS source_rows
FROM hft.market_data
PREWHERE symbol IN ({_quoted_symbols(symbols)})
WHERE type = 'Tick'
  AND toDate({local_ts}) BETWEEN toDate('{date_from}') AND toDate('{date_to}')
  AND (toHour({local_ts}) * 60 + toMinute({local_ts})) >= 540
  AND (toHour({local_ts}) * 60 + toMinute({local_ts})) < 810
  AND price_scaled > 0
GROUP BY symbol, trade_date, end_ns
ORDER BY trade_date, end_ns, symbol
FORMAT JSONEachRow
""".strip()


def _l2_bar_query(date_from: str, date_to: str) -> str:
    local_ts = "fromUnixTimestamp64Nano(exch_ts,'Asia/Taipei')"
    bucket = f"toStartOfInterval({local_ts}, INTERVAL 5 MINUTE)"
    end_ns = f"toUnixTimestamp({bucket}) * 1000000000 + 300000000000"
    valid_quote = (
        "type='BidAsk' AND length(bids_price)>0 AND length(asks_price)>0 "
        "AND bids_price[1]>0 AND asks_price[1]>bids_price[1]"
    )
    spread = "(asks_price[1]-bids_price[1])/1000000.0"
    gap = (
        "greatest("
        "if(length(asks_price)>1 AND asks_price[2]>0,(asks_price[2]-asks_price[1])/1000000.0,0.0),"
        "if(length(bids_price)>1 AND bids_price[2]>0,(bids_price[1]-bids_price[2])/1000000.0,0.0),"
        f"{spread})"
    )
    depth = "arraySum(bids_vol)+arraySum(asks_vol)"
    directed_tick = "type='Tick' AND trade_direction!=0 AND volume>0"
    return f"""
SELECT
    symbol,
    toString(toDate({local_ts})) AS trade_date,
    {end_ns} AS end_ns,
    if(countIf({valid_quote})>0, toNullable(avgIf({spread},{valid_quote})), NULL) AS spread_mean,
    if(countIf({valid_quote})>0, toNullable(quantileExactIf(0.95)({gap},{valid_quote})), NULL) AS gap_p95,
    if(countIf({valid_quote})>0, toNullable(avgIf({depth},{valid_quote})), NULL) AS depth_mean,
    if(sumIf(volume,{directed_tick})>0,
       toNullable(sumIf(trade_direction*volume,{directed_tick})/sumIf(volume,{directed_tick})),
       NULL) AS signed_aggressiveness,
    countIf({valid_quote}) AS quote_rows,
    countIf(type='Tick') AS tick_rows
FROM hft.market_data
PREWHERE symbol IN ({_quoted_symbols(TXF_CONTRACTS)})
WHERE toDate({local_ts}) BETWEEN toDate('{date_from}') AND toDate('{date_to}')
  AND (toHour({local_ts}) * 60 + toMinute({local_ts})) >= 540
  AND (toHour({local_ts}) * 60 + toMinute({local_ts})) < 810
GROUP BY symbol, trade_date, end_ns
ORDER BY trade_date, end_ns, symbol
FORMAT JSONEachRow
""".strip()


def _row_to_bar(row: Mapping[str, Any], *, symbol: str | None = None) -> OhlcBar:
    return OhlcBar(
        symbol=symbol or str(row["symbol"]),
        trade_date=str(row["trade_date"]),
        end_ns=int(row["end_ns"]),
        open=float(row["open_scaled"]) / PRICE_SCALE,
        high=float(row["high_scaled"]) / PRICE_SCALE,
        low=float(row["low_scaled"]) / PRICE_SCALE,
        close=float(row["close_scaled"]) / PRICE_SCALE,
        volume=float(row["volume"]),
    )


def run_clickhouse_diagnostic(
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    bar_query = _five_minute_bar_query(date_from, date_to)
    l2_query = _l2_bar_query(date_from, date_to)
    bar_rows = _run_clickhouse_query(bar_query)
    l2_rows = _run_clickhouse_query(l2_query)

    rows_by_symbol: dict[str, list[OhlcBar]] = {}
    txf_bars: list[OhlcBar] = []
    for row in bar_rows:
        source_symbol = str(row["symbol"])
        trade_date = str(row["trade_date"])
        if source_symbol in TXF_CONTRACTS:
            if source_symbol == front_contract_for_date(trade_date):
                txf_bars.append(_row_to_bar(row, symbol="TXF"))
        else:
            rows_by_symbol.setdefault(source_symbol, []).append(_row_to_bar(row))

    electronic = build_equal_weight_basket(
        rows_by_symbol,
        symbols=ELECTRONIC_SYMBOLS,
        min_valid=19,
        name="electronic",
    )
    financial = build_equal_weight_basket(
        rows_by_symbol,
        symbols=FINANCIAL_SYMBOLS,
        min_valid=10,
        name="financial",
    )
    l2_by_end_ns: dict[int, L2Bar] = {}
    selected_l2_rows = 0
    for row in l2_rows:
        if str(row["symbol"]) != front_contract_for_date(str(row["trade_date"])):
            continue
        selected_l2_rows += int(row["quote_rows"]) + int(row["tick_rows"])
        l2_by_end_ns[int(row["end_ns"])] = L2Bar(
            end_ns=int(row["end_ns"]),
            spread_mean=float(row["spread_mean"]) if row["spread_mean"] is not None else None,
            gap_p95=float(row["gap_p95"]) if row["gap_p95"] is not None else None,
            depth_mean=float(row["depth_mean"]) if row["depth_mean"] is not None else None,
            signed_aggressiveness=(
                float(row["signed_aggressiveness"])
                if row["signed_aggressiveness"] is not None
                else None
            ),
        )

    canonical_rows = [
        {key: row[key] for key in sorted(row)}
        for row in bar_rows
        if str(row["symbol"]) not in TXF_CONTRACTS
        or str(row["symbol"]) == front_contract_for_date(str(row["trade_date"]))
    ]
    content_digest = sha256(
        json.dumps(canonical_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    provenance = {
        "source": "Docker ClickHouse hft.market_data (read-only)",
        "date_from": date_from,
        "date_to": date_to,
        "timezone": "Asia/Taipei",
        "timestamp_semantics": "exch_ts nanoseconds; bars timestamped by interval end",
        "raw_tick_rows_aggregated": sum(int(row["source_rows"]) for row in canonical_rows),
        "selected_l2_and_tick_rows_aggregated": selected_l2_rows,
        "five_minute_rows": len(canonical_rows),
        "bar_query_sha256": sha256(bar_query.encode("utf-8")).hexdigest(),
        "l2_query_sha256": sha256(l2_query.encode("utf-8")).hexdigest(),
        "aggregated_content_sha256": content_digest,
        "price_scale": "ClickHouse x1,000,000 converted explicitly to research float",
        "bar_building_version": "cd_free_qt_cx_taiwan_v0_5m_tick_ohlc_v1",
        "universe_label": "retrospective_fixed_universe",
    }
    return build_diagnostic_payload(
        sorted(txf_bars, key=lambda bar: bar.end_ns),
        electronic,
        financial,
        l2_by_end_ns=l2_by_end_ns,
        provenance=provenance,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", default="2026-01-27")
    parser.add_argument("--date-to", default="2026-06-04")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("feasibility_diagnostic_iteration31.json"),
    )
    args = parser.parse_args(argv)
    payload = run_clickhouse_diagnostic(date_from=args.date_from, date_to=args.date_to)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "dates": payload["coverage"]["date_count"],
                "fully_valid_dates": payload["coverage"]["fully_valid_date_count"],
                "events": payload["event_scorecard"]["total_events"],
                "event_digest_sha256": payload["event_scorecard"]["event_digest_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
