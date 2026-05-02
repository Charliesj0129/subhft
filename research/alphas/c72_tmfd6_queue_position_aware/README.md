# C72 — TMFD6 Queue-Position-Aware Maker (C60 overlay)

**Run**: `alpha-research-20260419-inst-options` | **Round**: R5 | **Status**: prototype

## Intent

Overlay on C60 (PROMOTED TMFD6 R47-minimal, R1 PROMOTE_CONDITIONAL). Adds a
per-side **L1 queue-depth gate**: quote buy only when `bid_qty <= threshold`;
quote sell only when `ask_qty <= threshold`. Targets the hypothesis that
thin-near-side-queue quoting secures better queue position and improves
per-trip edge.

## Observability resolution (Researcher T1 flag #3)

Real "self-queue-position" is a simulation-internal attribute (our
arrival-rank in the queue). Using it would re-introduce the PowerProb
14× pessimism that R47 SKILL empirically DISABLED. C72 uses a
**CK-observable proxy**: gate on top-of-book `bid_qty` / `ask_qty` from
the tick event. This avoids the PowerProb trap but is a DIFFERENT
mechanism from the original "queue-position-near-top" concept.

## Mechanism

Preserves C60 baseline exactly:
- L1 spread gate (≥ 5 pt)
- D4 QI skew (threshold 0.10, widen 1 tick, enabled)
- D1/D2/D3 disabled
- Linear inventory skew (0.2 ticks/contract; non-|pos|-gated)
- max_pos = 2 canonical

Adds:
- **L0-new queue-depth gate**:
  - Buy side: admit only if `bid_qty <= queue_depth_max_bid` (default 5)
  - Sell side: admit only if `ask_qty <= queue_depth_max_ask` (default 5)
  - Independent per-side gates.
  - T5 sweeps threshold ∈ {2, 5, 10, 20}.

## Cost Model (MANDATORY CITATION)

- **Source**: `shared-context.yaml#cost_model.TMF` (inst RT 1.5 pt, ESTIMATED)
- **PROMOTE flag**: `requires_broker_confirmation_before_live: true`
- **Dominance check required**: per T5, must show per-trip improvement
  >=30% OR adverse-selection reduction >=20% vs C60 at same fill retention.

## Files

- `impl.py` — `TmfD6QueuePositionAwareMaker` (`MakerStrategy`) +
  `C72Alpha` (`AlphaProtocol` shim).
- `manifest.yaml` — full manifest, T1 carry-forward flags,
  `dominance_risk: true` prerequisite.
- `README.md` — this file.
- `test_alpha.py` — 50+ tests including queue-depth gate boundaries,
  per-side independence, disable/enable switch, C60-baseline-equivalent
  when gate off.
- `__init__.py` — `ALPHA_CLASS = C72Alpha`.

## Key risks for DA T2 / T5

1. **Lipton-PSS 2013 adverse-selection**: thin near-side queue is
   empirically correlated with price movement AWAY from that side
   (adverse for the maker on that side). C72 gate may quote
   INTO adverse flow rather than capture priority.
2. **Dominance vs C60**: T1 scenario showed C72 PnL ≤ C60 at ±30% RT
   under +30% per-trip edge assumption. Must validate empirically.
3. **R47 D2 Queue precedent**: D2 Queue layer was disabled in C60 for
   PowerProb pessimism reasons. C72's L1-depth gate is a different
   mechanism but same conceptual family — caution.
4. **Threshold sensitivity**: T5 sweeps {2, 5, 10, 20} because the right
   threshold depends on empirical TMFD6 L1 queue distribution.
