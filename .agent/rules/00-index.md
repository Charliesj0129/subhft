# Agent Rules Index

Read only the file needed for the task. These rules are compact guardrails; canonical details live in source, `docs/architecture/`, runbooks, and task skills.

| File | Scope |
| --- | --- |
| `01-core-laws.md` | HFT allocator/cache/async/precision/boundary laws |
| `05-project-structure.md` | Layout and session hooks |
| `10-hft-performance.md` | Hot-path performance checklist |
| `15-security.md` | Secrets, logs, network, Docker |
| `20-data-flow.md` | Runtime/recording flow invariants |
| `25-architecture-governance.md` | Boundaries, queues, durability, alpha, exposure |
| `26-multi-broker-governance.md` | Broker protocol, isolation, latency, credentials |
| `30-git.md` | Commit and git hygiene |
| `40-ops.md` | Docker, services, live config changes |
| `50-testing.md` | Coverage, naming, HFT test focus |
| `55-enforcement.md` | Pre-commit, discipline, CI gates |
| `60-agent-workflow-governance.md` | Git-state safety and multi-agent coordination |
| `70-research-data.md` | ClickHouse research data and export contract |

Related: `.agent/memory/`, `.agent/skills/00-index.md`, `docs/MODULES_REFERENCE.md`, `docs/architecture/`, `docs/runbooks/alpha-development-workflow.md`.
