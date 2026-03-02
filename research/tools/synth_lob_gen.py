from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(slots=True)
class SyntheticLOBConfig:
    n_rows: int = 20_000
    rng_seed: int = 42
    tick_interval_ms: float = 2.0
    regime_block_min: int = 200
    regime_block_max: int = 500
    regimes: tuple[str, ...] = ("trending", "mean_reverting", "volatile")
    spread_mean_bps: float = 5.0
    queue_ar_coef: float = 0.92
    generator_version: str = "v1"


_DTYPE = np.dtype(
    [
        ("bid_qty", "f8"),
        ("ask_qty", "f8"),
        ("bid_px", "f8"),
        ("ask_px", "f8"),
        ("mid_price", "f8"),
        ("spread_bps", "f8"),
        ("volume", "f8"),
        ("local_ts", "i8"),
    ]
)


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def generate_lob_data(config: SyntheticLOBConfig) -> tuple[np.ndarray, dict[str, Any]]:
    n_rows = max(1, int(config.n_rows))
    rng = np.random.default_rng(int(config.rng_seed))
    arr = np.zeros(n_rows, dtype=_DTYPE)

    tick_ns = int(max(1.0, float(config.tick_interval_ms)) * 1_000_000.0)
    base_mid = 100.0
    tick_size = 0.01
    q = 0.0

    i = 0
    regimes_covered: set[str] = set()
    regime_to_idx = {name: idx for idx, name in enumerate(config.regimes)}
    if not regime_to_idx:
        regime_to_idx = {"trending": 0, "mean_reverting": 1, "volatile": 2}
    regime_names = tuple(regime_to_idx.keys())

    while i < n_rows:
        block = int(rng.integers(max(1, config.regime_block_min), max(config.regime_block_max, 1) + 1))
        regime = regime_names[int(rng.integers(0, len(regime_names)))]
        regimes_covered.add(regime)
        trend_dir = float(rng.choice(np.asarray([-1.0, 1.0], dtype=np.float64)))

        end = min(n_rows, i + block)
        for row_idx in range(i, end):
            eps_q = float(rng.normal(0.0, 0.03))
            if regime == "trending":
                regime_drift = 0.02 * trend_dir
                spread_mult = 1.0
                vol_scale = 0.8
            elif regime == "mean_reverting":
                regime_drift = -0.10 * q
                spread_mult = 0.8
                vol_scale = 0.6
            else:
                regime_drift = float(rng.normal(0.0, 0.015))
                spread_mult = 2.0
                vol_scale = 1.6

            q = (float(config.queue_ar_coef) * q) + regime_drift + eps_q
            q = _clip(q, -0.99, 0.99)

            px_noise = float(rng.normal(0.0, 0.03 * vol_scale))
            impact = 0.08 * q
            base_mid = max(1.0, base_mid + (impact + px_noise) * tick_size)

            spread_noise = 1.0 + abs(q) * 0.25 + abs(float(rng.normal(0.0, 0.08)))
            spread_bps = max(0.5, float(config.spread_mean_bps) * spread_mult * spread_noise)
            half_spread = (base_mid * spread_bps / 10_000.0) / 2.0
            bid_px = max(0.01, base_mid - half_spread)
            ask_px = max(bid_px + 0.0001, base_mid + half_spread)

            total_depth = max(10.0, 1800.0 * (1.0 + 0.35 * abs(q) + abs(float(rng.normal(0.0, 0.12)))))
            bid_qty = max(1.0, total_depth * (1.0 + q) / 2.0)
            ask_qty = max(1.0, total_depth * (1.0 - q) / 2.0)

            volume = max(1.0, float(rng.gamma(shape=2.0, scale=4.0 * (1.0 + vol_scale))))

            arr[row_idx]["bid_qty"] = bid_qty
            arr[row_idx]["ask_qty"] = ask_qty
            arr[row_idx]["bid_px"] = bid_px
            arr[row_idx]["ask_px"] = ask_px
            arr[row_idx]["mid_price"] = (bid_px + ask_px) / 2.0
            arr[row_idx]["spread_bps"] = spread_bps
            arr[row_idx]["volume"] = volume
            arr[row_idx]["local_ts"] = row_idx * tick_ns

        i = end

    digest = hashlib.sha256(arr.tobytes()[:1024]).hexdigest()
    params = asdict(config)
    params["regimes"] = list(config.regimes)

    meta: dict[str, Any] = {
        "dataset_id": f"synthetic_lob_{config.generator_version}_seed{int(config.rng_seed)}",
        "source_type": "synthetic",
        "source": "synthetic_lob_gen",
        "owner": "research",
        "schema_version": 1,
        "rows": int(arr.shape[0]),
        "fields": [str(name) for name in arr.dtype.names or ()],
        "rng_seed": int(config.rng_seed),
        "generator_script": "research/tools/synth_lob_gen.py",
        "generator_version": str(config.generator_version),
        "parameters": params,
        "regimes_covered": sorted(regimes_covered),
        "data_fingerprint": digest,
        "lineage": {
            "parent": None,
            "derived_from": f"SyntheticLOBConfig(rng_seed={int(config.rng_seed)})",
        },
        "data_ul": 5,
        "created_at": datetime.now(UTC).isoformat(),
    }
    return arr, meta


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate realistic synthetic LOB data with UL5 metadata.")
    parser.add_argument("--out", required=True, help="Output .npy path")
    parser.add_argument("--meta-out", default=None, help="Optional metadata output path")
    parser.add_argument("--n-rows", type=int, default=20_000)
    parser.add_argument("--rng-seed", type=int, default=42)
    parser.add_argument("--tick-interval-ms", type=float, default=2.0)
    parser.add_argument("--regime-block-min", type=int, default=200)
    parser.add_argument("--regime-block-max", type=int, default=500)
    parser.add_argument("--spread-mean-bps", type=float, default=5.0)
    parser.add_argument("--queue-ar-coef", type=float, default=0.92)
    parser.add_argument("--generator-version", default="v1")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = SyntheticLOBConfig(
        n_rows=int(args.n_rows),
        rng_seed=int(args.rng_seed),
        tick_interval_ms=float(args.tick_interval_ms),
        regime_block_min=int(args.regime_block_min),
        regime_block_max=int(args.regime_block_max),
        spread_mean_bps=float(args.spread_mean_bps),
        queue_ar_coef=float(args.queue_ar_coef),
        generator_version=str(args.generator_version),
    )
    arr, meta = generate_lob_data(cfg)

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(out_path, arr)

    meta_path = Path(args.meta_out).resolve() if args.meta_out else out_path.with_suffix(out_path.suffix + ".meta.json")
    meta["data_file"] = str(out_path)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    print(f"[synth_lob_gen] wrote data: {out_path}")
    print(f"[synth_lob_gen] wrote meta: {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

