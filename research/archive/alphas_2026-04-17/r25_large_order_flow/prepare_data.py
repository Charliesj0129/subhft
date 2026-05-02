"""R25 data preparation: precompute FeatureEngine v3 features from L1 data.

Loads TMFD6 L1 .npy files, feeds each event through FeatureEngine to produce
lob_shared_v3 feature tuples (27 slots), and saves pre-computed features as
structured .npy files for efficient backtest replay.

Also reports sweep event frequency as a data sanity check: how many times
does mid_price_x2 move >= 2 ticks in the same direction within 5 events?

Usage:
    uv run python -m research.alphas.r25_large_order_flow.prepare_data

Output:
    research/data/processed/r25/{TMFD6_YYYY-MM-DD_fe_v3.npy}
    research/data/processed/r25/{TMFD6_YYYY-MM-DD_fe_v3.npy.meta.json}
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import structlog

_HERE = Path(__file__).resolve().parent
_RESEARCH_ROOT = _HERE.parent.parent
_PROJECT_ROOT = _RESEARCH_ROOT.parent
for _p in (_PROJECT_ROOT, _RESEARCH_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from hft_platform.feature.engine import FeatureEngine

logger = structlog.get_logger("r25.prepare_data")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_L1_DIR = _RESEARCH_ROOT / "data" / "raw" / "tmfd6"
_OUT_DIR = _RESEARCH_ROOT / "data" / "processed" / "r25"

# Feature output dtype — 27 features from lob_shared_v3 + timestamp
_N_FEATURES = 27
FE_V3_DTYPE = np.dtype(
    [
        ("ts_ns", "i8"),
        ("features", "i8", (_N_FEATURES,)),
    ]
)

# L1 input dtype (matches ch_batch_export L1_DTYPE)
L1_DTYPE = np.dtype(
    [
        ("bid_px", "f8"),
        ("ask_px", "f8"),
        ("bid_qty", "f8"),
        ("ask_qty", "f8"),
        ("mid_price", "f8"),
        ("spread_bps", "f8"),
        ("volume", "f8"),
        ("local_ts", "i8"),
    ]
)

# Price scale: L1 data has float prices (NTD), FeatureEngine needs x10000 int
_PRICE_TO_X10000 = 10_000


def _l1_to_lob_stats_tuple(
    row: np.void,
    symbol: str = "TMFD6",
) -> tuple:
    """Convert L1 row to LOBStatsEvent-compatible tuple.

    Tuple layout (tagged):
        ("lobstats", symbol, ts, mid_price_x2, spread_scaled, imbalance,
         best_bid, best_ask, bid_depth, ask_depth)
    """
    bid_px = float(row["bid_px"])
    ask_px = float(row["ask_px"])
    bid_qty = float(row["bid_qty"])
    ask_qty = float(row["ask_qty"])
    ts = int(row["local_ts"])

    best_bid = int(round(bid_px * _PRICE_TO_X10000))
    best_ask = int(round(ask_px * _PRICE_TO_X10000))
    mid_price_x2 = best_bid + best_ask
    spread_scaled = best_ask - best_bid

    total_qty = bid_qty + ask_qty
    imbalance = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0

    return (
        "lobstats",
        symbol,
        ts,
        mid_price_x2,
        spread_scaled,
        imbalance,
        best_bid,
        best_ask,
        int(bid_qty),
        int(ask_qty),
    )


def _count_sweeps(
    features: np.ndarray,
    sweep_min_ticks: int = 2,
    sweep_max_events: int = 5,
    ofi_threshold: int = 50,
) -> dict:
    """Count sweep events in pre-computed feature data.

    Returns statistics about sweep frequency for feasibility assessment.
    """
    n = len(features)
    if n == 0:
        return {"total_events": 0, "sweeps": 0, "confirmed": 0}

    tick_x10000 = 10_000  # 1 tick in mid_price_x2 units
    sweep_count = 0
    confirmed_count = 0

    cum_delta = 0
    event_count = 0
    direction = 0
    prev_mid_x2 = 0

    for i in range(n):
        feat = features[i]["features"]
        mid_x2 = int(feat[2])  # _FE_MID_PRICE_X2
        ofi_ema5s = int(feat[22])  # _FE_OFI_EMA5S

        if i == 0:
            prev_mid_x2 = mid_x2
            continue

        delta = mid_x2 - prev_mid_x2
        prev_mid_x2 = mid_x2

        if delta == 0:
            if event_count > 0:
                event_count += 1
                if event_count > sweep_max_events:
                    cum_delta = 0
                    event_count = 0
                    direction = 0
            continue

        move_dir = 1 if delta > 0 else -1

        if direction == 0 or move_dir == direction:
            direction = move_dir
            cum_delta += delta
            event_count += 1
        else:
            cum_delta = delta
            event_count = 1
            direction = move_dir

        if event_count > sweep_max_events:
            cum_delta = delta
            event_count = 1
            direction = move_dir

        sweep_ticks = abs(cum_delta) // tick_x10000
        if sweep_ticks >= sweep_min_ticks:
            sweep_count += 1
            # Check OFI confirmation
            ofi_same_sign = (direction > 0 and ofi_ema5s > 0) or (direction < 0 and ofi_ema5s < 0)
            if ofi_same_sign and abs(ofi_ema5s) >= ofi_threshold:
                confirmed_count += 1

            # Reset after detection
            cum_delta = 0
            event_count = 0
            direction = 0

    return {
        "total_events": n,
        "sweeps": sweep_count,
        "confirmed": confirmed_count,
        "sweep_rate_per_1k": round(sweep_count / max(n, 1) * 1000, 2),
        "confirmed_rate_per_1k": round(confirmed_count / max(n, 1) * 1000, 2),
    }


def process_file(l1_path: Path, out_dir: Path) -> dict | None:
    """Process one L1 .npy file through FeatureEngine v3."""
    stem = l1_path.stem  # e.g., "TMFD6_2026-03-26_l1"
    date_str = stem.split("_")[1]  # e.g., "2026-03-26"
    out_name = f"TMFD6_{date_str}_fe_v3"
    out_path = out_dir / f"{out_name}.npy"
    meta_path = out_dir / f"{out_name}.npy.meta.json"

    if out_path.exists():
        logger.info("skipping_existing", path=str(out_path))
        # Still load and return sweep stats
        data = np.load(str(out_path))
        sweep_stats = _count_sweeps(data)
        sweep_stats["date"] = date_str
        sweep_stats["status"] = "cached"
        return sweep_stats

    logger.info("processing", file=l1_path.name, date=date_str)

    l1_data = np.load(str(l1_path))
    n = len(l1_data)
    if n == 0:
        logger.warning("empty_file", path=str(l1_path))
        return None

    symbol = "TMFD6"

    # Initialize FeatureEngine — emit_events=False to avoid allocation,
    # read computed state via get_feature_tuple() after each process call.
    fe = FeatureEngine(feature_set_id="lob_shared_v3", emit_events=False)

    # Pre-allocate output
    output = np.zeros(n, dtype=FE_V3_DTYPE)

    for i in range(n):
        row = l1_data[i]
        ts_ns = int(row["local_ts"])
        stats_tuple = _l1_to_lob_stats_tuple(row, symbol=symbol)

        fe.process_lob_stats(stats_tuple, local_ts_ns=ts_ns)

        values = fe.get_feature_tuple(symbol)
        output[i]["ts_ns"] = ts_ns
        if values is not None:
            for j in range(min(len(values), _N_FEATURES)):
                output[i]["features"][j] = int(values[j])

    # Save
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(str(out_path), output)

    # Compute fingerprint
    h = hashlib.sha256(output.tobytes()).hexdigest()

    # Write metadata
    meta = {
        "dataset_id": out_name,
        "source_type": "derived",
        "source": str(l1_path),
        "generator": "r25_large_order_flow.prepare_data",
        "schema_version": 3,
        "rows": n,
        "feature_set": "lob_shared_v3",
        "n_features": _N_FEATURES,
        "symbols": ["TMFD6"],
        "date": date_str,
        "data_fingerprint": h,
        "data_ul": 5,
        "created_at": datetime.now(UTC).isoformat(),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    # Sweep statistics
    sweep_stats = _count_sweeps(output)
    sweep_stats["date"] = date_str
    sweep_stats["status"] = "new"

    logger.info(
        "processed",
        date=date_str,
        rows=n,
        sweeps=sweep_stats["sweeps"],
        confirmed=sweep_stats["confirmed"],
    )

    return sweep_stats


def main() -> None:
    """Process all TMFD6 L1 files and report sweep frequency."""
    l1_files = sorted(_L1_DIR.glob("TMFD6_*_l1.npy"))

    if not l1_files:
        logger.error("no_l1_files", dir=str(_L1_DIR))
        return

    logger.info("found_files", count=len(l1_files))

    all_stats: list[dict] = []
    for f in l1_files:
        stats = process_file(f, _OUT_DIR)
        if stats is not None:
            all_stats.append(stats)

    # Summary report
    total_events = sum(s["total_events"] for s in all_stats)
    total_sweeps = sum(s["sweeps"] for s in all_stats)
    total_confirmed = sum(s["confirmed"] for s in all_stats)
    n_days = len(all_stats)

    logger.info(
        "sweep_frequency_report",
        days=n_days,
        total_events=total_events,
        total_sweeps=total_sweeps,
        total_confirmed=total_confirmed,
        sweeps_per_day=round(total_sweeps / max(n_days, 1), 1),
        confirmed_per_day=round(total_confirmed / max(n_days, 1), 1),
        sweep_rate_per_1k=round(total_sweeps / max(total_events, 1) * 1000, 2),
        confirmed_rate_per_1k=round(total_confirmed / max(total_events, 1) * 1000, 2),
    )

    # Per-day breakdown
    for s in all_stats:
        logger.info(
            "per_day",
            date=s["date"],
            events=s["total_events"],
            sweeps=s["sweeps"],
            confirmed=s["confirmed"],
            status=s.get("status", ""),
        )

    # Save summary
    summary_path = _OUT_DIR / "r25_data_summary.json"
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "alpha_id": "r25_large_order_flow",
        "instrument": "TMFD6",
        "data_source": "research/data/raw/tmfd6/",
        "feature_set": "lob_shared_v3",
        "n_days": n_days,
        "total_events": total_events,
        "total_sweeps": total_sweeps,
        "total_confirmed": total_confirmed,
        "sweeps_per_day": round(total_sweeps / max(n_days, 1), 1),
        "confirmed_per_day": round(total_confirmed / max(n_days, 1), 1),
        "per_day": all_stats,
        "created_at": datetime.now(UTC).isoformat(),
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    logger.info("summary_saved", path=str(summary_path))


if __name__ == "__main__":
    main()
