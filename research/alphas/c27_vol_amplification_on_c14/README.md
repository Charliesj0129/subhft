# C27 — Vol-Percentile Amplification on C14

Round R14 candidate. T2 CONDITIONAL-APPROVE (0/0/1 FAILs — S2 methodology audit).

## What this is

A modulator layer on C14 (the R6 PROMOTE). C14 quotes TXF rolling front-
month with static `max_pos=3`. C27 replaces that with a *state-switched*
`max_pos`:

- Baseline: `max_pos=3` (C14's structural optimum).
- Amplified: `max_pos=4` when within-day 1-min realized-vol percentile >
  `P90`, released at `P70` (hysteresis).

**Source of the hypothesis**: R14-T1 §2 bucket analysis on C14's actual
OOS fills shows the P95 bucket (top 5% of vol minutes) has per-RT edge
of +28.20 pt vs P20's +5.98 pt (**4.7× ratio**, OOS 18,853 trips).

**Inversion of R7 C13**: C13 used the same signal as a DISABLE trigger
(suppress quoting in high-vol minutes); C13 was KILLED. C27 uses it as
an AMPLIFY trigger — mechanical inverse. Not the same kill direction.

## Critical T2 finding (S2 FAIL)

Researcher's `explore.py` reconstructed round-trips via FIFO matching
across the full day, producing per-trip gross ~8× inflated vs the R6
scorecard's authoritative position-boundary per-trip accounting. So:

- Researcher's claim: +290K NTD/day OOS uplift.
- Challenger-corrected (÷8): **+36K NTD/day**.
- T5 backtest MUST use position-boundary accounting (the same harness
  as `round-6/artifacts/backtest-revised/run_c14_backtest_revised.py`)
  to produce the authoritative number.

## Critical strict-baseline requirement

C14's R6-REVISE backtest leaks beyond `max_pos=3` on 31/40 days. The
"C14 +262K NTD/day" baseline figure includes positions of 4-5 contracts
at session close. C27's uplift must be measured vs a **strict
`max_pos=3`** C14 re-baseline, not the leaky R6 figure.

The T5 driver therefore runs:
1. C14 baseline with strict `max_pos=3` enforcement (no leakage).
2. C27 with baseline=3, amplified=4.
3. Uplift = C27 - strict_C14.

## Files

| File | Purpose |
| ---- | ------- |
| `impl.py` | `C27VolAmplifiedMaker` (wraps C14 `TxfFrontMonthMaker` via composition) + `C27Alpha` (AlphaProtocol shim) + `C27Params` |
| `vol_gate.py` | `VolPercentileGate` — within-day 1-min realized-vol percentile tracker with hysteresis; resets on day-boundary / `on_gap()` |
| `manifest.yaml` | AlphaManifest + T1/T2 governance + methodology constraints |
| `tests/test_c27.py` | Unit tests — state switch, percentile reset, gap clear, scaled-int preservation, warmup + statistical check |
| `README.md` | This file |

## Parameters

| Parameter | Default | Notes |
| --------- | ------: | ----- |
| `spread_threshold_pts` | 3 | inherits C14 |
| `max_pos_baseline` | 3 | C14 structural optimum |
| `max_pos_amplified` | **4** | amplified state (33% more capacity) |
| `inventory_skew_tenths` | 2 | inherits C14 |
| `vol_percentile_threshold` | **0.90** | amplify-trigger |
| `vol_percentile_release` | 0.70 | hysteresis-release |
| `vol_window_seconds` | 60 | minute-bucket width (informational) |
| `warmup_minutes` | 10 | min completed minutes before gate can fire |

## SWITCH semantics (inherited from C14)

C27 runs only on TXF front-month. Must NOT run concurrently with the
deployed TMFD6 R47 max_pos=1 (R51-C1b kill direction). In live deploy,
the strategy registry entry's `switch_from_strategy` flag is retained
from C14's pattern.

## Relationship to R7 C13 (KILLED)

C13 used the same signal for DISABLE (suppress quoting in high-vol).
Killed because: suppressing quoting during high-vol minutes cut off
exactly the most-profitable RTs. The post-mortem measured +14.6×
PnL/min in the gated-high vs gated-low bucket — which motivated this
inverted candidate.
