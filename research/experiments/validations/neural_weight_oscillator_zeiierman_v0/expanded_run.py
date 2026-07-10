"""Reproduce the expanded TXF primary and TMF transfer evaluation."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.bars import build_bars

from .backtest import (
    build_front_month_chain,
    evaluate_markets,
    render_expanded_markdown,
)
from .direct_db_bars import (
    load_bars,
    load_docker_day_bars,
    merge_contract_bars,
    save_bars,
)

F6_DB_DATES = (
    "2026-05-21",
    "2026-05-22",
    "2026-05-25",
    "2026-05-26",
    "2026-05-27",
    "2026-05-28",
    "2026-05-29",
    "2026-06-01",
    "2026-06-02",
    "2026-06-03",
    "2026-06-04",
)
MARKETS = {"txf": "TXFF6", "tmf": "TMFF6"}


def _artifact_path(artifact_dir: Path, market: str) -> Path:
    return artifact_dir / f"neural_weight_oscillator_{market}_f6_incremental_bars.npz"


def extract_incremental_bars(
    *,
    artifact_dir: Path,
    bar_min: int,
) -> dict[str, dict[str, object]]:
    """Extract frozen missing dates through the native container client."""
    summary: dict[str, dict[str, object]] = {}
    for market, symbol in MARKETS.items():
        parts = []
        included_dates = []
        excluded_dates = []
        for date in F6_DB_DATES:
            bars = load_docker_day_bars(
                symbol=symbol,
                date=date,
                bar_min=bar_min,
            )
            if len(bars.date) == 0:
                excluded_dates.append({"date": date, "reason": "frozen_quality_gate"})
                print(
                    json.dumps(
                        {"market": market, "date": date, "status": "excluded_by_quality_gate"}
                    ),
                    flush=True,
                )
                continue
            parts.append(bars)
            included_dates.append(date)
            print(json.dumps({"market": market, "date": date, "bars": len(bars.date)}), flush=True)
        if not parts:
            raise RuntimeError(f"no valid F6 bars reconstructed for {symbol}")
        combined = merge_contract_bars(parts)
        path = _artifact_path(artifact_dir, market)
        save_bars(path, combined)
        summary[market] = {
            "symbol": symbol,
            "requested_dates": list(F6_DB_DATES),
            "included_dates": included_dates,
            "excluded_dates": excluded_dates,
            "bars": int(len(combined.date)),
            "artifact": str(path),
            "bbo_valid": int(
                ((combined.bid_open > 0) & (combined.ask_open >= combined.bid_open)).sum()
            ),
        }
    metadata_path = artifact_dir / "neural_weight_oscillator_incremental_bars_meta.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema": "research.neural_weight_oscillator.incremental_bars.v1",
                "generated_at": datetime.now(UTC).isoformat(),
                "source": "hft.market_data",
                "source_access": "read_only_clickhouse_aggregated_tick_and_asof_bbo",
                "extract_session_tpe": "08:30-14:00",
                "research_session_tpe": "08:45-13:30",
                "governance": "expanded_retrospective_oos",
                "markets": summary,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def evaluate_expanded(
    *,
    raw_dir: Path,
    artifact_dir: Path,
    bar_min: int,
    output: Path,
    report: Path,
) -> dict[str, object]:
    """Combine canonical history with incremental bars and evaluate both markets."""
    chains = {}
    overlap_parity = {}
    actual_f6_dates = {}
    for market in MARKETS:
        contract_inputs = {
            suffix: build_bars(raw_dir, f"{market}{suffix}", bar_min, session="day")
            for suffix in ("b6", "c6", "d6", "e6", "f6")
        }
        canonical_f6 = contract_inputs["f6"]
        db_f6 = load_bars(_artifact_path(artifact_dir, market))
        actual_f6_dates[market] = sorted(set(str(date) for date in db_f6.date))
        common_dates = sorted(set(canonical_f6.date) & set(db_f6.date))
        field_diffs = {}
        for field in ("open", "high", "low", "close", "volume", "bid_open", "ask_open"):
            canonical_values = []
            db_values = []
            for date in common_dates:
                canonical_values.extend(getattr(canonical_f6, field)[canonical_f6.date == date])
                db_values.extend(getattr(db_f6, field)[db_f6.date == date])
            if len(canonical_values) != len(db_values):
                field_diffs[field] = {"comparable": False}
                continue
            delta = np.abs(np.asarray(canonical_values) - np.asarray(db_values))
            field_diffs[field] = {
                "comparable": True,
                "max_abs": float(np.nanmax(delta)) if delta.size else None,
                "allclose": bool(np.allclose(canonical_values, db_values, equal_nan=True)),
            }
        overlap_parity[market] = {"dates": common_dates, "fields": field_diffs}
        contract_inputs["f6"] = db_f6
        chains[market] = build_front_month_chain(contract_inputs)

    result = evaluate_markets(chains)
    result["economic_primary_verdict"] = result["primary_verdict"]
    result["economic_transfer_verdict"] = result["transfer_verdict"]
    result["primary_verdict"] = "not_promotable_source_snapshot_break"
    result["transfer_verdict"] = "diagnostic_only_source_snapshot_break"
    result["generated_at"] = datetime.now(UTC).isoformat()
    result["data_sources"] = {
        "canonical_history": str(raw_dir),
        "incremental_f6": {
            "dates": list(F6_DB_DATES),
            "actual_dates": actual_f6_dates,
            "method": "read_only_clickhouse_aggregated_tick_and_asof_bbo_reconstruction",
            "artifacts": {
                market: str(_artifact_path(artifact_dir, market)) for market in MARKETS
            },
            "canonical_overlap_parity": overlap_parity,
            "parity_status": "source_snapshot_break",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    report.write_text(render_expanded_markdown(result), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bar-min", type=int, default=5)
    parser.add_argument("--raw-dir", type=Path, default=Path("research/data/raw"))
    parser.add_argument("--artifact-dir", type=Path, default=Path("reports/codex"))
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "research/experiments/validations/neural_weight_oscillator_zeiierman_v0/"
            "result_expanded_txf_tmf_5m_day.json"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/codex/neural_weight_oscillator_expanded_txf_tmf_report.md"),
    )
    args = parser.parse_args()
    if not args.skip_extract:
        extract_incremental_bars(
            artifact_dir=args.artifact_dir,
            bar_min=args.bar_min,
        )
    result = evaluate_expanded(
        raw_dir=args.raw_dir,
        artifact_dir=args.artifact_dir,
        bar_min=args.bar_min,
        output=args.output,
        report=args.report,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "report": str(args.report),
                "primary_verdict": result["primary_verdict"],
                "transfer_verdict": result["transfer_verdict"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
