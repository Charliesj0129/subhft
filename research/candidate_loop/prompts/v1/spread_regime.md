---
prompt_id: spread_regime__v1
schema_ref: research/candidate_loop/prompts/v1/candidate.schema.json
primitive_version: prim_v1
---

# Candidate generation — family `spread_regime`

You generate alpha candidates for the TAIFEX TXF L2 candidate loop v1.

## Output contract

- Output EXACTLY the requested number of JSONL lines: one JSON object per
  line, nothing else (no markdown fences, no commentary, no header line — the
  pipeline prepends provenance).
- Every object must validate against `candidate.schema.json` (schema_version
  `cand_v1`) shipped beside this prompt.
- `family` MUST be `"spread_regime"`.
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

Bet on regime-conditioned signals: a base signal (imbalance, microprice
displacement, flow) gated by exactly ONE `regime_filter` comparison on
spread or depth, e.g. `"spread_ticks() <= 2"`. Vary the regime
threshold, the base signal, and the horizon. The evaluator reports
in-regime vs out-of-regime IC, so the regime must carry the edge.

## Example (pretty-printed for readability; emit it as ONE line)

```json
{
  "name": "obi_tight_spread_gate",
  "family": "spread_regime",
  "hypothesis": "Book imbalance is informative only when the spread is tight; wide spreads mark uncertainty where imbalance is noise.",
  "features": [
    {
      "name": "obi_l2",
      "formula": "book_imbalance(2)"
    }
  ],
  "signal_formula": "zscore(obi_l2, '2000_events')",
  "label": "future_mid_return(horizon='500ms')",
  "horizon": "500ms",
  "expected_sign": "positive",
  "regime_filter": "spread_ticks() <= 2",
  "cost_risk": "Tight-spread-only trading reduces opportunity count.",
  "latency_risk": "Regime flag itself can flip between signal and entry.",
  "falsification_tests": [
    "Out-of-regime IC matches in-regime IC"
  ]
}
```
