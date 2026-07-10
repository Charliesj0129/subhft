"""T1G extreme-imbalance read-only feasibility diagnostic.

This is an additive research helper for Iteration 20. It computes only
decision-time features from governed raw TXF ticks. It does not create orders,
does not tune thresholds on same-day data, and does not alter production paths.
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from research.t1.regime_viability import NS_PER_MINUTE, _session_start_ns
from src.hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_CLEAR_EVENT,
    DEPTH_EVENT,
    DEPTH_SNAPSHOT_EVENT,
    EV_TYPE_MASK,
    SELL_EVENT,
)

DEFAULT_RAW_DIR = Path("research/data/raw")
DEFAULT_OUT_DIR = Path("research/experiments/validations/t1g_extreme_imbalance_v0")
PRIMARY_MONTHS = ("D6", "E6")
DEFAULT_HORIZONS_MINUTES = (5, 15, 30)
DEFAULT_TMF_ROUND_TRIP_COST_PTS = 8.0


@dataclass(frozen=True)
class WindowFeature:
    signed_imbalance: float | None
    return_pts: float | None
    gross_qty: float
    tick_count: int


@dataclass(frozen=True)
class DecisionFeature:
    contract: str
    date: str
    decision_time_ns: int
    signed_imbalance: float
    return_pts: float
    gross_qty: float
    tick_count: int


@dataclass(frozen=True)
class BboQuote:
    ts_ns: int
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


@dataclass(frozen=True)
class ExecutableLabel:
    horizon_minutes: int
    direction: int
    gross_pts: float
    net_pts: float
    entry_bid: float
    entry_ask: float
    exit_bid: float
    exit_ask: float
    entry_spread_pts: float
    exit_spread_pts: float


def signed_trade_imbalance(ticks: np.ndarray, *, start_ns: int, end_ns: int) -> WindowFeature:
    """Compute signed trade imbalance over the half-open [start_ns, end_ns) window."""
    mask = (ticks["exch_ts"] >= start_ns) & (ticks["exch_ts"] < end_ns)
    window = ticks[mask]
    if len(window) == 0:
        return WindowFeature(None, None, 0.0, 0)

    qty = window["qty"].astype(np.float64, copy=False)
    gross_qty = float(np.sum(qty))
    if gross_qty <= 0:
        return WindowFeature(None, None, gross_qty, int(len(window)))

    side = window["side"].astype(np.float64, copy=False)
    signed = float(np.sum(side * qty) / gross_qty)
    ret = float(window["price"][-1] - window["price"][0])
    return WindowFeature(signed, ret, gross_qty, int(len(window)))


def _percentile(values: Sequence[float], q: float) -> float:
    return round(float(np.percentile(np.asarray(values, dtype=np.float64), q)), 10)


def _direction_for_reversal(return_pts: float) -> int:
    if return_pts < 0:
        return 1
    if return_pts > 0:
        return -1
    return 0


def assign_prior_date_branches(
    rows: Sequence[DecisionFeature],
    *,
    min_prior_rows: int = 20,
) -> list[dict[str, Any]]:
    """Assign branches using thresholds computed only from strictly earlier dates."""
    ordered = sorted(rows, key=lambda r: (r.date, r.decision_time_ns, r.contract))
    assigned: list[dict[str, Any]] = []
    for row in ordered:
        prior = [r for r in ordered if r.date < row.date]
        out: dict[str, Any] = asdict(row)
        if len(prior) < min_prior_rows:
            out.update(
                {
                    "branch": "insufficient_prior",
                    "direction": 0,
                    "thresholds": None,
                    "prior_rows": len(prior),
                    "threshold_policy": "strict_prior_dates_only",
                }
            )
            assigned.append(out)
            continue

        thresholds = {
            "imbalance_q10": _percentile([r.signed_imbalance for r in prior], 10),
            "imbalance_q90": _percentile([r.signed_imbalance for r in prior], 90),
            "return_q30": _percentile([r.return_pts for r in prior], 30),
            "return_q70": _percentile([r.return_pts for r in prior], 70),
        }
        if row.signed_imbalance >= thresholds["imbalance_q90"] and row.return_pts >= thresholds["return_q70"]:
            branch = "extreme_high_imbalance_momentum"
            direction = 1
        elif row.signed_imbalance <= thresholds["imbalance_q10"] and row.return_pts <= thresholds["return_q30"]:
            branch = "extreme_low_imbalance_reversal"
            direction = _direction_for_reversal(row.return_pts)
        else:
            branch = "none"
            direction = 0

        out.update(
            {
                "branch": branch,
                "direction": direction,
                "thresholds": thresholds,
                "prior_rows": len(prior),
                "threshold_policy": "strict_prior_dates_only",
            }
        )
        assigned.append(out)
    return assigned


def branch_scorecard(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    branches: dict[str, dict[str, Any]] = {}
    for row in rows:
        branch = str(row["branch"])
        item = branches.setdefault(branch, {"events": 0, "unique_dates": set()})
        item["events"] += 1
        item["unique_dates"].add(str(row.get("date")))

    cleaned = {
        branch: {"events": int(data["events"]), "unique_dates": len(data["unique_dates"])}
        for branch, data in sorted(branches.items())
    }
    candidate_events = sum(
        data["events"]
        for branch, data in cleaned.items()
        if branch not in {"none", "insufficient_prior"}
    )
    return {
        "events": len(rows),
        "candidate_events": int(candidate_events),
        "branches": cleaned,
    }


def _quote_at_or_before(
    quotes: Sequence[BboQuote],
    target_ns: int,
    ts_index: Sequence[int] | None = None,
) -> BboQuote | None:
    if not quotes:
        return None
    timestamps = ts_index if ts_index is not None else [q.ts_ns for q in quotes]
    idx = bisect_right(timestamps, target_ns) - 1
    if idx < 0:
        return None
    quote = quotes[idx]
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask <= quote.bid:
        return None
    return quote


def executable_label_from_quotes(
    quotes: Sequence[BboQuote],
    *,
    decision_time_ns: int,
    direction: int,
    horizon_minutes: int,
    round_trip_cost_pts: float = DEFAULT_TMF_ROUND_TRIP_COST_PTS,
    ts_index: Sequence[int] | None = None,
) -> ExecutableLabel | None:
    if direction == 0:
        return None
    entry = _quote_at_or_before(quotes, decision_time_ns, ts_index)
    exit_quote = _quote_at_or_before(
        quotes,
        decision_time_ns + horizon_minutes * NS_PER_MINUTE,
        ts_index,
    )
    if entry is None or exit_quote is None:
        return None

    if direction > 0:
        gross_pts = exit_quote.bid - entry.ask
    else:
        gross_pts = entry.bid - exit_quote.ask
    net_pts = gross_pts - round_trip_cost_pts
    return ExecutableLabel(
        horizon_minutes=horizon_minutes,
        direction=direction,
        gross_pts=round(float(gross_pts), 10),
        net_pts=round(float(net_pts), 10),
        entry_bid=float(entry.bid),
        entry_ask=float(entry.ask),
        exit_bid=float(exit_quote.bid),
        exit_ask=float(exit_quote.ask),
        entry_spread_pts=round(float(entry.ask - entry.bid), 10),
        exit_spread_pts=round(float(exit_quote.ask - exit_quote.bid), 10),
    )


def branch_label_scorecard(
    rows: Sequence[dict[str, Any]],
    *,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
) -> dict[str, Any]:
    candidate_branches = {"extreme_high_imbalance_momentum", "extreme_low_imbalance_reversal"}
    branches: dict[str, dict[str, Any]] = {}
    candidate_labeled_events = 0
    for row in rows:
        branch = str(row.get("branch"))
        if branch not in candidate_branches:
            continue
        if any(row.get(f"label_{horizon}m_net_pts") is not None for horizon in horizons_minutes):
            candidate_labeled_events += 1
        branch_data = branches.setdefault(branch, {"events": 0, "unique_dates": set(), "horizons": {}})
        branch_data["events"] += 1
        branch_data["unique_dates"].add(str(row.get("date")))
        for horizon in horizons_minutes:
            value = row.get(f"label_{horizon}m_net_pts")
            if value is None:
                continue
            horizon_key = f"{horizon}m"
            branch_data["horizons"].setdefault(horizon_key, []).append(float(value))

    cleaned: dict[str, dict[str, Any]] = {}
    for branch, data in sorted(branches.items()):
        horizons: dict[str, dict[str, Any]] = {}
        for horizon in horizons_minutes:
            horizon_key = f"{horizon}m"
            values = data["horizons"].get(horizon_key, [])
            if not values:
                horizons[horizon_key] = {
                    "events": 0,
                    "mean_net_pts": None,
                    "median_net_pts": None,
                    "positive_ratio": None,
                    "remove_best_mean_net_pts": None,
                }
                continue
            sorted_values = sorted(values)
            remove_best = sorted_values[:-1]
            horizons[horizon_key] = {
                "events": len(values),
                "mean_net_pts": round(float(np.mean(values)), 10),
                "median_net_pts": round(float(np.median(values)), 10),
                "positive_ratio": round(float(sum(v > 0.0 for v in values) / len(values)), 10),
                "remove_best_mean_net_pts": (
                    round(float(np.mean(remove_best)), 10) if len(remove_best) > 0 else None
                ),
            }
        cleaned[branch] = {
            "events": int(data["events"]),
            "unique_dates": len(data["unique_dates"]),
            "horizons": horizons,
        }
    return {
        "candidate_labeled_events": candidate_labeled_events,
        "branches": cleaned,
    }


def _paired_primary_tick_paths(raw_dir: Path, months: Iterable[str]) -> list[tuple[str, str, Path]]:
    paths: list[tuple[str, str, Path]] = []
    for month in months:
        txf = f"TXF{month}"
        tmf = f"TMF{month}"
        txf_dir = raw_dir / txf.lower()
        tmf_dir = raw_dir / tmf.lower()
        l2_txf = {p.name.split("_")[1] for p in txf_dir.glob(f"{txf}_*_l2.hftbt.npz")}
        l2_tmf = {p.name.split("_")[1] for p in tmf_dir.glob(f"{tmf}_*_l2.hftbt.npz")}
        tick_txf = {p.name.split("_")[1] for p in txf_dir.glob(f"{txf}_*_ticks.npy")}
        tick_tmf = {p.name.split("_")[1] for p in tmf_dir.glob(f"{tmf}_*_ticks.npy")}
        for date in sorted(l2_txf & l2_tmf & tick_txf & tick_tmf):
            if date < "2026-03-31":
                continue
            paths.append((txf, date, txf_dir / f"{txf}_{date}_ticks.npy"))
    return paths


def _tmf_l2_path(raw_dir: Path, txf_contract: str, date: str) -> Path:
    tmf_contract = txf_contract.replace("TXF", "TMF", 1)
    return raw_dir / tmf_contract.lower() / f"{tmf_contract}_{date}_l2.hftbt.npz"


def _current_bbo_quote(
    ts_ns: int | None,
    bid_book: dict[float, float],
    ask_book: dict[float, float],
) -> BboQuote | None:
    if ts_ns is None or not bid_book or not ask_book:
        return None
    bid = max(bid_book)
    ask = min(ask_book)
    if ask <= bid:
        return None
    return BboQuote(
        ts_ns=int(ts_ns),
        bid=float(bid),
        ask=float(ask),
        bid_qty=float(bid_book[bid]),
        ask_qty=float(ask_book[ask]),
    )


def _apply_hftbt_depth_row(row: Any, bid_book: dict[float, float], ask_book: dict[float, float]) -> None:
    ev_flags = int(row["ev"])
    ev_type = ev_flags & EV_TYPE_MASK
    if ev_type == DEPTH_CLEAR_EVENT:
        bid_book.clear()
        ask_book.clear()
        return
    if ev_type not in {DEPTH_EVENT, DEPTH_SNAPSHOT_EVENT}:
        return

    px = float(row["px"])
    qty = float(row["qty"])
    if px <= 0.0:
        return
    book = bid_book if (ev_flags & BUY_EVENT) else (ask_book if (ev_flags & SELL_EVENT) else None)
    if book is None:
        return
    if qty <= 0.0:
        book.pop(px, None)
    else:
        book[px] = qty


def load_bbo_quotes_from_hftbt_npz(path: Path) -> list[BboQuote]:
    data = np.load(path, mmap_mode="r", allow_pickle=False)["data"]
    bid_book: dict[float, float] = {}
    ask_book: dict[float, float] = {}
    quotes: list[BboQuote] = []
    last_ts: int | None = None

    def append_quote(ts_ns: int | None) -> None:
        quote = _current_bbo_quote(ts_ns, bid_book, ask_book)
        if quote is not None:
            quotes.append(quote)

    for row in data:
        ts = int(row["exch_ts"])
        if last_ts is not None and ts != last_ts:
            append_quote(last_ts)
        _apply_hftbt_depth_row(row, bid_book, ask_book)
        last_ts = ts
    append_quote(last_ts)
    return quotes


def _fill_target_quotes(
    targets: Sequence[int],
    target_idx: int,
    *,
    before_ts: int | None,
    quote: BboQuote | None,
    out: dict[int, BboQuote],
) -> int:
    while target_idx < len(targets) and (before_ts is None or targets[target_idx] < before_ts):
        if quote is not None and targets[target_idx] >= quote.ts_ns:
            out[targets[target_idx]] = quote
        target_idx += 1
    return target_idx


def _bbo_quote_from_hftbt_group(group: np.ndarray, ts_ns: int) -> BboQuote | None:
    ev_types = group["ev"] & EV_TYPE_MASK
    depth = group[(ev_types == DEPTH_EVENT) | (ev_types == DEPTH_SNAPSHOT_EVENT)]
    if len(depth) == 0:
        return None

    bids = depth[((depth["ev"] & BUY_EVENT) > 0) & (depth["px"] > 0.0) & (depth["qty"] > 0.0)]
    asks = depth[((depth["ev"] & SELL_EVENT) > 0) & (depth["px"] > 0.0) & (depth["qty"] > 0.0)]
    if len(bids) == 0 or len(asks) == 0:
        return None

    bid_idx = int(np.argmax(bids["px"]))
    ask_idx = int(np.argmin(asks["px"]))
    bid = float(bids["px"][bid_idx])
    ask = float(asks["px"][ask_idx])
    if ask <= bid:
        return None
    return BboQuote(
        ts_ns=int(ts_ns),
        bid=bid,
        ask=ask,
        bid_qty=float(bids["qty"][bid_idx]),
        ask_qty=float(asks["qty"][ask_idx]),
    )


def load_target_bbo_quotes_from_hftbt_npz(path: Path, *, target_ts_ns: Iterable[int]) -> dict[int, BboQuote]:
    targets = sorted({int(ts) for ts in target_ts_ns})
    if not targets:
        return {}

    data = np.load(path, mmap_mode="r", allow_pickle=False)["data"]
    timestamps = data["exch_ts"]
    quotes: dict[int, BboQuote] = {}
    for target in targets:
        idx = int(np.searchsorted(timestamps, target, side="right") - 1)
        if idx < 0:
            continue
        quote_ts = int(timestamps[idx])
        lo = int(np.searchsorted(timestamps, quote_ts, side="left"))
        hi = int(np.searchsorted(timestamps, quote_ts, side="right"))
        quote = _bbo_quote_from_hftbt_group(data[lo:hi], quote_ts)
        if quote is not None:
            quotes[target] = quote
    return quotes


def compute_decision_features(
    raw_dir: Path,
    *,
    months: Sequence[str] = PRIMARY_MONTHS,
    window_minutes: int = 15,
    step_minutes: int = 15,
    min_ticks_per_window: int = 20,
    latest_entry_horizon_minutes: int = 30,
) -> list[DecisionFeature]:
    features: list[DecisionFeature] = []
    window_ns = window_minutes * NS_PER_MINUTE
    step_ns = step_minutes * NS_PER_MINUTE
    for contract, date, tick_path in _paired_primary_tick_paths(raw_dir, months):
        ticks = np.load(tick_path, mmap_mode="r", allow_pickle=False)
        session_start = _session_start_ns(date, tz_offset_hours=8)
        first_decision = session_start + window_ns
        last_decision = session_start + (285 - latest_entry_horizon_minutes) * NS_PER_MINUTE
        for decision_ns in range(first_decision, last_decision + 1, step_ns):
            feature = signed_trade_imbalance(ticks, start_ns=decision_ns - window_ns, end_ns=decision_ns)
            if (
                feature.signed_imbalance is None
                or feature.return_pts is None
                or feature.tick_count < min_ticks_per_window
            ):
                continue
            features.append(
                DecisionFeature(
                    contract=contract,
                    date=date,
                    decision_time_ns=decision_ns,
                    signed_imbalance=feature.signed_imbalance,
                    return_pts=feature.return_pts,
                    gross_qty=feature.gross_qty,
                    tick_count=feature.tick_count,
                )
            )
    return features


def attach_executable_labels(
    raw_dir: Path,
    rows: Sequence[dict[str, Any]],
    *,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
    round_trip_cost_pts: float = DEFAULT_TMF_ROUND_TRIP_COST_PTS,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((str(row["contract"]), str(row["date"])), []).append(dict(row))

    labeled: list[dict[str, Any]] = []
    for (contract, date), group_rows in grouped.items():
        l2_path = _tmf_l2_path(raw_dir, contract, date)
        target_ts: set[int] = set()
        for row in group_rows:
            if row.get("branch") not in {"extreme_high_imbalance_momentum", "extreme_low_imbalance_reversal"}:
                continue
            decision_time_ns = int(row["decision_time_ns"])
            target_ts.add(decision_time_ns)
            target_ts.update(decision_time_ns + int(horizon) * NS_PER_MINUTE for horizon in horizons_minutes)
        quote_map = load_target_bbo_quotes_from_hftbt_npz(l2_path, target_ts_ns=target_ts) if l2_path.exists() else {}
        for row in group_rows:
            out = dict(row)
            out["label_source"] = str(l2_path)
            out["label_status"] = "not_candidate"
            if row.get("branch") in {"extreme_high_imbalance_momentum", "extreme_low_imbalance_reversal"}:
                out["label_status"] = "missing_tmf_bbo"
                labels = []
                decision_time_ns = int(row["decision_time_ns"])
                entry_quote = quote_map.get(decision_time_ns)
                for horizon in horizons_minutes:
                    exit_quote = quote_map.get(decision_time_ns + int(horizon) * NS_PER_MINUTE)
                    label = None
                    if entry_quote is not None and exit_quote is not None:
                        label = executable_label_from_quotes(
                            [entry_quote, exit_quote],
                            decision_time_ns=decision_time_ns,
                            direction=int(row["direction"]),
                            horizon_minutes=int(horizon),
                            round_trip_cost_pts=round_trip_cost_pts,
                            ts_index=[entry_quote.ts_ns, exit_quote.ts_ns],
                        )
                    if label is None:
                        continue
                    labels.append(label)
                    prefix = f"label_{horizon}m"
                    out[f"{prefix}_gross_pts"] = label.gross_pts
                    out[f"{prefix}_net_pts"] = label.net_pts
                    out[f"{prefix}_entry_spread_pts"] = label.entry_spread_pts
                    out[f"{prefix}_exit_spread_pts"] = label.exit_spread_pts
                if len(labels) == len(horizons_minutes):
                    out["label_status"] = "labeled"
                elif labels:
                    out["label_status"] = "partially_labeled"
            labeled.append(out)
    return sorted(labeled, key=lambda r: (str(r["date"]), int(r["decision_time_ns"]), str(r["contract"])))


def build_report(
    *,
    raw_dir: Path = DEFAULT_RAW_DIR,
    months: Sequence[str] = PRIMARY_MONTHS,
    min_prior_rows: int = 20,
    label_executable: bool = False,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
    round_trip_cost_pts: float = DEFAULT_TMF_ROUND_TRIP_COST_PTS,
) -> dict[str, Any]:
    features = compute_decision_features(raw_dir, months=months)
    assigned = assign_prior_date_branches(features, min_prior_rows=min_prior_rows)
    if label_executable:
        assigned = attach_executable_labels(
            raw_dir,
            assigned,
            horizons_minutes=horizons_minutes,
            round_trip_cost_pts=round_trip_cost_pts,
        )
    by_contract: dict[str, int] = {}
    for row in assigned:
        contract = str(row["contract"])
        by_contract[contract] = by_contract.get(contract, 0) + 1
    report = {
        "schema": "research.t1g_extreme_imbalance_feasibility.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate": "t1g_txf_extreme_imbalance_reversal_momentum_v0",
        "diagnostic_type": (
            "backfill_evidence_read_only_labeled" if label_executable else "backfill_evidence_read_only_no_trade"
        ),
        "raw_dir": str(raw_dir),
        "months": list(months),
        "feature_contract": {
            "window_minutes": 15,
            "step_minutes": 15,
            "min_ticks_per_window": 20,
            "threshold_policy": "strict_prior_dates_only",
            "min_prior_rows": min_prior_rows,
            "labels_computed": label_executable,
            "orders_created": False,
        },
        "coverage": {
            "decision_rows": len(assigned),
            "unique_dates": len({str(row["date"]) for row in assigned}),
            "rows_by_contract": by_contract,
            "labeled_candidate_rows": sum(1 for row in assigned if row.get("label_status") == "labeled"),
        },
        "scorecard": branch_scorecard(assigned),
        "sample_rows": assigned[:10],
        "full_rows": assigned,
        "inference_policy": (
            "executable_label_diagnostic_only_no_promotion"
            if label_executable
            else "feature_feasibility_only_no_edge_claim_no_promotion"
        ),
        "production_behavior_changed": False,
        "cost_model_changed": False,
    }
    if label_executable:
        report["label_contract"] = {
            "execution_instrument": "TMF",
            "horizons_minutes": list(horizons_minutes),
            "round_trip_cost_pts": round_trip_cost_pts,
            "long_execution": "entry_buy_at_tmf_ask_exit_sell_at_tmf_bid",
            "short_execution": "entry_sell_at_tmf_bid_exit_buy_at_tmf_ask",
            "quote_policy": "latest_valid_tmf_bbo_at_or_before_decision_and_horizon",
        }
        report["label_scorecard"] = branch_label_scorecard(assigned, horizons_minutes=horizons_minutes)
    return report


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--months", default="D6,E6")
    parser.add_argument("--min-prior-rows", type=int, default=20)
    parser.add_argument("--label-executable", action="store_true")
    parser.add_argument("--horizons-minutes", default="5,15,30")
    parser.add_argument("--round-trip-cost-pts", type=float, default=DEFAULT_TMF_ROUND_TRIP_COST_PTS)
    parser.add_argument("--output-name", default="feasibility_diagnostic.json")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    horizons = tuple(int(v.strip()) for v in args.horizons_minutes.split(",") if v.strip())
    report = build_report(
        raw_dir=Path(args.raw_dir),
        months=tuple(m.strip() for m in args.months.split(",") if m.strip()),
        min_prior_rows=args.min_prior_rows,
        label_executable=args.label_executable,
        horizons_minutes=horizons,
        round_trip_cost_pts=args.round_trip_cost_pts,
    )
    out_path = out_dir / args.output_name
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "full_rows": f"{len(report['full_rows'])} rows omitted from stdout"}, indent=2))


if __name__ == "__main__":
    main()
