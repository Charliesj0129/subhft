# Candidate Research Refinement Loop

## Objective

Build a multi-iteration candidate research refinement loop.

Each iteration reads the latest research artifacts, selects the highest-value research route, processes one candidate or one candidate group, updates the relevant artifact, and emits the next `recommended_research_route`.

The loop advances the candidate pool toward one of these outcomes:

- `ready_for_paper`
- `evidence_backfill_candidate`
- `sample_expansion_candidate`
- `hypothesis_review_candidate`
- `parity_repair_candidate`
- `artifact_repair_candidate`
- `archive_candidate`

## Implemented Surface

The route-correctness slice is exposed through:

- `python -m research.factory refinement-iteration`
- `research/reports/readiness_refinement_iteration.json`
- `research/reports/readiness_candidate_archive_decision.json`
- `python -m research.factory readiness-backfill-queue`

`refinement-iteration` currently executes `archive_candidate_set` as a non-destructive
recommendation and projects the next route from candidates outside the archived group.
The evidence queue is advancement-aware and only queues candidates classified as
`evidence_backfill_candidate`; all other routes are explicitly skipped.

The every-third-iteration `literature_refresh` scheduler remains deferred to a later
slice. Other routes fail closed with `route_not_implemented_in_this_slice` until their
route artifacts are implemented.

## Inputs

Read the latest available artifacts:

- `readiness_candidate_advancement.json`
- `readiness_candidate_archive_decision.json`
- `readiness-backfill-queue` artifact
- validation summary
- metrics artifacts
- OOS evidence artifacts
- replay / paper / live parity artifacts
- experiment logs
- literature refresh artifacts, when available

## Iteration Flow

Each iteration must:

1. Determine the current candidate pool state.
2. Select exactly one primary route:
   - `prepare_paper_candidate`
   - `backfill_evidence`
   - `expand_sample`
   - `review_hypothesis`
   - `repair_parity`
   - `repair_artifact_integrity`
   - `archive_candidate_set`
   - `literature_refresh`
3. Process one candidate or one candidate group.
4. Produce or update the corresponding artifact:
   - paper preparation artifact
   - evidence backfill artifact
   - sample expansion artifact
   - hypothesis review artifact
   - parity repair artifact
   - artifact integrity repair artifact
   - archive decision artifact
   - literature refresh artifact
5. Update candidate status and next action.
6. Emit the next `recommended_research_route`.

## Literature Refresh Rule

Every 3 completed iterations, run `literature_refresh`.

Use `arxiv_mcp` first. When unavailable, use high-quality web search.

Search topics should focus on:

- futures intraday alpha
- Taiwan futures / index futures microstructure
- order book imbalance
- volatility regime
- option strategies
- volatility risk premium
- intraday momentum / reversal
- execution cost
- slippage
- queue position
- replay / paper / live parity

Each paper entry must include:

- title
- authors
- year
- source
- URL or identifier
- research claim
- market mechanism
- tradable hypothesis
- required data
- expected signal
- risk or failure mode
- how to test in this repo

Literature refresh can produce:

- `literature_seed_candidate`
- `paper_basis` update for an existing candidate
- `ready_for_paper` support when validation evidence is already strong

## Ready for Paper Requirements

A candidate can be marked `ready_for_paper` only when it has:

- linked papers
- hypothesis from paper or explicit research rationale
- implementation mapping
- validation evidence
- OOS evidence
- replay / paper / live parity evidence
- risk notes
- next paper/live action

## Route Rules

Use these rules to select the next route:

1. Candidate satisfies edge, sample, drawdown, OOS, parity, and promotion readiness → `prepare_paper_candidate`.
2. Candidate has acceptable edge/sample/drawdown and mainly lacks evidence artifacts → `backfill_evidence`.
3. Candidate has plausible edge but insufficient trade count, OOS days, or distribution evidence → `expand_sample`.
4. Candidate has weak net edge, unstable edge after cost, or PnL dominated by few trades → `review_hypothesis`.
5. Candidate has signal timing, direction, size, entry/exit, session, risk, or position drift → `repair_parity`.
6. Candidate has missing metrics, schema ambiguity, unreadable artifacts, or inconsistent validation summary → `repair_artifact_integrity`.
7. Candidate set has multiple failed core conditions and low research value → `archive_candidate_set`.
8. Every third iteration → `literature_refresh`.

## Quality Standards

Maintain these standards:

- net edge > 10 pts/trade
- full cost deduction:
  - fees
  - tax
  - slippage
  - bid-ask spread
  - latency adverse selection
  - force-flat cost
  - residual MtM
- OOS validation
- drawdown control
- replay / paper / live parity
- promotion readiness

All conclusions must come from artifacts, metrics, logs, tests, papers, or explicitly marked gaps.

## Scope

Allowed work:

- research artifact projections
- decision helpers
- evidence artifacts
- metrics projections
- unit tests
- audit helpers
- documentation
- literature refresh artifacts

Preserve behavior of:

- production trading
- risk engine
- broker adapter
- position sizing
- session / force-flat
- cost model
- production config

## Verification

Each iteration should run the smallest useful verification set:

- focused tests for the modified decision rule
- relevant unit tests
- artifact command
- summary count aggregation check
- route uniqueness check
- ruff for touched files
- research audit or the closest available audit command

## Gate Hardening (2026-06-14 review remediation)

A Codex review of the working tree found that several gates were advisory or
fail-open. These invariants are now enforced (`research/factory.py`,
`research/t1/regime_viability.py`) and regression-tested
(`tests/unit/test_research_factory.py`,
`tests/unit/research/test_t1f_expiration_v_reversal.py`):

- **Drawdown gate is fail-closed.** A candidate is paper/live eligible only
  when `drawdown_within_2x_average_monthly_net_pnl` is *explicitly* `True`; a
  missing gate (None / absent) blocks exactly like a failure — absence is "not
  proven", never implicit agreement.
- **Canonical metric gates eligibility.** When a track declares a
  risk-controlled (stop-exit) edge as its canonical metric (T1-F), that metric
  must clear the > 10 pt floor before the candidate can `PROCEED` or become
  research-eligible. A candidate cannot promote on the legacy time-exit edge
  alone while failing its declared canonical metric.
- **Scaffold paths are confined to `research/alphas`.** A candidate id is
  validated as a plain identifier (no path separators / `..`) and resolved
  scaffold paths are verified to stay under the alpha root before any write.
- **Intake readiness runs canonical validation.** `validate_spec()` runs on the
  candidate spec (with family `legs` / `greeks_exposure` merged in) so a
  nonempty-but-invalid value (unsupported market / timeframe, malformed risk /
  cost block) blocks the intake instead of failing later in the audit; family
  blocks survive into the scaffolded `spec.yaml`.
- **Raw data exports are never committed.** Local ClickHouse BBO/tick backups
  (`backups/`) are gitignored — re-derivable and proprietary.

## Required Report Format

Each iteration must report:

- iteration_index:
- selected route:
- candidate or candidate group:
- literature_refresh triggered:
- artifact produced:
- candidate status changes:
- ready_for_paper updates:
- summary:
- recommended_research_route:
- validation results:
- unresolved gaps:
- next action:
