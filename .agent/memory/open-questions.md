# Open Questions (unresolved decisions)

Record here: unresolved decisions with what blocks them and who decides
(user/evidence/time). Do NOT record: questions answerable by reading source —
answer those instead. Move resolved items to architecture-decisions.md or
failed-attempts.md.

## MODULES_REFERENCE.md description staleness (narrowed 2026-07-07)
COUNTS RESOLVED 2026-07-06; CLASS/FILE IDENTIFIERS RESOLVED 2026-07-07
(Sonnet pilot, see model-routing.md): every col2/col3 class & file token
re-verified against src/; 32 stale tokens marked `[DRIFT: nearest-actual]`
inline (the 2026-04-01 scan predated large renames — options/reports/tca/
backtest class lists — and deleted feature/execution/data_quality/config/core
sources; 2 initial false positives on Rust `#[pyclass]` identifiers were
removed in review/meta-review, outcome PARTIAL). Doc provenance comment
updated to say identifiers verified, prose not.
REMAINING (all defer to USER — curation/design, not mechanical):
(a) Responsibility PROSE beyond identifiers still un-re-verified (e.g.
"5 mixins", "7-step dispatch", "IC=+0.116", service/queue counts);
(b) whether to REGENERATE the class lists from source vs keep the 32 [DRIFT]
annotations (a doc this drifted arguably wants regeneration, which is a
curation choice, not a mechanical pass);
(c) undocumented packages healing/ infra/ replay/ and the stale `scripts/`
row (references a src/hft_platform/scripts dir that does not exist; no backtick
token there to annotate). Decides: USER.

## OrderIntent §7 parity producer fields (opened 2026-06-03)
Live parity for session/risk/force-flat dimensions needs a producer-side
OrderIntent change plus a future `hft.order_intents` ClickHouse migration —
ruled blocked_by_scope. Decides: USER. Partial fix landed
(`session_phase` stamped in runner phase filter).

## shioaji 1.5.4 dependabot PR (opened ~2026-07)
Whether to close it (superseded by the 1.5.3 migration branch) or retarget
the migration to 1.5.4 directly. Decides: USER, after 1.5.3 harness results.
