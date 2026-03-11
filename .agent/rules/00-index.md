# Agent Rules Index

Rules are auto-loaded by agents from `.agent/rules/`.

| File                            | Scope                                               | Lines |
| ------------------------------- | --------------------------------------------------- | ----- |
| `01-core-laws.md`               | HFT Laws: memory, precision, async, Rust boundary   | ~30   |
| `05-project-structure.md`       | Directory layout, lifecycle hooks, session protocol | ~45   |
| `10-hft-performance.md`         | Latency checklist, anti-patterns, CPU tuning        | ~50   |
| `15-security.md`                | Credentials, logging, network, Docker security      | ~25   |
| `20-data-flow.md`               | Hot path pipeline, recording path, verification     | ~36   |
| `25-architecture-governance.md` | CE-M2/M3 design, ADR process, module boundaries     | ~100  |
| `26-multi-broker-governance.md` | Multi-broker protocol, isolation, credential rules   | ~60   |
| `30-git-workflow.md`            | Commit messages, branch strategy, pre-commit        | ~45   |
| `40-ops.md`                     | Docker Compose, service health, common ops          | ~55   |
| `50-testing.md`                 | Coverage goals, test pyramid, what to test          | ~25   |

## Related Context

- **Memory**: `.agent/memory/` — module gotchas, lessons learned
- **Library**: `docs/architecture/` — architecture docs (canonical source of truth)
- **Skills**: `.agent/skills/` — skill SKILL.md files
- **Workflows**: `.agent/workflows/` — step-by-step procedures
- **Evals**: `.agent/evals/` — evaluation scripts for normalizer, LOB, risk
