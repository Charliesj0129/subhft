# Feature Plane Parity Gate

Guards against **promoted-family feature drift** across the two compute paths that
share the platform's `FeatureEngine`:

1. **Python** — live/research path (`FeatureEngine(kernel_backend="python")`).
2. **hftbacktest** — research/backtest path (`HftBacktestAdapter(feature_mode="lob_feature")`).

The **promoted family** is the minimal microstructure set marked with
`FEATURE_FLAG_PROMOTED` in `src/hft_platform/feature/registry.py`:
`mid_price_x2`, `spread_scaled`, `depth_imbalance_ppm`, `microprice_x2`,
`l1_imbalance_ppm`, `ofi_l1_raw`, `ofi_l1_cum`, `ofi_l1_ema8`, `spread_ema8_scaled`,
`depth_imbalance_ema8_ppm`. EMA features allow `parity_atol=1` (f64→i64 rounding);
everything else must match exactly.

## Run it

```bash
make feature-parity
uv run python -m hft_platform.cli feature parity --feature-set lob_shared_v2
```

The command runs a deterministic synthetic book sequence (warmup ramp, one-sided book,
gap-triggered reset + re-warm) through every available path and prints a JSON report to
stdout. Exit code is non-zero on any divergence — suitable as a CI/ops gate. It runs in CI
in the **Tests & Coverage** job (`.github/workflows/ci.yml`).

## Reading the report

```json
{
  "ok": false,
  "feature_set_id": "lob_shared_v3",
  "comparisons": [
    {
      "pair": "python vs hftbacktest_shared",
      "ok": false,
      "first_divergence": {
        "frame_index": 12, "symbol": "PARITYR1", "timestamp": 2500,
        "feature_id": "ofi_l1_ema8", "index": 13,
        "expected": 25, "actual": 31, "abs_diff": 6, "tolerance": 1
      }
    }
  ]
}
```

`first_divergence` pinpoints the exact path pair, frame, symbol, timestamp, feature, and
expected/actual values — the starting point for any investigation.

## When it fails

1. Compare how `LOBEngine` and `_hbt_utils.build_lob_event` derive `LOBStatsEvent` fields for the divergent frame.
2. **Reproduce** with the unit suite: `uv run pytest tests/unit/test_feature_promoted_parity.py -v`.
3. **Do not** promote a feature family to live/shadow while this gate is red — the whole
   point is that research/replay/live agree before promotion.

## Related

- Harness: `src/hft_platform/feature/parity.py` (reusable: `run_self_test`,
  `compare_paths`, `build_synthetic_frames`).
- Tests: `tests/unit/test_feature_promoted_parity.py`,
  `tests/unit/test_feature_parity_cli.py`,
  `tests/integration/test_feature_engine_parity.py` (full adapter feed loop).
