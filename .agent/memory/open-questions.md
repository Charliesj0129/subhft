# Open Questions (unresolved decisions)

Record here: unresolved decisions with what blocks them and who decides
(user/evidence/time). Do NOT record: questions answerable by reading source —
answer those instead. Move resolved items to architecture-decisions.md or
failed-attempts.md.

## MODULES_REFERENCE.md description staleness (narrowed 2026-07-06)
COUNTS RESOLVED 2026-07-06 (Tier-1 pilot delegation, see
model-routing.md): true values are 23 top-level / 30 nested packages,
372 Python files; doc corrected, both package metrics now explicit. The
count ambiguity that caused the drift (37 vs ~24) was top-level-vs-nested.
REMAINING: row descriptions/class lists are still from the 2026-04-01 scan
and were NOT re-verified (comment in the doc says so). Decides: evidence
(a per-row read pass). Blocked by: nobody — just needs a docs task.

## OrderIntent §7 parity producer fields (opened 2026-06-03)
Live parity for session/risk/force-flat dimensions needs a producer-side
OrderIntent change plus a future `hft.order_intents` ClickHouse migration —
ruled blocked_by_scope. Decides: USER. Partial fix landed
(`session_phase` stamped in runner phase filter).

## shioaji 1.5.4 dependabot PR (opened ~2026-07)
Whether to close it (superseded by the 1.5.3 migration branch) or retarget
the migration to 1.5.4 directly. Decides: USER, after 1.5.3 harness results.
