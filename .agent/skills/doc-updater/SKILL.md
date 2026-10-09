---
name: doc-updater
description: "Reconcile docs, codemaps, and runbooks with the current source tree, verifying every path and count. Use when docs drift from code, after a module move or rename, or to refresh docs/CODEMAPS. Not for inventing behavior that source does not show."
---

# Doc updater

Read the source you document; never write from recall.

- Codemaps live in `docs/CODEMAPS/` (`architecture.md`, `backend.md`, `data.md`,
  `dependencies.md`); the module index is `docs/MODULES_REFERENCE.md`; the
  architecture overview is `docs/architecture/current-architecture.md`.
- When code and doc disagree, decide which is authoritative: update the doc if
  the code is right; flag a governance violation if the doc is the rule.
- Mark unresolved discrepancies `[DRIFT: nearest-actual]`; do not guess.
- Prove each path with `rg --files | rg <path>` (add `--hidden` for `.agent/`).
  Take counts from a command, not memory. Keep secrets, account IDs, and
  production hostnames out beyond existing conventions.
- Governing docs (AGENTS.md, rules, skills): run `make agent-docs-check`, and
  add an `.agent/CHANGELOG.md` line.

## Done when

Every referenced path exists, no behavior is invented, drift is marked, and the
diff is reviewed.
