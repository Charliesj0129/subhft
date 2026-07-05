# Current Risks (live register — prune aggressively)

Record here: active risks with owner + expiry/resolution condition. Do NOT
record: permanent invariants (project-overview.md) or resolved items (delete,
noting resolution in the relevant lessons file). A stale register is worse
than none.

## RISK: unpushed local commits (verified 2026-07-06)
25 commits exist only on this machine, across 3 local-only branches with no
upstream: `chore/shioaji-153-validation-harness`,
`research-flow/edge-evidence-parity-hardening`,
`research/replay-parity-field-set`. `main` is behind origin/main by 14.
Treat these commits as irreplaceable; re-verify with
`git log --branches --not --remotes --oneline | wc -l` at session start.
Expires: when branches gain upstreams or are merged. Owner: Charlie.

## RISK: shioaji 1.5.3 migration in flight (since 2026-06-16)
Pin is `shioaji==1.3.3`. 1.5.3 = full Rust `_core` rewrite; upgrade PR is
HELD pending adapter validation (live + Docker checks owed). A 1.5.4
dependabot PR decision is also pending. Do NOT bump the pin or merge SDK
PRs without `make shioaji-guard` green + the validation harness.
Expires: when the upgrade PR lands or is closed. Owner: Charlie.

## RISK: production engine order mode is SIM (since 2026-06-15)
After a reconnect CPU-spin incident, the production engine was restarted in
SIM order mode — it is NOT live trading. Do not assume live; do not flip
modes without explicit user request.
Expires: when the user re-enables live mode. Owner: Charlie.

## RISK: production restart procedure is non-obvious
`docker compose restart` and immediate auto-restart race the broker's
session release → engine comes up logged-out/unsubscribed while
FeedState=CONNECTED masks it. Only safe sequence: stop → wait 60s → start,
then verify login/subscription metrics (see failed-attempts.md). A durable
code fix (boot grace-period for recorder_data_loss; restart backoff) is owed.
Expires: when the durable fix lands. Owner: Charlie.

## RISK: prometheus_client pinned <0.25
0.25.0 corrupts MutexValue (TypeError on /metrics). Do not "upgrade to fix
deprecation warnings". Expires: upstream fix verified. Owner: any.
