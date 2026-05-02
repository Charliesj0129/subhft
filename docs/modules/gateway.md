# gateway — CE-M2 Intent Routing Gateway

> **Package**: `src/hft_platform/gateway/`
> **Runtime Plane**: Decision/Execution boundary
> **Hot-Path**: 7-step dispatch pipeline
> **Default**: Disabled (`HFT_GATEWAY_ENABLED=0`)

## Overview

Optional gateway service providing idempotency, exposure tracking, policy-based gating, and leader election for high-availability. Serializes all intent processing through a 7-step synchronous pipeline.

## 7-Step Dispatch Pipeline

```
1. Dedup Check    → IdempotencyStore.check_or_reserve(key)
2. Policy Gate    → GatewayPolicy.gate(intent, storm_state)
3. Exposure Check → ExposureStore.check_and_update(key, intent)
4. Risk Evaluate  → RiskEngine.evaluate(intent)
5. Command Create → RiskEngine.create_command(intent)
6. Broker Dispatch→ OrderAdapter._api_queue.put_nowait(cmd)
7. Dedup Commit   → IdempotencyStore.commit(key, approved, reason)
```

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `service.py` | `GatewayService` | 7-step orchestrator with HA support |
| `exposure.py` | `ExposureStore`, `ExposureLimitError` | Per-account/strategy/symbol notional tracking |
| `dedup.py` | `IdempotencyStore`, `IdempotencyRecord` | LRU-bounded deduplication with disk persistence |
| `policy.py` | `GatewayPolicy`, `GatewayPolicyMode` | NORMAL/DEGRADE/HALT FSM |
| `leader_lease.py` | `FileLeaderLease` | fcntl-based single-host leader election |
| `channel.py` | `LocalIntentChannel`, `TypedIntentFrame` | Intent queuing with TTL and DLQ |

## ExposureStore

Thread-safe per-(account, strategy, symbol) notional tracker:

- Atomic CAS: check + update in ~200ns (lock scope = dict + integer only)
- CANCEL intents bypass tracking
- Zero-balance eviction (CE2-12) when at cardinality limit
- Max 10,000 entries (env `HFT_EXPOSURE_MAX_SYMBOLS`)
- Optional Rust fast-path (`HFT_EXPOSURE_RUST=1`)

## IdempotencyStore

LRU-bounded dedup window with optional disk persistence:

- O(1) check/commit via OrderedDict
- Window size: `HFT_DEDUP_WINDOW_SIZE` (default 10,000)
- Persistence: JSONL atomic write (temp+fsync+rename)
- Rust fast-path optional (`HFT_DEDUP_RUST=1`)

## GatewayPolicy (Mode FSM)

| Mode | NEW/AMEND | CANCEL | FORCE_FLAT | Halt-Exempt |
|------|-----------|--------|------------|-------------|
| NORMAL | Allow | Allow | Allow | N/A |
| DEGRADE | Block | Allow | Allow | Allow |
| HALT | Block | Allow* | Allow | Allow |

*CANCEL in HALT requires `HFT_GATEWAY_HALT_CANCEL=1` (default)

Auto-transitions: NORMAL → DEGRADE on StormGuard STORM; DEGRADE → NORMAL on recovery.

## LocalIntentChannel

```python
channel = LocalIntentChannel(maxsize=4096, ttl_ms=500)
channel.submit_nowait(intent)  # Hot-path entry
envelope = await channel.receive()
```

- TTL expiry: expired envelopes routed to bounded DLQ
- Typed fast-path: `TypedIntentFrame` avoids OrderIntent allocation
- DLQ size: `HFT_INTENT_DLQ_SIZE` (default 1,000)

## Leader Lease (HA)

- File-lock based (`fcntl.LOCK_EX | LOCK_NB`)
- Non-blocking acquire prevents event loop stalls
- Heartbeat JSON on each successful tick
- Standby rejection: reason "NOT_LEADER"
- Prototype: single-host only

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_GATEWAY_ENABLED` | `0` | Enable CE-M2 gateway |
| `HFT_EXPOSURE_MAX_SYMBOLS` | `10000` | Max exposure entries |
| `HFT_DEDUP_WINDOW_SIZE` | `10000` | Dedup LRU capacity |
| `HFT_DEDUP_PERSIST_ENABLED` | `1` | Persist dedup on commit |
| `HFT_INTENT_CHANNEL_SIZE` | `4096` | Intent queue capacity |
| `HFT_INTENT_TTL_MS` | `500` | Intent envelope TTL |
| `HFT_GATEWAY_HA_ENABLED` | `0` | Enable leader election |
| `HFT_GATEWAY_HALT_CANCEL` | `1` | Allow CANCEL in HALT |
| `HFT_GATEWAY_DEGRADE_ON_STORM` | `1` | Auto-degrade on STORM |
