# Architecture Export (from .agent)

This directory contains commit-trackable exports mirrored from `.agent/` outputs.

Source mapping:
- `.agent/library/current-architecture.md` -> `docs/architecture/current-architecture.md`
- `.agent/library/target-architecture.md` -> `docs/architecture/target-architecture.md`
- `.agent/library/c4-model-current.md` -> `docs/architecture/c4-model-current.md`
- `.agent/library/cluster-evolution-backlog.md` -> `docs/architecture/cluster-evolution-backlog.md`
- `.agent/library/design-review-artifacts.md` -> `docs/architecture/design-review-artifacts.md`
- `.agent/rules/25-architecture-governance.md` -> `docs/architecture/architecture-governance-rules.md`

Update policy:
- Treat `docs/architecture/` as the commit artifact path.
- When `.agent/` versions change, re-sync these files before commit.
