# spread_pressure

## Hypothesis

When the current bid-ask spread widens above its EMA-8 baseline **and** the depth
imbalance confirms directional pressure, the combination predicts short-term price
movement. A widening spread signals the market maker is widening for adverse
selection; the depth imbalance provides the direction.

## Formula

```
spread_diff = spread_ema8_scaled − spread_scaled   # positive = spread currently tighter than EMA
signal = spread_diff × sign(depth_imbalance_ema8_ppm) / max(|spread_ema8_scaled|, 1)
```

Signal is dimensionless (normalized by spread_ema8). Used for ranking only — never
as a price.

## Feature Engine Indices (lob_shared_v1, schema_version=1)

| Index | Feature                     | Type      | Scale  |
|-------|-----------------------------|-----------|--------|
| 3     | `spread_scaled`             | stateless | ×10000 |
| 14    | `spread_ema8_scaled`        | rolling   | ×10000 |
| 15    | `depth_imbalance_ema8_ppm`  | rolling   | PPM    |

Warmup mask: `(1 << 14) | (1 << 15)` = `0xC000` (both rolling features require ≥2 ticks)

## Metadata

- `alpha_id`: `spread_pressure`
- `tier`: `TIER_2` (EMA-based; no LOB arrays required)
- `complexity`: `O(1)`
- `rust_module`: TBD (no existing module maps cleanly)
- `latency_profile`: `shioaji_sim_p95_v2026-02-28`
- `paper_refs`: None (microstructure signal from first principles)

## Gate Status

- Gate A: DRAFT (manifest + data-field + complexity checks)
- Gate B: tests in `tests/test_spread_pressure.py` (≥16 tests)
- Gate C: pending ResearchBacktestRunner run
- Gate D: pending Sharpe/drawdown thresholds (requires latency_profile ✓)
- Gate E: pending shadow sessions
