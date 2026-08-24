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
- 2026-07-29: `/home/charlie/backups/hft_platform/hft_platform-20260729T091332Z-c8642fa16354.bundle`
  verified all refs and covered pre-correction HEAD; campaign/source snapshots were also checksum-copied to
  `/home/charlie/backups/hft_platform/alpha-mining-20260729/`.
- 2026-07-29: `/home/charlie/backups/hft_platform/hft-alpha-mining-correction-clean-20260729T091928Z-1e5c158b06a4.bundle`
  verified all refs and covered correction commit `1e5c158b`.
- 2026-07-30: `/home/charlie/backups/hft-alpha-mining-v21-79b3febd-20260730.bundle`
  verified all refs and covered alpha-mining v2.1 commit `79b3febd`.
- 2026-07-31: `/home/charlie/backups/alpha-mining-v21-recovered-20260731T024811Z-0e1975dbdd3d.bundle`
  verified all refs and covered the market-data/cost-profile repair HEAD `0e1975db`.
Expires: when a remote-backup/push-cadence decision lands. Owner: Charlie.

## RISK: shioaji 1.5.6 deployed to old host in SIM only; live order path unproven (since 2026-07-19)
Pin is `shioaji==1.5.6` (cd280dbc, 2026-07-15). Old-host deploy accepted
2026-07-19 on image v4.1 (`latest` retagged; rollback anchor `c79974da41d9`
= 1.3.3): quote-only gates + 30-min SIM soak + full-engine SIM readyz-ready.
Required fix 3b1c10c8 (1.5.6 account properties raise AuthError pre-login).
NOT yet proven: live (non-sim) order path — the 07-18 real RTT probe was
aborted with 305/305 broker rejections (reason query skipped); Monday
day-session full-universe data flow. pool=4 session fit PROVEN 2026-07-19
evening (5/5 logins, zero 451). See runbook "Runtime validation
2026-07-17→19" + its evening follow-up.
Expires: Monday confirmation + a validated live order round-trip. Owner: Charlie.

## RISK: production engine order mode is SIM (since 2026-06-15)
After a reconnect CPU-spin incident, the production engine was restarted in
SIM order mode — it is NOT live trading. Do not assume live; do not flip
modes without explicit user request.
TRAP RESOLVED 2026-07-19 evening: host `.env` flipped to
`HFT_ORDER_MODE=sim` (Charlie-run, backup `.env.bak-*` kept; verified via
`docker compose config` = sim). A plain `docker compose up -d` now boots SIM
— back-to-live requires Charlie editing `.env` (or explicit inline override),
which is the intended manual gate.
Expires: when the user re-enables live mode. Owner: Charlie.

## RISK: broker session budget at zero headroom (since 2026-07-19)
Full engine on current code = 1 order client (separate since e7505036,
2026-04-27) + N quote-pool conns. Sinopac cap = 5. Production shape pool=4
RESTORED 2026-07-19 evening (5/5 logins, zero 451; the 07-18/19 lingering
session was reaped broker-side ~31 h after the probe). Standing constraint:
full engine = 5 real sessions = cap, ZERO headroom — any probe/tool login
requires the engine stopped first; a lingering session (e.g. after an
aborted probe) blocks the engine's own 5th login until broker-side reaping.
Restart discipline: stop→60s+→start (433be777's 451 backoff deployed and
observed working on v4.1).
Expires: if Sinopac raises the cap or the pool shape is redesigned. Owner: Charlie.

## RISK: prometheus_client pinned <0.25
0.25.0 corrupts MutexValue (TypeError on /metrics). Do not "upgrade to fix
deprecation warnings". Expires: upstream fix verified. Owner: any.

## RISK: stale mining artifacts promote a tautological winner (since 2026-08-23)
`research/combinatorial/results/` is gitignored (`.gitignore:322`, PR #435) and
every file in it is dated 2026-07-25. The self-correlation guard landed
2026-08-19 (`6ce84168`, `has_self_correlation`), so the artifacts predate the
fix and were never regenerated. `WINNER_DAILY.json` reads
`sign(ts_corr(mid, mid, 50))` — a tautology whose 13.4 `day_sharpe` measures
drift — and self-correlated expressions survive in `daily_hunt_qualifying.json`
(`depth_imb, imb, micro_dev, mid, spread`) and `oos_hunt_qualifying.json`
(`mid, spread`). `WINNER_BARS`/`WINNER_OOS` are clean. Nothing will flag these:
no diff, no CI gate, no `git status`, and no generator stamp inside the files.
Per `feedback_no_cross_candidate_kill_shortcuts` the re-screen scope is not
mine to decide; nothing has been deleted or regenerated.
Expires: when the artifacts are regenerated under the guard, or quarantined.
Owner: Charlie.

## RISK: #443 ships unarmed; four order-path fixes are merged but undeployed (since 2026-08-22)
PRs #442–#446 are on `origin/main` and CI-green, and **none of them is running
in production** — the host serves a bind-mounted source mosaic that matches no
single commit (see `deployed_source_is_a_bind_mounted_mosaic`). #443 in
particular is inert even once deployed: production sets neither
`HFT_STORMGUARD_ORDER_RTT_WARM_US` nor `HFT_STORMGUARD_ORDER_RTT_STORM_US`, so
the newly-connected input has no thresholds to trip. Do not treat "merged" as
"the double-counted-position hazard is closed in the field".
Expires: on a deploy with per-batch authorization + threshold values from
Charlie. Owner: Charlie.
