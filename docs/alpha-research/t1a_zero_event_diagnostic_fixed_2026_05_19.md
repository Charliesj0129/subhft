# T1-A Zero-Event Diagnostic

- Spec: `docs/superpowers/specs/2026-05-19-t1a-zero-event-diagnostic-design.md`
- Spec sha256: `4d2bf2df832cf66af3160b0cdf349673571588e31950193e9bfef52f72b96802`
- Commit: `f4deecd31f583ce38fc2b44ae1a16d9802ef1928`
- Viability event CSV: `research/experiments/validations/T1_regime_viability_audit_v0_fixed_20260519/20260519T100217Z_opening_range_events.csv`
- Viability event count: 7
- Viability summary events: 7
- Coverage `would_emit` count: 7
- Freshness: summary=57 input=57 match=True

## Inputs

- `research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0_fixed_20260519/20260519T095812Z_opening_range_coverage.csv` (sha256 `69aceddd8712...`)

## Verdict: **DETECTOR_BUG** (primary reason: **A1**)

- A1: P(missing_opening)=22.09% >= 20%

## Cause Histogram

| cause | count | pct |
| --- | ---: | ---: |
| missing_opening | 19 | 22.1% |
| missing_post | 15 | 17.4% |
| zero_opening_rv | 1 | 1.2% |
| no_break | 8 | 9.3% |
| break_below_8pt | 0 | 0.0% |
| rv_ratio_below_1.25 | 36 | 41.9% |
| vwap_filter_fail | 0 | 0.0% |
| would_emit | 7 | 8.1% |

## Conditional Probabilities

| metric | value |
| --- | ---: |
| P_post_present | 60.47% |
| P_break_given_post | 84.62% |
| P_mag_ge_8_given_break | 100.00% |
| P_rv_ratio_ge_1_25_given_break | 15.91% |
| P_vwap_ok_given_qualifying | 100.00% |
| P_would_emit | 8.14% |

## Contract-Month Breakdown

| contract | year_month | cause | count |
| --- | --- | --- | ---: |
| TXFB6 | 2026-01 | missing_opening | 1 |
| TXFB6 | 2026-01 | rv_ratio_below_1.25 | 2 |
| TXFB6 | 2026-01 | would_emit | 2 |
| TXFB6 | 2026-02 | missing_opening | 2 |
| TXFB6 | 2026-02 | no_break | 1 |
| TXFB6 | 2026-02 | rv_ratio_below_1.25 | 2 |
| TXFC6 | 2026-02 | missing_opening | 1 |
| TXFC6 | 2026-02 | rv_ratio_below_1.25 | 1 |
| TXFC6 | 2026-03 | no_break | 3 |
| TXFC6 | 2026-03 | rv_ratio_below_1.25 | 8 |
| TXFC6 | 2026-03 | would_emit | 1 |
| TXFD6 | 2026-01 | missing_opening | 3 |
| TXFD6 | 2026-01 | missing_post | 1 |
| TXFD6 | 2026-01 | no_break | 1 |
| TXFD6 | 2026-02 | missing_opening | 2 |
| TXFD6 | 2026-02 | missing_post | 3 |
| TXFD6 | 2026-02 | no_break | 1 |
| TXFD6 | 2026-02 | rv_ratio_below_1.25 | 1 |
| TXFD6 | 2026-03 | missing_opening | 2 |
| TXFD6 | 2026-03 | rv_ratio_below_1.25 | 5 |
| TXFD6 | 2026-03 | would_emit | 1 |
| TXFD6 | 2026-04 | missing_opening | 2 |
| TXFD6 | 2026-04 | rv_ratio_below_1.25 | 6 |
| TXFD6 | 2026-04 | would_emit | 1 |
| TXFE6 | 2026-02 | missing_opening | 1 |
| TXFE6 | 2026-02 | missing_post | 1 |
| TXFE6 | 2026-03 | missing_opening | 1 |
| TXFE6 | 2026-03 | missing_post | 10 |
| TXFE6 | 2026-03 | rv_ratio_below_1.25 | 2 |
| TXFE6 | 2026-03 | would_emit | 1 |
| TXFE6 | 2026-03 | zero_opening_rv | 1 |
| TXFE6 | 2026-04 | missing_opening | 3 |
| TXFE6 | 2026-04 | no_break | 2 |
| TXFE6 | 2026-04 | rv_ratio_below_1.25 | 5 |
| TXFE6 | 2026-04 | would_emit | 1 |
| TXFE6 | 2026-05 | missing_opening | 1 |
| TXFE6 | 2026-05 | rv_ratio_below_1.25 | 4 |

## Interpretation

The original 0-event blocker is resolved. Current viability output has
`events=7`, and coverage `would_emit=7`, so A5 no longer fires.

The remaining verdict is `DETECTOR_BUG` with primary reason A1 because
`missing_opening=19/86 = 22.09%` exceeds the pre-registered 20% sanity
threshold. Treat this as a data/session-window quality defect to investigate
before promoting T1-A. It does not block producing a regime-conditioned
scorecard, but it does block any claim that T1-A v0 is fully clean.
