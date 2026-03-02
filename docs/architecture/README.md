# Architecture <-> Agent Sync

This directory is the commit-trackable architecture artifact path and is
kept aligned with `.agent` architecture/rules documents.

## Canonical Pair Mapping

- `.agent/library/current-architecture.md` <-> `docs/architecture/current-architecture.md`
- `.agent/library/target-architecture.md` <-> `docs/architecture/target-architecture.md`
- `.agent/library/c4-model-current.md` <-> `docs/architecture/c4-model-current.md`
- `.agent/library/cluster-evolution-backlog.md` <-> `docs/architecture/cluster-evolution-backlog.md`
- `.agent/library/design-review-artifacts.md` <-> `docs/architecture/design-review-artifacts.md`
- `.agent/rules/25-architecture-governance.md` <-> `docs/architecture/architecture-governance-rules.md`

## Extended Library Mirror

Additional `.agent/library/*.md` files are mirrored 1:1 into this directory
by basename to reduce drift between project docs and agent knowledge base.

## Update Policy

1. Any change to the mapped files must be synced on both sides in the same PR.
2. If only one side exists, create the missing counterpart.
3. Keep file names stable to preserve deterministic sync.
