# Alpha Mining v3 Wave 0 — immutable locked-evidence replay

## Packet

ROLE: bounded read-only investigation executor. Task type: investigation; Tier-3 evidence surface but ZERO project edits. ROI reason: large independent artifact replay can run in parallel and isolate context.

GOAL
Replay the existing v2.1 campaign's locked-validation candidate rows through the CURRENT Wave-0 validation code, using saved governed datasets only. Produce cited counts per leg and aggregate. This is diagnostic evidence, not alpha evidence and not permission to access final_holdout.

BRANCH / VENUE
Worktree: /tmp/hft-alpha-mining-v3-wave0
Branch expected: alpha/mining-v3-validation-wave0-20260802, dirty only with orchestrator's Wave-0 changes.
Immutable source campaign: /home/charlie/hft_platform/research/experiments/runs/20260731T115449_alpha_mining_v21_full_processpool
You may create scratch script/output ONLY under /tmp/alpha-v3-wave0-replay-*; do not edit any repository file or artifact.

FILES ALLOWED TO READ
research/combinatorial/{smma_runner.py,smma_validation.py,smma_dataset.py,tick_dataset.py,bidask.py,kbar.py,tick.py,expression_eval.py,gp_alpha_adapter.py}; saved campaign run_manifest/search_space/trials/dataset files.

OFF LIMITS
All repo writes; research/experiments/runs/** writes; .agent memory; configs; cost profiles; registry; .env/secrets; network/live ClickHouse; git mutations; final_holdout statistics or unlocks.

METHOD CONSTRAINTS
1. Select only trials.jsonl rows with stage == locked_validation, preserving row order.
2. Load each leg's saved dataset with its governed loader. Build family features/signals using current code and the candidate's frozen threshold/direction/horizon.
3. Use build_split_plan and pass ONLY locked_validation signal/target slices to permutation. Execution may use the full index grid with signal NaN outside locked, matching MiningRun._locked; do not summarize/use final_holdout values.
4. Use feature_history_bars_for_expression and _exact_horizon_inputs; per-contract cost mode matching manifest.
5. Recompute actual_trials/effective_trials/trial_sharpe_std exactly as current runner can from ledger/search_space. Use a bounded, explicitly reported resample_samples=199. Apply per-leg BH q=.10 exactly as runner.
6. Report per leg: locked row count, raw locked passes, BH survivors, permutation reason histogram, min/median informative clusters, feature-history exact count, failure-reason histogram. Aggregate counts too. Compare to old stored passed counts without treating either as alpha conclusion.
7. Verify no final_holdout access log/artifact was changed. Record before/after SHA256 for every split_access.jsonl (or at minimum all six) and state equality. Do not write into campaign.
8. If any step cannot be reproduced exactly, stop that portion and state the approximation; never fabricate.

VERIFICATION
- scratch script must run from /tmp worktree under `uv run python ...`.
- campaign split_access SHA256 before == after for all six legs.
- no repository file edits by you.
- no live/network calls.

STOP CONDITIONS
Need for credentials/live data; loader integrity failure; any temptation to read/summarize final_holdout; runtime over 20 minutes; required production edit; unexpected source branch.

REPORT CONTRACT (final message, <=120 lines)
## Changed files (must say none; list scratch paths separately)
## Commands run (verbatim + key excerpts)
## Not verified
## Blockers or deviations from packet
Then concise replay table/evidence with exact counts and caveats. Do not make implementation recommendations unless directly evidenced.

## Executor final report

## Changed files

None in the repository.

Scratch file only:

- `/tmp/alpha-v3-wave0-replay-20260802.py`

## Commands run

- `git branch --show-current && git status --short && find ...`
  - Confirmed branch `alpha/mining-v3-validation-wave0-20260802` and expected Wave-0 dirty paths.
- `rg -n ...` / `sed -n ...` / `nl -ba ...`
  - Matched replay logic to current runner: trial counts and Sharpe dispersion at [smma_runner.py](/tmp/hft-alpha-mining-v3-wave0/research/combinatorial/smma_runner.py:2065), locked-only execution and validation at [smma_runner.py](/tmp/hft-alpha-mining-v3-wave0/research/combinatorial/smma_runner.py:2132), BH at [smma_runner.py](/tmp/hft-alpha-mining-v3-wave0/research/combinatorial/smma_runner.py:2188).
- `jq ... run_manifest.json/search_space.json/checkpoint.json/trials.jsonl`
  - Confirmed 65 locked rows and frozen per-contract cost mode.
- `python3 -m py_compile /tmp/alpha-v3-wave0-replay-20260802.py`
  - Passed.
- `UV_CACHE_DIR=/tmp/.uv-cache uv run python /tmp/alpha-v3-wave0-replay-20260802.py`
  - Failed immediately: `ModuleNotFoundError: research`; no replay occurred.
- `PYTHONPATH=. UV_CACHE_DIR=/tmp/.uv-cache uv run python /tmp/alpha-v3-wave0-replay-20260802.py`
  - Passed in 18.0s; replayed all 65 locked rows.
- `sha256sum .../split_access.jsonl`
  - All six hashes matched the script’s before/after values.
- `rg -n 'final_holdout' .../split_access.jsonl || true`
  - No matches.
- `git status --short`
  - Same repository path set as intake; this executor made no repository edits.

## Not verified

- Production-strength 2,000-resample results; this bounded replay intentionally used 199.
- Final holdout statistics or outcomes; they were not accessed.
- Live ClickHouse data or network services.
- Alpha validity: this is conditional harness evidence only.

## Blockers or deviations from packet

No blockers. The first invocation needed `PYTHONPATH=.` because the scratch script resides outside the repository; the corrected invocation completed normally.

## Replay evidence

| Leg | Rows | Old raw/BH | New raw/BH | Permutation reasons | Clusters min/median | Exact history |
|---|---:|---:|---:|---|---:|---:|
| bidask-2m | 4 | 0/0 | 0/0 | excluded blocks: 4 | 14/14 | 4 |
| bidask-h1h2h4 | 19 | 0/0 | 0/0 | ok: 19 | 18/18 | 19 |
| kbar-2m | 14 | 0/0 | 0/0 | excluded blocks: 14 | 14/14 | 14 |
| kbar-h1h2h4 | 20 | 0/0 | 0/0 | ok: 20 | 18/18 | 20 |
| tick-2m | 1 | 0/0 | 0/0 | insufficient clusters: 1 | 9/9 | 1 |
| tick-h1h2h4 | 7 | 0/0 | 0/0 | ok: 7 | 12/12 | 7 |
| Aggregate | 65 | 0/0 | 0/0 | ok 46; exclusions 18; insufficient 1 | min 9; median-of-leg-medians 14 | 65 |

Aggregate failure-reason histogram; candidates may fail multiple gates:

- `cluster_bootstrap`: 62
- `deflated_sharpe`: 64
- `permutation`: 61
- `insufficient_trade_activity`: 34
- `walk_forward`: 23

Every candidate received exact finite feature-history evidence under the new implementation. The only permutation activity precondition failure was `tick-2m`, with 9 informative clusters versus the frozen minimum of 10.

All six `split_access.jsonl` hashes were unchanged:

- bidask-2m: `24ac23316fdf3d795d311a9ff98031b7a2a225a9c74413de781938e80d70fd24`
- bidask-h1h2h4: `70b55a8daf47c9364139b3534bf3d2a680aa85fca1877bd10f177b2b2667a188`
- kbar-2m: `e04b6c1780186bc5edd4e01f321ee53dc846575c48f0bcc543833399d6d57248`
- kbar-h1h2h4: `a3787583dc4e7661e37d7a37f1fc4bdb10ded099bd78c6cf48edd88786e75ba8`
- tick-2m: `d8dbdea0ebb7ecb9eb1b0e8e749776d20212f585ed8131b5650ec34ec99a1770`
- tick-h1h2h4: `857db4d40bae6a243657264c7f50bfee209fb60ebf654c0d39b83a19a1c9af4c`

The unchanged zero-survivor result is not an alpha conclusion: Wave-0 changed the evidence definition, and the replay used only 199 resamples.

## Review verdict

SUCCESS — orchestrator inspected the scratch implementation, independently reran all 65 rows in 17.5 seconds, reproduced every aggregate count, and verified all six split-access hashes remained unchanged.
