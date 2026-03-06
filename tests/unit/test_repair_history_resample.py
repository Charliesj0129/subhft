from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "repair_history_resample.py"
    spec = importlib.util.spec_from_file_location("repair_history_resample", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parser_accepts_repeatable_inputs() -> None:
    mod = _load_module()
    parser = mod.build_parser()
    args = parser.parse_args(
        [
            "--input",
            "data/a.parquet",
            "--input",
            "data/b.parquet.part",
            "--out",
            "outputs/x.parquet",
        ]
    )
    assert args.inputs == ["data/a.parquet", "data/b.parquet.part"]
    assert args.target_ms == 1000


def test_run_repairs_missing_bars_and_emits_report(tmp_path: Path) -> None:
    mod = _load_module()

    # 0s,1s,4s,5s => missing bars at 2s,3s
    ts_base = 1_700_000_000_000_000_000
    ts = np.array(
        [
            ts_base + 0 * 1_000_000_000,
            ts_base + 1 * 1_000_000_000,
            ts_base + 4 * 1_000_000_000,
            ts_base + 5 * 1_000_000_000,
        ],
        dtype=np.int64,
    )
    px = np.array([100_000, 100_200, 100_800, 101_000], dtype=np.int64)
    vol = np.array([10, 12, 15, 11], dtype=np.int64)

    raw = pl.DataFrame(
        {
            "symbol": ["TXF"] * ts.size,
            "exchange": ["FUT"] * ts.size,
            "exch_ts": ts,
            "price_scaled": px,
            "volume": vol,
        }
    )

    in_path = tmp_path / "fragment.parquet"
    out_path = tmp_path / "repaired.parquet"
    report_path = tmp_path / "repaired_report.json"
    raw.write_parquet(in_path)

    cfg = mod.RepairConfig(
        input_paths=(in_path,),
        output_path=out_path,
        report_path=report_path,
        target_ms=1000,
        cv_ratio=0.2,
        harmonics=3,
        seed=7,
    )

    out_file, rep_file = mod.run(cfg)
    assert out_file == out_path
    assert rep_file == report_path
    assert out_path.exists()
    assert report_path.exists()

    repaired = pl.read_parquet(out_path).sort("bar_ts")
    assert repaired.height == 6
    assert repaired["is_imputed"].sum() >= 2

    # Ensure full 1-second grid.
    diffs = np.diff(repaired["bar_ts"].to_numpy())
    assert np.all(diffs == 1_000_000_000)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["rows_output"] == 6
    assert len(payload["groups"]) == 1
    g = payload["groups"][0]
    assert set(g["methods_considered"]) == {"linear", "pchip", "kalman", "ar1", "harmonic"}
    assert set(g["close_method_weights"].keys()) == {"linear", "pchip", "kalman", "ar1", "harmonic"}
