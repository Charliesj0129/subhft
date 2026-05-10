# c75 — Multi-Window OFI Taker

**Status:** GATE_B (TIER_2) · **Instrument:** TMFD6 · **Strategy type:** taker · **FE schema:** lob_shared_v3

## Formula (post-D2 form)

```
flow_signal = 0.667 * ofi_l1_ema5s
            + 0.333 * ofi_l1_ema30s
```

A 2-term frozen-weights composite over multi-window L1 OFI. The original
draft included a third term `0.1 * deep_depth_momentum_x1000`; the
2026-05-06 depth-parity decision dropped that term because the backtest
adapter's `_build_l1_bidask_event` (`src/hft_platform/backtest/adapter.py:436`)
emits only L1, so MLDM (FE-v3 idx 20) collapses to zero in Gate C. See
`docs/runbooks/c75-depth-parity-decision-2026-05-06.md` for D1 (extend
adapter) follow-up.

## Fire gate

The discrete fire decision is exposed via `should_fire()` -> `+1 / -1 / 0`:

1. **Warm-up:** until `_update_count >= 300`, returns 0.
2. **Spread-regime gate:** if `spread_ema30s > 1.5 * spread_ema300s`
   (anomalous-widen), returns 0.
3. **Threshold gate:** if `abs(flow_signal) > 1.5 * rolling_stdev(flow_signal, 300)`,
   return `sign(flow_signal)`; else 0.

`research/backtest/alpha_strategy_bridge.AlphaStrategyBridge.on_stats`
calls `should_fire()` after `update()` and zeroes the recorded signal when
the gate is closed (Codex adversarial-review 2026-05-06 finding 4).

## Frozen weights & rationale

Weights come from the Cont-Kukanov 2014 multi-window OFI lineage. The
original 0.6 / 0.3 / 0.1 split was renormalised to 0.667 / 0.333 (preserving
the 6:3 short/long-window ratio) when the third term was dropped. **No
per-day calibration is performed** — c75 is a zero-free-parameter
candidate.

## Latency profile

`r47_maker_shioaji_p95_v2026-04-24_measured` — per
`docs/architecture/latency-baseline-shioaji-sim-vs-system.md`. P95
place_order ~92.7 ms, P95 cancel_order ~58.7 ms; taker only uses
place_order + cancel_order (no update_order).

## FE-v3 indices consumed

| Idx | Feature                | Role             |
| --- | ---------------------- | ---------------- |
| 22  | `ofi_l1_ema5s`         | short-window OFI |
| 23  | `ofi_l1_ema30s`        | medium-window OFI|
| 25  | `spread_ema30s`        | regime current   |
| 26  | `spread_ema300s`       | regime baseline  |

(Idx 20 `deep_depth_momentum_x1000` is **not** consumed — see D2 decision.)

## Tests

`pytest research/alphas/c75_tmf_mw_ofi_taker/tests/` covers:

* Frozen weights match the manifest (regression for D2).
* `should_fire()` returns 0 pre-warmup.
* `should_fire()` returns 0 in anomalous spread regime.
* `should_fire()` returns +/-1 in normal regime when signal exceeds
  `1.5 * rolling_stdev`.
* `update()` accepts both `features=` kwarg and individual canonical-name
  kwargs (mirroring `_FE_KEYS_V3` enrichment in the bridge).

## Cross-references

* Plan: `~/.claude/plans/scalable-inventing-forest.md` (Step 7 D2).
* Depth-parity decision: `docs/runbooks/c75-depth-parity-decision-2026-05-06.md`.
* npz format zoo: `docs/runbooks/npz-formats-2026-05-06.md`.
* Bridge contract: `research/backtest/alpha_strategy_bridge.py` (FE-v3
  enrichment + should_fire gate).
