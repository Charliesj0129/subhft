# Deployment

Production deploys target THESHOW (`charl@100.91.176.126:/home/charl/subhft`).
Canonical procedure: `docs/runbooks/deployment.md`. This file is the compact
guardrail form — when in doubt, open the runbook.

## Laws

- **D1 Manual + per-batch authorization.** Never deploy on your own initiative,
  and never carry a previous batch's approval forward. `HFT_ORDER_MODE=live`
  cutover is a separate red line.
- **D2 No git writes on the host.** No `pull` / `checkout` / `reset` / `stash` /
  `clean`. The host tree is permanently divergent and carries live production
  edits that exist nowhere else. Read-only git is required by D3.
- **D3 Diff against the last-deployed commit, not against local.** Fetch the host
  file, diff it against `git show <LAST_DEPLOYED_SHA>:<path>`. Non-empty means a
  live edit: build the candidate by hand and prove candidate-vs-host contains
  only your hunks.
- **D4 Back up every overwritten file first** to
  `~/deploy_backup_<UTC-date>/<batch>/` with `MD5SUMS.txt`.
- **D5 Class decides mechanism.** A = bind-mounted `src/`/`config/`/`scripts/`
  → `stop` → wait 60 s → `start`. B = monitoring rules → reload API, no restart.
  C = env/compose/image → `up -d` = recreate.
- **D6 Never `up -d`** for Class A/B. `up -d` recreates the container and
  destroys the writable layer holding `/app/outputs` (autonomy evidence, not
  bind-mounted). Class C must rescue it with `docker cp` first.
- **D6a Never `docker compose restart` the engine.** Refuted 2026-06-21 and
  2026-06-22: it races the broker's 5-session release, so `order_client` fails
  and the quote facades come back logged out while `FeedState` still reads
  `CONNECTED`. Stop, wait 60 s, start, and verify `subscribed_count` and the
  login flags — not `FeedState`.
- **D7 Validate on the host before restarting**: `py_compile` in the engine
  container for code, `promtool check rules` for alert rules. Local gates are a
  precondition, not a substitute.
- **D8 One incident, one restart.** Each restart re-logs-in all four quote
  facades. Batch fixes; a ready fix waits for its batch.
- **D9 Name the pass criteria before restarting**, as specific series with
  expected direction, and capture their pre-restart baseline.
- **D10 Append to `~/deploy_ledger.tsv`** after every batch. Without it D3 has
  no SHA to diff against — host `git status` shows deployed files as plain `M`.

## Reject on sight

- `git pull` / `git reset --hard` / `git checkout <sha>` on the host
- `docker compose up -d` for a code-only change
- copying a host config file wholesale without a D3 diff
- restarting without a captured pre-restart baseline
- claiming a deploy verified from `docker compose ps` alone
- `.env` edits expected to take effect via `restart` (env is creation-time)
- `grep -c 451` as a 451 check — matches timestamps; use `code: ?451`

## Verification floor

Every deploy re-reads, and reports with actual values:

```bash
docker inspect hft-engine --format '{{.State.StartedAt}} {{.RestartCount}} {{.State.Health.Status}}'
docker compose ps                                        # 11/11 healthy
docker compose logs hft-engine --since 10m | grep -icE 'code: ?451|Too Many Connections'
docker compose logs hft-engine --since 10m | grep -iE '"level": ?"(error|critical)"'
curl -s localhost:9090/metrics | grep '^hft_quote_conn_subscribed_count'   # 4 x 74
curl -s localhost:9090/metrics | grep '^shioaji_thread_alive'              # per conn_id, 1.0
```

Plus change-specific checks. Report exact commands run and name what was not run.

## Ordering

`hft ops rearm-platform` via `docker compose exec` runs in a separate process
and cannot reach the live controller — re-arm **before** the restart. When a
batch has both rules and code, reload Prometheus first (reversible), restart
the engine second (not).
