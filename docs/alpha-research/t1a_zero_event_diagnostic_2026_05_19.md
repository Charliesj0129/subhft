# T1-A Zero-Event Diagnostic

- Spec: `docs/superpowers/specs/2026-05-19-t1a-zero-event-diagnostic-design.md`
- Spec sha256: `4d2bf2df832cf66af3160b0cdf349673571588e31950193e9bfef52f72b96802`
- Commit: `a6af810c162d3e798c59ccb81bfc525a9a992762`
- Viability event CSV: `research/experiments/validations/T1_regime_viability_audit_v0/20260513T153706Z_opening_range_events.csv`
- Viability event count: 0
- Viability summary events: 0
- Coverage `would_emit` count: 5
- Freshness: summary=57 input=57 match=True

## Inputs

- `research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0/20260513T154522Z_opening_range_coverage.csv` (sha256 `5e5c071bc219...`)
- `research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0/20260513T155004Z_opening_range_coverage.csv` (sha256 `cd938b0ea3ca...`)

## Verdict: **DETECTOR_BUG** (primary reason: **A5**)

- A5: coverage would_emit=5 != viability events=0
- A1: P(missing_opening)=29.07% >= 20%

## Cause Histogram

| cause | count | pct |
| --- | ---: | ---: |
| missing_opening | 25 | 29.1% |
| missing_post | 15 | 17.4% |
| zero_opening_rv | 1 | 1.2% |
| no_break | 8 | 9.3% |
| break_below_8pt | 0 | 0.0% |
| rv_ratio_below_1.25 | 32 | 37.2% |
| vwap_filter_fail | 0 | 0.0% |
| would_emit | 5 | 5.8% |

## Conditional Probabilities

| metric | value |
| --- | ---: |
| P_post_present | 53.49% |
| P_break_given_post | 82.61% |
| P_mag_ge_8_given_break | 100.00% |
| P_rv_ratio_ge_1_25_given_break | 13.16% |
| P_vwap_ok_given_qualifying | 100.00% |
| P_would_emit | 5.81% |

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
| TXFD6 | 2026-03 | missing_opening | 5 |
| TXFD6 | 2026-03 | rv_ratio_below_1.25 | 3 |
| TXFD6 | 2026-04 | missing_opening | 5 |
| TXFD6 | 2026-04 | rv_ratio_below_1.25 | 4 |
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

Verdict `DETECTOR_BUG` (primary reason A5) means
`coverage_audit_opening_range` and `detect_opening_range_events` disagree on
whether v0 should emit. The coverage-derived classifier identifies 5 rows as
`would_emit`; the viability runner emits 0 events over 57 audited trading days.
Per spec Section 3 V1, the next step is a separate fix plan that reconciles the
two code paths. Do not modify v0 spec or thresholds.

Spec follow-up plan to be authored:
`docs/superpowers/plans/2026-05-2X-t1a-detector-bug-fix.md`
