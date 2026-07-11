"""Frozen raw-L2 execution replay for cd_free_qt_cx_taiwan_v0 Iteration 32."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from bisect import bisect_left
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from src.hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_EVENT,
    DEPTH_SNAPSHOT_EVENT,
    EV_TYPE_MASK,
    SELL_EVENT,
)

TAIPEI = ZoneInfo("Asia/Taipei")
LANES = ("baseline_sweep_cisd", "correlated_channel", "main_pair_channel")
STOP_ATR_VALUES = (0.75, 1.0)
TARGET_ATR_VALUES = (1.0, 1.5, 2.0)
DEFAULT_LATENCY_NS = 57_000_000
DEFAULT_COSTS = (0.0, 3.0, 6.0)


def contract_for_date(date: str) -> str:
    if date <= "2026-02-18":
        return "TXFB6"
    if date <= "2026-03-18":
        return "TXFC6"
    if date <= "2026-04-15":
        return "TXFD6"
    if date <= "2026-05-20":
        return "TXFE6"
    return "TXFF6"


def stage_for_date(date: str) -> str:
    if date <= "2026-04-15":
        return "development"
    if date <= "2026-05-20":
        return "primary_oos"
    return "confirmation_oos"


def cutoff_ns_for_date(date: str) -> int:
    return int(datetime.fromisoformat(f"{date}T13:25:00").replace(tzinfo=TAIPEI).timestamp() * 1e9)


@dataclass(frozen=True, slots=True)
class BboQuote:
    ts_ns: int
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float
    local_ts_ns: int = 0
    seq_no: int = 0

    @property
    def valid(self) -> bool:
        return (
            self.ts_ns > 0
            and self.bid > 0.0
            and self.ask > self.bid
            and self.bid_qty > 0.0
            and self.ask_qty > 0.0
        )


@dataclass(frozen=True, slots=True)
class Bar:
    end_ns: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class Alert:
    event_id: str
    lane: str
    direction: int
    ts_ns: int
    date: str
    stage: str = "unknown"
    contract: str = ""
    regime: str = "unavailable"
    prior_leader: str = "unavailable"


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    latency_ns: int
    cutoff_by_date: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class Trade:
    event_id: str
    lane: str
    model: str
    direction: int
    date: str
    stage: str
    contract: str
    regime: str
    prior_leader: str
    entry_ts_ns: int
    exit_ts_ns: int
    entry_px: float
    exit_px: float
    entry_spread_pts: float
    exit_spread_pts: float
    latency_adverse_pts: float | None
    exit_reason: str
    atr: float | None = None
    stop_atr: float | None = None
    target_atr: float | None = None

    @property
    def gross_points(self) -> float:
        return (self.exit_px - self.entry_px) * self.direction


@dataclass(frozen=True, slots=True)
class ReplayResult:
    trades: tuple[Trade, ...]
    rejections: Mapping[str, int]


def _session_bounds_ns(date: str) -> tuple[int, int]:
    start = datetime.fromisoformat(f"{date}T09:00:00").replace(tzinfo=TAIPEI)
    end = datetime.fromisoformat(f"{date}T13:30:00").replace(tzinfo=TAIPEI)
    return int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)


def load_session_bbo_from_hftbt_npz(path: Path, *, date: str) -> list[BboQuote]:
    """Reconstruct BBOs only at timestamps containing actual depth updates."""
    start_ns, end_ns = _session_bounds_ns(date)
    with np.load(path, allow_pickle=False) as archive:
        data = archive["data"]
    timestamps = data["exch_ts"]
    lo = int(np.searchsorted(timestamps, start_ns, side="left"))
    hi = int(np.searchsorted(timestamps, end_ns, side="right"))
    session = data[lo:hi]
    if not len(session):
        return []

    flags = session["ev"]
    event_types = flags & EV_TYPE_MASK
    quote_ts, group_ids = np.unique(session["exch_ts"], return_inverse=True)
    n_groups = len(quote_ts)
    depth_mask = (event_types == DEPTH_EVENT) | (event_types == DEPTH_SNAPSHOT_EVENT)
    bid_mask = depth_mask & ((flags & BUY_EVENT) != 0) & (session["qty"] > 0.0)
    ask_mask = depth_mask & ((flags & SELL_EVENT) != 0) & (session["qty"] > 0.0)

    best_bid = np.full(n_groups, -np.inf, dtype=np.float64)
    best_ask = np.full(n_groups, np.inf, dtype=np.float64)
    np.maximum.at(best_bid, group_ids[bid_mask], session["px"][bid_mask])
    np.minimum.at(best_ask, group_ids[ask_mask], session["px"][ask_mask])

    bid_qty = np.zeros(n_groups, dtype=np.float64)
    ask_qty = np.zeros(n_groups, dtype=np.float64)
    best_bid_rows = bid_mask & (session["px"] == best_bid[np.maximum(group_ids, 0)])
    best_ask_rows = ask_mask & (session["px"] == best_ask[np.maximum(group_ids, 0)])
    np.maximum.at(bid_qty, group_ids[best_bid_rows], session["qty"][best_bid_rows])
    np.maximum.at(ask_qty, group_ids[best_ask_rows], session["qty"][best_ask_rows])

    local_ts = np.zeros(n_groups, dtype=np.int64)
    np.maximum.at(local_ts, group_ids, session["local_ts"])
    valid = (
        np.isfinite(best_bid)
        & np.isfinite(best_ask)
        & (best_bid > 0.0)
        & (best_ask > best_bid)
        & (bid_qty > 0.0)
        & (ask_qty > 0.0)
    )
    indices = np.flatnonzero(valid)
    return [
        BboQuote(
            ts_ns=int(quote_ts[idx]),
            bid=float(best_bid[idx]),
            ask=float(best_ask[idx]),
            bid_qty=float(bid_qty[idx]),
            ask_qty=float(ask_qty[idx]),
            local_ts_ns=int(local_ts[idx]),
        )
        for idx in indices
    ]


def _quote_timestamps(quotes: Sequence[BboQuote]) -> list[int]:
    return [quote.ts_ns for quote in quotes]


def first_eligible_quote(
    quotes: Sequence[BboQuote],
    *,
    confirmation_ns: int,
    latency_ns: int,
) -> BboQuote | None:
    """Return the first valid BBO strictly after confirmation and latency."""
    target_ns = confirmation_ns + latency_ns
    idx = bisect_left(_quote_timestamps(quotes), target_ns)
    while idx < len(quotes):
        quote = quotes[idx]
        if quote.ts_ns > confirmation_ns and quote.valid:
            return quote
        idx += 1
    return None


def _quote_at_or_before(quotes: Sequence[BboQuote], ts_ns: int) -> BboQuote | None:
    idx = bisect_left(_quote_timestamps(quotes), ts_ns + 1) - 1
    while idx >= 0:
        quote = quotes[idx]
        if quote.valid:
            return quote
        idx -= 1
    return None


def wilder_atr_by_end(bars: Sequence[Bar], *, period: int = 14) -> dict[int, float | None]:
    """Compute causal Wilder ATR values keyed by confirmed bar end timestamp."""
    if period <= 0:
        raise ValueError("period must be positive")
    ordered = sorted(bars, key=lambda bar: bar.end_ns)
    true_ranges: list[float] = []
    output: dict[int, float | None] = {}
    atr: float | None = None
    previous_close: float | None = None
    for bar in ordered:
        tr = bar.high - bar.low
        if previous_close is not None:
            tr = max(tr, abs(bar.high - previous_close), abs(bar.low - previous_close))
        true_ranges.append(float(tr))
        if len(true_ranges) == period:
            atr = float(np.mean(true_ranges))
        elif len(true_ranges) > period and atr is not None:
            atr = ((period - 1) * atr + tr) / period
        output[bar.end_ns] = atr
        previous_close = bar.close
    return output


def _entry_price(alert: Alert, quote: BboQuote) -> float:
    return quote.ask if alert.direction > 0 else quote.bid


def _exit_price(direction: int, quote: BboQuote) -> float:
    return quote.bid if direction > 0 else quote.ask


def _latency_adverse(
    alert: Alert,
    entry_quote: BboQuote,
    decision_quote: BboQuote | None,
) -> float | None:
    if decision_quote is None:
        return None
    decision_mid = (decision_quote.bid + decision_quote.ask) / 2.0
    entry_mid = (entry_quote.bid + entry_quote.ask) / 2.0
    return (entry_mid - decision_mid) * alert.direction


def _trade(
    alert: Alert,
    entry: BboQuote,
    exit_quote: BboQuote,
    *,
    model: str,
    reason: str,
    decision_quote: BboQuote | None,
    atr: float | None = None,
    stop_atr: float | None = None,
    target_atr: float | None = None,
) -> Trade:
    return Trade(
        event_id=alert.event_id,
        lane=alert.lane,
        model=model,
        direction=alert.direction,
        date=alert.date,
        stage=alert.stage,
        contract=alert.contract,
        regime=alert.regime,
        prior_leader=alert.prior_leader,
        entry_ts_ns=entry.ts_ns,
        exit_ts_ns=exit_quote.ts_ns,
        entry_px=_entry_price(alert, entry),
        exit_px=_exit_price(alert.direction, exit_quote),
        entry_spread_pts=entry.ask - entry.bid,
        exit_spread_pts=exit_quote.ask - exit_quote.bid,
        latency_adverse_pts=_latency_adverse(alert, entry, decision_quote),
        exit_reason=reason,
        atr=atr,
        stop_atr=stop_atr,
        target_atr=target_atr,
    )


def _prepare_entry(
    alert: Alert,
    quotes: Sequence[BboQuote],
    config: ReplayConfig,
) -> tuple[BboQuote | None, BboQuote | None, str | None]:
    cutoff = config.cutoff_by_date.get(alert.date)
    if cutoff is None:
        return None, None, "missing_cutoff"
    if alert.ts_ns >= cutoff:
        return None, None, "late_alert"
    entry = first_eligible_quote(
        quotes,
        confirmation_ns=alert.ts_ns,
        latency_ns=config.latency_ns,
    )
    if entry is None:
        return None, None, "missing_entry_bbo"
    if entry.ts_ns >= cutoff:
        return None, None, "entry_after_cutoff"
    return entry, _quote_at_or_before(quotes, alert.ts_ns), None


def _force_flat_quote(quotes: Sequence[BboQuote], cutoff_ns: int) -> BboQuote | None:
    idx = bisect_left(_quote_timestamps(quotes), cutoff_ns)
    while idx < len(quotes):
        if quotes[idx].valid:
            return quotes[idx]
        idx += 1
    return None


def simulate_fixed_risk(
    alerts: Sequence[Alert],
    quotes_by_date: Mapping[str, Sequence[BboQuote]],
    *,
    atr_by_alert: Mapping[str, float | None],
    stop_atr: float,
    target_atr: float,
    config: ReplayConfig,
) -> ReplayResult:
    trades: list[Trade] = []
    rejected: Counter[str] = Counter()
    busy_until_by_date: dict[str, int] = {}
    model = f"fixed_{stop_atr:g}x{target_atr:g}"
    for alert in sorted(alerts, key=lambda item: (item.ts_ns, item.event_id)):
        if alert.direction not in {-1, 1}:
            rejected["invalid_direction"] += 1
            continue
        if alert.ts_ns <= busy_until_by_date.get(alert.date, -1):
            rejected["position_open"] += 1
            continue
        quotes = quotes_by_date.get(alert.date, ())
        if not quotes:
            rejected["missing_l2_date"] += 1
            continue
        atr = atr_by_alert.get(alert.event_id)
        if atr is None or not np.isfinite(atr) or atr <= 0.0:
            rejected["missing_atr"] += 1
            continue
        entry, decision_quote, reason = _prepare_entry(alert, quotes, config)
        if entry is None:
            rejected[str(reason)] += 1
            continue
        entry_px = _entry_price(alert, entry)
        stop_px = entry_px - alert.direction * stop_atr * atr
        target_px = entry_px + alert.direction * target_atr * atr
        cutoff = config.cutoff_by_date[alert.date]
        start_idx = bisect_left(_quote_timestamps(quotes), entry.ts_ns + 1)
        exit_quote: BboQuote | None = None
        exit_reason = ""
        for quote in quotes[start_idx:]:
            if quote.ts_ns >= cutoff:
                break
            if not quote.valid:
                continue
            observable = quote.bid if alert.direction > 0 else quote.ask
            stop_hit = observable <= stop_px if alert.direction > 0 else observable >= stop_px
            target_hit = observable >= target_px if alert.direction > 0 else observable <= target_px
            if stop_hit:
                exit_quote = quote
                exit_reason = "stop"
                break
            if target_hit:
                exit_quote = quote
                exit_reason = "target"
                break
        if exit_quote is None:
            exit_quote = _force_flat_quote(quotes, cutoff)
            exit_reason = "force_flat"
        if exit_quote is None:
            rejected["unresolved_exit"] += 1
            continue
        trades.append(
            _trade(
                alert,
                entry,
                exit_quote,
                model=model,
                reason=exit_reason,
                decision_quote=decision_quote,
                atr=float(atr),
                stop_atr=stop_atr,
                target_atr=target_atr,
            )
        )
        busy_until_by_date[alert.date] = exit_quote.ts_ns
    return ReplayResult(tuple(trades), dict(sorted(rejected.items())))


def simulate_structural(
    alerts: Sequence[Alert],
    cisd_events: Sequence[Alert],
    quotes_by_date: Mapping[str, Sequence[BboQuote]],
    *,
    config: ReplayConfig,
) -> ReplayResult:
    trades: list[Trade] = []
    rejected: Counter[str] = Counter()
    busy_until_by_date: dict[str, int] = {}
    cisd_by_date: dict[str, list[Alert]] = {}
    for event in cisd_events:
        cisd_by_date.setdefault(event.date, []).append(event)
    for values in cisd_by_date.values():
        values.sort(key=lambda item: (item.ts_ns, item.event_id))

    for alert in sorted(alerts, key=lambda item: (item.ts_ns, item.event_id)):
        if alert.direction not in {-1, 1}:
            rejected["invalid_direction"] += 1
            continue
        if alert.ts_ns <= busy_until_by_date.get(alert.date, -1):
            rejected["position_open"] += 1
            continue
        quotes = quotes_by_date.get(alert.date, ())
        if not quotes:
            rejected["missing_l2_date"] += 1
            continue
        entry, decision_quote, reason = _prepare_entry(alert, quotes, config)
        if entry is None:
            rejected[str(reason)] += 1
            continue
        cutoff = config.cutoff_by_date[alert.date]
        force_quote = _force_flat_quote(quotes, cutoff)
        exit_quote: BboQuote | None = None
        exit_reason = "force_flat"
        for cisd in cisd_by_date.get(alert.date, []):
            if cisd.direction != -alert.direction or cisd.ts_ns <= entry.ts_ns:
                continue
            candidate = first_eligible_quote(
                quotes,
                confirmation_ns=cisd.ts_ns,
                latency_ns=config.latency_ns,
            )
            if candidate is not None and candidate.ts_ns < cutoff:
                exit_quote = candidate
                exit_reason = "opposite_cisd"
            break
        if exit_quote is None:
            exit_quote = force_quote
        if exit_quote is None:
            rejected["unresolved_exit"] += 1
            continue
        trades.append(
            _trade(
                alert,
                entry,
                exit_quote,
                model="structural",
                reason=exit_reason,
                decision_quote=decision_quote,
            )
        )
        busy_until_by_date[alert.date] = exit_quote.ts_ns
    return ReplayResult(tuple(trades), dict(sorted(rejected.items())))


def _max_drawdown(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    cumulative = np.cumsum(values)
    peaks = np.maximum.accumulate(np.r_[0.0, cumulative])[1:]
    return float(np.max(peaks - cumulative, initial=0.0))


def summarize_trades(
    trades: Sequence[Trade],
    *,
    costs: Sequence[float] = (0.0, 3.0, 6.0),
) -> dict[str, dict[str, float | int | None]]:
    output: dict[str, dict[str, float | int | None]] = {}
    for cost in costs:
        net = np.asarray([trade.gross_points - cost for trade in trades], dtype=np.float64)
        positive = float(net[net > 0].sum()) if net.size else 0.0
        negative = float(-net[net < 0].sum()) if net.size else 0.0
        daily: dict[str, float] = {}
        for trade, value in zip(trades, net, strict=True):
            daily[trade.date] = daily.get(trade.date, 0.0) + float(value)
        daily_values = np.asarray(list(daily.values()), dtype=np.float64)
        total = float(net.sum()) if net.size else 0.0
        best_day = float(daily_values.max()) if daily_values.size else 0.0
        positive_trades = net[net > 0]
        negative_trades = -net[net < 0]
        output[f"{cost:g}pt"] = {
            "n_trades": int(net.size),
            "n_days": len(daily),
            "net_total": round(total, 6),
            "net_mean": round(float(net.mean()), 6) if net.size else None,
            "net_median": round(float(np.median(net)), 6) if net.size else None,
            "win_rate": round(float(np.mean(net > 0)), 6) if net.size else None,
            "profit_factor": round(positive / negative, 6) if negative > 0.0 else None,
            "max_drawdown_points": round(_max_drawdown(net), 6),
            "best_day_loo_total": round(total - best_day, 6),
            "top_trade_share_of_positive": round(float(positive_trades.max()) / positive, 6)
            if positive > 0.0
            else None,
            "worst_loss_share": round(float(negative_trades.max()) / negative, 6)
            if negative > 0.0
            else None,
            "force_flat_share": round(
                sum(trade.exit_reason == "force_flat" for trade in trades) / len(trades), 6
            )
            if trades
            else None,
        }
    return output


def _run_clickhouse_query(sql: str, *, timeout_seconds: int = 300) -> list[dict[str, Any]]:
    normalized = sql.lstrip().upper()
    if not normalized.startswith(("SELECT", "WITH")):
        raise ValueError("execution replay accepts read-only SELECT/WITH queries only")
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


def _raw_bbo_query(contract: str, date: str) -> str:
    if contract != contract_for_date(date):
        raise ValueError(f"contract/date violates frozen front chain: {contract}/{date}")
    local_ts = "fromUnixTimestamp64Nano(exch_ts,'Asia/Taipei')"
    return f"""
SELECT
    exch_ts,
    ingest_ts AS local_ts_ns,
    seq_no,
    bids_price[1] / 1000000.0 AS bid,
    asks_price[1] / 1000000.0 AS ask,
    bids_vol[1] AS bid_qty,
    asks_vol[1] AS ask_qty
FROM hft.market_data
PREWHERE symbol = '{contract}'
WHERE type = 'BidAsk'
  AND toDate({local_ts}) = toDate('{date}')
  AND (toHour({local_ts}) * 60 + toMinute({local_ts})) >= 540
  AND (toHour({local_ts}) * 60 + toMinute({local_ts})) <= 810
  AND length(bids_price) > 0 AND length(asks_price) > 0
  AND length(bids_vol) > 0 AND length(asks_vol) > 0
  AND bids_price[1] > 0 AND asks_price[1] > bids_price[1]
  AND bids_vol[1] > 0 AND asks_vol[1] > 0
ORDER BY exch_ts, ingest_ts, seq_no
FORMAT JSONEachRow
""".strip()


def _load_direct_clickhouse_bbo(
    contract: str,
    date: str,
) -> tuple[list[BboQuote], dict[str, Any]]:
    rows = _run_clickhouse_query(_raw_bbo_query(contract, date), timeout_seconds=600)
    quotes = [_quote_from_row(row) for row in rows]
    digest = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return quotes, {
        "source": "docker_clickhouse:hft.market_data",
        "query_identity": "ordered_raw_bidask_day_session_v1",
        "contract": contract,
        "date": date,
        "quote_rows": len(quotes),
        "content_sha256": digest,
        "first_ts_ns": quotes[0].ts_ns if quotes else None,
        "last_ts_ns": quotes[-1].ts_ns if quotes else None,
    }


def _load_date_bbo(
    contract: str,
    date: str,
    *,
    bbo_cache_dir: Path | None = None,
) -> tuple[list[BboQuote], dict[str, Any]]:
    path = Path("research/data/raw") / contract.lower() / f"{contract}_{date}_l2.hftbt.npz"
    if path.exists():
        quotes = load_session_bbo_from_hftbt_npz(path, date=date)
        meta_path = Path(str(path) + ".meta.json")
        metadata = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        return quotes, {
            "source": "governed_local_hftbt_npz",
            "path": str(path),
            "contract": contract,
            "date": date,
            "quote_rows": len(quotes),
            "data_fingerprint": metadata.get("data_fingerprint"),
            "row_count": metadata.get("row_count"),
            "first_ts_ns": quotes[0].ts_ns if quotes else None,
            "last_ts_ns": quotes[-1].ts_ns if quotes else None,
        }
    cache_path = (
        bbo_cache_dir / f"{contract}_{date}_bbo.jsonl" if bbo_cache_dir is not None else None
    )
    if cache_path is not None and cache_path.exists():
        cache_bytes = cache_path.read_bytes()
        rows = [json.loads(line) for line in cache_bytes.splitlines() if line]
        quotes = [_quote_from_row(row) for row in rows]
        return quotes, {
            "source": "docker_clickhouse_exported_raw_bbo_jsonl",
            "path": str(cache_path),
            "contract": contract,
            "date": date,
            "quote_rows": len(quotes),
            "content_sha256": hashlib.sha256(cache_bytes).hexdigest(),
            "first_ts_ns": quotes[0].ts_ns if quotes else None,
            "last_ts_ns": quotes[-1].ts_ns if quotes else None,
        }
    return _load_direct_clickhouse_bbo(contract, date)


def _quote_from_row(row: Mapping[str, Any]) -> BboQuote:
    return BboQuote(
        ts_ns=int(row["exch_ts"]),
        bid=float(row["bid"]),
        ask=float(row["ask"]),
        bid_qty=float(row["bid_qty"]),
        ask_qty=float(row["ask_qty"]),
        local_ts_ns=int(row["local_ts_ns"]),
        seq_no=int(row["seq_no"]),
    )


def _txf_bar_query(date_from: str, date_to: str) -> str:
    contracts = "'TXFD6','TXFE6','TXFF6'"
    local_bucket = "bucket + INTERVAL 8 HOUR"
    bucket_5m = "toStartOfInterval(bucket, INTERVAL 5 MINUTE)"
    end_ns = f"toUnixTimestamp({bucket_5m} + INTERVAL 5 MINUTE) * 1000000000"
    return f"""
SELECT
    symbol,
    toString(toDate({local_bucket})) AS trade_date,
    {end_ns} AS end_ns,
    argMin(open_scaled, bucket) / 1000000.0 AS open,
    max(high_scaled) / 1000000.0 AS high,
    min(low_scaled) / 1000000.0 AS low,
    argMax(close_scaled, bucket) / 1000000.0 AS close,
    sum(tick_count) AS source_rows
FROM hft.ohlcv_1m
WHERE symbol IN ({contracts})
  AND toDate({local_bucket}) BETWEEN toDate('{date_from}') AND toDate('{date_to}')
  AND (toHour({local_bucket}) * 60 + toMinute({local_bucket})) >= 540
  AND (toHour({local_bucket}) * 60 + toMinute({local_bucket})) < 810
  AND close_scaled > 0
GROUP BY symbol, trade_date, end_ns
ORDER BY trade_date, end_ns, symbol
FORMAT JSONEachRow
""".strip()


def _ema(values: Sequence[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    output: list[float] = []
    current: float | None = None
    for value in values:
        current = value if current is None else alpha * value + (1.0 - alpha) * current
        output.append(float(current))
    return output


def classify_regimes(
    bars: Sequence[Bar],
    atr_by_end: Mapping[int, float | None],
) -> dict[int, str]:
    ordered = sorted(bars, key=lambda bar: bar.end_ns)
    ema_fast = _ema([bar.close for bar in ordered], 20)
    ema_slow = _ema([bar.close for bar in ordered], 100)
    prior_atr: list[float] = []
    regimes: dict[int, str] = {}
    for idx, bar in enumerate(ordered):
        atr = atr_by_end.get(bar.end_ns)
        if atr is None or atr <= 0.0 or idx == 0:
            regimes[bar.end_ns] = "unavailable"
        else:
            high_vol = len(prior_atr) >= 30 and atr > float(np.percentile(prior_atr, 75))
            spread = (ema_fast[idx] - ema_slow[idx]) / atr
            slope = (ema_fast[idx] - ema_fast[idx - 1]) / atr
            if high_vol:
                regimes[bar.end_ns] = "high_vol"
            elif abs(spread) >= 0.35 and abs(slope) >= 0.02:
                regimes[bar.end_ns] = "trend"
            else:
                regimes[bar.end_ns] = "range"
        if atr is not None and np.isfinite(atr):
            prior_atr.append(float(atr))
    return regimes


def _latest_by_timestamp(
    timestamps: Sequence[int],
    values: Mapping[int, Any],
    target_ns: int,
) -> Any:
    idx = bisect_left(timestamps, target_ns + 1) - 1
    return values.get(timestamps[idx]) if idx >= 0 else None


def _scorecard_bundle(trades: Sequence[Trade]) -> dict[str, Any]:
    stages = sorted({trade.stage for trade in trades})
    regimes = sorted({trade.regime for trade in trades})
    leaders = sorted({trade.prior_leader for trade in trades})
    return {
        "all": summarize_trades(trades),
        "by_stage": {
            stage: summarize_trades([trade for trade in trades if trade.stage == stage])
            for stage in stages
        },
        "by_direction": {
            "long": summarize_trades([trade for trade in trades if trade.direction > 0]),
            "short": summarize_trades([trade for trade in trades if trade.direction < 0]),
        },
        "by_regime": {
            regime: summarize_trades([trade for trade in trades if trade.regime == regime])
            for regime in regimes
        },
        "by_prior_leader": {
            leader: summarize_trades([trade for trade in trades if trade.prior_leader == leader])
            for leader in leaders
        },
    }


def _merge_rejections(target: Counter[str], source: Mapping[str, int]) -> None:
    for reason, count in source.items():
        target[reason] += int(count)


def build_iteration32_payload(  # noqa: C901 - sequential frozen replay orchestration
    diagnostic_path: Path,
    *,
    date_from: str = "2026-04-16",
    date_to: str = "2026-06-04",
    bars_jsonl: Path | None = None,
    bbo_cache_dir: Path | None = None,
) -> dict[str, Any]:
    diagnostic_bytes = diagnostic_path.read_bytes()
    diagnostic = json.loads(diagnostic_bytes)
    event_digest = diagnostic["event_scorecard"]["event_digest_sha256"]
    leaders = {
        row["date"]: str(row.get("leader") or "unavailable")
        for row in diagnostic["lead_lag"]["daily_estimates"]
    }

    print("iteration32: loading causal TXF bars", file=sys.stderr, flush=True)
    if bars_jsonl is not None:
        bar_bytes = bars_jsonl.read_bytes()
        bar_rows = [json.loads(line) for line in bar_bytes.splitlines() if line]
    else:
        bar_rows = _run_clickhouse_query(_txf_bar_query("2026-04-15", date_to))
    selected_bar_rows = [
        row
        for row in bar_rows
        if str(row["symbol"]) == contract_for_date(str(row["trade_date"]))
    ]
    bars = [
        Bar(
            end_ns=int(row["end_ns"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )
        for row in selected_bar_rows
    ]
    atr_by_end = wilder_atr_by_end(bars, period=14)
    regime_by_end = classify_regimes(bars, atr_by_end)
    bar_timestamps = sorted(atr_by_end)

    def to_alert(row: Mapping[str, Any]) -> Alert:
        date = str(row["cycle_key"])[:10]
        ts_ns = int(row["ts_ns"])
        regime = _latest_by_timestamp(bar_timestamps, regime_by_end, ts_ns) or "unavailable"
        return Alert(
            event_id=str(row["event_id"]),
            lane=str(row["kind"]),
            direction=int(row["direction"]),
            ts_ns=ts_ns,
            date=date,
            stage=stage_for_date(date),
            contract=contract_for_date(date),
            regime=str(regime),
            prior_leader=leaders.get(date, "unavailable"),
        )

    event_rows = diagnostic["events"]
    alerts = [
        to_alert(row)
        for row in event_rows
        if row["kind"] in LANES and date_from <= str(row["cycle_key"])[:10] <= date_to
    ]
    cisd = [
        to_alert(row)
        for row in event_rows
        if row["kind"] == "cisd" and date_from <= str(row["cycle_key"])[:10] <= date_to
    ]
    atr_by_alert = {
        alert.event_id: _latest_by_timestamp(bar_timestamps, atr_by_end, alert.ts_ns)
        for alert in alerts
    }
    dates = sorted({alert.date for alert in alerts})
    cutoff_by_date = {date: cutoff_ns_for_date(date) for date in dates}
    config = ReplayConfig(latency_ns=DEFAULT_LATENCY_NS, cutoff_by_date=cutoff_by_date)

    trade_groups: dict[str, list[Trade]] = {}
    rejection_groups: dict[str, Counter[str]] = {}
    coverage: dict[str, Any] = {}
    for date in dates:
        contract = contract_for_date(date)
        print(f"iteration32: loading {contract} {date} raw BBO", file=sys.stderr, flush=True)
        date_alerts_all = [alert for alert in alerts if alert.date == date]
        date_cisd = [event for event in cisd if event.date == date]
        quotes, provenance = _load_date_bbo(contract, date, bbo_cache_dir=bbo_cache_dir)
        print(
            f"iteration32: replaying {date} with {len(quotes)} BBO snapshots",
            file=sys.stderr,
            flush=True,
        )
        coverage[date] = provenance
        date_quotes = {date: quotes}
        for lane in LANES:
            date_alerts = [alert for alert in date_alerts_all if alert.lane == lane]
            if not date_alerts:
                continue
            structural = simulate_structural(date_alerts, date_cisd, date_quotes, config=config)
            key = f"{lane}:structural"
            trade_groups.setdefault(key, []).extend(structural.trades)
            _merge_rejections(rejection_groups.setdefault(key, Counter()), structural.rejections)
            for stop_atr in STOP_ATR_VALUES:
                for target_atr in TARGET_ATR_VALUES:
                    key = f"{lane}:fixed_{stop_atr:g}x{target_atr:g}"
                    fixed = simulate_fixed_risk(
                        date_alerts,
                        date_quotes,
                        atr_by_alert=atr_by_alert,
                        stop_atr=stop_atr,
                        target_atr=target_atr,
                        config=config,
                    )
                    trade_groups.setdefault(key, []).extend(fixed.trades)
                    _merge_rejections(rejection_groups.setdefault(key, Counter()), fixed.rejections)

    results = {}
    for key in sorted(trade_groups):
        trades = sorted(trade_groups[key], key=lambda trade: (trade.entry_ts_ns, trade.event_id))
        results[key] = {
            "trades": [asdict(trade) | {"gross_points": trade.gross_points} for trade in trades],
            "rejections": dict(sorted(rejection_groups.get(key, {}).items())),
            "scorecard": _scorecard_bundle(trades),
        }

    bar_digest = hashlib.sha256(
        json.dumps(selected_bar_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "cd_free_qt_cx_taiwan_v0.execution_replay.iteration32.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate": "cd_free_qt_cx_taiwan_v0",
        "route": "backfill_evidence",
        "date_from": date_from,
        "date_to": date_to,
        "execution_profile": {
            "profile": "vm_ul6_strict/sim_stress_v2026-02-26",
            "local_decision_pipeline_latency_us": 1000,
            "submit_ack_latency_ms": 56.0,
            "total_latency_ms": 57.0,
            "costs_round_trip_points": {"zero_extra": 0.0, "baseline": 3.0, "stress": 6.0},
            "force_flat_local_time": "13:25:00 Asia/Taipei",
        },
        "input_provenance": {
            "diagnostic_path": str(diagnostic_path),
            "diagnostic_sha256": hashlib.sha256(diagnostic_bytes).hexdigest(),
            "event_digest_sha256": event_digest,
            "txf_bar_query_identity": "front_chain_5m_from_ohlcv_1m_v1",
            "txf_bar_cache_path": str(bars_jsonl) if bars_jsonl is not None else None,
            "txf_bar_rows": len(selected_bar_rows),
            "txf_bar_sha256": bar_digest,
            "bbo_by_date": coverage,
        },
        "alert_counts": dict(sorted(Counter(alert.lane for alert in alerts).items())),
        "alert_dates": dates,
        "prior_lead_lag": {
            "role": "contextual_stratification_only_no_independent_trade_rule",
            "policy": diagnostic["lead_lag"]["policy"],
            "leader_counts_full": diagnostic["lead_lag"]["leader_counts"],
        },
        "results": results,
        "production_behavior_changed": False,
        "risk_behavior_changed": False,
        "broker_behavior_changed": False,
        "position_sizing_changed": False,
        "session_or_force_flat_changed": False,
        "cost_model_changed": False,
        "ready_for_paper": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diagnostic",
        type=Path,
        default=Path(__file__).with_name("feasibility_diagnostic_iteration31.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("execution_replay_iteration32.json"),
    )
    parser.add_argument("--date-from", default="2026-04-16")
    parser.add_argument("--date-to", default="2026-06-04")
    parser.add_argument("--bars-jsonl", type=Path)
    parser.add_argument("--bbo-cache-dir", type=Path)
    args = parser.parse_args(argv)
    payload = build_iteration32_payload(
        args.diagnostic,
        date_from=args.date_from,
        date_to=args.date_to,
        bars_jsonl=args.bars_jsonl,
        bbo_cache_dir=args.bbo_cache_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "dates": len(payload["alert_dates"]),
                "alerts": payload["alert_counts"],
                "result_groups": len(payload["results"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
