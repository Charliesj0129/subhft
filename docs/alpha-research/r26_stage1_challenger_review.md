# R26 Stage 1 Challenger Review: L2 OFI + Hawkes + L1 Large Trade Filter

**Date**: 2026-04-01
**Reviewer**: Challenger Agent
**Artifact reviewed**: `outputs/team_artifacts/alpha-research/stage1_paper_intake.md`
**Verdict**: **REJECT** (5 unresolved challenges)

---

## Challenge 1: MLOFI on TMFD6 Is Structurally Degenerate (L2-L5 Adds Near-Zero Information)

**Concern**: The Researcher's core thesis rests on Xu et al. [A2] showing "out-of-sample R^2 improves monotonically with each additional level" on Nasdaq stocks. TMFD6 is a mini-futures contract with a 1-point tick size (10 NTD). On TMFD6:

- L1 = best bid/ask (spread typically 1-3 pts in March)
- L2 = 1 tick deeper = literally 1 point away from BBO
- L3 = 2 points away, L4 = 3 points, L5 = 4 points

In Xu et al.'s Nasdaq study, L2-L10 spans many tick sizes (Nasdaq tick = $0.01 but typical spread is multiple cents). On TMFD6, L5 is only 4 ticks from BBO. The entire L1-L5 depth window is compressed into a range where:

1. **Retail-dominated order flow means no institutional depth ladder.** Institutional participants (if any) would place orders further out or use iceberg/hidden orders invisible to the public book.
2. **R18 MLOFI microprice already tested L2-L5 and found L1-only IC=0.217 > full MLOFI IC=0.206.** The Researcher cites this direction but omits this directly contradictory result from their own prior round. L2-L5 depth was tested and found to add NEGATIVE information on a TWSE equity. Why would TMFD6 mini-futures be different?

**Evidence**:
- R18 detrended IC memory: "L2-L5 depth added nothing (L1-only IC=0.217 > full MLOFI IC=0.206)"
- TMFD6 March median spread = 3 pts (memory: tmfd6_microstructure_analysis)
- Xu et al. validated on Nasdaq (tick size $0.01, typical spread 1-5 cents = 1-5 ticks, L10 = 10+ ticks from BBO)

**Resolution required**: Before proceeding to Stage 2, extract TMFD6 L2-L5 data from ClickHouse and compute:
1. Per-level update frequency (how often does L3/L4/L5 actually change?)
2. Per-level incremental IC over L1-only OFI at 1s, 5s, 30s horizons
3. If L2-L5 update frequency < 10% of L1 frequency, or incremental IC < 0.01 at all horizons, this thesis is dead on arrival

---

## Challenge 2: R25 Falsified the Large Trade / Metaorder Thesis on TMFD6

**Concern**: The Researcher proposes an "L1 Large Trade Gate" requiring 5+ consecutive same-side trades as a regime filter. R25 already tested this exact mechanism and was KILLED:

- **41,000 sweeps/day** detected on TMFD6 in March — these are NOT rare institutional events, they are normal microstructure noise
- **82% of sweeps are one-shot** — no sustained same-direction OFI at 5s, directly falsifying the metaorder thesis
- **Mean return after sweep = 0.01 bps** — 133x below RT cost
- **60s returns are NEGATIVE (-0.07 bps)** — sweeps mean-revert, not continue

The Researcher acknowledges R25 in their risks section (point 7-8) but continues to propose the large trade gate as if R25's results are merely "a risk" rather than a conclusive empirical falsification on the exact same instrument.

Vaglica et al. [C3] and Maitrier et al. [C4] validate metaorder detection on LSE and Euronext respectively — deep liquid markets with genuine institutional execution algorithms. TMFD6 is a retail mini-futures contract. R25 proved TMFD6 does not have detectable metaorders from L1-L5 public data.

**Evidence**:
- R25 memory: "82% of sweeps are one-shot. Metaorder thesis falsified."
- R25 memory: "TMFD6 does not have detectable metaorders at L1"
- R25: 41K sweeps/day = every 0.7 seconds on average. Not a "regime gate" — it is nearly always active

**Resolution required**: The Researcher must either:
1. Provide a rigorous definition of "large trade" that differs from R25's sweep detection AND explain why it would produce different results, OR
2. Drop the large trade gate component entirely and justify the remaining 2-component signal (MLOFI + Hawkes only) as sufficient

---

## Challenge 3: Hawkes Calibration on 20 Trading Days Is Statistically Irresponsible

**Concern**: The three candidates all require Hawkes process calibration with state-dependent kernels. The parameter count is significant:

- **HW-DOFI**: 5 MLOFI weights (alpha), Hawkes baseline (mu), kernel strength (alpha_h), state amplification (gamma), decay rate (beta), sweep threshold (min_length), signal threshold = **~11 parameters**
- **MLOFI-DIV**: Branching ratio estimation window, endogeneity threshold (0.8), divergence weighting, sweep parameters = **~8 parameters**
- **HIBDP**: Rolling window (5s), MLOFI change threshold, Hawkes beta (2.0), EMA halflife (60s), breakout multiplier (2.0), volume threshold (3x median), sweep window (2s) = **~10 parameters**

We have **20 trading days** of TMFD6 data. Even splitting into IS/OOS 50/50 gives 10 days for calibration. With 8-11 parameters and 10 calibration days:

- Effective degrees of freedom per parameter: ~1 day
- Any walk-forward validation will have 2-4 folds at best
- Hawkes branching ratio estimation alone needs hundreds of events per window to be stable (Wu et al. [B3] used months of Eurex data)

The Researcher acknowledges this (risk #5: "overfitting on 20 trading days") but still proposes "strong regularization" as the fix. Regularization does not create data. With a 10-parameter model on 10 calibration days, you cannot distinguish signal from noise regardless of regularization method.

**Evidence**:
- TMFD6 data inventory: 20 actual trading days (memory: tmfd6_data_inventory)
- R14: "Data scarcity: 22 trading days, 4-5 with both sessions. Insufficient for most statistical tests."
- Wu et al. [B3] calibration: Bund and DAX futures, multiple months of tick data
- Morariu-Patrichi [B2]: MLE estimation on stock data spanning months

**Resolution required**: 
1. Specify the minimum data requirement for stable Hawkes calibration (literature suggests 60+ days minimum)
2. If 20 days is acknowledged as insufficient, this should be a Stage 2 BLOCKER, not a "risk to manage"
3. Consider: can we calibrate on TXFD6 (large TAIEX futures) and transfer to TMFD6? If so, is TXFD6 data available?

---

## Challenge 4: Signal Horizon Mismatch — The R16-R18 Killer Is Unaddressed

**Concern**: The Researcher correctly identifies signal horizon mismatch as risk #6 ("This was the killer for R16-R18") but provides no quantitative analysis of whether R26's signals survive the 36ms Shioaji P95 latency.

The literature cited validates on:
- Xu et al. [A2]: Contemporaneous 10-second bucket regression on Nasdaq (no latency consideration)
- Wu et al. [B3]: Eurex futures with co-location (sub-ms latency)
- Cont et al. [A1]: NYSE with data-center proximity

R14 established a "TXFD6 directional signal hard ceiling of ~0.001 bps capturable alpha." R16-R18 tested 60+ papers and ALL FAILED due to signal-horizon mismatch. The Researcher's claim that adding L2-L5 and Hawkes dynamics somehow escapes this ceiling has no empirical backing.

Furthermore, the detrended IC gate (feedback memory) warns that EMA-smoothed signals (which all three candidates use for Hawkes intensity estimation) are prone to trend contamination. The Researcher does not mention the detrended IC gate requirement anywhere in the proposal.

**Evidence**:
- R14: "directional signal hard ceiling ~0.001 bps" (memory)
- R16: "60+ papers, ALL FAILED. Signal-horizon mismatch" (memory)
- Shioaji P95 RTT: ~36ms (latency baseline doc)
- Detrended IC gate: "monotonically increasing IC with horizon = red flag for trend contamination" (feedback memory)

**Resolution required**:
1. Explicitly commit to the detrended IC gate as a mandatory validation step in Stage 2
2. Estimate the expected signal decay half-life for each candidate. If the half-life is < 100ms, explain how the signal is tradeable at 36ms latency
3. Address why R26 would escape the 0.001 bps ceiling that killed R14-R18. "More features" is not sufficient — quantify the expected improvement

---

## Challenge 5: Survivorship Bias in Literature and the "Novel Combination" Fallacy

**Concern**: The Researcher claims "no paper combines all three components (MLOFI + Hawkes + large trade filter) in a single framework" as a positive. This is actually a red flag. There are two possible explanations for why no one has combined them:

1. **Nobody thought of it** — unlikely given that MLOFI (2019), state-dependent Hawkes (2018), and metaorder detection (2009) have all existed for years and the HFT research community is well-connected
2. **It was tried and did not work** — negative results are rarely published (publication bias)

More critically, all 15 cited papers validate on deep, liquid Western markets:
- A1-A2: NYSE, Nasdaq (maker-taker, rebates, institutional flow, HFT competition)
- B1-B4: LVMH, Euronext Paris, Eurex (co-located MM, deep books)
- B5, C1: Nasdaq, CME (FOD data, order IDs)
- C5 (Kang): Korean equities — closest to Taiwan but still equities, not mini-futures

TMFD6 characteristics that invalidate transferability:
- **No maker rebates** (TAIFEX charges both sides)
- **Retail-heavy** (no detectable institutional metaorders per R25)
- **Compressed book depth** (L5 = 4 ticks from BBO)
- **Lower daily volume** than any instrument in the cited studies
- **Snapshot-based L5 data** (aggregate levels, not individual orders — Zotikov [C1] is inapplicable)

The Researcher acknowledges risks 10-12 partially but frames them as "open questions" rather than structural barriers. Given that R8-R25 (17 consecutive rounds) all failed on TMFD6, the burden of proof should be on the proposal to show why THIS combination would succeed, not merely that each component works individually on different markets.

**Evidence**:
- 17 consecutive failed alpha rounds on TMFD6 (R8-R25)
- R14: Pulido Trap — "LOB imbalance IS the MM's optimal strategy. Outsiders see the signal but can't extract PnL"
- TAIFEX fee structure: no rebates, pure cost at both sides
- R25: "L1 microstructure alpha on TAIFEX mini-futures is exhausted at retail cost — both continuous AND event-based"

**Resolution required**:
1. Identify at least one empirical validation of MLOFI (not just OFI) on a retail-dominated, no-rebate futures market
2. If no such validation exists, acknowledge this as a fundamental transferability risk and specify the concrete Stage 2 test that would resolve it (e.g., raw MLOFI incremental R^2 on TMFD6 L5 data before any Hawkes or gate overlay)
3. Address the Pulido Trap: if L2-L5 information is visible to MMs who are already quoting optimally, how does a retail latency participant (36ms) extract value?

---

## Minor Concerns (not blocking, but should be addressed)

### M1: Cherry-picked Hawkes literature
The Researcher cites 5 Hawkes papers [B1-B5] that all support the thesis. Missing from the review:
- Bacry et al. (2015) — "Hawkes Processes in Finance" — survey noting calibration instability in near-critical regime
- Rambaldi et al. (2017) — documented that Hawkes model fit deteriorates significantly out-of-sample for shorter calibration windows
- The Researcher should cite at least one paper documenting Hawkes limitations

### M2: Paper [A5] (Bieganowski 2026) is crypto
Validating OFI features on Binance cryptocurrency futures and applying conclusions to TMFD6 (regulated equity index futures) is a significant stretch. Crypto markets have 24/7 trading, no circuit breakers, different participant mix, and fundamentally different microstructure.

### M3: Candidate B branching ratio threshold (0.8) is arbitrary
The 0.8 threshold for regime switching (momentum vs mean-reversion) has no justification in the proposal. This is effectively another free parameter that must be calibrated on 20 days.

---

## Summary of Challenges

| # | Challenge | Severity | Status |
|---|-----------|----------|--------|
| 1 | L2-L5 structurally degenerate on TMFD6 (R18 already showed L2-L5 adds nothing) | CRITICAL | Unresolved |
| 2 | Large trade gate falsified by R25 (41K/day, 82% one-shot, mean-reverting) | CRITICAL | Unresolved |
| 3 | 11 parameters on 20 trading days = guaranteed overfitting | HIGH | Unresolved |
| 4 | Signal horizon mismatch unaddressed (R14-R18 killer, no detrended IC gate) | HIGH | Unresolved |
| 5 | No MLOFI validation on retail/no-rebate futures; Pulido Trap unaddressed | HIGH | Unresolved |

## Verdict: REJECT

Five unresolved challenges, two CRITICAL. The proposal is well-researched in terms of literature survey but fundamentally ignores the platform's own empirical history:

1. **R18 proved L2-L5 adds nothing on TWSE instruments** — the core "L2 OFI" thesis is already empirically contradicted
2. **R25 proved metaorders are undetectable on TMFD6** — the "large trade gate" is already falsified
3. **R14-R18 proved signal-horizon mismatch kills microstructure alpha at 36ms latency** — no mechanism proposed to escape this
4. **20 trading days cannot calibrate an 11-parameter Hawkes model** — this is not a "risk to manage" but a data insufficiency blocker

The Researcher must resolve at minimum Challenges 1 and 2 (both are empirically falsifiable with existing data) before this proposal can proceed to Stage 2. Specifically:

- **Immediate data test**: Extract TMFD6 L2-L5 from ClickHouse, compute per-level update frequency and incremental IC. If L2-L5 adds < 0.01 IC over L1, drop MLOFI and the entire proposal collapses.
- **Redefine or drop large trade gate**: Show how R26's detection differs from R25's sweep detection, with quantitative evidence of different outcomes.
- **Commit to detrended IC gate**: All candidate evaluations must pass the detrended IC gate (mandatory feedback rule).
- **Data sufficiency plan**: Either accumulate 60+ trading days before calibration, or demonstrate a transfer learning approach from TXFD6/other instruments.
