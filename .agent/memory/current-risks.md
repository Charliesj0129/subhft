# Current Risks (live register — prune aggressively)

Record here: active risks with owner + expiry/resolution condition. Do NOT
record: permanent invariants (project-overview.md) or resolved items (delete,
noting resolution in the relevant lessons file). A stale register is worse
than none.

## RISK: unpushed local commits — RESOLVED 2026-07-08 (entry kept for the residual ref divergence)
User-approved pushes created upstreams for `docs/agent-knowledge-distillation`,
`research-flow/edge-evidence-parity-hardening`, `research/replay-parity-field-set`;
`git log --branches --not --remotes` = 0 (every local commit is on a remote ref).
RESIDUAL: local `chore/shioaji-153-validation-harness` diverges from origin's
same-named branch (PR #371's older lineage) — push rejected non-fast-forward.
Its commits ARE backed up (contained in the pushed docs/agent-knowledge-
distillation chain). Never force-push; resolve the ref when #371 is
retargeted to 1.5.5 or closed. `main` remains behind origin/main by 14.
Expires: when #371 end-state is decided. Owner: Charlie.

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
