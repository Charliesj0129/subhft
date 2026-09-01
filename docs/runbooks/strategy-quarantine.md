# Runbook: Strategy Quarantine Durability

## Scope

What to do when `StrategyQuarantineUndurable` or `StrategyQuarantinePersistFailing`
fires. Both mean the same thing: a strategy is latched **in memory only**, so a
restart releases it with no operator authorisation.

This is a **hard restart blocker**. Do not restart the engine while either alert
is firing.

## Why the alert exists

`StrategyHealthGovernor.quarantine_async` latches the strategy immediately and
writes the durable record off the event loop, because the write goes through
`runtime_state_store.locked_state` — a bounded `flock` poll (2 s deadline) plus
two fsyncs, which cannot run on a 1 ms loop budget.

The latch and its record can therefore disagree:

```
  latched in memory                      durable record
  -----------------                      --------------
  quarantine_async()  ---- 0 ms ------->  strategy skipped
        |
        +-- to_thread(persist) ------->  [ flock contended ]
        |                                        |
        |  wait 200 ms, then resume dispatch     |  ...still waiting
        |                                        v
        |                                 write lands  -> durable = 1
        |
        +-- OR the write FAILS -----------> X  (never lands)
                                              undurable = 1, and a
                                              restart releases the strategy
```

`strategy_quarantine_undurable{strategy=...} == 1` marks the window. It is
cleared only by that quarantine's OWN completion — durability is keyed by
quarantine token, so a late completion from an earlier, already-re-armed
quarantine cannot clear it.

## Triage

| Check | Command |
|---|---|
| Which strategy | `strategy_quarantine_undurable == 1` in Prometheus, label `strategy` |
| Whether the write failed or is merely slow | `increase(strategy_quarantine_persist_failed_total[10m]) > 0` |
| The reason it was quarantined | engine log, `strategy_quarantined` with `quarantine_token` |
| The write failure itself | engine log, `strategy_quarantine_persist_failed` |
| Whether the record actually landed | inspect the runtime state store on the host |

## Actions

1. **Do not restart.** A restart while `undurable == 1` re-arms the strategy
   without authorisation. `restore_persisted_quarantines` can only hydrate what
   was written.
2. If `strategy_quarantine_persist_failed_total` is increasing, the state store
   is the problem — check disk space and whether another process (the operator
   CLI, a second engine) is holding the `flock`.
3. Once the underlying cause is fixed, the next quarantine write clears the
   gauge. If the strategy must stay quarantined across a restart and the write
   cannot be made to land, keep the engine up until it can.
4. Re-arm deliberately with `hft ops rearm-strategy` once the condition that
   caused the quarantine is resolved. Re-arm retires the durability state for
   that strategy explicitly.

## Related

- `docs/runbooks/strategy-rollback.md` — rolling back a promoted alpha
- `src/hft_platform/ops/strategy_governor.py` — the latch and its persistence
- `src/hft_platform/ops/runtime_state_store.py` — the lock these writes contend on
