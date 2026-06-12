---
prompt_id: order_book_imbalance__v1
schema_ref: research/candidate_loop/prompts/v1/candidate.schema.json
primitive_version: prim_v1
---

# Candidate generation — family `order_book_imbalance`

You generate alpha candidates for the TAIFEX TXF L2 candidate loop v1.

## Output contract

- Output EXACTLY the requested number of JSONL lines: one JSON object per
  line, nothing else (no markdown fences, no commentary, no header line — the
  pipeline prepends provenance).
- Every object must validate against `candidate.schema.json` (schema_version
  `cand_v1`) shipped beside this prompt.
- `family` MUST be `"order_book_imbalance"`.
- `label` MUST be exactly `future_mid_return(horizon='<horizon>')` where
  `<horizon>` equals the candidate's `horizon` field.
- `expected_sign` is `'positive'` or `'negative'` — the IC sign you predict.
- `hypothesis` is 20-500 chars of falsifiable microstructure reasoning.

## prim_v1 primitives (the ONLY callables allowed in formulas)

```
mid_price()
spread_ticks()
depth_sum(side, levels)        # side in {'bid','ask'}, levels in 1..5
book_imbalance(levels)         # levels in 1..5
microprice()
depth_delta(side, levels, window)
trade_imbalance(window)
future_mid_return(horizon)     # LABEL ONLY - never in features/signal/regime
```

Transforms: `zscore(x, window)` (default `'2000_events'`),
`negative_zscore(x, window)`, `ema(x, window)`, `clip(x, lo, hi)` (numeric
literals, `lo < hi`).

## Windows and horizons

- window: `'N_events'` with N in 10..10000, or `'Nms'`/`'Ns'` in 50ms..60s
- horizon: `'N_events'` with N in 1..10000, or `'Nms'`/`'Ns'` in 100ms..30s

## Hard limits (violations become dead candidates, not retries)

- <= 6 features; inlined signal AST <= 64 nodes; call depth <= 3.
- `regime_filter`: `""` (always-on) or exactly ONE comparison, e.g.
  `"spread_ticks() <= 2"`.
- `proposed_new_primitives` may only PROPOSE (with
  `not_executable_in_v1: true`); a proposed name used in any formula kills
  the candidate.
- Identical inlined signal+regime+horizon = `DUPLICATE_ALPHA`. Vary
  structure (levels, windows, horizons, normalization), not names.

## Family guidance

Bet on multi-level book pressure: `book_imbalance(L)` across L, depth
differences (`depth_sum('bid', L) - depth_sum('ask', L)`), z-scored or
EMA-smoothed over different windows, optionally spread-gated. Vary level
count, smoothing window, and horizon so the batch spans the family's
parameter space.

## Example (pretty-printed for readability; emit it as ONE line)

```json
{
  "name": "obi_l3_z_fast",
  "family": "order_book_imbalance",
  "hypothesis": "Persistent 3-level bid-side depth dominance precedes short-horizon upward mid moves as makers reposition.",
  "features": [
    {
      "name": "obi_l3",
      "formula": "book_imbalance(3)"
    }
  ],
  "signal_formula": "zscore(obi_l3, '1000_events')",
  "label": "future_mid_return(horizon='500ms')",
  "horizon": "500ms",
  "expected_sign": "positive",
  "regime_filter": "",
  "cost_risk": "Imbalance flips often; turnover may exceed the cost proxy.",
  "latency_risk": "L1-driven component decays within 1ms.",
  "falsification_tests": [
    "IC sign flips between train and validation"
  ]
}
```
