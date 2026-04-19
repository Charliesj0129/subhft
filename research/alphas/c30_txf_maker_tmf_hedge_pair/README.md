# C30 — TXFD6 passive maker with TMFD6 delta hedge (cross-instrument MM pair)

**Round**: R5 (alpha-research-20260418-reboot)
**DA T2 verdict**: APPROVE (0 Tier-1/Tier-2 FAILs, 6 Lead-attention WARNs —
all addressable in T5 scorecard)
**Status**: PROTOTYPE — implementation complete, T5 backtest pending.

## Mechanism (one paragraph)

Quote TXFD6 passively (R47-minimal: spread gate + max_pos + fixed
inventory skew; no PE / queue / MFG signal layers). Each passive TXFD6
fill adds to inventory. Once |TXF_inventory| (in notional points) crosses
a configurable threshold (default 20 pt = 1 TXF contract in
notional-equivalent to 20 TMF), a taker hedge is sent on TMFD6 at the
far-side top-of-book, sized at `|TXF_contracts| × 20` TMF contracts (the
20:1 notional ratio: 200 NTD/pt on TXF vs 10 NTD/pt on TMF). After the
hedge, TXF inventory remains in place; the TMF position provides an
offsetting delta. Flatten all residuals at end of day.

## H3 differentiation from TX-TMF leadlag (R26/R28 KILL)

The killed TX-TMF leadlag class used a **directional predictor** — TXF
price moves were used to forecast TMFD6 direction (scalar IC alpha, RT
cost 7.4 pt vs edge 2.47 pt).

C30 does **NOT** use any cross-instrument predictor. Specifically:

1. The TXF leg quotes only off its own book (spread gate, fill side, own
   inventory). No TMF data enters its decision path.
2. The TMF hedge is triggered solely by TXF inventory magnitude, not by
   any price-prediction relationship. A random-timing hedge would
   reproduce positive MTM per cycle (Researcher empirical check in the
   T1 counterfactual).
3. Edge source is TXF half-spread capture minus drift during inventory
   accumulation minus TMF RT cost. No leadlag term appears.

Mechanism class = cross-instrument pair MM (execution structure). Not a
repackaged leadlag.

## Layered reference

| Layer | Component | Source |
|-------|-----------|--------|
| L1 spread gate | `_TxfMakerLeg._on_bidask` at `spread_threshold_pts` | R47 convention (`spread_threshold_pts=5` matches deployed TMFD6 R47 baseline) |
| L2 skew | `inventory_skew_tenths=2` (0.2 tick per contract) | R47 structural best practice |
| L3 hedge trigger | `_TmfHedgeLeg.compute_hedge` on |TXF_pos_pts| >= trigger | C30 novel element; T5 brackets trigger |
| L4 hedge execution | TMF far-side TOB + configurable slippage | T5 will model slippage 0..1 pt per DA WARN #4 |

## Scaled-integer conventions

- **CK storage**: price fields are scaled x1,000,000 (TickData `scale` default).
- **Live platform**: TXFD6 and TMFD6 prices are scaled x10,000 in production.
- **This module operates on the `scale` field of the incoming `TickData`** —
  no hard-coded scale factor. All arithmetic uses integer math; the only
  floats are MTM reporting and cycle statistics in the downstream backtest
  harness.

## Cost model (strict cite from `memory/feedback_taifex_fee_structure.md`)

User-confirmed 2026-04-18:

| Leg | RT cost (pt) | Point value (NTD/pt) | Median spread (pt) | Cost drag |
|-----|-------------:|---------------------:|-------------------:|----------:|
| TXFD6 maker | 3.0 | 200 | 4.3 | 75% (WARN) |
| TMFD6 hedge taker | 4.0 | 10 | 2 (current regime) | 200% if measured as spread capture (does **not** apply to hedge leg) |

**Per covered cycle**: `txf_amortized = n_fills × 1.5 pt` (half RT per
maker fill) + `tmf_hedge = 4 pt` per hedge event. The T1 counterfactual
showed avg cycle cost ≈ 191 pt; avg cycle MTM ≈ +198 pt on upper-bound
fills; net ≈ +7 pt per covered cycle at 100% queue share.

DA WARN #1 (bright-line 75% TXF / 200% TMF-hedge cost drag): structural;
do **not** widen the TXF spread gate below 5 without fresh OOS TXFD6
regime evidence.

## Coexistence with deployed R47 (TMFD6 max_pos=1 maker)

The C30 TMF hedge leg and the deployed R47 TMFD6 maker leg **share the
TMFD6 instrument**. Both active simultaneously implies combined
exposure:

    combined_TMF_pos = r47_pos (in [-1, +1])
                     + c30_tmf_hedge_pos (in [-60, +60] at |TXF|=3)

Position accounting MUST distinguish the two flows via `strategy_id`
(CLAUDE.md architecture rule 12, ExposureStore cardinality). The T5
scorecard reports combined exposure on days when both strategies would
have been live.

## T5 backtest plan (executor's own checklist)

Per DA WARN coverage:

1. **Queue-share bracket**: run `{1, 2, 5, 10}%` — report net PnL/day at each.
   DA flagged q=1% as NEG-EV; q≥2% is the qualifying threshold.
2. **Inventory-trigger bracket**: run `{10, 20, 40}` pts — report cycle
   count, avg cycle net, daily net at each.
3. **Bid/ask execution** (not mid) per DA H6 ruling. Hedge leg uses TMF
   far-side TOB + configurable slippage.
4. **|pos|-quartile decomposition** per C22 lesson: PnL should concentrate
   at the upper-|pos| quartiles (those are the cycles that produce
   hedges). If |pos|=3 share does not increase share of total PnL
   post-hedge, the hedge mechanism is not providing its expected benefit.
5. **Baseline comparison**: C14 TXF sole-maker on same OOS days (no
   hedge) vs C30 (with hedge). Report uplift delta.
6. **Counterfactual partition**: gate-active (hedge-in-flight) vs
   gate-inactive windows on baseline C14 — guards against C13-inversion
   class.

## Shadow-kill criterion

TXFD6 sp_med < 3 pt for 3 consecutive sessions → auto-disable (matches
the TMFD6 Mar→Apr regime-shift trajectory that killed TMFD6-centric
variants).

## Files

- `impl.py` — `TxfTmfPairMaker`, `_TxfMakerLeg`, `_TmfHedgeLeg`,
  `C30Alpha` (AlphaProtocol conformance)
- `manifest.yaml` — Gate-A-ready metadata, cost model, governance links
- `tests/test_alpha.py` — unit tests (scaled-int assertions, monotonic
  time, factory fixtures per `hft-test-hft`)
- `README.md` — this file
