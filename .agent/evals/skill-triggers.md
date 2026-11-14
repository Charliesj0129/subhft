# Skill trigger cases

Regression cases for skill descriptions: the description is the only thing a
model sees when choosing a skill, so an edit to one can silently break
discovery. Each line is a task statement, the skill that should load, and (in
the last block) statements that must NOT load a neighbouring skill. Statements
are generic process examples; keep proprietary strategy content out of this file.

## When and how to run

- **When**: after adding, renaming, or rewording any skill description, or
  after consolidating skills.
- **How**: in a clean session of the tool under test (`claude -p` with stream-json
  or `codex exec`), give each statement and read which skill the tool loads.
  Spot-check 15 of the cases; any miss on a core row is a description bug.
- **Target**: at least 13/15 hits, and no counter-example loading the wrong skill.
- **Record**: one line in `.agent/memory/model-routing.md` (`skill-triggers: 14/15`
  plus the missed IDs). Fix the description, or update the case in the same commit
  with a CHANGELOG line saying why. Never reword a case to make it pass.

## Cases (statement -> expected skill)

| ID | Statement | Expected skill |
|---|---|---|
| T1 | "Split this 12-file mechanical rename across subagents." | `delegation` |
| T2 | "Review this diff for hot-path problems before I commit." | `strict-code-review` |
| T3 | "What's the current state of the repo before we plan the change?" | `preflight` |
| T4 | "Commit these two test files without touching my other edits." | `git-safety` |
| T5 | "Set up a worktree so I can keep working while this runs." | `git-safety` |
| T6 | "Save what we learned today to memory and wrap up." | `memory-update` |
| T7 | "Audit our prompts and skills for drift and duplication." | `agent-meta-audit` |
| T8 | "Which behaviors of the gateway service lack real assertions?" | `test-gap-analysis` |
| T9 | "After the restart subscribed_count dropped to zero. Find why; don't fix." | `bug-investigation` |
| T10 | "Refresh the codemaps; they no longer match src." | `doc-updater` |
| T11 | "Remove the per-tick allocation in the normalizer." | `hft-hot-path-dev` |
| T12 | "Write tests for the new risk gate including HALT with cancels." | `hft-test-hft` |
| T13 | "The LOB engine drops updates after a reconnect." | `hft-market-data` |
| T14 | "Fills come back with strategy_id UNKNOWN." | `hft-execution` |
| T15 | "WAL files keep growing and ClickHouse looks idle." | `hft-recorder` |
| T16 | "Write a migration adding a column to the fills table." | `clickhouse-io` |
| T17 | "Add a drill for the autonomy monitor's REDUCE_ONLY path." | `hft-ops` |
| T18 | "Metrics look flat, is the engine actually healthy?" | `runtime-diagnostics` |
| T19 | "Is this change ready to merge and deploy?" | `hft-release-gate` |
| T20 | "Add a second broker adapter behind the protocol." | `broker-integration` |
| T21 | "The contract rolled; regenerate the symbol universe." | `symbols-sync` |
| T22 | "Why isn't my HFT_* flag taking effect?" | `config-env` |
| T23 | "Implement on_gap and on_risk_feedback for the new strategy." | `hft-strategy` |
| T24 | "Design the quoting and inventory logic for a maker." | `hft-mm-design` |
| T25 | "This backtest shows huge PnL. Is the method trustworthy?" | `hft-backtest` |
| T26 | "Scaffold a new alpha from a paper and run Gate A-C." | `hft-alpha-research` |
| T27 | "Gate D blocked on feature-set version; why?" | `validation-gate` |
| T28 | "Is this signal's edge bigger than round-trip costs?" | `taifex-alpha-kill-criteria` |
| T29 | "What's the point value of the micro contract?" | `taifex-market-structure` |
| T30 | "Port this numpy kernel to Rust with a parity test." | `hft-rust` |
| T31 | "p99 normalize latency regressed; profile it." | `hft-latency-profiling` |
| T32 | "Where should a new hot-path stage sit, and what contract does it use?" | `hft-architect` |

## Counter-examples (must NOT load the named skill)

| ID | Statement | Must not load | Should load |
|---|---|---|---|
| N1 | "Fix a typo in a README." | `delegation` | none (direct) |
| N2 | "Push this branch to origin." | `git-safety` handling it alone | ask the user first (red line) |
| N3 | "Explain how Python asyncio queues work." | any `hft-*` skill | none |
| N4 | "Deploy the fix to the production host." | `hft-ops` | `.agent/rules/41-deployment.md` with the user's approval |
| N5 | "Enable the new alpha for live trading." | `hft-strategy` as authority | refuse under the freeze; ask the user |
| N6 | "Query the market_data table for yesterday's ticks." | `hft-recorder` | `clickhouse-io` |
