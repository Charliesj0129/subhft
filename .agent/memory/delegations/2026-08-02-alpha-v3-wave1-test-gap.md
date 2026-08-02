# Alpha Mining v3 Wave 1 — test-gap audit and re-audit

## Packet

Test-gap analysis handoff — read-only, no edits/git/network/final-holdout. Goal: audit current Wave 1 adaptive-search tests in /tmp/hft-alpha-mining-v3-wave1 for behavioral gaps. Tier-3 research governance. Map each new production branch/invariant to a test; specifically stress resume after partial G1, worker determinism, lineage row mismatch/tamper, candidate-budget exact/unused, no later-split influence, insufficient parents, semantic duplicate rejection, union effective-trial evidence restoration, default blind equivalence, adaptive final-holdout prohibition, and CLI campaign not exposing adaptive mode. Run tests read-only if needed. Cite file:line and give minimal behavior-named tests or break probes; distinguish blockers vs future scale gates. Verdict REQUEST-CHANGES if a safety invariant lacks executable coverage. No implementation. <=120 lines.

## Executor final report

## Re-audit verdict

**APPROVE — no remaining blocker in the requested Wave 1 invariants.**

| Required invariant | Executable evidence |
|---|---|
| Partial-G1 resume | `test_adaptive_resume_after_partial_generation_one_is_bit_identical` compares normalized ledger rows and `search_space.json` against uninterrupted execution ([test_smma_runner.py:1442](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:1442)) |
| 1/2-worker adaptive equivalence | `test_adaptive_search_is_deterministic_across_worker_counts` compares normalized generation/discovery rows and complete search-space evidence ([test_smma_runner.py:1421](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:1421)) |
| No later-split influence | Later selection/locked/final values are mutated while G1 candidates and generation evidence must remain identical ([test_smma_runner.py:1153](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:1153)) |
| Exact budget, unused tail, union restoration | End-to-end test asserts 72 candidates from a 73 budget, one unused slot, effective-trial conservation, restored counts/evidence, and no split access ([test_smma_runner.py:1485](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:1485)) |
| Per-row lineage | Wrong proposal links fail closed; changed proposal evidence also fails closed ([test_smma_runner.py:963](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:963), [test_smma_runner.py:948](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:948)) |
| Search-space/ledger integrity | Independent lineage-hash and candidate-union-hash mismatch tests now cover both branches ([test_smma_runner.py:1924](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:1924), [test_smma_runner.py:1958](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:1958)) |
| `blind_v1` default equivalence | Default mode retains semantic duplicates and legacy DSR dispersion behavior ([test_smma_runner.py:671](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:671), [test_smma_runner.py:936](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:936)) |
| Terminal stops | Incomplete conservation is explicit; resume is idempotent both with passing rows and zero discovery passes ([test_smma_runner.py:2201](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:2201), [test_smma_runner.py:2237](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:2237), [test_smma_runner.py:2275](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:2275)) |
| No final-holdout access | Adaptive+unlock config fails closed, and completed adaptive discovery asserts no `split_access.jsonl` ([test_smma_runner.py:1033](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:1033), [test_smma_runner.py:1499](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_smma_runner.py:1499)) |
| Campaign remains non-adaptive | All six campaign configurations assert feedback quota zero ([test_family_registry.py:467](/tmp/hft-alpha-mining-v3-wave1/tests/unit/test_family_registry.py:467)) |

Focused verification:

```text
114 passed in 10.23s
```

No final holdout was accessed. I made no tracked-file changes. Full repository CI was not run in this re-audit.

## Review verdict

APPROVE — the initial REQUEST-CHANGES gaps were all closed with executable regressions and production fail-closed checks.
