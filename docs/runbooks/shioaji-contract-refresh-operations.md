# Shioaji Contract Refresh Operations (Prototype)

## Runtime refresh behavior
- Background thread checks stale cache and refreshes on interval.
- Refresh is lock-guarded to avoid overlap.
- Diff is logged (`contract_refresh_diff`) and cached in-memory.

## New controls
- `HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY=none|diff|all`
  - `none`: only reload symbols/routes
  - `diff`: resubscribe if contract diff changes detected
  - `all`: always resubscribe after refresh

## Cache integrity
- `config/contracts.json` is written atomically via `write_contract_cache()`.

## Failure handling
- Refresh fetch failures are non-fatal; system continues with previous config.
- Check metrics/logs: `contract_refresh_total`, `contract_refresh_symbols_changed_total`.
