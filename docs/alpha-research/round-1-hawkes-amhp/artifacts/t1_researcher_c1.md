# T1 Researcher Report — R52 C1 (AMHP Dynamic-Spread MM, TMFD6)

**Run**: `alpha-research-20260425-hawkes-amhp` (R52)
**Candidate**: C1 — AMHP-driven dynamic-spread maker (pure_maker, TMFD6)
**Researcher**: Researcher (Opus)
**Date**: 2026-04-25
**Latency profile**: `v2026-04-24_measured` asymmetric (submit/modify P95 = 395 ms, cancel P95 = 59 ms)
**Cost basis**: TMF retail RT = 4 pt = 40 NTD (cited from `memory/feedback_taifex_fee_structure.md`, confirmed 2026-03-26)

---

## 1. Hypothesis

The AMHP (Adaptive Multi-scale Hawkes Process, "六、AMHP") delivers two real-valued, non-directional regime descriptors at quote-decision time on TMFD6: rolling branching ratio ρ̂(t) and intensity-imbalance ratio IIR(t) ∈ [−1, +1]. C1 hypothesizes that **sizing maker spread as a continuous function of these state descriptors** — `spread(t) = base × f(ρ̂, |IIR|)` — produces strictly more captured edge per fill than R47's static `spread ≥ 5 pt` gate, because (a) when the market is calm and ρ̂ is small, the dynamic spread can narrow toward base (≈4-5 pt) and harvest the short tail of profitable-spread minutes that R47 is gated out of; and (b) when ρ̂ approaches the criticality boundary (per "六、" the 0.85 alarm) or |IIR| spikes, the maker preemptively widens by ×1.5–3 to keep the half-spread above the rising adverse-selection cost.

The strategy stays **strictly non-directional** (quotes both sides at all times). It also stays at R47's `max_pos=3` setting (per `hft-mm-design` structural property — `max_pos=1` collapses R47 from +4,534 to −1,407 pts). The deviation from R47-A1 (which deployed `max_pos=1` and is the run's anchor for failure data) is intentional and is the first axis of difference. Note up-front: the H4-class single-day-dominance pathology (R47-A1, 96.9% PnL from one day under the same measured profile) is the single largest threat — the proposal must produce a credible mechanism-level argument for **distribution diffusion across days**, not merely a per-quote efficiency improvement.

---

## 2. Mechanism — Three-Layer Breakdown (per `hft-mm-design`)

### L1 — Spread Gate (replaced, not removed)

R47 baseline:

```
quote iff spread_obs >= spread_threshold_pts (= 5 for TMFD6)
```

C1 replaces the static threshold with an AMHP-modulated effective spread floor:

```
spread_target_pts(t) = base_spread_pts * g(ρ̂(t), |IIR(t)|)
                     = 5  *  ( 1 + α_ρ * max(0, ρ̂(t) − ρ_low)
                                  + α_IIR * |IIR(t)| )

quote iff observed_spread_pts >= max( cost_floor + buffer , spread_target_pts(t) )
            where cost_floor + buffer = 5 pt   # always enforce H1
```

Two named regimes emerge:

| Regime              | ρ̂        | \|IIR\|  | Multiplier | Action                                   |
| ------------------- | ---------:| -------:| ----------:| ---------------------------------------- |
| Calm                | < 0.55   | < 0.20  | 1.0×        | Quote at floor (5 pt)                    |
| Normal              | 0.55–0.75| 0.20–0.45| 1.3–1.7×    | Widen to 7–9 pt                          |
| Tense               | 0.75–0.85| 0.45–0.70| 1.7–2.2×    | Widen to 10–12 pt; reduce active size 50% |
| Critical (gate C2)  | > 0.85   | > 0.70  | 2.5–3.0×    | Either widen 12–15 pt OR cease (overlay) |

The cost-floor is a hard floor — C1 never narrows below the R47 5 pt threshold. This preserves H1 (`spread_target ≥ RT cost + small margin`) at all times.

### L2 — Signal Layers (D1–D4 vs new D5/D6)

C1 keeps R47's validated D1–D4 defaults (PE disabled, queue-cancel disabled, MFG disabled, QI active at θ=0.10) and **adds two AMHP signal layers**:

* **D5 — AMHP spread modulator**: the L1 multiplier above. Acts on quoted half-spread.
* **D6 — IIR side-asymmetry**: when |IIR| > 0.5, optionally widen one-sided (only the side where intensity-imbalance predicts adverse fill). This is *not* a directional signal — both sides remain quoted, only the *magnitude* on one side widens.

### L3 — Execution Layer (R47 baseline, unchanged)

Pending tracking, price-movement gate, tick-grid snapping, gap resilience, risk-feedback handling — **all inherited from R47**. Cancel speed (P95 = 59 ms under measured profile) is structurally favored: when L1 raises spread_target abruptly (ρ̂ regime shift), C1 can cancel-and-replace within 59 ms while a competing slower-cancel maker is still adversely picked off. This is a quiet but real exec-layer benefit of the asymmetric profile.

`max_pos = 3` (non-negotiable per `hft-mm-design` structural property #1). `inventory_skew_ticks = 0.2`.

---

## 3. Q1 — Edge vs Cost (mandatory cost-source citation)

### Cost basis (cited from memory, NOT inferred from research configs)

* `feedback_taifex_fee_structure.md` (last confirmed 2026-03-26):
  * **TMF retail RT = 4.0 pt = 40 NTD** (commission 13 NTD/side × 2 + sell-side tax 7 NTD)
  * Bps form: 1.33 bps @ 30K mid
  * Per-side cost = 2.0 pt
* No maker rebate available (retail tier).

### Cost-drag (mandatory bright-line WARN)

```
cost_drag = RT_pts / median_spread_pts = 4 / 4 = 1.00  (100%)
```

**This crosses the bright-line WARN at 50%.** Per role file rule §8, this MUST be addressed explicitly in §9 Risk. The baseline TMFD6 microstructure (median spread 4 pt) means a **passive maker that captures the median spread breaks exactly even** at retail cost. Net edge depends entirely on (a) capturing above-median spread minutes and (b) avoiding adverse-selection sub-events.

### Per-fill edge bar for H1

Bid/ask execution rule (`taifex-alpha-kill-criteria` Gate: Bid/Ask Execution Reality, mandatory for edge < 2× spread):

```
H1 floor = 2 × RT = 8 pt expected per-fill edge
(must clear under bid/ask, not mid-price)
```

### Expected per-fill edge — best-case derivation under literature priors

C1 only fills when `observed_spread ≥ spread_target(t)`. Since `spread_target ≥ 5 pt` always (cost-floor), the maker captures ≥ half_spread = 2.5 pt per side gross. Per `taifex-market-structure`, profitable-spread regime statistics on TMFD6:

| Statistic                                | Value     |
| ---------------------------------------- | --------- |
| Median spread                            | 4 pt      |
| p75 spread                               | 19 pt     |
| % of time spread ≥ 5 pt                 | 45.5%     |
| Avg spread when profitable (≥ 5 pt)     | 19.7 pt   |

Conditional on filling in the "profitable-spread" subset only, C1's gross half-spread is on average ~9.85 pt. Subtracting:

* adverse-selection ≈ 1.6 pt (R47 empirical, `hft-mm-design`)
* per-side cost = 2.0 pt
* expected gross edge per fill ≈ 9.85 − 1.6 − 2.0 = **+6.25 pt** ⇒ **fails H1 floor of 8 pt** by ~22%.

This is the **conditional**-on-profitable-fill arithmetic. R47's deployed `+4,534 pt / 12 days` baseline implicitly captured this same regime, so the average is consistent. AMHP's claimed advantage is the **sizing inside this regime**: when ρ̂ is high → widen further → capture more of the observed-spread surplus on the days where p75=19 pt is achievable; when ρ̂ is low → narrow (but never below cost-floor) → fill more often on calm days.

### AMHP-conditional gain bound (literature priors)

From the user-supplied research goal (`research_goal` in shared-context, "六、AMHP" + "七、應用場景"), AMHP's claimed regime-correlation with adverse selection runs ~0.30 for ρ̂ vs short-horizon adverse-fill probability, and ~0.4–0.5 for |IIR| vs forward signed move (per "七、" paragraph 1, citing 58–63% direction accuracy on 30-min windows). Translating to per-fill PnL improvement (under standard noise assumptions and a quadratic spread-utility curve), the modulator contributes a hypothesized **+1.5 to +3 pt per fill** above the base R47 fill quality.

If realized, expected per-fill edge becomes:

```
6.25 pt (R47 base, conditional)  +  ~2 pt (AMHP modulator gain)  ≈  8.25 pt
```

This **just clears H1 (≥ 8 pt)**, with ~3% margin. Tight. **The proposal is not arithmetically dominant — it is borderline and entirely dependent on the AMHP correlations being at the upper end of literature priors**.

### Q1 verdict

PASS-MARGINAL. C1 clears H1 only if AMHP modulator delivers ≥ 1.75 pt per fill above R47 baseline. Below that level it falls inside the H1 floor. T2/Empirical work (T2 DA + executor backtest) must measure the AMHP modulator's per-fill contribution directly on 31d TMFD6 — paper arithmetic is insufficient.

`cost_drag = 100%` is acknowledged as bright-line WARN; mitigated by L1 hard cost-floor (never narrow below 5 pt). Proposal does NOT attempt to break the cost-floor — it sizes spread *above* the floor.

---

## 4. Q2 — Horizon

* AMHP intensity λ*(t) updates per trade arrival (~1.8 trades/sec on TMFD6, per `taifex-market-structure`).
* ρ̂(t), |IIR(t)| MLE-fitted on rolling windows (ms-scale: 100 ms, min-scale: 1 min, hr-scale: 1 hr per "六、AMHP" multi-scale design).
* Quote re-evaluation cadence: ms-scale (every depth-update) — well within the asymmetric profile's cancel P95 = 59 ms.
* Inventory holding period: minutes (R47-class, intra-day mean-reversion through V-shape recovery).

**Verdict**: minutes intra-day. **Viable** per Q2 of `taifex-alpha-kill-criteria` (no horizon exhaustion penalty for maker-class minute-horizon). Stays inside the `long-horizon-all` exclusion.

---

## 5. Q3 — Structural fit

* `pure_maker` ∈ `scope.allowed_types` (shared-context.yaml line 41–42). **In scope.**
* Not a directional signal (both sides quoted always).
* Distinct from each `killed_directions` entry — full overlap analysis in §8.

**Verdict**: Structurally viable.

---

## 6. AMHP-Specific Derivation

### 6.1 Multi-Scale Exponential Kernel (per "六、AMHP")

Standard univariate exponential Hawkes:

```
λ(t) = μ + Σ_{t_i < t} α · exp(−β (t − t_i))
```

AMHP multi-scale extension uses K=3 stacked exponential kernels with distinct decay rates:

```
λ_AMHP(t) = μ(t)  +  Σ_{k=1..3} Σ_{t_i < t}  α_k · exp(−β_k (t − t_i))
```

| Scale         | β_k (decay rate)        | Half-life       | Purpose                                        |
| ------------- | ----------------------- | --------------- | ---------------------------------------------- |
| ms-scale      | β_ms ≈ ln(2)/0.1 s     | ~100 ms         | Burst-detection / micro-reaction               |
| min-scale     | β_min ≈ ln(2)/60 s     | ~1 min          | Short-horizon adverse-selection                |
| hr-scale      | β_hr ≈ ln(2)/3600 s    | ~1 hr           | Regime-state (session-context, news-aftermath) |

**Branching ratio per scale**: ρ_k = α_k / β_k. Aggregate ρ̂ = Σ_k ρ_k. Stability: ρ̂ < 1 required; ρ̂ → 1 = critical (per "六、" critical-monitoring threshold 0.85).

**Why three scales (not one, distinct from C6 baseline)**: empirical TMFD6 trade-arrival ACF on 5-day window will show three exponential humps (ms-scale burst, min-scale flow-clustering, hr-scale session-recovery). Single-scale Hawkes (C6 baseline) collapses these into one effective β, losing the regime-separability that drives the modulator's edge. **C6 vs C1 ablation in T6 Executor will tell us whether the multi-scale design adds value above vanilla Hawkes** — this is methodologically deliberate.

### 6.2 State-dependent baseline μ(t)

R47's static spread gate implicitly assumes μ is constant. AMHP allows μ to depend on observable LOB / cross-asset state:

```
μ(t)  =  μ_0  +  γ_lob · LOB_imbalance_z(t)
              +  γ_dist · distance_to_daily_limit_z(t)
              +  γ_io  · foreign_IO_zscore(t)
              +  γ_us  · US_overnight_return(t)         (only at session open ±30 min)
```

The covariates are all observable in real time on the platform (LOB imbalance via FeatureEngine v3 `lob_shared_v3`, foreign-IO via daily institutional-flow snapshot, US overnight return via daily reference). The state-dependent baseline is what differentiates AMHP from vanilla single-baseline Hawkes (C6) and makes the model day-context-aware — directly addresses the single-day-dominance pathology by giving the model a covariate that varies day-to-day.

### 6.3 Asymmetric excitation matrix (per "六、" 非對稱牛熊激勵矩陣)

Two parallel Hawkes processes for buy/sell flow with asymmetric self-excitation:

```
λ_buy(t)  = μ_buy  + Σ α_buy_buy   · K(t − t_i^buy)   + Σ α_sell_buy  · K(t − t_i^sell)
λ_sell(t) = μ_sell + Σ α_buy_sell  · K(t − t_i^buy)   + Σ α_sell_sell · K(t − t_i^sell)
```

Per "六、", panic-sell α_sell_sell typically runs **1.3–1.8× α_buy_buy** in stress regimes. C1 uses this ratio R = α_sell_sell / α_buy_buy as a continuous regime indicator:

```
IIR(t) = (λ_buy(t) − λ_sell(t)) / (λ_buy(t) + λ_sell(t))   ∈ [−1, +1]
```

R is a slower-moving regime label; IIR is the per-tick continuous signal used by the modulator.

### 6.4 Critical-monitoring gate (ρ̂ > 0.85)

When aggregate ρ̂(t) crosses 0.85 (per "六、" criticality alarm; cited example: 2024 yen carry-trade collapse, ρ̂ broke 0.85 in 20 min), the L1 modulator forces:

* spread multiplier = 3.0 (effective spread ≥ 15 pt)
* active size cap → 1 contract
* (optional) cease quoting for τ_cooldown = 60–120 s

This overlays cleanly with C2 (which is the standalone exec-support form of this same gate). C2 vs C1+C2 stacking is a follow-on T6 question.

---

## 7. 31-Day Empirical Sketch (CK queries on TMFD6 2026-01-27 → 2026-03-26)

The Researcher will not run `impl.py` (Executor's job) but provides the queries Executor needs. Data source: ClickHouse `hft.market_data` on TMFD6, 58 calendar days, ~31d effective active window, L1–L5 levels available.

### Q-A: Trade ACF (justifies multi-scale kernel)

```sql
-- per-day signed-trade-arrival autocorrelation at three lags
SELECT
  toDate(exch_ts/1e9) AS day,
  corrTrade(deltas, 0.1)  AS ms_acf,
  corrTrade(deltas, 60)   AS min_acf,
  corrTrade(deltas, 3600) AS hr_acf
FROM hft.market_data
WHERE symbol='TMFD6' AND toDate(exch_ts/1e9) BETWEEN '2026-01-27' AND '2026-03-26'
GROUP BY day;
```

Expected: nontrivial ACF at all three lags ⇒ multi-scale design justified. Flat at min/hr ⇒ collapse to single-scale (and C1 reduces to a more elaborate C6).

### Q-B: ρ̂ (rolling MLE) vs forward adverse-fill rate

Per-day fit a rolling ρ̂(t) on a 5-min window. Bin into deciles. For each decile, measure:

```
adverse_fill_rate = P(unfavorable price move > 1 pt within 30 s of fill)
```

Required correlation: corr(ρ̂_decile, adverse_fill_rate) ≥ 0.30 across days. **Per `taifex-alpha-kill-criteria` Gate: Detrended IC**, this must be measured on detrended adverse-fill, not raw. If corr < 0.20 detrended, KILL (signal does not separate adverse regimes).

### Q-C: |IIR| vs side-skipping benefit

For each fill, measure `pnl_per_fill` conditional on |IIR| at fill time. Required: top-decile |IIR| has lower per-fill PnL than bottom-decile by ≥ 1 pt. If top-decile is *not* worse (or AMHP's directional info is reversed), C1 reduces to the static R47 baseline and the modulator adds nothing.

### Q-D: Day-distribution diffusion check (single-day-dominance audit)

This is the **most important** query, given R47-A1 found 96.9% from one day:

```sql
-- simulated daily PnL under C1's modulated spread vs R47 base
-- (DA's H4 §4 audit: max_day must be ≤ 25% of total)
WITH daily_pnl AS (
  SELECT toDate(exch_ts/1e9) AS day, sum(pnl_pt) AS pnl
  FROM simulated_c1_fills
  WHERE day BETWEEN '2026-01-27' AND '2026-03-26'
  GROUP BY day
)
SELECT
  count() AS days,
  sum(pnl) AS total,
  max(pnl) / sum(pnl) AS max_day_pct,
  countIf(pnl > 0) AS winning_days
FROM daily_pnl;
```

Required: `winning_days ≥ 5`, `max_day_pct ≤ 25%`, jackknife sign-stable, bootstrap CI excludes zero. **If the C1-simulated PnL rolls up the same single-day signature as A1 (1 winning day = 96.9%), the AMHP modulator did not diffuse the signal — KILL. This is the gate the executor backtest must clear.**

### Q-E: ρ̂(t) > 0.85 frequency & yen-carry-style event check

Measure on 31d: how many minute-buckets per day saw ρ̂ > 0.85. If average frequency < 0.5%/day, the criticality gate (C2 overlay path) is essentially decorative under TMFD6 microstructure. If average > 5%/day, the C1 modulator is in critical regime too often — the static "always quote at floor=5 pt" R47 baseline may dominate. Expected band: 1–4%/day.

---

## 8. H3 Overlap Statement (mandatory differentiation)

### 8.1 vs MLOFI-Hawkes (R27 KILL: Predictive R²=0.000001, TXF→TMF lead-lag inverted)

C1 differs along three independent axes:

* **(a) Non-directional**: C1 quotes both sides at all times. The spread modulator changes *magnitude*, not sign. R27 was a directional taker (predict direction → trade in that direction); R27's inverted lead-lag was fatal because the model committed to one side. C1's modulator can be wrong about regime and still profits — wrong-regime ⇒ wrong spread size ⇒ slightly suboptimal fill ratio, not a directionally adverse trade.
* **(b) Role**: C1 sizes maker spread; R27 predicted forward signed return. Different output space, different loss function.
* **(c) Mechanism**: C1 uses ρ̂ (self-excitation strength, *univariate per side*) and aggregate IIR (*intensity-imbalance ratio*). R27 used multivariate cross-MLOFI projection — fundamentally different feature set. C1 does not depend on the TXF→TMF cross-direction at all (that's C4's territory).

### 8.2 vs Omori-aftershock (R30 KILL: 4.7 pt RT consumed 92% of edge)

R30 was a power-law-Hawkes-driven aftershock entry strategy. C1 differs:

* **(a) Kernel form**: exponential multi-scale, NOT power-law. β_k decay rates are bounded; Omori's t^(−p) tail is heavy and produces long-horizon "still-active" predictions where R30 stayed in trades for minutes-to-hours. C1's quote re-evaluation cadence is ms-scale.
* **(b) Cost-edge math (own arithmetic, NOT inheriting R30's verdict)**:
  * R30: 4.7 pt RT consumed 92% of edge ⇒ residual edge ≈ 0.4 pt.
  * C1: per §3, expected gross edge per fill ≈ 6.25 pt (R47 base, profitable-spread regime); cost-floor 5 pt is enforced by L1; AMHP modulator adds 1.5–3 pt under literature priors. Best-case net ≈ 6.25 + 2 − cost-already-subtracted ≈ 8.25 pt. **Cost ratio**: 4 / 8.25 ≈ 48%, **not 92%**. The structural difference: R30 was a *taker* eating the spread; C1 is a *maker* that earns the spread *gated to profitable-spread regime only*.
* **(c) Branching ratio role**: C1 uses ρ̂ as a regime-conditioner on quote sizing. R30 used Hawkes intensity as an entry timer.

### 8.3 vs spread-conditional-maker (R16 KILL: -8 pts/fill median, adverse-selection trap)

R16 was killed because it conditioned quotes on **observed spread alone**. The trap: when spread widened due to adverse selection arriving, R16 widened → traders picked off the wider quotes → adverse fills compounded. C1 differs:

* **External regime signal**: ρ̂ and IIR are derived from the **AMHP fitted on trade arrivals**, not from the current spread. They lead the spread (per "七、" paragraph 2: AMHP intensity rises before spread widens — that's the whole claim).
* **Floor enforcement**: C1's spread-target is the floor for *quoting*, not for fills. If observed spread ≥ target, quote at the target. R16's bug was quoting at the observed spread itself when it widened — C1 holds the target steady or wider, so it does not re-aim into a hostile widening.
* **Non-spread feature space**: R16 had only one input (current spread). C1 has ρ̂, IIR, asymmetric α ratio, plus session covariates in μ(t). Higher-dimensional regime model.

### 8.4 vs single-day-dominance-pathology (R47-A1, profile-invariant)

Acknowledged H4 risk. Mechanism for diffusion (§9):

* The state-dependent μ(t) gives day-level covariates (LOB-imbalance-z, distance-to-limit, foreign-IO-z, US-overnight-return) ⇒ different days produce different baseline μ ⇒ AMHP fits different ρ̂, IIR distributions per day ⇒ the modulator behaves differently per day.
* The asymmetric α matrix gives the model *per-day* bull/bear regime sensitivity (parameter estimated from that day's flow), unlike R47's static spread gate which is calendar-blind.
* These together are the proposed diffusion mechanism. **It is a hypothesis** — the empirical Q-D test in §7 is the falsifier.

---

## 9. Risk — What Could KILL This

| # | Risk                                                  | Mechanism                                                                                                                                                                 | Falsifier                                                              | KILL severity |
| - | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | :-----------: |
| 1 | **Single-day-dominance pathology**                    | AMHP modulator does not diffuse signal across days; one outlier day continues to dominate (R47-A1 pattern, profile-invariant per shared-context killed_directions L160-163) | Q-D shows max_day_pct > 25% OR winning_days < 5                        | **CRITICAL**  |
| 2 | **AMHP correlations below literature priors**        | ρ̂ vs adverse-fill correlation < 0.20 detrended (Q-B falsifier)                                                                                                            | Modulator gain < 1.75 pt/fill ⇒ falls below H1 floor                   | HIGH          |
| 3 | **Cost-drag bright-line WARN unaddressed empirically**| 100% cost drag (RT/median spread); the L1 floor protects against narrowing below cost but does not guarantee positive net edge on calm days                              | Cost-conditional fill PnL audit shows negative on calm days too        | HIGH          |
| 4 | **Multi-scale design collapse** (vs C6 baseline)     | If trade ACF on TMFD6 is flat at min/hr lags, multi-scale provides no information beyond C6 single-scale                                                                  | Q-A shows ms_acf ≫ min_acf ≈ hr_acf ≈ 0                                | MEDIUM (reduce to C6; not full kill) |
| 5 | **Latency-feasibility on 395 ms submit profile**     | Quote-update on regime change takes 395 ms P95 to activate; if regime transitions faster than 395 ms, modulator's information has decayed before quote is on book           | Q-E plus regime-transition timing: if median ρ̂-transition < 200 ms, modulator stale on activation | MEDIUM        |
| 6 | **Implicit reliance on max_pos=3 V-shape recovery**  | If C1 changes fill distribution enough that V-shape recovery does not fire, reverts to max_pos=1 economics (-1,407 pt regime per `r47_structural_properties.md`)         | Per-fill inventory trajectory simulation; if average peak inventory < 2.0, V-shape mechanism missing | HIGH          |
| 7 | **R52 meta-finding (per-quote granularity)**         | Last run's meta-finding: per-quote/minute-granularity filters cannot escape A1's day-granularity pathology under retail cost. C1 *is* per-quote granularity                | Repeats R-1 meta-finding — ONLY the day-level state covariates (foreign-IO, US-overnight) provide day-granularity. If those covariates' coefficients γ_io, γ_us are not significantly nonzero, C1 collapses to per-quote and inherits the meta-kill | **CRITICAL**  |

Risks #1 and #7 are tightly linked: both come from the day-granularity vs per-quote-granularity tension. The proposal's diffusion claim rests almost entirely on the day-level covariates in μ(t). If those covariates are not load-bearing, C1 is structurally indistinguishable from a more elaborate per-quote filter — and the most recent run already established that class is killed.

### A1-tightened survivor criteria (mandatory citation, shared-context lines 100-107)

C1's promotion path requires:

* §4 max_day ≤ 25% of total PnL ⇒ falsifier query Q-D
* §6 winning_days ≥ 5 ⇒ falsifier query Q-D
* jackknife sign-stable ⇒ falsifier query Q-D
* bootstrap CI excludes zero ⇒ falsifier query Q-D
* fills on ≥ 5 distinct days ⇒ falsifier query Q-D
* profile-conditionality caveat in proposal: **acknowledged here — all expectations conditioned on `v2026-04-24_measured` asymmetric profile (submit/modify P95 = 395 ms, cancel P95 = 59 ms). Different broker, different day-level latency, different conclusions.**

---

## 10. Pre-T2 Self-Assessment

Quantitative summary against H gates:

| Gate                                  | Status                        | Notes                                                                                       |
| ------------------------------------- | :---------------------------: | ------------------------------------------------------------------------------------------- |
| H1 — Edge ≥ 2× RT                    | PASS-MARGINAL                | 8.25 pt vs 8 pt floor under upper-band priors; FAIL under lower-band                       |
| H2 — Horizon viable                  | PASS                         | Minutes intra-day; not in long-horizon-all exhaustion                                       |
| H3 — Non-overlap with killed         | PASS                         | Differentiated from MLOFI-Hawkes, Omori, spread-conditional-maker (§8)                     |
| H4 — Day-distribution diffusion      | **CONTINGENT**                | Diffusion mechanism is hypothesis-only; falsifier is Q-D in T6 executor backtest            |
| H5 — Latency-feasibility (395/59)    | PASS-CONTINGENT               | Cancel-P95=59 ms favors fast regime-transition; submit-P95=395 ms creates risk #5 — Q-E test|
| Cost-source citation                 | PASS                         | TMF RT 4 pt cited from `feedback_taifex_fee_structure.md`                                   |
| Cost-drag bright-line                | **WARN (100%)**               | Acknowledged in Risk #3; mitigated by L1 hard cost-floor                                    |
| Per-quote/minute meta-finding        | **CONTINGENT**                | Risk #7 identifies the structural threat; rests on day-level covariates                     |
| `scope.allowed_types`                | PASS                         | pure_maker is in scope                                                                      |

The proposal is borderline on H1 and contingent on two falsifier outcomes (Q-D for H4, day-level covariate significance for the meta-finding). Neither is decidable without empirical data, and the role boundary forbids the Researcher from running impl.py.

**Self-kill consideration**: should I declare γ SELF-KILL now?

* Arguments for self-kill: (i) H1 is borderline; (ii) the most-recent meta-finding from run R-1 says per-quote/minute-granularity filters can't escape A1's pathology under retail cost; (iii) C1 is per-quote granularity at its core, with day-level covariates as its only diffusion mechanism, and those covariates are speculative.
* Arguments for forward to T2: (i) the day-level covariates (foreign-IO z, US-overnight return, distance-to-limit) are *genuinely* day-granularity and *are* a structural difference from the previously-killed per-quote class — the meta-finding does not preclude day-level state augmentation, only filtering at per-quote level alone; (ii) AMHP is the user-specified first candidate sourced directly from "六、AMHP + 七、 paragraph 2" — declining T2 review without DA's independent kill-checklist would be premature; (iii) the empirical falsifiers are well-defined and will produce a clean kill in T6 if any of Q-A, Q-B, Q-C, Q-D, Q-E fails — the proposal does not need to cling to a hopeful prior to be worth examining.

The borderline H1 + critical H4 contingency is not, on its own, a self-kill — it is an empirical-test-required state. The role file allows γ SELF-KILL only when "your quantitative derivation independently fails any H gate." Mine does not *fail* any gate; H1 and H4 are PASS-MARGINAL and CONTINGENT respectively, which is exactly the territory T2 + T6 are designed to resolve. Forwarding to T2 is therefore the disciplined choice.

---

## Verdict: γ FORWARD-TO-T2

**Reasons:**

1. **H1 PASS-MARGINAL** (edge ≈ 8.25 pt vs 8 pt floor under upper-band literature priors); not a clean fail — empirical 31d AMHP fit + Q-B/Q-C correlation measurement in T6 will resolve.
2. **H3 cleanly differentiated** from MLOFI-Hawkes (non-directional, univariate ρ̂/IIR), Omori (exponential not power-law, maker not taker, own cost-edge math 48% not 92%), and R16 spread-conditional-maker (external-regime signal, not observed-spread feedback loop).
3. **H4 CONTINGENT, not failing** — diffusion mechanism (state-dependent μ with day-level covariates: foreign-IO-z, US-overnight, distance-to-limit) is a defensible structural answer to A1's single-day-dominance pathology; falsifier Q-D is well-defined and is the executor T6 deliverable.
4. **H5 cancel-favored** — asymmetric profile (P95 cancel 59 ms vs submit 395 ms) is structurally beneficial to dynamic-spread re-quoting; submit-side risk acknowledged in Risk #5.
5. **Cost-source cited correctly** from `memory/feedback_taifex_fee_structure.md` (TMF RT 4 pt = 40 NTD); cost-drag = 100% bright-line WARN explicitly acknowledged and addressed via L1 hard floor.
6. **Risk #7 (meta-finding inheritance) is the single largest open question** — DA T2 should explicitly evaluate whether C1's day-level covariates (γ_lob, γ_dist, γ_io, γ_us in μ(t)) are load-bearing enough to escape the recent meta-kill. If DA judges this insufficient, the right place for that judgment is T2, not researcher self-kill.

**Hand-off note for DA T2**: please apply Kill Checklist with particular attention to (a) Risk #7 / meta-finding inheritance, (b) the H1 borderline arithmetic, and (c) whether the §6 derivation (state-dependent μ + asymmetric α + multi-scale kernel) is rigorous enough to stand independently of the user-source "六、AMHP" claims, given that prior runs have shown literature priors do not always survive contact with TMFD6 microstructure.
