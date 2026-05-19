# T1-A Zero-Event Diagnostic — Design Spec

**Date:** 2026-05-19
**Track:** T1 — TXF Higher-Timeframe Regime → TMF Expression (diagnostic layer)
**Status:** Spec frozen pending user review
**Upstream blocker:** `docs/superpowers/plans/2026-05-19-regime-conditioned-t1-revalidation.md` Task 8 — T1-A v0 produced 0 events / 57 audited days, so regime-partition has nothing to score.
**Charter:** `~/.claude/projects/-home-charlie-hft-platform/memory/track_t1_opened_2026_05_13.md`
**Discipline:** `~/.claude/projects/-home-charlie-hft-platform/memory/txf_led_research_discipline_2026_05_13.md`

---

## 1. Scope & Question

**Question:** On 57 audited trading days, why did T1-A v0 detector emit 0 events? Classify the root cause into exactly one of three categories:

1. **DETECTOR_BUG** — implementation defect in `detect_opening_range_events` or its inputs
2. **V0_RULE_TOO_STRICT** — implementation correct, but frozen v0 thresholds reject substantially all observed market behavior on TAIFEX
3. **DATA_COVERAGE_NARROW** — implementation correct, thresholds appropriate, but the available paired-day universe is too sparse or skewed to produce events

These three diagnoses each trigger a different downstream action (Section 3), so misclassification has high cost. The diagnostic exists to prevent that misclassification.

### Outputs

- Per-cause rejection histogram (counts + percentages over total)
- Six conditional probabilities (Section 2)
- Per-contract × per-month breakdown of rejection causes
- A single terminal verdict ∈ {DETECTOR_BUG, V0_RULE_TOO_STRICT, DATA_COVERAGE_NARROW, INCONCLUSIVE}
- Generated markdown + JSON artifact

### Inputs

- Existing coverage CSVs at `research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0/` (two files dated 2026-05-13)
- If those are stale or incomplete, freshly regenerated via the existing `python -m research.t1.regime_viability coverage` subcommand using **identical frozen v0 parameters** (no parameter change permitted)

### Hard prohibitions

- No modification to v0 detector parameters (`opening_minutes=30`, `confirm_minutes=30`, `min_break_points=8.0`, `min_rv_ratio=1.25`)
- No new instrumentation added to the detector — all diagnostic columns already exist in `coverage_audit_opening_range`'s output
- No reverse-fitting parameters from the histogram
- Verdict thresholds (Section 3) are frozen in this spec; not tunable at CLI invocation
- Analyst commentary may not override the verdict (PV4 in Section 3)

### Out of scope

- Fixing any detector bug discovered (separate spec / plan if V1 fires)
- Rewriting T1-A v0 (separate brainstorm if V2 fires)
- Broadening the universe or sourcing more paired days (escalation to user if V3 fires)
- Running the regime-partition CLI (already implemented; will run only after a successful upstream fix)
- T1-B / T1-C brainstorms (independent, still queued)

---

## 2. Architecture & Data Flow

```
Existing coverage CSV(s)
  research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0/*.csv
   ↓
Loader (new):     concatenate + dedupe on (contract, trading_day)
   ↓
Classifier (new): each row → exactly one terminal rejection_cause
   ↓
Aggregator (new): per-cause counts + conditional probabilities +
                  per-contract × per-month breakdown
   ↓
Verdict engine (new): apply pre-registered V1 / V2 / V3 rules in order
   ↓
Reporter (new):   markdown + JSON
```

### 2.1 Components (all new)

| Path | Purpose |
| --- | --- |
| `research/tools/t1_a_zero_event_diagnostic/__init__.py` | package marker |
| `research/tools/t1_a_zero_event_diagnostic/load.py` | read + concat + dedupe coverage CSVs |
| `research/tools/t1_a_zero_event_diagnostic/classify.py` | row → rejection_cause |
| `research/tools/t1_a_zero_event_diagnostic/aggregate.py` | histograms + conditional probabilities + breakdown grid |
| `research/tools/t1_a_zero_event_diagnostic/verdict.py` | three-way classifier + fallback INCONCLUSIVE |
| `research/tools/t1_a_zero_event_diagnostic/cli.py` | CLI entrypoint |
| `tests/unit/research/t1_a_zero_event_diagnostic/` | unit tests (see Section 4) |
| `docs/alpha-research/t1a_zero_event_diagnostic_2026_05_19.md` | generated verdict artifact |
| `~/.claude/projects/-home-charlie-hft-platform/memory/t1a_zero_event_diagnostic_2026_05_19.md` | memory snapshot |

### 2.2 Reuse (no change)

- `research/t1/regime_viability.py:284-450` — `coverage_audit_opening_range` (already emits all required columns)
- The two existing CSVs in the validations directory
- Frozen v0 parameters as a literal constant set inside `verdict.py`

### 2.3 Rejection cause taxonomy (terminal, mutually exclusive, ordered)

Derived from `detect_opening_range_events` (`research/t1/regime_viability.py:215-256`). Classifier walks gates in this order and assigns the first failing gate:

| # | rejection_cause | Trigger condition (from coverage row) |
| --- | --- | --- |
| 1 | `missing_opening` | `coverage_status == "missing_opening"` |
| 2 | `missing_post` | `coverage_status == "missing_post"` |
| 3 | `zero_opening_rv` | `coverage_status == "ok"` AND `break_magnitude_vs_prior_realized_vol is None` AND `realized_vol_ratio is None` |
| 4 | `no_break` | `coverage_status == "ok"` AND `break_side == "none"` AND not row 3 |
| 5 | `break_below_8pt` | `break_side in {"up","down"}` AND `break_magnitude_pts < 8.0` |
| 6 | `rv_ratio_below_1.25` | `break_magnitude_pts >= 8.0` AND (`realized_vol_ratio is None` OR `realized_vol_ratio < 1.25`) |
| 7 | `vwap_filter_fail` | `break_magnitude_pts >= 8.0` AND `realized_vol_ratio >= 1.25` AND VWAP-side disagreement: (`break_side == "up"` AND `vwap_side_at_break == "below"`) OR (`break_side == "down"` AND `vwap_side_at_break == "above"`) |
| 8 | `would_emit` | passes all v0 gates; cross-check: `event_selected_by_v0 == True` |

A consistency invariant: for every row, the classifier-derived `would_emit` flag must equal the row's pre-computed `event_selected_by_v0`. Failure of that invariant on any row indicates a divergence between `detect_opening_range_events` and `coverage_audit_opening_range` and is itself a DETECTOR_BUG (covered by V1.A consistency check, see Section 3).

### 2.4 Conditional probabilities (used by verdict)

Computed over the deduped coverage row set:

- `P_post_present = 1 − (N_missing_opening + N_missing_post) / N_total`
- `P_break_given_post = N(break_side != "none") / N_post_present`
- `P_mag_ge_8_given_break = N(break_magnitude_pts >= 8.0) / N(break_side != "none")`
- `P_rv_ratio_ge_1_25_given_break = N(realized_vol_ratio >= 1.25) / N(break_side != "none")`
- `P_vwap_ok_given_qualifying = N(vwap pass) / N(break_magnitude_pts >= 8 AND realized_vol_ratio >= 1.25)`
- `P_would_emit = N(would_emit) / N_total`

Each ratio with denominator 0 is reported as `null` (strict JSON, no NaN).

### 2.5 Loader rules

- Concatenate all `--coverage-csv <path>` inputs (multi-input allowed)
- Dedupe key: `(contract, trading_day)` — T1-A v0 emits at most one event per pair-day, so duplicates are re-runs
- Tie-breaker on dedup: keep the row with the latest `bbo_last_time` (most-complete data snapshot); if `bbo_last_time` is missing on both, keep the lexicographically last input path's row
- Record sha256 per input path in `run_config.coverage_csv_sha256_by_path`
- Reject empty input set with exit code != 0 (no `INCONCLUSIVE` masking)

---

## 3. Pre-Registered Verdict Rules

Three terminal verdicts, mutually exclusive, evaluated **in order**. The first verdict that fires wins. A fourth fallback `INCONCLUSIVE` covers the case where none fire.

### V1. `DETECTOR_BUG`

Fires if **ANY**:

- **A1.** `P(missing_opening) >= 0.20` — 1/5+ days have no opening window. Either session_start_ns timezone bug or wholly empty BBO file shape.
- **A2.** `P(missing_post) >= 0.20` AND `P(missing_opening) < 0.05` — opening always present but post systematically missing → confirm-window timestamp arithmetic bug.
- **A3.** `P(zero_opening_rv) >= 0.20` — 1/5+ days where opening_mid has no variance → quote-pollution, dedup bug, or mid computation degenerate.
- **A4.** `P_break_given_post <= 0.10` AND `P(missing_post) < 0.10` — post window present but break rate < 10%. On TAIFEX day-session, BBO crossing an opening-range boundary within 30min should happen on the majority of days; a near-zero rate suggests boundary computation or comparison-operator bug.
- **A5.** Consistency invariant violated on ≥ 1 row: classifier-derived `would_emit` disagrees with the row's `event_selected_by_v0`.

**Downstream action:** open follow-up plan `docs/superpowers/plans/2026-05-2X-t1a-detector-bug-fix.md`; do NOT modify v0 spec or thresholds.

### V2. `V0_RULE_TOO_STRICT`

Fires if (V1 did not fire) AND **ALL**:

- **B1.** `P_break_given_post >= 0.30` — breaks happen at a reasonable rate
- **B2.** `P_would_emit <= 0.10` AND total `would_emit` count = 0
- **B3.** At least one of:
  - **B3a.** `P_mag_ge_8_given_break <= 0.20` — 8pt threshold filters out 80%+ of breaks
  - **B3b.** `P_rv_ratio_ge_1_25_given_break <= 0.30` AND N(`break_magnitude_pts >= 8`) ≥ 5 — RV expansion filter dominant rejector
  - **B3c.** `P_vwap_ok_given_qualifying <= 0.30` AND N(qualifying) ≥ 5 — VWAP filter dominant rejector

**Downstream action:** open follow-up plan that **KILLs T1-A v0** and triggers a fresh T1-A v1 brainstorm (or skips T1-A entirely and moves to T1-B/T1-C). **No parameter tweaking on v0.**

### V3. `DATA_COVERAGE_NARROW`

Fires if (V1 + V2 did not fire) AND **ANY**:

- **C1.** Per-contract trading-day count < 20 for ≥ 2 of {B6, C6, D6, E6}
- **C2.** Pair availability gap: more than 30% of `audited_trading_days` are days where only one of TXF/TMF has the legacy `.npy` file present
- **C3.** Calendar gap: > 14 consecutive calendar days within the audited range with zero `would_emit` AND zero `break_given_post`

**Downstream action:** pause T1-A diagnosis; produce a "data availability snapshot" doc; wait for more paired data OR escalate to user for universe-broadening decision. **No parameter tweaking, no T1-A KILL.**

### V4. `INCONCLUSIVE` (fallback)

If none of V1 / V2 / V3 fire, report all counts + escalate to user with options. Do not invent a fourth verdict category.

### Pre-registered prohibitions on verdict logic

- **PV1.** Verdict thresholds (the numeric constants in V1–V3) are frozen in this spec; not tunable at CLI invocation. Tests assert literal values.
- **PV2.** Verdict logic runs once per CSV input set; no "what-if" sweeps over thresholds.
- **PV3.** If multiple V1 sub-conditions fire simultaneously, report all but verdict remains `DETECTOR_BUG` (no priority sorting between A1–A5 — they all imply the same downstream action).
- **PV4.** Verdict cannot be overridden in the markdown by an analyst note. Analyst commentary goes into a separate `## Interpretation` section *after* the verdict row, never in place of it.

---

## 4. Testing, Validation & Determinism

### 4.1 Unit tests

| Test | Path | Asserts |
| --- | --- | --- |
| `test_classify_row_missing_opening` | `tests/unit/research/t1_a_zero_event_diagnostic/test_classify.py` | `coverage_status="missing_opening"` → `missing_opening` |
| `test_classify_row_missing_post` | same | `coverage_status="missing_post"` → `missing_post` |
| `test_classify_row_zero_opening_rv` | same | `coverage_status="ok"` AND `break_magnitude_vs_prior_realized_vol is None` AND `realized_vol_ratio is None` → `zero_opening_rv` |
| `test_classify_row_no_break` | same | `break_side="none"` not row 3 → `no_break` |
| `test_classify_row_break_below_8pt` | same | `break_magnitude_pts=5.0` → `break_below_8pt` |
| `test_classify_row_rv_ratio_below` | same | mag=10, rv_ratio=1.0 → `rv_ratio_below_1.25` |
| `test_classify_row_vwap_filter_up_below` | same | mag=10, rv=2.0, `break_side="up"`, `vwap_side_at_break="below"` → `vwap_filter_fail` |
| `test_classify_row_vwap_filter_down_above` | same | symmetric: `break_side="down"`, `vwap_side_at_break="above"` → `vwap_filter_fail` |
| `test_classify_row_would_emit_matches_event_selected` | same | `event_selected_by_v0=True` AND all gates satisfied → `would_emit` AND consistency invariant holds |
| `test_classify_row_consistency_violation_flagged` | same | hand-crafted row where derived `would_emit` disagrees with `event_selected_by_v0` flag → classifier records violation in returned metadata |
| `test_classify_row_exhaustive_disjoint` | same | every fixture row assigned exactly one cause |
| `test_aggregate_histogram_counts` | `tests/unit/research/t1_a_zero_event_diagnostic/test_aggregate.py` | per-cause counts sum to total rows |
| `test_aggregate_conditional_probabilities` | same | six conditional probs in `[0,1]` or `None`; numerator/denominator math correct on fixture |
| `test_aggregate_conditional_zero_denominator_is_none` | same | denominator-0 case → `None`, not `NaN`, not `0.0` |
| `test_aggregate_per_contract_per_month_breakdown` | same | contract × month grid matches hand-counted fixture |
| `test_load_dedupe_on_contract_trading_day` | `tests/unit/research/t1_a_zero_event_diagnostic/test_load.py` | duplicate `(contract, trading_day)` keeps row with later `bbo_last_time` |
| `test_load_dedupe_tie_breaker_on_missing_bbo_last_time` | same | both rows missing `bbo_last_time` → keep row from lexicographically last input path |
| `test_load_records_sha256_per_path` | same | run_config carries sha256 for each input |
| `test_load_empty_input_raises` | same | empty CSV set → exception (CLI converts to non-zero exit) |
| `test_verdict_v1_a1_missing_opening` | `tests/unit/research/t1_a_zero_event_diagnostic/test_verdict.py` | 30% missing_opening rate → `DETECTOR_BUG` with reason A1 |
| `test_verdict_v1_a2_missing_post` | same | 25% missing_post, 1% missing_opening → `DETECTOR_BUG` reason A2 |
| `test_verdict_v1_a3_zero_rv` | same | 25% zero_opening_rv → `DETECTOR_BUG` reason A3 |
| `test_verdict_v1_a4_low_break_rate` | same | post-present, break_rate=5% → `DETECTOR_BUG` reason A4 |
| `test_verdict_v1_a5_consistency_violation` | same | any classifier-vs-flag disagreement → `DETECTOR_BUG` reason A5 |
| `test_verdict_v2_too_strict_via_8pt` | same | breaks plentiful, 90% < 8pt → `V0_RULE_TOO_STRICT` reason B3a |
| `test_verdict_v2_too_strict_via_rv` | same | breaks plentiful, mag OK, rv mostly < 1.25 → `V0_RULE_TOO_STRICT` reason B3b |
| `test_verdict_v2_too_strict_via_vwap` | same | qualifying ≥ 5, vwap pass < 30% → `V0_RULE_TOO_STRICT` reason B3c |
| `test_verdict_v3_data_coverage_narrow_c1` | same | only B6+D6 with ≥ 20 days → `DATA_COVERAGE_NARROW` reason C1 |
| `test_verdict_inconclusive_when_no_rule_fires` | same | ambiguous histogram → `INCONCLUSIVE` |
| `test_verdict_priority_v1_over_v2_over_v3` | same | fixture satisfying V1.A1 AND V2.B3a returns `DETECTOR_BUG` (V1 wins) |
| `test_verdict_thresholds_are_literal_constants` | same | the numeric constants in verdict module match the spec exactly (no env override) |
| `test_cli_emits_markdown_and_json` | `tests/unit/research/t1_a_zero_event_diagnostic/test_cli.py` | both artifacts present; JSON has `verdict`, `causes`, `conditional_probs`, `run_config.coverage_csv_sha256_by_path` |
| `test_cli_rejects_empty_coverage_input` | same | empty CSV set → exit code != 0; no INCONCLUSIVE masking |
| `test_cli_concatenates_multiple_csv_inputs` | same | `--coverage-csv a.csv --coverage-csv b.csv` dedup-merges both |
| `test_cli_strict_json` | same | output JSON parseable with `allow_nan=False`; no NaN/Infinity |

### 4.2 Determinism

- Single seed (`HFT_T1A_DIAG_SEED=20260519`), reserved for future tie-breaking; current implementation has no random ops.
- Input CSV sha256 recorded per path in `run_config.coverage_csv_sha256_by_path`.
- JSON: sorted keys + `allow_nan=False` (NaN / Infinity → `null`).
- `git rev-parse HEAD` → `run_config.git_sha`.
- Spec sha recorded in `run_config.spec_sha256`.

### 4.3 Coverage-CSV freshness check (run-time)

The CLI computes `audited_trading_days_in_input = N_total_dedup` and prints a comparison line against the summary JSON sibling file (`*_summary.json`) when present:

```
audited_trading_days  summary=57  input=57  match=true
```

If `match=false`, exit code 0 still but markdown includes a top-of-file warning banner. Regeneration is then a user choice, not automated.

---

## 5. Deliverables & Prohibition List

### 5.1 Deliverables (build order)

1. `.gitignore` allowlist patch for `research/tools/t1_a_zero_event_diagnostic/` (single commit; without this all subsequent commits are blind)
2. Package skeleton + classifier
3. Loader + dedupe + sha256
4. Aggregator + conditional probabilities + breakdown grid
5. Verdict engine + tests
6. CLI + tests
7. Run on existing `T1_A_opening_range_definition_coverage_audit_v0/*.csv`
8. (Conditional) Re-run `python -m research.t1.regime_viability coverage --raw-dir research/data/raw_legacy --months B6,C6,D6,E6 --out-dir <new>` if existing CSVs do not match the summary's `audited_trading_days`
9. Generated verdict markdown + memory entry

### 5.2 Prohibition list

- **P1.** No modification to T1-A v0 detector parameters (8.0 / 1.25 / 30 / 30).
- **P2.** No new instrumentation columns added to `coverage_audit_opening_range`; classifier consumes only existing columns.
- **P3.** No reverse-fitting parameters from the histogram.
- **P4.** Verdict thresholds (V1.A1–A5, V2.B1–B3c, V3.C1–C3) are spec-frozen; tests assert literal constants.
- **P5.** No "what-if" sweeps over thresholds at runtime.
- **P6.** Verdict cannot be overridden by analyst markdown. Interpretation is a separate section.
- **P7.** Empty input is a hard CLI error, not `INCONCLUSIVE`.
- **P8.** No `git add -f` on `research/tools/t1_a_zero_event_diagnostic/`; the `.gitignore` patch (deliverable 1) is the canonical fix.

### 5.3 Out-of-scope follow-ups (named, not built)

- Detector bug fix (separate plan, triggered only if V1 fires)
- T1-A v1 spec authoring (separate brainstorm, triggered only if V2 fires)
- Universe-broadening data-availability decision (escalation, triggered only if V3 fires)
- T1-B / T1-C brainstorms (independent of this diagnostic outcome; still queued)
- Regime-partition CLI run (already implemented; will run only after a successful upstream fix re-produces a non-empty T1-A event CSV)

---

## 6. Risks & Open Questions

### 6.1 Risks

| Risk | Mitigation |
| --- | --- |
| Existing 2026-05-13 coverage CSVs cover fewer than 57 audited days reported in the summary | Deliverable 7's freshness check; deliverable 8 regenerates with identical frozen params |
| `coverage_audit_opening_range` subtly diverges from `detect_opening_range_events` (e.g. break detection uses strict vs non-strict comparison) | A5 consistency invariant + `test_classify_row_consistency_violation_flagged` |
| Dedup on `(contract, trading_day)` hides legitimate intra-day repeats | T1-A v0 spec emits at most one event per pair-day; dedup matches spec |
| Conditional-prob denominators small → noisy verdict | B3 sub-rules require N ≥ 5 in numerator basis; V4 INCONCLUSIVE catches small-N ambiguity |
| Analyst writes an alternative verdict in markdown | PV4 + tests; reviewer rejects PRs that violate |
| `.gitignore` continues to block new tool packages | Deliverable 1 fixes this category permanently for the new package; future packages need a one-line patch each |

### 6.2 Open questions (resolved at plan time)

- Coverage CSV freshness: trust existing if `audited_trading_days_in_input == summary.audited_trading_days`; otherwise regenerate (Section 4.3 + deliverable 8).
- Whether to record per-month breakdown in JSON or only markdown: **both** (JSON for programmatic follow-up, markdown for human read).
- Whether to surface `event_selected_by_v0` discrepancies as warnings vs hard verdict: **hard verdict** (V1.A5) — any discrepancy is a defect.

---

## 7. Cross-References

- Upstream blocker plan: `docs/superpowers/plans/2026-05-19-regime-conditioned-t1-revalidation.md`
- T1-A v0 detector implementation: `research/t1/regime_viability.py:215-256` (detect) + `:284-450` (coverage)
- T1-A v0 frozen spec: `docs/alpha-research/t1a_opening_range_expansion_spec_2026_05_13.md`
- Existing coverage CSVs: `research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0/`
- Track T1 charter: `~/.claude/projects/-home-charlie-hft-platform/memory/track_t1_opened_2026_05_13.md`
- TXF-led research discipline: `~/.claude/projects/-home-charlie-hft-platform/memory/txf_led_research_discipline_2026_05_13.md`
- R65 closure (single-day-dominance pathology, source of "no reverse-fitting" principle): `~/.claude/projects/-home-charlie-hft-platform/memory/r65_closure_2026_05_11.md`
- Regime-partition spec (downstream consumer): `docs/superpowers/specs/2026-05-19-regime-conditioned-t1-revalidation-design.md`
