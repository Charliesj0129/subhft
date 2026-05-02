# C17 — R47 Maker on TMF Rolling Front-Month

Round R10 candidate. APPROVED at T2 (2026-04-17). TMF analog of C14 (R6 PROMOTE).

## What this is

R47 maker strategy running on whichever TMF contract is currently front-
month. TMF front-month rotates through TMFB6 → TMFC6 → TMFD6 in the
R10 CK dataset. Same strategy as C14; different instrument.

## Relation to C14 (TXF PROMOTE)

- **Structurally identical** strategy class (`TmfFrontMonthMaker` mirrors
  `TxfFrontMonthMaker`).
- **Imports** R47 sub-states via composition; imports from C14 are NOT
  used (research artifacts are self-contained).
- **Differs only in**: cost model (TMF RT 4.0 pt vs TXF RT 0.48 pt),
  point value (10 NTD/pt vs 200 NTD/pt), default `spread_threshold_pts`
  (5 vs 3 — TMF's 4.0 pt RT needs a wider viable spread).

## SWITCH semantics (important)

C17 **replaces** the deployed TMFD6 R47 max_pos=1 strategy. They MUST
NOT run simultaneously — doing so would replicate the R51-C1b TMFD6-
multi-instrument kill direction (net −109K NTD).

Live deployment (post-shadow) requires:
1. Disable the existing `R47_MAKER_TMF` in `strategies.yaml`.
2. Flatten any open TMFD6 position at the current mid.
3. Enable `C17_TMF_FRONTMONTH_MAKER`.
4. Operator re-confirms before the first session.

This is documented in `SHADOW_DEPLOY.md` (created post-PROMOTE at T8).

## Rollover rule

Volume-crossover with calendar fallback — same as C14. Calendar windows
from R10-T1 §3:

| Contract | Window |
| -------- | ------ |
| TMFB6 | 2026-01-26 → 2026-02-25 |
| TMFC6 | 2026-02-26 → 2026-03-18 |
| TMFD6 | 2026-03-19 → 2026-04-14 |

See `frontmonth.py::FrontMonthSelector`.

Boundary behaviour: flatten outgoing → clear price memory → open
incoming. No cross-contract carry. The research harness supplies the
flattening trade at the session close of the outgoing contract at a
mid-price approximation; real execution cost is modelled in T5.

## Parameters

Defaults mirror C14 except `spread_threshold_pts`:

| Parameter | C14 (TXF) | C17 (TMF) | Rationale |
| --------- | --------: | --------: | --------- |
| `spread_threshold_pts` | 3 | **5** | TMF RT 4.0 pt requires larger viable spread |
| `max_pos` | 3 | 3 | R47 structural optimum — same |
| `inventory_skew_tenths` | 2 | 2 | 0.2 ticks/contract — same |
| Signal layers | disabled | disabled | R47 minimal — same |

## Files

| File | Purpose |
| ---- | ------- |
| `impl.py` | `TmfFrontMonthMaker` (MakerStrategy) + `C17Alpha` (AlphaProtocol shim) |
| `frontmonth.py` | TMF front-month selector + rollover helpers |
| `manifest.yaml` | AlphaManifest + cost model + governance + SWITCH semantics |
| `tests/test_c17.py` | Unit tests mirroring C14's (scale, rollover, gap, selector) |
| `README.md` | This document |
