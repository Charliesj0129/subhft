# Agent Rules Index

Rules are auto-loaded by agents from `.agent/rules/`.

| File                              | Scope                                                 | Lines |
| --------------------------------- | ----------------------------------------------------- | ----- |
| `01-core-laws.md`                 | HFT Laws: memory, precision, async, Rust boundary     | ~39   |
| `05-project-structure.md`         | Directory layout, lifecycle hooks, session protocol   | ~36   |
| `10-hft-performance.md`           | Latency checklist, anti-patterns, CPU tuning         | ~38   |
| `15-security.md`                  | Credentials, logging, network, Docker security        | ~26   |
| `20-data-flow.md`                 | Hot path pipeline, recording path, verification       | ~38   |
| `25-architecture-governance.md`   | CE-M2/M3 design, ADR process, module boundaries       | ~88   |
| `26-multi-broker-governance.md`   | Multi-broker protocol, isolation, credential rules    | ~49   |
| `30-git.md`                       | Commits, branches, pre-commit, hygiene                | ~63   |
| `40-ops.md`                       | Docker Compose, service health, common ops            | ~46   |
| `50-testing.md`                   | Coverage goals, test pyramid, what to test            | ~32   |
| `55-enforcement.md`               | Pre-commit hooks, discipline rules, CI gates          | ~43   |
| `60-agent-workflow-governance.md` | Agent mutual exclusion, blast radius, conflict protocol | ~99 |
| `70-research-data.md`             | ClickHouse research data access, export CLI, formats  | ~99   |

## Related Context

- **Memory**: `.agent/memory/` — module gotchas, lessons learned
- **Library**: `docs/architecture/` — architecture docs (canonical source of truth)
- **Skills**: `.agent/skills/` — skill SKILL.md files
- **Workflows**: `.agent/workflows/` — step-by-step procedures
- **Evals**: `.agent/evals/` — evaluation scripts for normalizer, LOB, risk
- **Alpha development workflow** (canonical, end-to-end for new factor authors): `docs/runbooks/alpha-development-workflow.md` — covers Gates A–F, replay-parity, latency-audit, kill ledger, and the loop_v1 L11 live-registry freeze.
