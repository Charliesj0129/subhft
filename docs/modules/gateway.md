# gateway

## Purpose

CE-M2 order gateway — serialized intent processing pipeline. Opt-in via `HFT_GATEWAY_ENABLED=1`.

## Key Files

- `src/hft_platform/gateway/service.py`: `GatewayService` — main 7-step pipeline (256 lines).
- `src/hft_platform/gateway/channel.py`: `LocalIntentChannel` — asyncio.Queue wrapper for `IntentEnvelope`.
- `src/hft_platform/gateway/dedup.py`: `IdempotencyStore` — LRU cache with JSON persist on shutdown.
- `src/hft_platform/gateway/exposure.py`: `ExposureStore` — per-strategy/symbol net qty + notional limits.
- `src/hft_platform/gateway/policy.py`: `GatewayPolicy` — mode FSM (ALLOW_ALL / CANCEL_ONLY / REJECT_ALL).

## Processing Pipeline

```
1. dedup.check_or_reserve(key)     → cached hit? return early
2. policy.gate(intent, sg_state)   → CANCEL_ONLY blocks non-cancel
3. exposure.check_and_update(key)  → reject if position limit exceeded
4. risk_engine.evaluate(intent)    → synchronous CPU check
5. risk_engine.create_command()    → materialize OrderCommand
6. dedup.commit(key, approved)     → persist decision
7. order_adapter._api_queue.put_nowait(cmd) → dispatch
```

## Typed Intent Fast Path

When `HFT_TYPED_INTENT_CHANNEL=1`, StrategyRunner emits tuple payloads tagged `"typed_intent_v1"`.
Gateway wraps these in `TypedIntentEnvelope` and delays deserialization until after dedup+policy+exposure.

## Metrics

- `gateway_dispatch_latency_ns`: End-to-end pipeline histogram (steps 1-7).
- `gateway_reject_total{reason}`: Rejection counter by reason code.
- `gateway_dedup_hits_total`: Dedup cache hit counter.
- `gateway_intent_channel_depth`: Channel queue depth gauge.

## Configuration

- `HFT_GATEWAY_ENABLED=1`: Enable gateway pipeline (bootstrap.py).
- `HFT_EXPOSURE_MAX_SYMBOLS`: Max distinct symbols per exposure store (default 10000).
- `HFT_DEDUP_TTL_S`: Dedup entry TTL (default 300s).

## Gotchas

- Metrics use **deferred imports** (`from ... import MetricsRegistry` inside methods) to avoid circular import chains.
- `ExposureLimitError` hard-caps symbol cardinality. Commit dedup entry as rejected to prevent retry loops.
- On risk rejection, exposure must be **explicitly released** to avoid phantom exposure buildup.
