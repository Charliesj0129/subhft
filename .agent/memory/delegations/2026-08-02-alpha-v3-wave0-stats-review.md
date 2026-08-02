# Alpha Mining v3 Wave 0 — statistics/provenance review

## Packet

Please perform formal final Tier-3 adversarial review on current Wave0 diff. Read AGENTS/CLAUDE/relevant rules and strict-code-review Step0; inspect actual git diff vs 28f37adb, not prior summaries. No edits/git state changes. Verify every prior finding resolved, look for new high/medium correctness/stat/provenance/resume/API/campaign-control issues. Run focused read-only tests. Output findings ranked, evidence, verdict; note sandbox-only failures.

## Executor final report

## Findings

No remaining confirmed HIGH or MEDIUM findings.

LOW:

1. [.agent/memory/current_session.md:8](</tmp/hft-alpha-mining-v3-wave0/.agent/memory/current_session.md:8>) is stale: it records 157 tests and lists the campaign-control test/review as future work, although both are complete. A resumed operator could repeat work or misread readiness. Update before landing.

2. [src/hft_platform/cli/_alpha.py:369](</tmp/hft-alpha-mining-v3-wave0/src/hft_platform/cli/_alpha.py:369>) grew from 1,549 to 1,612 lines and still triggers the non-blocking HFT-S001 module-size warning. This is maintainability debt, not a runtime blocker; future campaign-control logic should be extracted.

3. [test_combinatorial_expression_eval.py](</tmp/hft-alpha-mining-v3-wave0/tests/unit/test_combinatorial_expression_eval.py>) remains untracked. It must be included in the narrow landing or the AST window-detection regression coverage will be omitted.

## Resolved during review

- Resume artifact tuple/list mismatch fixed by JSON-normalizing before hashing/comparison at [_alpha.py:375](</tmp/hft-alpha-mining-v3-wave0/src/hft_platform/cli/_alpha.py:375>).
- Permutation with too few exchangeable blocks now fails closed at [smma_validation.py:753](</tmp/hft-alpha-mining-v3-wave0/research/combinatorial/smma_validation.py:753>), with a frozen minimum of ten.
- Exact feature-history claims without calendar/vector/positive history now raise at [smma_validation.py:993](</tmp/hft-alpha-mining-v3-wave0/research/combinatorial/smma_validation.py:993>).

## Evidence

- Actual diff inspected against `28f37adb`; `git diff --check` clean.
- Focused suite: `165 passed, 1 deselected`; isolated multiprocessing test: `1 passed`.
- Final three regressions: `3 passed in 1.70s`.
- Ruff: `All checks passed!`.
- Test hygiene: exit 0; eight unrelated advisory-only findings, zero critical.
- Discipline: exit 0 with repository-wide non-blocking warnings.
- Production controls: 20/20 positive passes, 0/100 null survivors.
- Inside-sandbox multiprocessing failed only because forkserver socket creation was denied; the same test passed outside the sandbox.
- No final-holdout access, frozen-floor relaxation, or alpha-evidence interpretation was introduced.

Not independently completed: clean changed-file mypy due pre-existing imported-module errors and duplicate-module configuration; break-probe because reviewer authority prohibited edits; live ClickHouse replay/campaign launch. Parent separately reports `make check` and the complete 166-test scope green.

## Verdict

**APPROVE-WITH-NITS** — Wave 0 has no remaining correctness or governance blocker. Update the stale session record and include the untracked regression test before landing.

## Review verdict

APPROVE-WITH-NITS — no remaining correctness or governance blocker; orchestrator updated the stale session record and will include the untracked regression test in the narrow landing.
