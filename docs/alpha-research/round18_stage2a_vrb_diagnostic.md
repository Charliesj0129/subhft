# Round 18 Stage 2a: VRB Data Diagnostic Results

**Date**: 2026-03-26
**Status**: KILLED -- all 3 kill gates failed

## Result: VRB is DEAD

### Kill Gate 1: Trigger Frequency -- FAIL
- Sessions analyzed: 16 day-session dates (08:45-13:45 Taiwan time)
- Total triggers: **2** (both on 2026-01-27, a low-activity early day)
- Average triggers/session: **0.12** (threshold: >= 1.0)
- The RV_5m/RV_1h ratio never exceeds 2.0 on any typical day. Max observed ratio across all data: 1.90
- Even relaxing threshold to 1.5: only 2 triggers on best day

### Kill Gate 2: Direction Accuracy -- FAIL
- Only 2 events, both wrong direction (EMA slope positive, actual 1h return negative)
- Insufficient sample for statistical conclusion, but 0/2 is not promising

### Kill Gate 3: Time-of-Day -- FAIL
- 100% of triggers in one 30-min bucket (10:45-11:15)
- Both triggers are from the same day (2026-01-27), a startup data day with low tick count

## Root Cause Analysis

The VRB concept fails on TMFD6 because:

1. **TMFD6 1-minute returns are extremely smooth**. RV_1h ranges 0.0005-0.0015 with low intra-session variance. There is no dramatic "compression then expansion" pattern.

2. **The RV_5m/RV_1h ratio is structurally bounded**. The max ratio observed is 1.90 on the most volatile day. The 2.0 threshold is unreachable. Even at 1.5, triggers are extremely rare.

3. **Mini-TAIEX futures are a retail-dominated, low-tick-rate market** (1.8 ticks/sec). Unlike ES/SPY where institutional flow creates clear vol compression/expansion cycles, TMFD6's volatility evolves gradually.

4. **Only 16 day-session dates available** (not 58 as estimated from total row count, which includes night session and duplicates). The 20-day rolling percentile window is impossible.

## Data Quality Note

The "58 days" in the project brief counted ALL days including night sessions. Day session (08:45-13:45) only has **16 dates** with significant gaps:
- 2026-01-27 to 2026-02-06 (9 days, but early days have low tick counts)
- Gap: 2026-02-07 to 2026-02-22
- 2026-02-23 to 2026-02-25 (3 days)
- Gap: 2026-02-26 to 2026-03-19
- 2026-03-20 to 2026-03-26 (6 days, 03-23 partial, 03-26 partial)

This data sparsity is a systemic issue for ALL R18 candidates, not just VRB.

## Diagnostic Script
`research/experiments/validations/vrb_diagnostic/vrb_diagnostic.py`

## Cost Model Used
3.92 pts = 1.19 bps RT (per Challenger correction)
