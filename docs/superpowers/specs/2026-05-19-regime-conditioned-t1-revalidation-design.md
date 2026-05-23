# Regime-Conditioned T1 Revalidation — Design Spec

**Date:** 2026-05-19
**Track:** T1 — TXF Higher-Timeframe Regime → TMF Expression
**Status:** Spec frozen pending user review
**Charter:** `~/.claude/projects/-home-charlie-hft-platform/memory/track_t1_opened_2026_05_13.md`
**Discipline:** `~/.claude/projects/-home-charlie-hft-platform/memory/txf_led_research_discipline_2026_05_13.md`
**Regime input:** `research/data/derived/regime_panels_front_1s/front_month_single_regime_intraday_active_h30s.csv` (active-only daily dominant regime, dominance ≥ 0.55, horizon 30s)

---

## 1. Scope & Question

**Question:** Do the three Track-T1 candidate hypotheses (T1-A opening-range expansion, T1-B vol-compression breakout, T1-C VWAP failed-reclaim) exhibit structurally different return profiles between **regime 3 (HIGH_VOL_RANGE, 49 single-dominance days)** and **regime 5 (HIGH_VOL_DOWNTREND_CRASH, 50 single-dominance days)**?

**Output type:** **Post-hoc regime partition audit.** Not a regime-gated strategy.

**Why post-hoc only:**

- The active-only daily dominant regime label is an end-of-day aggregation over 1s snapshots. At the T1-A trigger time (≤ 11:00 Taipei) the label is not yet known. Using it as an ex-ante entry filter would inject look-ahead bias.
- Post-hoc partition still answers the structural question: is the hypothesis **dependent** on regime, **invariant** across regime, or **failing** in every regime? Each verdict has a distinct downstream consequence (see Section 3).
- The ex-ante regime predictability study ("Study B" — can the first-30m of a day predict end-of-day regime label?) is a named follow-up, not in this spec.

**Universe (matches Track T1 charter):**

- Signal leg: TXF{B6, C6, D6, E6} front-month continuous
- Execution leg: TMF{B6, C6, D6, E6} front-month continuous
- All paired days from `research/data/raw_legacy/`
- Day-session only (08:45–13:45 Asia/Taipei); night session excluded in v0

**Out of scope (this spec):**

- Ex-ante regime prediction (Study B)
- New T1 candidate hypotheses (no T1-D/E/F)
- Any parameter change to T1-A/B/C v0
- Cross-regime portfolio mixing or sizing
- L2 features in entry logic (Track T1 hard prohibition)

---

## 2. Architecture & Data Flow

```
T1-A audit (frozen v0, 2026-05-13 spec)
T1-B audit (frozen v0, this spec)
T1-C audit (frozen v0, this spec)
   ↓ shared per-event row schema (Section 2.3)
Regime-join layer
   ↓ left-join on (contract_root, date) against active-only regime CSV
Regime-partition scorecard
   ↓ per (hypothesis × regime) cell metrics + KILL/PROCEED/INCONCLUSIVE verdict
Verdict writer
   ↓ docs/alpha-research/t1_regime_partition_2026_05_19.md
```

### 2.1 Components (new code)

| Component | Path | Purpose |
| --- | --- | --- |
| `t1_regime_partition` package | `research/tools/t1_regime_partition/__init__.py` | package marker |
| Regime-join | `research/tools/t1_regime_partition/regime_join.py` | left-join audit rows × regime CSV on (contract_root, date) for both TMF and TXF roots |
| Scorecard | `research/tools/t1_regime_partition/scorecard.py` | per-cell metrics + KILL/PROCEED/INCONCLUSIVE logic |
| CLI | `research/tools/t1_regime_partition/cli.py` | `regime-partition` subcommand, emits markdown + JSON |
| Shared audit runner skeleton | `research/tools/t1_audits/__init__.py`, `runner.py` | row schema + I/O + frozen-param guard |
| T1-A audit runner | `research/tools/t1_audits/t1a_opening_range.py` | implements T1-A v0 spec (2026-05-13) |
| T1-B audit runner | `research/tools/t1_audits/t1b_vol_compression.py` | implements T1-B v0 spec (this spec, sibling doc) |
| T1-C audit runner | `research/tools/t1_audits/t1c_vwap_failed_reclaim.py` | implements T1-C v0 spec (this spec, sibling doc) |
| Unit tests | `tests/unit/research/t1_regime_partition/`, `tests/unit/research/t1_audits/` | see Section 4 |
| T1-B v0 spec | `docs/alpha-research/t1b_vol_compression_breakout_spec_2026_05_19.md` | mirror of T1-A spec format |
| T1-C v0 spec | `docs/alpha-research/t1c_vwap_failed_reclaim_spec_2026_05_19.md` | mirror of T1-A spec format |
| Generated verdict | `docs/alpha-research/t1_regime_partition_2026_05_19.md` | output artifact |

### 2.2 Reuse (no change)

- Legacy `*_bidask.npy` / `*_ticks.npy` via `hftbt_to_legacy_bridge` (see `/taifex_l2_bridge_2026_05_13`)
- Latency profile id `r47_maker_shioaji_p95_v2026-04-24_measured` (see `/shioaji_broker_asymmetric_latency_2026_04_24`)
- Active-only daily dominant regime CSV (Section header)

### 2.3 Shared per-event row schema

All three T1 audit runners produce rows with the following columns. Mirrors and extends the T1-A v0 schema:

```
hypothesis_id        # "t1a" / "t1b" / "t1c"
contract             # "TXFD6/TMFD6" etc. (signal/execution pair)
date                 # YYYY-MM-DD Asia/Taipei
trigger_time_ns      # entry trigger exch_ts (ns)
direction            # +1 long / -1 short
txf_entry_ref        # TXF reference price (scaled int x1e6)
tmf_executable_entry # TMF taker fill (scaled int x1e6); long pays ask, short hits bid
mfe_15m_pts, mae_15m_pts
mfe_30m_pts, mae_30m_pts
mfe_60m_pts, mae_60m_pts
return_15m_pts, return_30m_pts, return_60m_pts   # pre-cost
net_30m_pts          # return_30m_pts - 8.0 (primary gate column)
stop_structure_breached
time_to_mfe_s, time_to_mae_s
reverted_to_range
vwap_reclaim_failed_or_passed
```

### 2.4 Data-flow rules

1. **Contract root mapping.** Audit `contract` field is parsed into TMF root (e.g. `TMFD6`) and TXF root (e.g. `TXFD6`). Both join against the regime CSV. **Primary scorecard uses TMF-root regime** (execution leg). **Ablation table uses TXF-root regime.**
2. **Daily join key.** Join key is `(contract_root, date)`. `trigger_time_ns` is preserved but not used for join — daily label is daily.
3. **INVALID (regime_id = -1) days.** Rows tagged `regime_id_for_scorecard = NaN`; reported as a separate "no-regime-label coverage" bucket; not used in any scorecard cell.
4. **Sub-dominance (`selected=False`).** Rows on mixed-regime days (dominance < 0.55) get `regime_id_for_scorecard = NaN`; reported as a separate "mixed-regime" bucket; not folded into regime-3 or regime-5 cells.
5. **Regime CSV provenance.** sha256 of the regime CSV is computed at run start and stored in the output JSON `run_config`.

---

## 3. Hypotheses, Pre-Registration & Verdict Logic

### 3.1 Pre-registered expected directions

| Hypothesis | Mechanism | Expected regime-3 (HIGH_VOL_RANGE) | Expected regime-5 (HIGH_VOL_DOWNTREND_CRASH) |
| --- | --- | --- | --- |
| T1-A opening-range expansion | regime persists after early breakout | **FAIL** (range absorbs / reverts) | **PASS** (trend/crash extends) |
| T1-B vol-compression breakout | quiet → directional release | MIXED (range can compress without release) | **PASS** (compression precedes crash leg) |
| T1-C VWAP failed-reclaim | session imbalance persists | **FAIL** (VWAP reclaimed in range) | **PASS** (failed reclaim = continuation) |

These directions are committed before the run. Post-hoc rationalization of opposite findings is prohibited (P5–P6, Section 5).

### 3.2 Per-cell PROCEED rule

A `(hypothesis × regime)` cell qualifies as **PROCEED-conditional-on-regime** if **ALL** hold:

- N_events ≥ 20 within the cell
- Cross-contract ≥ 3 of {B6, C6, D6, E6} present
- median `net_30m_pts` > 0
- profit factor PF > 1.2
- positive-days fraction > 55%
- remove-best-1 mean ≥ 0 or "near-flat" (mean stays within 50% of the all-trades mean magnitude, same sign)
- single-day-dominance < 60% (max single-day PnL share of total |PnL|)
- stop-breach rate not worse than overall-T1A all-day baseline by > 15 percentage points

### 3.3 Per-cell KILL rule

A cell is **KILLED** if **ANY** hold:

- median ≤ 0 AND remove-best-1 mean < 0
- single-day-dominance ≥ 60% (per `/r65_closure`, `/r47_revalidation` precedents)
- cohort flip: across {B6, C6, D6, E6} present in cell, PF max − PF min > 1.0 AND median sign disagreement (at least one contract median > 0 and at least one < 0)

### 3.4 INCONCLUSIVE

Anything else. Verdict markdown reports the failing condition and the additional N needed to reach the smallest PROCEED bar.

### 3.5 Hypothesis-level verdict combination

For each hypothesis the markdown reports three rows:

1. All-day baseline (no regime filter)
2. Regime-3 cell
3. Regime-5 cell

Combination rules:

| Pattern | Verdict | Downstream action |
| --- | --- | --- |
| regime-3 KILL + regime-5 PROCEED-conditional + baseline near-flat | **regime-conditional candidate** | escalate to Study B (ex-ante regime predictability) |
| both regime cells KILL | **structural KILL of hypothesis** | regime label cannot rescue; close hypothesis |
| both regime cells PROCEED | **regime-invariant** | Study B unnecessary; baseline rules |
| baseline PROCEED but regime split disagrees | **diagnostic note**, baseline rules |
| both regime cells INCONCLUSIVE | **needs more days**, report N |

---

## 4. Testing, Validation & Determinism

### 4.1 Unit tests

| Test | Path | Asserts |
| --- | --- | --- |
| `test_regime_join_left_join_preserves_audit_rows` | `tests/unit/research/t1_regime_partition/test_regime_join.py` | audit row count unchanged; INVALID rows tagged not dropped |
| `test_regime_join_tmf_vs_txf_root_dual_partition` | same | both TMF-root and TXF-root regime columns present; non-identical on synthetic divergent fixture |
| `test_regime_join_excludes_non_selected_days` | same | `selected=False` rows → `regime_id_for_scorecard = NaN`, surfaced in separate bucket |
| `test_regime_join_records_csv_sha256` | same | run_config carries CSV sha256 |
| `test_scorecard_metrics_minimal` | `tests/unit/research/t1_regime_partition/test_scorecard.py` | N, median, PF, pos-days, p10, max-loss, remove-best-1/2/3, single-day-dominance on hand-crafted fixture |
| `test_scorecard_kill_on_single_day_dominance` | same | KILL fires at dominance ≥ 60% with median > 0 |
| `test_scorecard_kill_on_cohort_flip` | same | sign disagreement across B6/C6/D6/E6 with PF spread > 1.0 → KILL |
| `test_scorecard_proceed_requires_all_gates` | same | flipping any single gate to fail → not PROCEED |
| `test_scorecard_inconclusive_with_n_needed` | same | INCONCLUSIVE message includes additional N |
| `test_cli_emits_markdown_and_json` | `tests/unit/research/t1_regime_partition/test_cli.py` | both artifacts produced; JSON schema-valid |
| `test_audit_row_schema_parity_across_hypotheses` | `tests/unit/research/t1_audits/test_row_schema.py` | T1-A/B/C rows share schema |
| `test_frozen_params_guard` | `tests/unit/research/t1_audits/test_frozen_params.py` | mutating any frozen param raises; lockfile values match spec |

### 4.2 Determinism rules

- All sampling (bootstrap, remove-best) uses fixed seed (`HFT_T1_REGIME_SEED=20260519`).
- Output JSON keys sorted; floats rounded to 4dp in markdown, full precision in JSON.
- Regime CSV sha256 recorded in `run_config`.
- Run-config dict recorded: commit SHA, regime CSV path + sha256, T1-A/B/C spec doc paths + sha256, latency profile id, seed.
- Re-running with the same inputs reproduces byte-identical JSON output.

### 4.3 Known boundary conditions

- B6 has 100% `trade_direction = 0` (see `/taifex_l2_bridge_2026_05_13`). T1-A entry does not depend on signed flow. T1-B/T1-C v0 specs must declare the same; the frozen-params guard test asserts no signed-flow column is read.
- Bootstrap CI is seed-stable; regenerating the regime CSV shifts CI legitimately — this is expected and documented.
- Cell with N < 20 cannot KILL or PROCEED — it is INCONCLUSIVE by definition.

---

## 5. Deliverables & Prohibition List

### 5.1 Deliverables (build order)

1. T1-B v0 frozen spec (`docs/alpha-research/t1b_vol_compression_breakout_spec_2026_05_19.md`)
2. T1-C v0 frozen spec (`docs/alpha-research/t1c_vwap_failed_reclaim_spec_2026_05_19.md`)
3. Shared T1 audit runner skeleton (`research/tools/t1_audits/`)
4. T1-A audit runner (build if absent; verify in plan phase)
5. T1-B audit runner
6. T1-C audit runner
7. Regime-join library (`research/tools/t1_regime_partition/regime_join.py`)
8. Scorecard library (`research/tools/t1_regime_partition/scorecard.py`)
9. CLI (`research/tools/t1_regime_partition/cli.py` → `regime-partition`)
10. Unit tests for all above
11. Final verdict markdown (`docs/alpha-research/t1_regime_partition_2026_05_19.md`) — generated artifact

### 5.2 Prohibition list (binding for this spec)

- **P1.** No parameter change to T1-A v0 (frozen 2026-05-13).
- **P2.** T1-B and T1-C v0 must be fully frozen in their spec docs before first audit run. No "try a few" iterations.
- **P3.** No use of the regime label as an ex-ante entry filter in any audit runner under this spec.
- **P4.** No combining regime-3 + regime-5 cells into a single "high-vol" superset after seeing results.
- **P5.** No promotion claim from this run. Best possible verdict = "regime-conditional candidate, Study B justified".
- **P6.** No new T1-D/E/F created from cell observations.
- **P7.** No L2 features in any of the three audit runners. L2 permitted only as quote/spread guard per Track T1 charter.
- **P8.** INVALID (regime_id = -1) days reported but excluded from scorecard cells.
- **P9.** `selected=False` (mixed-regime) days reported separately, not folded into either cell.
- **P10.** All metrics produced in a single CLI invocation; no manual cherry-picking from intermediate JSON.

### 5.3 Out-of-scope follow-ups (named, not built)

- Study B: ex-ante regime predictability from first-30m features
- T1-D night session
- Position sizing by regime
- Combining with `cd600` attribution signatures (`persistent_brake_exit`, `spread_widening`) — those candidates are closed per `/cd600_closure`

---

## 6. Risks & Open Questions

### 6.1 Risks

| Risk | Mitigation |
| --- | --- |
| Sample collapse: 49d / 50d cell becomes < 20 events after cross-contract filter | Pre-registered: cell INCONCLUSIVE, not KILL; report N needed |
| TMF-root vs TXF-root regime divergence ambiguates partition | Both reported; primary = TMF-root (execution leg); ablation = TXF-root |
| 1s-snapshot carry-book artifact on daily regime label | Mitigated by user's active-only methodology; this spec cites the active-only CSV explicitly |
| T1-A runner not yet implemented | Plan phase scopes T1-A runner build; if absent and spec frozen, that's a build dep, not a re-spec |
| Regime CSV regeneration changes verdict | sha256 recorded in `run_config`; re-run on regeneration is acceptable, documented |
| Trade-direction missing on B6 | T1-A/B/C entry must not depend on signed flow; frozen-params guard test enforces |

### 6.2 Open questions (resolved at plan time)

- TMF-root primary vs TXF-root primary: **TMF-root** (execution leg = PnL realization).
- Days with TMF=regime-5 / TXF=regime-3: both partitions reported; primary table uses TMF-root; user-visible note flags divergent days.
- Mid-cycle regime CSV regeneration: not in-spec; sha256 frozen at run start.

---

## 7. Cross-References

- Track T1 charter: `~/.claude/projects/-home-charlie-hft-platform/memory/track_t1_opened_2026_05_13.md`
- TXF-led research discipline: `~/.claude/projects/-home-charlie-hft-platform/memory/txf_led_research_discipline_2026_05_13.md`
- T1-A v0 spec: `docs/alpha-research/t1a_opening_range_expansion_spec_2026_05_13.md`
- TAIFEX L2 bridge / trade-direction caveats: `~/.claude/projects/-home-charlie-hft-platform/memory/taifex_l2_bridge_2026_05_13.md`
- Shioaji broker asymmetric latency profile: `~/.claude/projects/-home-charlie-hft-platform/memory/shioaji_broker_asymmetric_latency_2026_04_24.md`
- R65 closure (single-day-dominance + cohort-flip pathology): `~/.claude/projects/-home-charlie-hft-platform/memory/r65_closure_2026_05_11.md`
- cd600 closure: `~/.claude/projects/-home-charlie-hft-platform/memory/cd600_closure_2026_05_13.md`
- Regime input CSV: `research/data/derived/regime_panels_front_1s/front_month_single_regime_intraday_active_h30s.csv`
