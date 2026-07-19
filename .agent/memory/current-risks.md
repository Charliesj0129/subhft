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

## RISK: shioaji 1.5.6 deployed to old host in SIM only; live order path unproven (since 2026-07-19)
Pin is `shioaji==1.5.6` (cd280dbc, 2026-07-15). Old-host deploy accepted
2026-07-19 on image v4.1 (`latest` retagged; rollback anchor `c79974da41d9`
= 1.3.3): quote-only gates + 30-min SIM soak + full-engine SIM readyz-ready.
Required fix 3b1c10c8 (1.5.6 account properties raise AuthError pre-login).
NOT yet proven: live (non-sim) order path — the 07-18 real RTT probe was
aborted with 305/305 broker rejections (reason query skipped); Monday
day-session full-universe data flow; pool=4 session fit (see session-budget
risk). See runbook "Runtime validation 2026-07-17→19".
Expires: Monday confirmation + a validated live order round-trip. Owner: Charlie.

## RISK: production engine order mode is SIM (since 2026-06-15)
After a reconnect CPU-spin incident, the production engine was restarted in
SIM order mode — it is NOT live trading. Do not assume live; do not flip
modes without explicit user request.
TRAP (added 2026-07-19): host `.env` still says `HFT_ORDER_MODE=live`; the
SIM posture exists ONLY as an inline env override on the running container
(currently also `HFT_QUOTE_CONNECTIONS=3`). A plain `docker compose up -d`
without overrides boots LIVE order mode on 1.5.6 (`latest`=v4.1).
Expires: when the user re-enables live mode. Owner: Charlie.

## RISK: broker session budget at zero headroom + one lingering session (since 2026-07-19)
Full engine on current code = 1 order client (separate since e7505036,
2026-04-27) + N quote-pool conns. Sinopac cap = 5. With pool=4 the 5th login
451'd persistently on 2026-07-19 — exactly one unidentified broker-side
session lingers (all harness tools logout; crash-loops die pre-login).
Mitigation: engine runs with inline `HFT_QUOTE_CONNECTIONS=3` (subs 99/99/98,
under the 120/conn cap). Monday pre-market: retry pool=4; if it fits, restore
production shape. Standing constraint: live order mode = 5 real sessions =
cap — any probe/tool login requires the engine stopped.
Note: restart-procedure fix 433be777 (stop→60s→start races; 451 backoff
75s×2) is now DEPLOYED and observed working (v4.1, REVISION bfa46e1d);
stop→60s+→start discipline stays best practice.
Expires: lingering session identified/reaped + pool shape decided. Owner: Charlie.

## RISK: prometheus_client pinned <0.25
0.25.0 corrupts MutexValue (TypeError on /metrics). Do not "upgrade to fix
deprecation warnings". Expires: upstream fix verified. Owner: any.
