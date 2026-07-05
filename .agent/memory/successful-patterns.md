# Successful Patterns (confirmed >=2 times, or single-confirmed ops recipes)

Record here: recipes confirmed to work — delegation packet shapes, debugging
sequences, safe ops procedures, export/validation flows. Do NOT record:
one-time luck; anything already canonical in a rule/skill (link instead).
Promote mature patterns into a rule or skill and replace the entry with a
pointer.

## Production engine restart (confirmed 2026-06-21, 2026-06-22)
stop → wait 60s → start. Then verify: quote connections logged in
(`hft_quote_conn_logged_in` = 1 for all facades) AND `subscribed_count`
equals the full universe — FeedState=CONNECTED alone is a false signal.

## Clearing stale boot-latch reduce-only without restart (2026-06-18)
In-container `hft ops rearm-platform` — the engine supervisor file-watches
`runtime_state.json` and force-clears. Restarting instead RE-LATCHES.

## SDK upgrade guarded by surface diff + golden (2026-06-16)
`scripts/shioaji_api_diff/` captures per-version API surfaces in throwaway
venvs (`make shioaji-surface`), builds a human runbook (`make shioaji-diff` →
`docs/runbooks/shioaji-version-diff.md`), and a CI golden guard
(`make shioaji-guard`) fails on silent surface drift. Reuse this pattern for
any pinned-SDK upgrade.

## Guarded ClickHouse access for analysis (standing)
`make ch-query-guard-check` / `ch-query-guard-run` — read-only policy,
memory/time/result limits, evidence artifacts. Use instead of raw clients
for any exploratory query.

## Faithful research kills (standing, many confirmations)
Pre-register the spec; keep floors frozen; test the candidate's OWN stop
rule; require cross-contract + beta-neutral checks before believing any
positive. Cheap refutations first (sample size, no-stop artifact,
single-day dominance) — see failed-attempts.md patterns list.
