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

## Procedure

```bash
# Always work against a fresh broker contract cache. If the engine is
# running it has already refreshed config/contracts.json hourly; otherwise
# run the engine briefly or call the broker explicitly.

make rebuild-symbols-yaml

# Review the diff before committing — month codes (E6→F6→G6…) and TXO
# strike ranges should evolve, stock list should be stable.
git diff config/symbols.yaml

# Commit + restart the live engine to pick up the new partition.
git add config/symbols.yaml
git commit -m "chore(symbols): regenerate config/symbols.yaml after contract roll"
git push

# On the deploy host:
ssh ops@<host> 'cd /home/charl/subhft && git pull && docker compose restart hft-engine'
```

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
