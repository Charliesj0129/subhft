# C68 — TXF Rollover-Week Back-to-Front Passive Maker

**Run**: `alpha-research-20260419-inst-options` | **Round**: R4 | **Status**: prototype

## Intent

Passive maker on the back-month TXF contract DURING the 3-day rollover
window when that contract is transitioning to become the new front-month.
Targets the narrow-spread 12-16 pt window observed in the TXFD6 Feb
2026-02-23..25 transition (the sole direct analog).

## Critical framing correction from Researcher T1

The task-brief "back-front hedge pair" framing is **REJECTED**. T1
arithmetic:
- Per-trip gross: 14 pt (2 x half-spread on narrow 14 pt spread)
- Combined inst RT (both legs): 3 pt
- Adverse selection: 1.6 pt
- Passive-both-legs per-trip net: **+7.8 pt/RT** viable
- TAKE-hedge-leg per-trip net: +7.8 - (2 x 4.35) = **-0.9 pt/RT** (inverts sign)

C68 is therefore **SOLO PASSIVE MAKER** on the back-month with calendar
gating to activate only during the rollover window. The "hedge" is a
risk-offset discipline (wait for opposite-side passive fill on the SAME
instrument), NOT a cross-instrument taker leg.

## Mechanism (R47 three-layer + calendar gate)

- **L0 Calendar Gate (NEW)** — active only during
  `[rollover_window_start_date, rollover_window_end_date]` (inclusive).
- **L1 Spread Gate** — `spread >= 12 pt` (TXFD6 Feb analog median).
- **L2 Signal Layers** — all four (PE/Queue/MFG/QI) DISABLED (R47-minimal).
- **L3 Execution** — fresh-quote requoting, tick-grid snap, LINEAR 0.2
  ticks/contract inventory skew, max_pos=1 canonical.
- **Emergency unwind** — `emergency_unwind_required()` returns True if
  window closes with `|pos| > 0`. Caller MUST flatten via taker cross;
  spreads revert to 100+ pt outside the window.

## Cost Model (MANDATORY CITATION)

- **Source**: `outputs/team_artifacts/alpha-research/shared-context.yaml#cost_model.TXF`
- **Tier**: `institutional_estimate` (NOT broker-confirmed)
- **RT**: 1.5 pt per leg (combined 3 pt for both legs of a round trip)
- **PROMOTE flag** (MANDATORY before any live deploy):
  - `requires_broker_confirmation_before_live: true`
  - `requires_fresh_txfe6_transition_data: true` (post-2026-04-18)

## Files

- `impl.py` — `TxfRolloverBackFrontMaker` (`MakerStrategy` protocol) +
  `C68Alpha` (`AlphaProtocol` shim) + `is_in_rollover_window` helper.
- `manifest.yaml` — full manifest with `distinction_from_killed_classes`
  (R8-prior C36 retired-contract, R5-prior C30 hedge-qty, C33 cluster,
  deep-back-month), T1 carry-forward flags, two-gate PROMOTE prerequisite.
- `README.md` — this file.
- `test_alpha.py` — 50+ tests covering spread gate, calendar gate,
  all four D1-D4 signal layer absence, max_pos {1, 2, 3}, inventory skew,
  bid/ask execution, emergency_unwind_required, AlphaProtocol conformance.
- `__init__.py` — `ALPHA_CLASS = C68Alpha` registry entrypoint.

## Quick Smoke

```python
from datetime import date
from research.alphas.c68_txf_rollover_back_front_maker import (
    C68Alpha, C68Params,
)

params = C68Params(
    spread_threshold_pts=12,
    max_pos=1,
    rollover_window_start_date=date(2026, 4, 13),  # e.g. 3-day window
    rollover_window_end_date=date(2026, 4, 15),
)
alpha = C68Alpha(params=params, active_symbol="TXFE6")
alpha.maker.set_session_date(date(2026, 4, 14))
assert alpha.manifest.instrument == "TXFE6"
```

## Key risks for T5/T6

1. **Sample size**: 3-day analog window only. Walk-forward k=5 requires
   ~3 months of live shadow.
2. **Target-window data not yet in CK**: TXFE6 becoming new-front ~2026-04-15
   is after current CK snapshot (2026-04-14). T5 must use TXFD6 Feb analog
   or wait for post-2026-04-18 data.
3. **Competitive crowding**: 12-16 pt spread already reflects active makers.
   Marginal capture may be thin.
4. **Linear estimate is 5-8x optimistic** (R1 lesson). Fresh CK-direct in T5
   likely yields ~700K-1.1M NTD/year, not 5.6M.

## Live-path wrapper

NOT in scope for T4 — ships only if T7 PROMOTEs and user approves. If built,
would extend the C33 wrapper with a calendar-gate check against the active
TXF rollover schedule and dynamic instrument-alias resolution (TXFE6 ->
whatever is back-month at deploy time).
