# R18 Stage 1 Challenger Review

**Date**: 2026-03-26
**Reviewer**: Challenger Agent
**Artifact reviewed**: `docs/alpha-research/round18_stage1_survey.md`
**Status**: Complete
**Candidates**: A (Reversal-Conditional Maker), B (Spread-Gated Selective LP), C (Inventory-Bounded Hybrid)

---

## Overall Verdict: CONDITIONAL APPROVE (B only) / REJECT (A standalone) / DEFER (C)

**Bottom line**: There is ONE testable hypothesis here -- "passive market making on TMFD6 is viable when spread >= 5 pts." The three "directions" are flavors of the same idea. Direction B is the cleanest test. Direction A has a fatal rebate dependency that the survey underplays. Direction C is premature until B validates the core hypothesis. Stage 2a adverse selection data (43.7% at +5s, monotonically decreasing with spread width) is encouraging but has critical measurement limitations that must be acknowledged.

---

## Candidate A: Reversal-Conditional Maker (RCM)

### Challenge A-1: Albers' Edge is NEGATIVE Without Rebates -- This Was Already Known from R16

**Claim challenged**: Survey cites Albers et al. +0.71 bp/RT as evidence of reversal-based maker profitability, with expected IC range "+0.5 to +1.5 bp per roundtrip."

**Objection**: This exact challenge was raised in R16 Stage 1 (`round16_stage1_qfintr_survey.md`, Challenge #1): "Paper 2502.18625 (Binance) includes +1.0 bps/RT maker rebate. Strip rebate: raw capture = -0.29 bps/RT." The R16 challenger REJECTED Candidate A (Contrarian Fill-Probability-Aware OpMM) on this basis. The R18 survey acknowledges the rebate issue in "Key Risks" but then proceeds to list "+0.5 to +1.5 bp" as the expected IC range -- a number that implicitly includes the rebate.

**Data from prior rounds**: R16 Stage 2 prototype (`round16_stage2_prototype.md`) actually built and tested the Albers reversal classifier on TXFD6: OOS AUC = 0.556 (barely above random), filter pass rate 99.9% (rejects almost nothing), PnL improvement ~0 bps. The verdict was "MARGINAL -- filter adds negligible value on top of OpMM's existing spread gate."

**Key point**: RCM is not a new idea. It was proposed in R16, prototyped, and found to add negligible value. The survey does not cite this prior work.

**Severity**: HIGH. Recycling a killed candidate without acknowledging the prior result is a process failure. The honest framing is: "RCM was tested in R16 on TXFD6 and was marginal. We hypothesize TMFD6's wider spreads change the outcome." This is testable but must be stated.

### Challenge A-2: Reversal Classifier Features Require Trade-Side Data We Do Not Have

**Claim challenged**: Survey describes "Signal logic: When bid queue >> ask queue... identify when reversals occur" using Albers' 4 feature groups (price dynamics, LOB state, recent trades, queue survival times).

**Objection**: This was also flagged in R16: "Fill prob model needs buy/sell (not in TickEvent)." Our L1 data does not include trade-side classification. The Stage 2a adverse selection analysis explicitly notes: "Volume field is always 0 in this L1 quote dataset, so trades are inferred from mid-price changes." This is a crude proxy -- non-price-moving trades are invisible. Albers' "recent trades" feature group (trade arrival rate, signed trade flow) cannot be constructed from our data.

Furthermore, "queue survival times" (Albers' 4th feature group) require order-level data that TAIFEX does not provide to retail participants.

**Severity**: HIGH. 2 of 4 feature groups from Albers' model are unavailable. The classifier will be materially weaker than the paper's results.

**Verdict for A: REJECT as standalone direction.** The Albers reversal model has been tested (R16, marginal), depends on rebates we lack, and requires features we cannot construct. If pursued at all, it should be a minor enhancement to Direction B, not an independent direction.

---

## Candidate B: Spread-Gated Selective LP (SG-LP)

### Challenge B-1: Stage 2a "PASS" Has Upper-Bound Bias That Flatters the Result

**Claim challenged**: Stage 2a measured adverse selection at 43.7% (+5s, spread >= 5) and declared "PASS" against the 60% kill threshold.

**Objection**: The Stage 2a analysis itself states: "Only price-moving events are captured. Non-price-moving trades (passive fills within spread) are invisible. This biases the sample toward informed flow, making our adverse selection rates an UPPER BOUND on the true rate."

Wait -- this is backwards. If we only see price-moving events, we are measuring adverse selection among the most informationally-loaded trades. Non-price-moving trades (passive fills that don't move the mid) are MORE likely to be uninformed. Including them would LOWER the adverse selection rate, not raise it. The Stage 2a analysis claims the measured rate is an upper bound, which is actually favorable -- the true rate is likely lower.

However, there is a subtler problem: the "fills" in Stage 2a are not actual maker fills. They are mid-price changes used as a proxy. A real maker sitting at the bid gets filled when someone sells to them. The mid-price may or may not change on that fill. The fills that DO move the mid-price are precisely the ones where informed flow is arriving -- the maker's worst case. The fills that DON'T move mid (uninformed) are invisible in this analysis. So the 43.7% measures adverse selection among the informed-flow subset, not among all fills a maker would experience.

**Net assessment**: The true adverse selection rate for a maker at touch is probably LOWER than 43.7%, which is favorable. But the magnitude of the bias is unknown. Stage 2 should attempt to bound it (e.g., by estimating the fraction of fills that are price-moving vs non-price-moving using trade count proxies).

**Severity**: MEDIUM. The bias direction is favorable (true rate likely < 43.7%), but the unknown magnitude creates uncertainty. Not a blocker.

### Challenge B-2: The "45.5% Eligible Time" Understates the Effective Downtime

**Claim challenged**: Survey states TMFD6 has "45.5% profitable-spread time" when spread >= 5 pts, implying the strategy is active nearly half the session.

**Objection**: "Eligible time" is not the same as "active quoting time." A maker strategy must:
1. Wait for spread >= 5 (55% of time: idle)
2. Post a limit order at touch
3. Wait for fill (at 0.15 fills/sec in wide-spread regime per R16, median wait = several seconds to minutes)
4. If filled on one side, wait for the other side to fill OR cross the spread to unwind inventory

Steps 3-4 add significant idle time even within the "eligible" window. The R16 challenger estimated realistic RTs at 200-600/day for a single-contract maker on TMFD6 (`round16_tmfd6_challenger_review.md`). At 300 min per session and 400 RTs average, that's ~1.3 RTs/min -- one complete trade cycle every 45 seconds. This is feasible but means most of the "45.5% eligible time" is spent waiting, not earning.

**Critical dependency**: The fill rate during wide-spread periods determines whether this strategy generates enough trades for statistical validation. The R16 data showed fill rate drops from 3.5/s (tight spread) to 0.15/s (wide spread) -- a 23x reduction. At 0.15 fills/sec and 45.5% eligible time, expected fills per 5-hour session: ~1,230 one-sided fills. After queue priority (assume 30-50% hit rate for a 1-lot maker): 370-615 fills per side. RT completion within 60s: unknown but critical.

**Data required**: Stage 2 must simulate realistic RT completion rates before any P&L estimation. Without this, the "net P&L +5 pts/fill" from Stage 2a is half-trip P&L, not round-trip P&L.

**Severity**: MEDIUM-HIGH. Not fatal but the P&L estimates are single-leg, not round-trip. The second leg (inventory unwind) systematically faces worse economics.

### Challenge B-3: R16's "Wide Spread = Adverse Selection Trap" Finding on TXFD6 -- Why Is TMFD6 Different?

**Claim challenged**: Survey argues TMFD6 wide spreads are "benign" because they are common (45.5%), unlike TXFD6's rare wide spreads (2.1%).

**Objection**: The Stage 2a data actually SUPPORTS the survey's claim -- adverse selection decreases monotonically from 49.2% at [5-9] to 23.9% at [40+], and spread-vol correlation is near-zero (+0.051). This is genuinely different from the TXFD6 pattern.

However, the mechanism is not explained. WHY does adverse selection decrease with spread on TMFD6? Two hypotheses:
1. **Liquidity withdrawal**: Wide spreads occur when market makers withdraw (low information, not high information). Fills during these periods are from uninformed flow arriving into a thin book. This is the "benign" hypothesis.
2. **Selection bias in measurement**: At very wide spreads, the mid-price is noisier (wider jumps between bid/ask levels). A mid-price change at spread=40 could represent the bid quote disappearing and reappearing, not an informed trade. The decreasing adverse rate could be a measurement artifact of noisier mid-price estimates.

**Data required**: Distinguish these hypotheses by checking whether the decreasing adverse rate at wide spreads persists when using trade price (not mid-price) as the reference. If bid/ask quote updates (not trades) drive the mid-price changes at wide spreads, the measurement is noise, not signal.

**Severity**: MEDIUM. The data is favorable, but the mechanism should be understood before building a strategy on it.

### Challenge B-4: Corrected Cost Model (39.2 NTD, not 40 NTD)

**Claim challenged**: Survey states "RT cost: 4 pts (40 NTD)" throughout.

**Objection**: Per updated constraints, the corrected RT cost is 39.2 NTD (tax 6.6 + comm 13 per side), breakeven = 3.92 pts = 1.19 bps. The survey uses 40 NTD / 4 pts / 1.33 bps. This is a minor difference (~2%) but systematically biases all P&L estimates by ~0.08 pts per trade. Over 400 trades/day, this is +32 pts/day (+320 NTD) -- not nothing, but not a game-changer.

**Severity**: LOW. Correcting from 4.0 to 3.92 pts RT cost marginally improves economics. Not a challenge, just a housekeeping note.

**Verdict for B: CONDITIONAL APPROVE.** The core hypothesis is testable and Stage 2a data is encouraging. Conditions:

1. Stage 2 must estimate round-trip P&L (both legs), not just single-leg adverse selection
2. Must simulate realistic fill rate and queue priority for a 1-lot retail maker
3. Must add minimum-N kill gate: if expected complete RTs per session < 10, strategy is not viable
4. The mechanism behind decreasing adverse selection at wider spreads should be investigated

---

## Candidate C: Inventory-Bounded Hybrid (IBH)

### Challenge C-1: Parameter Count vs Data -- Classic Overfitting Setup

**Claim challenged**: IBH combines spread gate + reversal filter + A-S inventory skew + phi_8min directional filter, with parameters: gamma (risk aversion), spread threshold, reversal threshold, inventory cap, phi window, A-S sigma, A-S k (order arrival), A-S A (order arrival intensity).

**Objection**: That is 8+ free parameters. With 58 days of data (and only 45.5% eligible time), the effective sample for calibration is ~26 session-equivalents. At 400 RTs/day, total N ~ 10,400. For 8 parameters, the degrees-of-freedom-adjusted significance requires careful cross-validation -- but the survey proposes no validation methodology.

R13's A-S-style MM on TXFD6 showed: "P2-lite selective IS +3.80 but OOS FAIL." This is exactly the overfitting pattern: in-sample optimization finds profitable parameters, out-of-sample they fail. IBH has more parameters than R13's P2-lite, making overfitting MORE likely, not less.

**Severity**: HIGH. IBH should not be attempted until B validates the core hypothesis with zero signal parameters (just the spread gate).

### Challenge C-2: phi_8min IC=0.041 Was Measured as a Taker Signal, Not a Maker Skew Signal

**Claim challenged**: Survey cites phi_8min (IC=0.041, orthogonal to OFI, R17) as a directional filter for quote skewing.

**Objection**: IC=0.041 was measured as a predictor of forward returns (taker context: "if I enter a position in this direction, do I make money?"). Using it as a maker skew signal is a different application: "if I skew my quotes in this direction, do I get better fills?" These are related but not identical. A signal that predicts returns may not help a maker if:
- The signal is already reflected in the order flow (informed traders already acting on it)
- The signal's horizon (8 min) is longer than the typical inventory holding time
- The skew moves quotes away from BBO, reducing fill probability

No prior round has tested phi_8min as a maker skew signal. The IC=0.041 is not directly transferable.

**Severity**: MEDIUM. Not fatal, but the claimed "composite edge" from combining 4 components is speculative without any component being validated in the maker context.

**Verdict for C: DEFER until B completes.** IBH is premature. It adds complexity without first validating the simplest version of the hypothesis. If B shows positive results, C can be considered as an enhancement. If B fails, C fails with it.

---

## Cross-Cutting Challenges

### Challenge X-1: The Survey Presents One Hypothesis as Three Independent Directions

The existing challenger review at `docs/alpha-research/round18_stage1_challenger_review.md` already identified this: "R18 is best understood as 'R13 re-run on TMFD6 with a spread gate.'" I concur fully. The three "directions" share:
- Same instrument (TMFD6)
- Same mechanism (passive limit orders)
- Same economic thesis (spread > cost at wide spreads)
- Same kill condition (high adverse selection kills all three)

Framing them as independent directions is misleading. The survey should be reframed as: "One core hypothesis (SG-LP) with two optional enhancements (RCM reversal filter, IBH inventory framework)."

### Challenge X-2: Night Session Validity Not Addressed

TMFD6 night session (15:00-05:00) accounts for 59% of wide-spread trade proxies per Stage 2a. The survey does not discuss whether the strategy would operate during night session. Night session characteristics differ significantly:
- Different participant mix (more international, less retail)
- Different volatility pattern (US market hours overlap)
- R17 found Thursday night = -467 pts systematic selloff (C4 pattern)

If the strategy runs during night session, it must account for these differences. If it doesn't, eligible time drops from 45.5% to ~18% (day session only), dramatically reducing trade count.

### Challenge X-3: The Survey Does Not Cite Its Own Prior Work (R16 Prototypes)

The Albers reversal classifier was prototyped in R16 (`research/alphas/fill_prob_filter/`). The inventory skew optimization was prototyped in R16 (`research/alphas/inventory_skew_opt/`). Neither is cited. This creates a risk of repeating failed work without learning from it.

**Required**: Stage 2 must explicitly reference R16 prototype results and explain what has changed (instrument, spread regime, data size) that justifies re-testing.

---

## Summary Table

| # | Challenge | Target | Severity | Resolution Required |
|---|-----------|--------|----------|-------------------|
| A-1 | Albers edge negative without rebates; R16 already tested | A (RCM) | HIGH | Acknowledge R16 results; downgrade to B enhancement |
| A-2 | 2/4 Albers feature groups unavailable in our data | A (RCM) | HIGH | Cannot build full reversal classifier |
| B-1 | Stage 2a adverse selection is upper-bound (favorable bias) | B (SG-LP) | MEDIUM | Bound the bias magnitude |
| B-2 | 45.5% eligible time overstates active trading time | B (SG-LP) | MEDIUM-HIGH | Simulate realistic RT completion rates |
| B-3 | Mechanism for decreasing adverse selection at wide spread unknown | B (SG-LP) | MEDIUM | Investigate quote-update vs trade-driven mid changes |
| B-4 | RT cost should be 3.92 pts not 4.0 pts | All | LOW | Correct in all calculations |
| C-1 | 8+ parameters with 58 days = overfitting risk | C (IBH) | HIGH | Defer until B validates core hypothesis |
| C-2 | phi_8min IC measured in taker context, not maker skew | C (IBH) | MEDIUM | Not transferable without validation |
| X-1 | Three directions are one hypothesis | All | MEDIUM | Reframe honestly |
| X-2 | Night session characteristics unaddressed | All | MEDIUM | Define session scope |
| X-3 | R16 prototypes not cited | A, C | MEDIUM | Reference prior work |

---

## Recommended Stage 2 Sequencing

1. **First** (BLOCKER): Simulate realistic round-trip P&L for a 1-lot maker at spread >= 5 on TMFD6. Include: queue priority, both-leg completion, inventory holding time. If net RT P&L <= 0, kill all three directions.

2. **Second**: If RT P&L > 0, implement Direction B (SG-LP) as a minimal backtest: post at touch when spread >= 5, cross to unwind after 60s timeout or adverse move > threshold. No signal, no filter, no skew. Pure spread capture.

3. **Third** (optional): If B shows positive OOS P&L, test whether adding RCM-style reversal filter (using available features only -- not Albers' full model) improves results. If not, B alone proceeds.

4. **Do not attempt C** until B and optionally A are validated independently.
