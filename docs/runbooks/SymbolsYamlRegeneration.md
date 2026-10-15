# Symbols YAML Regeneration (Pool Mode)

## When to run

After any contract roll (typically the 3rd Wednesday of each month for
TXF/MXF/TMF/EXF) or when broker contract listings change (new TXO weekly
expiries appear, expired strikes drop). Symptoms that indicate the canonical
`config/symbols.yaml` is stale:

- `feed_subscription_retry_total{result="skip_permanent"}` is non-zero
- "Contract not found" log entries for codes that no longer exist on the
  broker (e.g. `MXFE6` after the May-2026 expiry settled)
- The hourly contract refresh produces no new "subscription_limit_reached"
  events but old shards still subscribe expired codes

## Why pool-mode engines do NOT auto-regenerate

Prior to 2026-05-23, `refresh_contracts_and_symbols` rewrote
`self._client.config_path` every hour. In `QuoteConnectionPool` mode that
path points at a per-conn shard (`/tmp/hft_quote_pool_*/symbols_group_<id>.yaml`)
which holds only this facade's partition. The hourly rewrite promoted
group 0's shard to the full 478-symbol universe and corrupted the
partition (see commit history near `contracts_runtime.py` and
`.agent/library/` for the 2026-05-23 incident).

The fix disables the in-process YAML rewrite in pool mode. Pool composition
is now driven by `config/symbols.yaml` at boot and never mutated at
runtime. Contract metrics + the contract cache JSON continue to refresh
hourly so `_get_contract` lookups stay fresh.

The trade-off: operators must regenerate the canonical YAML offline after
contract listings shift.

## Update 2026-09-20: the pool now rolls its own universe

`QuoteConnectionPool.roll_universe` (commit `b33c632a`) rebuilds the universe
from `symbols.list` plus the contract cache and adopts it without a restart.
`run_universe_roll_clock` calls it every 600 s. It still never writes
`config/symbols.yaml` (the 2026-05-23 guard stands): it rewrites the
per-connection shards and a runtime snapshot (`HFT_SYMBOLS_RUNTIME_SNAPSHOT`).

Two consequences:

- Whether this works on the host depends on the running code containing
  `b33c632a`. Check that first (see the checklist). Hash both sides with the
  same function (`sha1sum` of `git show main:<path>` against `sha1sum` on the
  host); `git hash-object` is not comparable with `sha1sum`.
- The canonical `config/symbols.yaml` is not updated by the roll. Regenerate it
  (Procedure below) so the file in the repo and on the host keeps matching the
  broker's listing.

## Contract roll checklist (monthly delivery, third Wednesday)

A monthly future stops trading at 13:30 on its delivery date
(`core/market_calendar.py:taifex_delivery_cutoff`). The roll compares delivery
dates with that cutoff, but the symbol builder filters by the exchange *date*.
So on the delivery day, after 13:30, the rebuilt universe still holds the
expiring contract and `validate_universe` refuses it. The first adoptable window
is after the 05:00 night close on the next day. The sequence below is pinned by
`TestOctoberExpiryTimeline` in `tests/unit/test_pool_universe_roll.py`.

Example: 2026-10-21 (TMFJ6 delivery), Taipei time.

| When | Expected behaviour | Where to see it |
|---|---|---|
| Delivery day, before 13:30 | Nothing changes; the expiring contract still trades | `hft_quote_pool_universe_roll_total{result="no_change"}` or `deferred_session` |
| 13:30 | R47 rebinds TMF/R1 to the next month (separate 60 s family clock, `ShioajiFamilyPopulator`; `tests/unit/test_contract_family_roll.py`) | Check the bound code is the new month. The gauge `strategy_bound_live_symbols` read 1 for a dead contract on 2026-09-16, so it is not enough alone |
| 13:30 to midnight | Universe roll is **refused** whenever a check runs outside a session. This is expected, not a fault | `result="refused"` and the `universe_roll_refused` log; `deferred_session` while a session is open |
| Midnight to 05:00 | Night session is open; the roll stays deferred | `result="deferred_session"` (the log line is debug level) |
| After 05:00, until the pre-open lead of the 08:45 session (default 15 min, `HFT_FACADE_RECONNECT_PRE_OPEN_LEAD_S`) | First roll is adopted: the settled code is unsubscribed, the new month subscribed | `result="rolled"` and the `universe_rolled` log (`added_count`, `removed_count`, `per_group`) |

Pass criteria for the roll (capture the baseline before it):

- `hft_quote_pool_universe_roll_total{result="rolled"}` rises by 1; no `build_errors`,
  `unplaceable` or `no_contracts` results (those are not expected, investigate).
- `hft_quote_conn_subscribed_count` summed over connections is close to
  `symbols` in the `universe_rolled` line. Codes the broker refuses are queued
  for retry, so a small gap is explained by `_failed_sub_symbols`, not by a failed roll.
- `FeedSubscriptionPermanentlyFailed` (`config/monitoring/alerts/rules.yaml`) is
  not firing for the settled codes.

If there is no `rolled` result by 08:30, or the running code predates
`b33c632a`, fall back to Procedure below. Deploying is a per-batch decision for
the operator.

## Procedure

Run the regeneration locally, on a branch. Never on the deploy host.

```bash
# Needs a fresh broker contract cache (config/contracts.json). A running
# engine refreshes it hourly; otherwise run the engine briefly.
make rebuild-symbols-yaml

# Review the diff: month codes (E6 -> F6 -> G6 ...) and TXO strike ranges
# should evolve, the stock list should be stable.
git diff config/symbols.yaml
```

`config/symbols.yaml` is a Do-NOT-Edit path: commit it on a feature branch and
let the operator decide on push and merge.

Deploying the file is a Class A deploy and follows
`.agent/rules/41-deployment.md` (D1-D10) and `docs/runbooks/deployment.md`.
In short:

1. D1: the operator authorises this batch explicitly.
2. D3: diff the host file against `git show <LAST_DEPLOYED_SHA>:config/symbols.yaml`,
   never against the local file; a non-empty result is a live edit to keep.
3. D4: back up the host file to `~/deploy_backup_<UTC-date>/<batch>/` with `MD5SUMS.txt`.
4. D5, D6a: `stop`, wait 60 s, `start`. Never `up -d`, never
   `docker compose restart` for the engine.
5. D9: name the pass criteria and capture their baseline before stopping.
6. D10: append the batch to `~/deploy_ledger.tsv`.

## Verification

```bash
# Per-conn shards should be ~universe/num_conns, written only at boot.
ssh ops@<host> 'docker exec hft-engine ls -la /tmp/hft_quote_pool_*/'
# Expect: 4 symbols_group_*.yaml files, all close in size, all with
# mtime matching container start time (NOT changing hourly).

# skip_permanent count should drop to ~0 after the next subscribe cycle.
ssh ops@<host> 'curl -sS localhost:9090/metrics | grep "skip_permanent"'

# No new "subscription_limit_reached" warnings for the corrected partition.
ssh ops@<host> 'cd /home/charl/subhft && docker compose logs --since 2h hft-engine | grep -i subscription_limit'
```

## Related

- Code fix: `contracts_runtime.py:_pool_shard_overwrite_guard` (2026-05-23)
- Log template rewrite: `subscription_manager.py:_log_truncate_event`
- Notification template: `notifications/templates.py:render_subscription_truncated`
