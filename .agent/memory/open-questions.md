# Open Questions (unresolved decisions)

Record here: unresolved decisions with what blocks them and who decides
(user/evidence/time). Do NOT record: questions answerable by reading source —
answer those instead. Move resolved items to architecture-decisions.md or
failed-attempts.md.

## MODULES_REFERENCE.md package-count drift (opened 2026-07-06)
The doc's auto-generated scan (2026-04-01) says 37 packages / ~210 files; a
post-cleanup note (2026-04-17 era) says ~24 packages. The doc needs a
regeneration pass. Decides: evidence (re-scan). Blocked by: nobody — just
needs a docs task.

## OrderIntent §7 parity producer fields (opened 2026-06-03)
Live parity for session/risk/force-flat dimensions needs a producer-side
OrderIntent change plus a future `hft.order_intents` ClickHouse migration —
ruled blocked_by_scope. Decides: USER. Partial fix landed
(`session_phase` stamped in runner phase filter).

## shioaji 1.5.4 dependabot PR (opened ~2026-07)
Whether to close it (superseded by the 1.5.3 migration branch) or retarget
the migration to 1.5.4 directly. Decides: USER, after 1.5.3 harness results.
