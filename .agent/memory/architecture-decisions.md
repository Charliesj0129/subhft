# Architecture Decisions (why-records)

Record here: decision, date, alternatives rejected and why, revisit-trigger.
Do NOT record: decisions readable from code; implementation detail.

## Gateway metrics use deferred imports (date unknown, pre-2026-07)
Decision: `gateway/service.py` imports `MetricsRegistry` inside functions.
Why: breaks a circular import with observability. Rejected: top-level import
(cycle). Revisit if observability is split into its own package.
Never "clean up" these imports to module level.

## Typed intent fast path emits tuples, not objects (pre-2026-07)
Decision: with `HFT_TYPED_INTENT_CHANNEL=1` (default), strategy intents are
`("typed_intent_v1", ...)` tuples — zero allocation on the hot path. Gateway
deserializes lazily only after dedup+policy+exposure pass.
Why: Allocator Law. Revisit only with a measured allocation-free alternative.

## Pool-mode engines never auto-rebuild symbols.yaml (2026-05-23, Fix 1)
Decision: `config/symbols.yaml` is operator-regenerated offline after each
contract roll (`make rebuild-symbols-yaml`); in-process rebuild was removed.
Why: the in-process rebuild overwrote per-connection shards.
Revisit: only with a shard-aware rebuild design.

## shioaji pinned at 1.3.3 (2026-06-01, re-confirmed 2026-06-16)
Decision: hold the SDK pin; 1.5.3 is a full Rust `_core.abi3.so` rewrite
(solace internals gone, enum moves, `bidask`->`bid_ask`, timeout defaults
changed). Migration proceeds via the surface-diff tool + golden guard
(`scripts/shioaji_api_diff/`, `make shioaji-guard`) on a dedicated branch.
Revisit-trigger: adapter rewrite validated end-to-end (see current-risks.md).

## Research modules imported by tracked code must be tracked (2026-07-12)
Decision: any research module a tracked file imports — including dynamic
`load_module(<file path>)` targets, which import-statement analysis cannot
see — is committed (`git add -f` past the blanket research/* gitignore).
Landed: 18 modules (pdq tools incl. tsi15 BASE_TOOL, p2_exec_predictor,
regime_lab/snapshot_builder); data_pipeline became the package the audit
canon already declared; candidate_loop + pdq tools allowlisted in
factory.py; ci.yml research-audit-strict continue-on-error removed (audit
0 errors / 0 warnings in a clean worktree). Skill-referenced local data
dirs got .gitkeep skeletons instead of data commits.
Rejected: converge-tools move of pdq files to legacy/ (breaks committed
test imports); loosening the agent-docs drift baseline (ratchet).
Revisit-trigger: archiving the pdq lane — move tools + tests together.
