# Neural Weight Oscillator Zeiierman v0

Strictly causal research reconstruction of the public TradingView indicator:

https://tw.tradingview.com/script/bfu1hmkS-Neural-Weight-Oscillator-Zeiierman/

## Fidelity

The publication exposes the component formulas, BWM relation, defaults, plot
names, and alert semantics. Its Pine body is encoded in the page payload, so
this candidate is a `disclosed_formula_causal_reconstruction`, not a claimed
1:1 Pine port.

The implementation fixes the published BWM preference before evaluation and
updates its adaptive linear layer only after each target horizon has elapsed.
The prefix-invariance audit verifies that appending future bars cannot alter
earlier oscillator values, learned weights, or signals.

## Frozen Evaluation

- Development: TXFD6 through 2026-04-15.
- Primary OOS: TXFE6 from 2026-04-16 through 2026-05-20.
- Confirmation OOS: TXFF6 from 2026-05-21 through 2026-06-04.
- Bar size/session: 5-minute TAIFEX day session.
- Execution: confirmed close signal to next-open ask/bid; intraday force-flat.

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run python -m \
  research.experiments.validations.neural_weight_oscillator_zeiierman_v0.backtest
```

Outputs:

- `result_5m_day.json`
- `reports/codex/neural_weight_oscillator_zeiierman_v0_report.md`

## Current Verdict

`NOT_CONFIRMED`. E6 long/short is positive before robustness checks, but becomes
negative when its best day is removed, is not beta-neutral significant, and
does not survive the F6 confirmation window. E6 long-only is also negative.
No parameter tuning should be performed against these OOS windows.

## Expanded TXF/TMF Diagnostic

The expanded runner builds the frozen B6-F6 front-month chain independently
for TXF and TMF. F6 is reconstructed from the current read-only ClickHouse
snapshot with the same day-session OHLC quality rules and next-open as-of BBO
semantics:

```bash
UV_CACHE_DIR=.uv-cache uv run python -m \
  research.experiments.validations.neural_weight_oscillator_zeiierman_v0.expanded_run
```

Outputs:

- `result_expanded_txf_tmf_5m_day.json`
- `reports/codex/neural_weight_oscillator_expanded_txf_tmf_report.md`
- `reports/codex/neural_weight_oscillator_{txf,tmf}_f6_incremental_bars.npz`

The result is diagnostic only. TXF F6 is positive at the 2-point stress level,
but has negative beta-neutral excess and no significant timing alpha. TMF F6
is negative and fails best-day leave-one-out. The 2026-06-01 through 2026-06-04
DB overlap also differs from the earlier canonical raw snapshot, so the run is
marked `source_snapshot_break` and cannot upgrade the candidate for promotion.
