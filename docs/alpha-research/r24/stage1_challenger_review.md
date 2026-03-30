# R24 Stage 1: Challenger Review

**Date**: 2026-03-29
**Reviewer**: Challenger Agent
**Target**: `docs/alpha-research/r24/stage1_literature_survey.md`

---

## Direction A: Fill Probability Modeling — CONDITIONAL APPROVE

### Challenge 1: The 0.5-2.0 bps improvement estimate is ungrounded and potentially misleading

The report claims 0.5-2.0 bps/trade cost reduction and then calls 0.5 bps "42% cost reduction." This arithmetic is wrong: 0.5 bps / 1.19 bps RT cost = 42%, but the denominator is the **full RT cost** — the optimizer can only influence the **placement decision** portion, not tax or commission. The actual fee breakdown (from memory, `feedback_taifex_fee_structure.md`): RT cost = 3.92 pts = tax 6.6 + comm 13 per side. The limit-vs-market decision saves at most 1 tick (1 pt on TMFD6) when it works, but the current heuristic **already** captures the easy cases (spread >= 2 pts + favorable Q ratio). The incremental improvement over the existing heuristic is the question, not the improvement over always-market.

**Evidence requested**: What is the current heuristic's fill rate and cost savings vs always-market? Without this baseline, "0.5-2.0 bps improvement" is aspirational, not evidence-based. The Albers 2025 reference of "1.2 pts/trade savings from passive placement" is the **total** passive vs aggressive gap — the current `ExecutionOptimizer` already captures a chunk of this. The ML model's incremental value could be near zero.

### Challenge 2: Cont & Kukanov 2012 is equities, not TAIFEX futures with 36ms RTT

The foundational paper (Cont & Kukanov 2012) models order placement in US equity markets with sub-millisecond execution latency. TAIFEX futures have measured **simulation API RTT of ~36ms** (from `latency-baseline-shioaji-sim-vs-system.md`). At this latency:

- Queue position is stale by the time the order reaches exchange.
- Fill probability is dominated by **latency** rather than LOB microstate at decision time.
- The ML model would be predicting fill probability based on a LOB snapshot that is 36ms old — in a market where TXFD6 median tick interval = 125ms (R13), that's nearly 30% of a tick lifetime.

**Evidence requested**: What fraction of limit order fill/cancel outcomes are explained by LOB state at order time vs by state changes during the 36ms transit? If transit-time dynamics dominate, the entire modeling premise is weakened. This needs a simple diagnostic: correlation between decision-time features and fill outcome vs arrival-time features and fill outcome.

### Challenge 3: Latency overhead kill gate (100us) may be insufficient

The kill gate says "latency overhead > 100us per decision." But the current `ExecutionOptimizer.decide()` is pure integer arithmetic — likely <1us. A logistic regression is probably <10us, but a "shallow NN" (mentioned as alternative) with feature normalization could exceed 100us easily, especially if it requires `numpy` operations. More importantly, **100us is generous** for a hot-path addition. The Allocator Law says no heap allocations on hot path. NN inference typically allocates.

**Evidence requested**: Will the model use pre-allocated buffers for inference? What is the expected inference path — pure integer math like the current heuristic, or numpy/torch?

### Challenge 4: Feature snapshot API does not exist

The report claims "expose feature snapshot at order-placement time to execution layer (~30 LOC)." I verified: `FeatureEngine` has NO `get_latest()` or equivalent API. Features are computed in `_compute_v2_features()` and `_compute_v3_aggregation()` and emitted as bus events. There is no snapshot accessor. This is not a 30 LOC change — it requires designing a thread-safe snapshot interface for the feature engine, which touches the hot path architecture. Realistically 100-200 LOC with proper locking/lock-free design, plus tests.

---

## Direction B: TXO Options Flow Pipeline — CONDITIONAL APPROVE (with strong caveats)

### Challenge 1: R17 found 99.7% quotes — no evidence trade ticks exist but weren't captured

The report states "the data pipeline gap is the blocker, not the signal" — implying TXO trade ticks exist but we failed to capture them. This is an **unverified assumption**. R17 explicitly found: "TXO data is 115K ticks (not 33M trades)." The 99.7% quotes could mean:

1. TXO genuinely has minimal trade activity at the front-month level during intraday (illiquid options).
2. The Shioaji API quote subscription doesn't deliver trade ticks for options.
3. Our subscription config filtered them out.

Only hypothesis 3 is fixable by "subscribing to TXO." Hypotheses 1 and 2 kill the direction entirely.

**Evidence requested**: Before committing ANY engineering effort, run a 1-day TXO subscription diagnostic: subscribe with explicit trade tick flag, count trade vs quote events, measure trade frequency per strike/expiry. This is <10 LOC and 1 day of data. If trade ticks are <100/day, the direction is dead. The report should have proposed this as Step 0.

### Challenge 2: Delta-hedging flow estimation requires Greeks computation — missing from infra prerequisites

The report lists "delta-hedging flow estimation" as a key signal but the infrastructure prerequisites mention only "options-specific feature engine module (~300-500 LOC)." Computing delta-hedging flow requires:

1. Implied volatility estimation from option prices (Black-Scholes or binomial).
2. Greeks computation (delta, gamma at minimum).
3. Estimation of dealer inventory / hedging demand.
4. Mapping hedging flow to expected futures order flow direction.

Steps 1-3 alone are a substantial quantitative library. The Lehalle et al. 2025 paper achieves 71.5% dealer hedging detection with an LLM-based approach — this is NOT a 300-500 LOC module. A realistic estimate is 1000-2000 LOC for a minimal Greeks + hedging flow pipeline, plus calibration data. The report significantly understates the complexity.

**Evidence requested**: Provide a concrete module breakdown with LOC estimates for each component of the options feature pipeline. The current "~300-500 LOC" is for put-call ratio only (trivially computable); the actual hedging flow signal requires an order of magnitude more work.

### Challenge 3: 4-week data accumulation should start NOW, but needs Step 0 diagnostic first

The recommendation says "Start data collection now (subscribe to TXO), but defer feature engineering." This is correct, but the report should make Step 0 (1-day diagnostic) **mandatory before** even starting accumulation. If TXO trade ticks don't exist in the API, 4 weeks of accumulating quote data is wasted effort.

---

## Direction C: Adaptive Execution Timing via Regime Detection — CONDITIONAL APPROVE (with significant concerns)

### Challenge 1: VRR is listed as "existing feature" but was NEVER REGISTERED in FeatureEngine

The report lists VRR (variance ratio) among the features to combine: "BurstDetector, VRR (variance ratio) feature, and toxicity_ema50." I verified the codebase: **`vrr_5_300_x1000` does NOT exist in the feature registry** (confirmed in `registry.py` — v3 has 27 features, indices 0-26, none is VRR). Per project memory: "vrr_5_300_x1000 was NEVER registered — dead code. Toxicity took slot [21] (R23)."

The report incorrectly counts VRR as an available asset. Without VRR, the regime classifier has 4 features instead of 5. This is not fatal but it undermines the claim that Direction C "uses existing features" with "no new data dependencies."

**Evidence requested**: Will VRR be implemented and registered as part of Direction C? If so, the LOC estimate needs to increase (VRR computation is ~50-100 LOC in the engine + registration + tests). If not, what replaces it as the volatility regime indicator? `spread_ema300s` is a spread level indicator, not a volatility ratio.

### Challenge 2: R22 showed VRR as CBS filter was KILLED (p=0.481) — why would a regime classifier work when individual features failed?

R22 explicitly tested VRR as a strategy filter and **killed it**: "CBS filter KILLED: p=0.481 (no threshold improves OOS)." R22 also killed `imbalance_mr_speed` as a filter (detrended IC negative, OOS random, CBS p=0.64).

The report's argument is that **combining** these features into a regime classifier will succeed where individual features failed. But this is the classic "multivariate rescue" trap:

- If individual features have no predictive power for execution quality (p>0.4), combining them with a threshold-based classifier is unlikely to create power from nothing.
- R17 explicitly tested multi-factor combinations and FAILED: "No combination beats 2330 alone."
- A threshold-based classifier on 5 features with no labeled training data = manual parameter search = overfitting to in-sample periods.

**Evidence requested**: Before building the classifier, run a simple diagnostic: compute the correlation between each candidate feature and realized fill quality (fill PnL or adverse movement within 30s of fill). If no individual feature shows Spearman |rho| > 0.05 with fill quality, the combination is dead on arrival. This takes ~50 LOC on existing ClickHouse data and should be Stage 2 Step 0.

### Challenge 3: "1-3 bps adverse selection reduction" estimate is unjustified

The report cites R23 toxicity Q5-Q1 = +3.5 pts adverse movement and claims "if we avoid trading in Q5 toxicity windows, that's direct PnL improvement." This reasoning has two problems:

1. **3.5 pts is unconditional**. The CONDITIONAL improvement (after accounting for missed profitable trades in Q5) could be much smaller or even negative if Q5 also contains high-volatility windows where our signals work best.
2. **Frequency reduction**: The kill gate says "trade frequency drops > 50% from gating" is too restrictive. But what if Q5 is 20% of time and contains 40% of the opportunities? The net effect on strategy PnL depends on the joint distribution of toxicity and signal quality, which has NOT been measured.

**Evidence requested**: Cross-tabulate toxicity quintile vs strategy signal quality (e.g., CBS or MM signal strength). If high-toxicity regimes also have the strongest signals, gating them kills both adverse selection AND alpha, netting zero.

---

## Cross-Cutting Concerns

### Concern 1: All 3 directions are execution optimization — is alpha research abandoned?

The survey title says "Infrastructure Gap Analysis" and all 3 directions optimize execution, not discover new alpha. After 10 rounds of killed alpha (R14-R23), this pivot makes sense, but the report should be **explicit** about this strategic decision. If L1 microstructure alpha is truly exhausted, the team should formally declare it and shift the research mandate. Currently this reads as implicit surrender without a clear pivot decision.

**Recommendation**: Add an explicit "Strategic Pivot" section acknowledging the shift from alpha discovery to execution optimization, with criteria for when to revisit alpha research (e.g., when new data sources like TXO trades become available, or when new exchange features like queue priority become accessible).

### Concern 2: Priority order C > A > B assumes C validates — what if C fails fast?

If Direction C fails (regime labels show no fill quality separation), does the team proceed directly to A? The report implies sequential execution but doesn't specify a contingency. If C fails, it actually STRENGTHENS A's case (features don't predict regime but ML might predict fill probability). The priority order should be explicitly contingency-planned.

### Concern 3: Paper reference quality varies — some citations appear title-matched

- **Coletta et al. 2025 (arXiv:2510.22206)**: "RL agent learns execution timing from LOB simulator." How is this "relevant for timeout/cancel decision" concretely? The connection to our timeout heuristic is tenuous.
- **arXiv:2512.17923 (Lehalle "Inferring Latent Market Forces")**: The title mentions LLMs. Using LLMs for real-time dealer hedging detection in an HFT system violates the Allocator Law and Async Law. The cited 71.5% accuracy comes from offline analysis. Practical applicability is near zero.
- **arXiv:2601.18804 ("Deep g-Pricing")**: This is about options pricing using deep learning. The claim that "implied volatility surface dynamics carry predictive information" is generic — HOW does this translate to a computable feature for our pipeline?

**Evidence requested**: For each paper, state the ONE concrete takeaway that maps to an implementable component in our system.

### Concern 4: LOC estimates are systematically optimistic

| Component | Report Estimate | Challenger Estimate | Reason |
|-----------|----------------|---------------------|--------|
| Feature snapshot API | 30 LOC | 100-200 LOC | No existing accessor, needs thread-safe design |
| Options feature module | 300-500 LOC | 1000-2000 LOC (with Greeks) | Greeks computation, IV estimation omitted |
| Regime classifier | 150 LOC | 150-300 LOC | If VRR needs implementation + label generation |
| Execution replay | 200 LOC | 300-500 LOC | CH query + LOB reconstruction + comparison framework |

Systematically understating effort leads to scope creep and abandoned directions.

---

## Overall Verdict: CONDITIONAL APPROVE

All three directions are **reasonable** given L1 alpha exhaustion, but each has unresolved challenges that must be addressed before committing engineering effort:

### Conditions for Approval

1. **Direction C (Regime)**: Run the fill-quality diagnostic FIRST (feature vs realized fill PnL correlation). If no feature shows |rho| > 0.05, kill immediately. Clarify VRR status — is it being implemented or replaced?

2. **Direction A (Fill Probability)**: Establish the BASELINE first — what is the current heuristic's fill rate and cost savings? Address the 36ms RTT stale-snapshot concern with a simple diagnostic.

3. **Direction B (TXO)**: Run the 1-day TXO subscription diagnostic as mandatory Step 0 before any commitment. If trade ticks < 100/day, the direction is dead regardless of paper evidence.

4. **Cross-cutting**: Add explicit "Strategic Pivot" framing. Adjust LOC estimates upward. Contingency plan the priority order.

None of these conditions require more than 1-2 days of diagnostic work. If the Researcher addresses them in Stage 2, I will not block progression.
