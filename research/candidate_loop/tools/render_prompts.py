"""Render the §11 generation prompts (``prompts/v1/<family>.md``) + schema JSON.

The prompts are FUNCTIONAL INPUTS, not documentation: ``generate`` reads their
frontmatter for ``prompt_id`` and hashes their exact bytes into provenance
(``prompt_sha256``).  Rendering them from this script keeps every prompt
mechanically in sync with the prim_v1 signatures and window/horizon domains in
``schema.py`` — ``tests/.../test_prompts.py`` asserts the committed files match
a fresh render byte-for-byte.

Run: ``uv run python -m research.candidate_loop.tools.render_prompts``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.candidate_loop.schema import (
    EVENT_WINDOW_MAX,
    EVENT_WINDOW_MIN,
    HORIZON_EVENT_MAX,
    HORIZON_EVENT_MIN,
    candidate_json_schema,
)

PROMPTS_DIR = Path("research/candidate_loop/prompts/v1")
SCHEMA_FILENAME = "candidate.schema.json"

# Family-specific guidance + a known-valid example candidate each.
FAMILY_GUIDANCE: dict[str, str] = {
    "order_book_imbalance": (
        "Bet on multi-level book pressure: `book_imbalance(L)` across L, depth\n"
        "differences (`depth_sum('bid', L) - depth_sum('ask', L)`), z-scored or\n"
        "EMA-smoothed over different windows, optionally spread-gated. Vary level\n"
        "count, smoothing window, and horizon so the batch spans the family's\n"
        "parameter space."
    ),
    "microprice": (
        "Bet on microprice displacement: `microprice() - mid_price()` as the core\n"
        "signal of where the true price sits inside the spread. Normalize by\n"
        "spread or z-score it; vary normalization window and horizon. Negative\n"
        "variants (fade the displacement) are legitimate distinct candidates."
    ),
    "depth_delta": (
        "Bet on depth CHANGE rather than level: `depth_delta(side, L, window)`\n"
        "differences between bid and ask capture replenishment vs depletion\n"
        "pressure. Vary side combination, level depth, delta window, and\n"
        "smoothing; depletion-fade and depletion-follow are both admissible."
    ),
    "trade_flow": (
        "Bet on signed trade flow: `trade_imbalance(window)` over varying\n"
        "windows, optionally normalized or regime-gated. NOTE: this family\n"
        "requires ClickHouse-verified trade_direction coverage >= 0.95 per day;\n"
        "days below that are skipped (dir_dirty), so effective_day_count will be\n"
        "lower — do not compensate by loosening anything."
    ),
    "spread_regime": (
        "Bet on regime-conditioned signals: a base signal (imbalance, microprice\n"
        "displacement, flow) gated by exactly ONE `regime_filter` comparison on\n"
        "spread or depth, e.g. `\"spread_ticks() <= 2\"`. Vary the regime\n"
        "threshold, the base signal, and the horizon. The evaluator reports\n"
        "in-regime vs out-of-regime IC, so the regime must carry the edge."
    ),
    "replenishment": (
        "Bet on queue replenishment dynamics: EMA-smoothed `depth_delta` after\n"
        "depletion events — does the book refill (stabilizing, fade the move) or\n"
        "keep draining (continuation)? Combine short-window depth deltas with\n"
        "longer smoothing; vary side, levels, and the two windows."
    ),
}

FAMILY_EXAMPLES: dict[str, dict[str, Any]] = {
    "order_book_imbalance": {
        "name": "obi_l3_z_fast",
        "family": "order_book_imbalance",
        "hypothesis": (
            "Persistent 3-level bid-side depth dominance precedes short-horizon "
            "upward mid moves as makers reposition."
        ),
        "features": [{"name": "obi_l3", "formula": "book_imbalance(3)"}],
        "signal_formula": "zscore(obi_l3, '1000_events')",
        "label": "future_mid_return(horizon='500ms')",
        "horizon": "500ms",
        "expected_sign": "positive",
        "regime_filter": "",
        "cost_risk": "Imbalance flips often; turnover may exceed the cost proxy.",
        "latency_risk": "L1-driven component decays within 1ms.",
        "falsification_tests": ["IC sign flips between train and validation"],
    },
    "microprice": {
        "name": "mp_disp_z_1s",
        "family": "microprice",
        "hypothesis": (
            "Microprice sitting above mid signals latent buy pressure that "
            "resolves into upward mid moves within a second."
        ),
        "features": [
            {"name": "mp_disp", "formula": "(microprice() - mid_price()) / (spread_ticks() + 1)"}
        ],
        "signal_formula": "zscore(mp_disp, '2000_events')",
        "label": "future_mid_return(horizon='1s')",
        "horizon": "1s",
        "expected_sign": "positive",
        "regime_filter": "",
        "cost_risk": "Sub-spread displacement may not clear the round-trip cost.",
        "latency_risk": "Microprice is L1-quote-driven; stale by 5ms.",
        "falsification_tests": ["Latency 1ms retention below threshold"],
    },
    "depth_delta": {
        "name": "dd_l3_diff_ema",
        "family": "depth_delta",
        "hypothesis": (
            "Sustained bid-side depth build relative to ask-side over 500ms "
            "marks accumulating passive interest that lifts the mid."
        ),
        "features": [
            {
                "name": "dd_diff",
                "formula": "depth_delta('bid', 3, '500ms') - depth_delta('ask', 3, '500ms')",
            }
        ],
        "signal_formula": "ema(dd_diff, '1s')",
        "label": "future_mid_return(horizon='2s')",
        "horizon": "2s",
        "expected_sign": "positive",
        "regime_filter": "",
        "cost_risk": "Depth changes without trades may never realize the move.",
        "latency_risk": "Delta windows shift meaningfully under 1ms re-anchor.",
        "falsification_tests": ["Edge concentrates in a single day"],
    },
    "trade_flow": {
        "name": "tf_imb_z_2s",
        "family": "trade_flow",
        "hypothesis": (
            "Two-second signed trade flow imbalance continues into the next "
            "second as aggressors walk the book."
        ),
        "features": [{"name": "tf_imb", "formula": "trade_imbalance('2s')"}],
        "signal_formula": "zscore(tf_imb, '5000_events')",
        "label": "future_mid_return(horizon='1s')",
        "horizon": "1s",
        "expected_sign": "positive",
        "regime_filter": "",
        "cost_risk": "Flow chasing pays the spread at the worst moments.",
        "latency_risk": "Aggressor information decays fastest of all families.",
        "falsification_tests": ["dir_dirty days dominate the usable sample"],
    },
    "spread_regime": {
        "name": "obi_tight_spread_gate",
        "family": "spread_regime",
        "hypothesis": (
            "Book imbalance is informative only when the spread is tight; wide "
            "spreads mark uncertainty where imbalance is noise."
        ),
        "features": [{"name": "obi_l2", "formula": "book_imbalance(2)"}],
        "signal_formula": "zscore(obi_l2, '2000_events')",
        "label": "future_mid_return(horizon='500ms')",
        "horizon": "500ms",
        "expected_sign": "positive",
        "regime_filter": "spread_ticks() <= 2",
        "cost_risk": "Tight-spread-only trading reduces opportunity count.",
        "latency_risk": "Regime flag itself can flip between signal and entry.",
        "falsification_tests": ["Out-of-regime IC matches in-regime IC"],
    },
    "replenishment": {
        "name": "rep_bid_l1_refill",
        "family": "replenishment",
        "hypothesis": (
            "Fast L1 bid replenishment after depletion signals committed passive "
            "buyers and precedes upward mid drift."
        ),
        "features": [
            {"name": "rep_bid", "formula": "ema(depth_delta('bid', 1, '200ms'), '2s')"}
        ],
        "signal_formula": "zscore(rep_bid, '2000_events')",
        "label": "future_mid_return(horizon='1s')",
        "horizon": "1s",
        "expected_sign": "positive",
        "regime_filter": "",
        "cost_risk": "Refill without aggression may produce no realized move.",
        "latency_risk": "Queue state is the fastest-moving input here.",
        "falsification_tests": ["Signal std collapses to zero on quiet days"],
    },
}

_TEMPLATE = """---
prompt_id: {family}__v1
schema_ref: research/candidate_loop/prompts/v1/{schema_filename}
primitive_version: prim_v1
---

# Candidate generation — family `{family}`

You generate alpha candidates for the TAIFEX TXF L2 candidate loop v1.

## Output contract

- Output EXACTLY the requested number of JSONL lines: one JSON object per
  line, nothing else (no markdown fences, no commentary, no header line — the
  pipeline prepends provenance).
- Every object must validate against `{schema_filename}` (schema_version
  `cand_v1`) shipped beside this prompt.
- `family` MUST be `"{family}"`.
- `label` MUST be exactly `future_mid_return(horizon='<horizon>')` where
  `<horizon>` equals the candidate's `horizon` field.
- `expected_sign` is `'positive'` or `'negative'` — the IC sign you predict.
- `hypothesis` is 20-500 chars of falsifiable microstructure reasoning.

## prim_v1 primitives (the ONLY callables allowed in formulas)

```
mid_price()
spread_ticks()
depth_sum(side, levels)        # side in {{'bid','ask'}}, levels in 1..5
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

- window: `'N_events'` with N in {ev_min}..{ev_max}, or `'Nms'`/`'Ns'` in 50ms..60s
- horizon: `'N_events'` with N in {hz_min}..{hz_max}, or `'Nms'`/`'Ns'` in 100ms..30s

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

{guidance}

## Example (pretty-printed for readability; emit it as ONE line)

```json
{example}
```
"""


def render_prompt(family: str) -> str:
    return _TEMPLATE.format(
        family=family,
        schema_filename=SCHEMA_FILENAME,
        ev_min=EVENT_WINDOW_MIN,
        ev_max=EVENT_WINDOW_MAX,
        hz_min=HORIZON_EVENT_MIN,
        hz_max=HORIZON_EVENT_MAX,
        guidance=FAMILY_GUIDANCE[family],
        example=json.dumps(FAMILY_EXAMPLES[family], indent=2, ensure_ascii=False),
    )


def render_schema_json() -> str:
    return json.dumps(candidate_json_schema(), indent=2, sort_keys=True) + "\n"


def render_all(prompts_dir: Path = PROMPTS_DIR) -> list[Path]:
    prompts_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for family in sorted(FAMILY_GUIDANCE):
        path = prompts_dir / f"{family}.md"
        path.write_text(render_prompt(family), encoding="utf-8")
        written.append(path)
    schema_path = prompts_dir / SCHEMA_FILENAME
    schema_path.write_text(render_schema_json(), encoding="utf-8")
    written.append(schema_path)
    return written


if __name__ == "__main__":
    for written_path in render_all():
        print(f"wrote {written_path}")
