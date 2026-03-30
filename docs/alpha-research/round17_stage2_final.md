# Round 17 Stage 2 Final Report (2026-03-26)

## Direction: Cost-Latency Adapted Strategy Search

### Constraints
- TMFD6 RT cost: 4 pts / 40 NTD / 1.33 bps (retail, no maker rebates)
- Shioaji P95 RTT: 36ms submit, 43ms modify, 47ms cancel
- Data: L1 order book (TMFD6 19 days, TXFD6 14 days)

---

## Candidate 2: Multi-Timescale Trend-Reversion (MSTR)

**Paper**: arXiv:2501.16772 (Safari & Schmidhuber 2025)

### Verdict: FAIL as standalone contrarian. PASS as momentum feature.

| Reviewer | Round 1 | Round 2 |
|----------|---------|---------|
| Researcher | FAIL contrarian, PASS momentum | Confirmed |
| Challenger | REJECT (4 unresolved) | APPROVE (8/8 resolved) |
| Execution | REJECT | REJECT standalone, PLAUSIBLE as CBS filter |

### Key Findings

1. **TMFD6 is a momentum instrument at sub-hour scales** (opposite of paper prediction)
   - phi_2-16min: positive rank IC (+0.037 to +0.049), all t > 2.5
   - phi_64min: weakly negative IC (not significant)

2. **Cubic reversion term NOT significant** on TMFD6
   - All c coefficients |t| < 2.0 — the paper's tradeable mechanism is absent

3. **phi_8min -> fwd_1min is a robust momentum signal**
   - IC = +0.041, t = 9.0 (confirmed by Newey-West 8.99, bootstrap 8.95, block-boot 9.59)
   - 0/20 days negative IC, 95% CI [+0.032, +0.050]
   - NOT redundant with OFI (Spearman r=0.294) or depth_imbalance (r=0.096)

4. **Not tradeable standalone** — IC=0.041 generates ~0.1 bps expected edge vs 1.33 bps cost

### Salvageable Output
**phi_8min as CBS momentum-exhaustion filter**: When phi_8min peaks and fades after a 40bps move, CBS contrarian entry is better timed. Low implementation cost (one EMA state variable), no new data needed. Needs backtest validation.

---

## Candidate 3: Regime-Adaptive OFI (RA-OFI)

**Papers**: arXiv:2505.17388 (Hu & Zhang), arXiv:2307.02375 (Tsaknaki BOCPD)

### Verdict: FAIL. No regime produces edge > cost.

| Reviewer | Round 1 | Round 2 |
|----------|---------|---------|
| Researcher | FAIL | Revised: multi-factor quiet IC=0.129, still FAIL |
| Challenger | REJECT (4 unresolved) | APPROVE (8/8 resolved) |
| Execution | REJECT | REJECT — cost-adjusted P&L negative in all regimes |

### Key Findings

1. **Spread-only regime classification is a bad classifier**
   - Original quiet IC (+0.046) was LOWER than unconditional (+0.061)

2. **Multi-factor regime (spread + rvol) reverses the finding**
   - True quiet (tight spread + low vol): IC = +0.129 (highest, but only 10.9% of time)
   - Volatile: IC = +0.043 (lowest — confirms spread-only artifact)

3. **No regime overcomes 4-pt cost barrier**

   | Regime | IC@5min | sigma_5min | Expected edge | Net after cost |
   |--------|---------|------------|---------------|----------------|
   | Quiet | +0.040 | ~6 bps | 0.19 bps | **-1.14 bps** |
   | Normal | +0.126 | ~8 bps | 0.80 bps | **-0.53 bps** |
   | Volatile | +0.110 | ~14 bps | 1.23 bps | **-0.10 bps** |

4. **Quiet regime contextually incompatible with CBS**: CBS triggers on 40bps moves which are NOT quiet-regime conditions

---

## Round 17 Structural Discoveries

| # | Finding | Impact |
|---|---------|--------|
| S1 | TMFD6 sub-hour = momentum (not mean-reversion) | Contradicts "universal" reversion. Trend-following > contrarian at 2-16 min |
| S2 | phi_8min orthogonal to OFI (r=0.29) | New feature dimension: price trend persistence vs order flow |
| S3 | Spread-only regime classification is broken | Must use multi-factor (spread + rvol) for regime-conditional research |
| S4 | IC=0.129 (best found) still < cost barrier | Confirms R16: no L1 signal alone overcomes 4pt RT cost |
| S5 | phi_8min + CBS momentum-exhaustion is the only actionable path | Pending backtest |

---

## Recommended Next Steps

1. **Round 18 (immediate)**: Backtest phi_8min as CBS momentum-exhaustion filter on 20-day TMFD6 data
2. **Future**: TX->TMF lead-lag (Candidate 1) — cross-asset signal may break single-instrument IC ceiling
3. **Platform**: Add phi_8min to FeatureEngine evaluation pipeline
4. **Knowledge base**: Document TMFD6 momentum finding + regime classification lesson
