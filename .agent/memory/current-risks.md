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
+ prod checks still owed). Dependabot #376 and draft #371 recommendations
pending user decision (close both; fresh PR from current lineage).
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
