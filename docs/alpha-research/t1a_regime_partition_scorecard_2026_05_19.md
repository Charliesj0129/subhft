# T1 Regime Partition - t1a

- Spec: `docs/superpowers/specs/2026-05-19-regime-conditioned-t1-revalidation-design.md`
- Audit CSV: `research/experiments/validations/T1_regime_viability_audit_v0_fixed_20260519/20260519T100217Z_opening_range_events.csv`
- Regime CSV: `research/data/derived/regime_panels_front_1s/front_month_single_regime_intraday_active_h30s.csv`
- Regime CSV sha256: `36c4dab242345a177250f1b5c4339cc5053a04855c54364a30d7a9ddf86acbac`
- Commit: `f4deecd31f583ce38fc2b44ae1a16d9802ef1928`

## Cell verdicts

| cell | n | n_days | n_contracts | median | PF | pos_days | rb1 | sdd | verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| baseline | 7 | 7 | 4 | -82.0000 | 0.3813 | 0.1429 | -112.3333 | 0.2943 | **INCONCLUSIVE** |
| regime_3 | 3 | 3 | 2 | -36.0000 | 2.1780 | 0.3333 | -59.0000 | 0.6853 | **INCONCLUSIVE** |
| regime_5 | 1 | 1 | 1 | -274.0000 | 0.0000 | 0.0000 | NA | 1.0000 | **INCONCLUSIVE** |
| mixed_regime | 0 | 0 | 0 | NA | NA | NA | NA | NA | **INCONCLUSIVE** |
| invalid_regime | 1 | 1 | 1 | -129.0000 | 0.0000 | 0.0000 | NA | 1.0000 | **INCONCLUSIVE** |
| missing_regime | 0 | 0 | 0 | NA | NA | NA | NA | NA | **INCONCLUSIVE** |

## TXF-root ablation verdicts

| cell | n | n_days | n_contracts | median | PF | pos_days | rb1 | sdd | verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| baseline | 7 | 7 | 4 | -82.0000 | 0.3813 | 0.1429 | -112.3333 | 0.2943 | **INCONCLUSIVE** |
| regime_3 | 1 | 1 | 1 | 257.0000 | NA | 1.0000 | NA | 1.0000 | **INCONCLUSIVE** |
| regime_5 | 2 | 2 | 2 | -155.0000 | 0.0000 | 0.0000 | -274.0000 | 0.8839 | **INCONCLUSIVE** |
| mixed_regime | 1 | 1 | 1 | -82.0000 | 0.0000 | 0.0000 | NA | 1.0000 | **INCONCLUSIVE** |
| invalid_regime | 1 | 1 | 1 | -129.0000 | 0.0000 | 0.0000 | NA | 1.0000 | **INCONCLUSIVE** |
| missing_regime | 0 | 0 | 0 | NA | NA | NA | NA | NA | **INCONCLUSIVE** |

## Reasons (non-PROCEED cells)
### baseline
- n_events 7 < 20 (additional_n_needed=13)

### regime_3
- n_events 3 < 20 (additional_n_needed=17)

### regime_5
- n_events 1 < 20 (additional_n_needed=19)

### mixed_regime
- n_events 0 < 20 (additional_n_needed=20)

### invalid_regime
- n_events 1 < 20 (additional_n_needed=19)

### missing_regime
- n_events 0 < 20 (additional_n_needed=20)

## Pre-registered expectations

(see spec §3.1)

## Interpretation

The T1-A v0 pipeline is now executable end to end: the fixed viability audit
emits 7 events and the regime-partition CLI runs without schema or empty-input
errors.

The research verdict is still not promotable. Every cell is `INCONCLUSIVE`
because the pre-registered N-floor requires at least 20 events; baseline has
only 7 and needs 13 more. The preliminary baseline shape is weak: median
`net_30m_pts=-82.0`, profit factor `0.3813`, positive-day fraction `14.29%`,
and stop-breach rate `85.71%`. These numbers are descriptive only under the
N-floor, but they lean negative rather than promising.

Operational next step: keep T1-A v0 frozen, do not parameter-search, and collect
or extend enough clean paired days to reach at least 20 detector events before
re-scoring. In parallel, investigate the A1 opening-window coverage defect from
`docs/alpha-research/t1a_zero_event_diagnostic_fixed_2026_05_19.md`.
