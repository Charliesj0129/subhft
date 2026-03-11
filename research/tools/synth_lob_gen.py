from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

_META_SOURCE = "synthetic_lob_gen"
_META_GENERATOR = "synth_lob_gen"


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


def _normalize_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for token in str(raw).split(","):
        sym = token.strip()
        if not sym or sym in seen:
            continue
        out.append(sym)
        seen.add(sym)
    return out


def generate_lob_data(config: SyntheticLOBConfig) -> tuple[np.ndarray, dict[str, Any]]:
    n_rows = max(1, int(config.n_rows))
    rng = np.random.default_rng(int(config.rng_seed))
    arr = np.zeros(n_rows, dtype=_DTYPE)

    tick_ns = int(max(1.0, float(config.tick_interval_ms)) * 1_000_000.0)
    base_mid = 100.0
    tick_size = 0.01
    q = 0.0
    ar_coef = float(config.queue_ar_coef)
    spread_mean = float(config.spread_mean_bps)

    i = 0
    regimes_covered: set[str] = set()
    regime_to_idx = {name: idx for idx, name in enumerate(config.regimes)}
    if not regime_to_idx:
        regime_to_idx = {"trending": 0, "mean_reverting": 1, "volatile": 2}
    regime_names = tuple(regime_to_idx.keys())

    # Pre-allocate scratch buffers once (reused across blocks, Allocator Law)
    max_block = max(config.regime_block_max, 1) + 1
    q_buf = np.empty(max_block, dtype=np.float64)
    mid_buf = np.empty(max_block, dtype=np.float64)
    spread_rng_buf = np.empty(max_block, dtype=np.float64)
    depth_rng_buf = np.empty(max_block, dtype=np.float64)
    volume_buf = np.empty(max_block, dtype=np.float64)

    # Pre-compute timestamps (avoids per-block np.arange allocation)
    all_ts = np.arange(n_rows, dtype=np.int64) * tick_ns

    _directions = np.asarray([-1.0, 1.0], dtype=np.float64)

    while i < n_rows:
        block = int(rng.integers(max(1, config.regime_block_min), max(config.regime_block_max, 1) + 1))
        regime = regime_names[int(rng.integers(0, len(regime_names)))]
        regimes_covered.add(regime)
        trend_dir = float(rng.choice(_directions))

        end = min(n_rows, i + block)
        block_len = end - i

        # Determine regime-constant parameters
        if regime == "trending":
            spread_mult = 1.0
            vol_scale = 0.8
        elif regime == "mean_reverting":
            spread_mult = 0.8
            vol_scale = 0.6
        else:
            spread_mult = 2.0
            vol_scale = 1.6

        # --- Phase 1: Sequential q + base_mid (AR(1) data dependency) ---
        # Must draw RNG per-tick in original order to preserve determinism.
        # Per tick draws: eps_q, [volatile: drift_rng], px_noise, spread_rng, depth_rng, volume
        for k in range(block_len):
            eps_q = float(rng.normal(0.0, 0.03))
            if regime == "trending":
                regime_drift = 0.02 * trend_dir
            elif regime == "mean_reverting":
                regime_drift = -0.10 * q
            else:
                regime_drift = float(rng.normal(0.0, 0.015))

            q = ar_coef * q + regime_drift + eps_q
            q = max(-0.99, min(0.99, q))

            px_noise = float(rng.normal(0.0, 0.03 * vol_scale))
            impact = 0.08 * q
            base_mid = max(1.0, base_mid + (impact + px_noise) * tick_size)

            # Store sequential results + RNG draws for vectorized phase 2
            q_buf[k] = q
            mid_buf[k] = base_mid
            spread_rng_buf[k] = float(rng.normal(0.0, 0.08))
            depth_rng_buf[k] = float(rng.normal(0.0, 0.12))
            volume_buf[k] = float(rng.gamma(shape=2.0, scale=4.0 * (1.0 + vol_scale)))

        # --- Phase 2: Vectorized derived-field computation (Cache Law) ---
        q_blk = q_buf[:block_len]
        mid_blk = mid_buf[:block_len]

        # Spread
        spread_noise_blk = 1.0 + np.abs(q_blk) * 0.25 + np.abs(spread_rng_buf[:block_len])
        spread_bps_blk = np.maximum(0.5, spread_mean * spread_mult * spread_noise_blk)
        half_spread_blk = (mid_blk * spread_bps_blk / 10_000.0) / 2.0

        # Prices
        bid_px_blk = np.maximum(0.01, mid_blk - half_spread_blk)
        ask_px_blk = np.maximum(bid_px_blk + 0.0001, mid_blk + half_spread_blk)

        # Depth
        abs_q = np.abs(q_blk)
        total_depth_blk = np.maximum(10.0, 1800.0 * (1.0 + 0.35 * abs_q + np.abs(depth_rng_buf[:block_len])))
        bid_qty_blk = np.maximum(1.0, total_depth_blk * (1.0 + q_blk) / 2.0)
        ask_qty_blk = np.maximum(1.0, total_depth_blk * (1.0 - q_blk) / 2.0)

        # Volume
        vol_blk = np.maximum(1.0, volume_buf[:block_len])

        # --- Block assignment (contiguous write, Cache Law) ---
        arr["bid_qty"][i:end] = bid_qty_blk
        arr["ask_qty"][i:end] = ask_qty_blk
        arr["bid_px"][i:end] = bid_px_blk
        arr["ask_px"][i:end] = ask_px_blk
        arr["mid_price"][i:end] = (bid_px_blk + ask_px_blk) / 2.0
        arr["spread_bps"][i:end] = spread_bps_blk
        arr["volume"][i:end] = vol_blk
        arr["local_ts"][i:end] = all_ts[i:end]

        i = end

    digest = hashlib.sha256(arr.tobytes()[:1024]).hexdigest()
    params = asdict(config)
    params["regimes"] = list(config.regimes)

    meta: dict[str, Any] = {
        "dataset_id": f"synthetic_lob_{config.generator_version}_seed{int(config.rng_seed)}",
        "source_type": "synthetic",
        "source": _META_SOURCE,
        "generator": _META_GENERATOR,
        "seed": int(config.rng_seed),
        "owner": "research",
        "schema_version": 1,
        "rows": int(arr.shape[0]),
        "fields": [str(name) for name in arr.dtype.names or ()],
        "symbols": [],
        "split": "full",
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


# ---------------------------------------------------------------------------
# v2 generator: OU-Hawkes-Markov model (Papers 026, 039, 120, 121, 122, 123)
# ---------------------------------------------------------------------------

#: Field names accepted in data_fields from a manifest for from_manifest() validation
_SUPPORTED_FIELDS: frozenset[str] = frozenset(f for f, _ in _DTYPE.descr)


@dataclass(slots=True)
class SyntheticLOBConfigV2:
    """Configuration for the OU-Hawkes-Markov synthetic LOB generator (v2).

    Regime order: 0=trending, 1=mean_reverting, 2=volatile
    """

    n_rows: int = 20_000
    rng_seed: int = 42
    tick_interval_ms: float = 2.0

    # Ornstein-Uhlenbeck queue imbalance params (Paper 123)
    # per-regime: (trending, mean_reverting, volatile)
    ou_theta: tuple[float, ...] = (0.02, 0.15, 0.05)
    ou_mu: tuple[float, ...] = (0.15, 0.0, 0.0)
    ou_sigma: tuple[float, ...] = (0.04, 0.025, 0.10)

    # Lévy jump component (Paper 123)
    jump_rate: float = 0.01  # Poisson rate per tick
    jump_sigma: float = 0.20  # jump size std dev

    # Markov regime transition matrix (Paper 039)
    # Rows = current regime, columns = next regime
    regime_transition: tuple[tuple[float, ...], ...] = (
        (0.70, 0.20, 0.10),  # from trending
        (0.15, 0.75, 0.10),  # from mean_reverting
        (0.10, 0.25, 0.65),  # from volatile
    )
    regime_min_ticks: int = 50  # minimum ticks before regime can switch

    # Hawkes self-exciting volume process (Papers 026, 120)
    # per-regime baseline intensities
    hawkes_baseline: tuple[float, ...] = (8.0, 5.0, 15.0)
    hawkes_excitation: float = 0.4  # self-excitation weight α_H
    hawkes_decay: float = 0.05  # exponential decay rate β_H

    # Spread resilience after jumps (Paper 121)
    spread_mean_bps: float = 5.0
    spread_recovery_gamma: float = 0.10  # half-life ≈ 7 ticks

    # Price impact (Papers 122, 124)
    price_impact_beta: float = 0.08

    generator_version: str = "v2"
    paper_refs: tuple[str, ...] = ("026", "039", "120", "121", "122", "123")

    @classmethod
    def from_manifest(cls, manifest: Any) -> "SyntheticLOBConfigV2":
        """Construct config from an AlphaManifest, validating data_fields."""
        fields = set(getattr(manifest, "data_fields", ()))
        unsupported = fields - _SUPPORTED_FIELDS
        if unsupported:
            raise ValueError(
                f"Manifest data_fields contains unsupported field(s): {sorted(unsupported)}. "
                f"Supported: {sorted(_SUPPORTED_FIELDS)}"
            )
        return cls()


def generate_lob_data_v2(config: SyntheticLOBConfigV2) -> tuple[np.ndarray, dict[str, Any]]:
    """Generate synthetic LOB data using the OU-Hawkes-Markov model (v2).

    This generator improves mathematical realism over v1 (AR(1)):
    - Queue imbalance follows an Ornstein-Uhlenbeck process with Lévy jumps (Paper 123)
    - Regime switching uses a persistent Markov chain (Paper 039)
    - Volume clustering uses a Hawkes self-exciting point process (Papers 026, 120)
    - Spread recovers exponentially after jump events (Paper 121)
    """
    n_rows = max(1, int(config.n_rows))
    rng = np.random.default_rng(int(config.rng_seed))
    arr = np.zeros(n_rows, dtype=_DTYPE)

    tick_ns = int(max(1.0, float(config.tick_interval_ms)) * 1_000_000.0)
    base_mid = 100.0
    tick_size = 0.01

    # Unpack OU params
    ou_theta = tuple(float(v) for v in config.ou_theta)
    ou_mu = tuple(float(v) for v in config.ou_mu)
    ou_sigma = tuple(float(v) for v in config.ou_sigma)
    dt = 1.0 / 100.0  # normalised time step (τ = 100 ticks)

    # Markov transition matrix rows → cumulative for sampling
    trans_cumul: list[np.ndarray] = []
    for row in config.regime_transition:
        row_arr = np.asarray(row, dtype=np.float64)
        row_arr = row_arr / row_arr.sum()  # normalise in case of floating imprecision
        trans_cumul.append(np.cumsum(row_arr))

    n_regimes = len(trans_cumul)
    regime_names = ("trending", "mean_reverting", "volatile")[:n_regimes]

    # Hawkes params
    hawkes_baseline = tuple(float(v) for v in config.hawkes_baseline)
    alpha_h = float(config.hawkes_excitation)
    beta_h = float(config.hawkes_decay)

    # Initialise state
    q = 0.0  # queue imbalance ∈ [-1, 1]
    regime_idx = 0  # start in trending
    ticks_in_regime = 0
    hawkes_intensity = hawkes_baseline[regime_idx]
    ticks_since_jump = 1000  # large → no residual spread impact at start
    regimes_covered: set[str] = set()

    for i in range(n_rows):
        # --- Markov regime transition (Paper 039) ---
        if ticks_in_regime >= config.regime_min_ticks:
            u = float(rng.uniform())
            cumul = trans_cumul[regime_idx]
            new_regime = int(np.searchsorted(cumul, u))
            new_regime = min(new_regime, n_regimes - 1)
            if new_regime != regime_idx:
                regime_idx = new_regime
                ticks_in_regime = 0
                hawkes_intensity = hawkes_baseline[regime_idx]

        regime_name = regime_names[regime_idx]
        regimes_covered.add(regime_name)
        ticks_in_regime += 1

        # --- OU queue imbalance with Lévy jumps (Paper 123) ---
        theta_r = ou_theta[regime_idx]
        mu_r = ou_mu[regime_idx]
        sigma_r = ou_sigma[regime_idx]
        eps = float(rng.standard_normal())
        dq = theta_r * (mu_r - q) * dt + sigma_r * (dt**0.5) * eps

        # Lévy jump
        jump = 0.0
        if float(rng.uniform()) < config.jump_rate:
            jump = float(rng.normal(0.0, config.jump_sigma))
            ticks_since_jump = 0
        else:
            ticks_since_jump += 1

        q = q + dq + jump
        q = _clip(q, -0.99, 0.99)

        # --- Hawkes volume (Papers 026, 120) ---
        # λ_t = λ_0 + Σ α_H * exp(-β_H * Δt)
        hawkes_intensity = hawkes_baseline[regime_idx] + (hawkes_intensity - hawkes_baseline[regime_idx]) * (
            1.0 - beta_h
        )
        hawkes_intensity = max(0.5, hawkes_intensity)
        # New event self-excitation: each tick a "trade" arrives proportional to intensity
        kappa = 2.0
        vol_scale = hawkes_intensity / kappa
        volume = max(1.0, float(rng.gamma(shape=kappa, scale=vol_scale)))
        # Update intensity for next tick (self-excitation from this event)
        hawkes_intensity += alpha_h * (volume / kappa)

        # --- Price impact (Papers 122, 124) ---
        px_noise = float(rng.normal(0.0, 0.03))
        impact = float(config.price_impact_beta) * q
        base_mid = max(1.0, base_mid + (impact + px_noise) * tick_size)

        # --- Spread with resilience after jump (Paper 121) ---
        spread_extra = 0.0
        if ticks_since_jump < 100:
            delta_s = abs(jump) * 3.0  # bps impact proportional to jump size
            spread_extra = delta_s * (1.0 - float(config.spread_recovery_gamma)) ** ticks_since_jump

        spread_noise = 1.0 + abs(q) * 0.25 + abs(float(rng.normal(0.0, 0.08)))
        if regime_name == "volatile":
            spread_mult = 2.0
        elif regime_name == "mean_reverting":
            spread_mult = 0.8
        else:
            spread_mult = 1.0
        spread_bps = max(0.5, float(config.spread_mean_bps) * spread_mult * spread_noise + spread_extra)

        half_spread = (base_mid * spread_bps / 10_000.0) / 2.0
        bid_px = max(0.01, base_mid - half_spread)
        ask_px = max(bid_px + 0.0001, base_mid + half_spread)

        total_depth = max(10.0, 1800.0 * (1.0 + 0.35 * abs(q) + abs(float(rng.normal(0.0, 0.12)))))
        bid_qty = max(1.0, total_depth * (1.0 + q) / 2.0)
        ask_qty = max(1.0, total_depth * (1.0 - q) / 2.0)

        arr[i]["bid_qty"] = bid_qty
        arr[i]["ask_qty"] = ask_qty
        arr[i]["bid_px"] = bid_px
        arr[i]["ask_px"] = ask_px
        arr[i]["mid_price"] = (bid_px + ask_px) / 2.0
        arr[i]["spread_bps"] = spread_bps
        arr[i]["volume"] = volume
        arr[i]["local_ts"] = i * tick_ns

    digest = hashlib.sha256(arr.tobytes()[:1024]).hexdigest()
    params: dict[str, Any] = {
        "n_rows": config.n_rows,
        "rng_seed": config.rng_seed,
        "tick_interval_ms": config.tick_interval_ms,
        "ou_theta": list(config.ou_theta),
        "ou_mu": list(config.ou_mu),
        "ou_sigma": list(config.ou_sigma),
        "jump_rate": config.jump_rate,
        "jump_sigma": config.jump_sigma,
        "regime_transition": [list(r) for r in config.regime_transition],
        "regime_min_ticks": config.regime_min_ticks,
        "hawkes_baseline": list(config.hawkes_baseline),
        "hawkes_excitation": config.hawkes_excitation,
        "hawkes_decay": config.hawkes_decay,
        "spread_mean_bps": config.spread_mean_bps,
        "spread_recovery_gamma": config.spread_recovery_gamma,
        "price_impact_beta": config.price_impact_beta,
    }

    meta: dict[str, Any] = {
        "dataset_id": f"synthetic_lob_{config.generator_version}_seed{int(config.rng_seed)}",
        "source_type": "synthetic",
        "source": _META_SOURCE,
        "generator": _META_GENERATOR,
        "seed": int(config.rng_seed),
        "owner": "research",
        "schema_version": 1,
        "rows": int(arr.shape[0]),
        "fields": [str(name) for name in arr.dtype.names or ()],
        "symbols": [],
        "split": "full",
        "rng_seed": int(config.rng_seed),
        "generator_script": "research/tools/synth_lob_gen.py",
        "generator_version": str(config.generator_version),
        "model_type": "ou_hawkes_markov",
        "paper_refs": list(config.paper_refs),
        "parameters": params,
        "regimes_covered": sorted(regimes_covered),
        "data_fingerprint": digest,
        "lineage": {
            "parent": None,
            "derived_from": f"SyntheticLOBConfigV2(rng_seed={int(config.rng_seed)})",
        },
        "data_ul": 5,
        "created_at": datetime.now(UTC).isoformat(),
    }
    return arr, meta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate realistic synthetic LOB data with UL5 metadata.")
    parser.add_argument("--out", required=True, help="Output .npy path")
    parser.add_argument("--meta-out", default=None, help="Optional metadata output path")
    parser.add_argument("--version", default="v1", choices=("v1", "v2"), help="Generator version")
    parser.add_argument("--n-rows", type=int, default=20_000)
    parser.add_argument("--rng-seed", type=int, default=42)
    parser.add_argument("--tick-interval-ms", type=float, default=2.0)
    # v1-specific args
    parser.add_argument("--regime-block-min", type=int, default=200)
    parser.add_argument("--regime-block-max", type=int, default=500)
    parser.add_argument("--spread-mean-bps", type=float, default=5.0)
    parser.add_argument("--queue-ar-coef", type=float, default=0.92)
    parser.add_argument("--generator-version", default=None, help="Override generator_version tag in metadata")
    parser.add_argument("--owner", default="research", help="Metadata owner")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols for metadata provenance")
    parser.add_argument("--split", default="full", help="Dataset split tag (train/validation/oos/full)")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    version = args.version

    if version == "v2":
        cfg_v2 = SyntheticLOBConfigV2(
            n_rows=int(args.n_rows),
            rng_seed=int(args.rng_seed),
            tick_interval_ms=float(args.tick_interval_ms),
            generator_version=str(args.generator_version) if args.generator_version else "v2",
        )
        arr, meta = generate_lob_data_v2(cfg_v2)
    else:
        cfg_v1 = SyntheticLOBConfig(
            n_rows=int(args.n_rows),
            rng_seed=int(args.rng_seed),
            tick_interval_ms=float(args.tick_interval_ms),
            regime_block_min=int(args.regime_block_min),
            regime_block_max=int(args.regime_block_max),
            spread_mean_bps=float(args.spread_mean_bps),
            queue_ar_coef=float(args.queue_ar_coef),
            generator_version=str(args.generator_version) if args.generator_version else "v1",
        )
        arr, meta = generate_lob_data(cfg_v1)

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(out_path, arr)

    meta_path = Path(args.meta_out).resolve() if args.meta_out else out_path.with_suffix(out_path.suffix + ".meta.json")
    meta["owner"] = str(args.owner or "research")
    meta["symbols"] = _normalize_symbols(args.symbols)
    meta["split"] = str(args.split or "full")
    meta["generator"] = str(meta.get("generator") or _META_GENERATOR)
    meta["seed"] = int(args.rng_seed)
    meta["data_file"] = str(out_path)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    print(f"[synth_lob_gen] wrote data: {out_path}")
    print(f"[synth_lob_gen] wrote meta: {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
