# Current Risks (live register — prune aggressively)

Record here: active risks with owner + expiry/resolution condition. Do NOT
record: permanent invariants (project-overview.md) or resolved items (delete,
noting resolution in the relevant lessons file). A stale register is worse
than none.

## RISK: commits accumulate unpushed between approved pushes (standing; tooling landed 2026-07-11)
Push is a per-operation human approval, so new local commits sit on one disk
until the next approved push (institutionalization batch pushed with
Charlie's approval 2026-07-11; branch synced at that point). Mitigation tooling: `make git-bundle-backup DEST=<dir>`
(`scripts/git_bundle_backup.py` — fail-closed: dest required + outside repo,
all refs, verified, covers HEAD, never overwrites). FIRST RUN BLOCKED until
Charlie approves a destination; record each run (date + dest + bundle name)
below this line.
Expires: when a remote-backup/push-cadence decision lands. Owner: Charlie.

## RISK: shioaji 1.5.x migration in flight (since 2026-06-16; retargeted to 1.5.5 on 2026-07-08)
Pin is `shioaji==1.3.3`. 1.5.x = full Rust `_core` rewrite; migration
retargeted to 1.5.5 (1.5.3→1.5.5 surface diff SAFE — see the runbook).
Do NOT bump the pin or merge SDK PRs without `make shioaji-guard` green +
the validation harness (Phase 0/1 vs 1.5.5 run locally 2026-07-08; sim soak
+ prod checks still owed). #371 and #376 both CLOSED on GitHub (verified
2026-07-11); the diverged local harness branch was retired the same day.
A fresh SDK PR from the current lineage is owed when the migration resumes.
Expires: when the migration lands or is abandoned. Owner: Charlie.

## RISK: production engine order mode is SIM (since 2026-06-15)
After a reconnect CPU-spin incident, the production engine was restarted in
SIM order mode — it is NOT live trading. Do not assume live; do not flip
modes without explicit user request.
Expires: when the user re-enables live mode. Owner: Charlie.

## RISK: production restart procedure is non-obvious (code fix landed 2026-07-08, prod deploy owed)
`docker compose restart` and immediate auto-restart race the broker's
session release → engine comes up logged-out/unsubscribed while
FeedState=CONNECTED masks it. Only safe sequence on the CURRENT prod build:
stop → wait 60s → start, then verify login/subscription metrics (see
failed-attempts.md). Durable fix committed 433be777 (recorder_data_loss boot
grace HFT_RECORDER_DATA_LOSS_BOOT_GRACE_S=60; 451 login backoff
HFT_LOGIN_CONNLIMIT_RETRIES=2 x HFT_LOGIN_CONNLIMIT_BACKOFF_S=75; transition
reason labels) but prod still runs build ff0b4994 — manual procedure stays
mandatory until a user-approved deploy.
Expires: when the fix is deployed to prod. Owner: Charlie.

## RISK: prometheus_client pinned <0.25
0.25.0 corrupts MutexValue (TypeError on /metrics). Do not "upgrade to fix
deprecation warnings". Expires: upstream fix verified. Owner: any.
