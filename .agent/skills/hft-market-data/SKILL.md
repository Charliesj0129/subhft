---
name: hft-market-data
description: "Market-data plane: quote callbacks, tick dispatch, normalizer, LOB and feature engines, Rust flags, reconnect. Use when editing feed_adapter, normalizer.py, lob_engine.py, or feature/. Not for order or execution code, or broker SDK API questions."
---

# Market-data plane

Hottest path in the system: broker callback to event bus. The laws and flow
diagram are in `AGENTS.md`; the full trace is `docs/architecture/pipeline-chains.md`.

```
Exchange -> [broker callback thread] QuoteRuntime._on_tick_impl
 -> TickDispatcher.enqueue_tick (bounded queue) -> [worker thread]
 -> MarketDataService.on_tick -> Normalizer -> LOBEngine -> FeatureEngine -> RingBufferBus
```

`HFT_FUSED_NORMALIZER=1` replaces normalize + LOB update + stats with one
Rust call (`RustNormalizerLobFused`); LOBEngine must then skip recomputing stats.

## Where things live

- `feed_adapter/`: `normalizer.py` (raw -> TickEvent/BidAskEvent, scaled int),
  `lob_engine.py`, `protocol.py` (`BrokerProtocol`), `broker_registry.py`;
  `shioaji/` and `fubon/` are the adapters over shared `_base/` runtimes
  (quote, session, subscription). Shioaji pieces: `quote_runtime.py`,
  `tick_dispatcher.py`, `session_runtime.py`, `reconnect_orchestrator.py`,
  `quote_connection_pool.py`, `facade.py`. Broker callbacks cross threads only via
  the dispatcher and `call_soon_threadsafe`.
- `feature/`: `engine.py` (FeatureEngine), `registry.py` (feature sets),
  `parity.py` (Python/Rust parity), `boundary.py`, `rollout.py`, `profile.py`.
- Module-level traps: `.agent/memory/module_gotchas.md`.

## Rust flags (fallbacks are explicit, check them)

| Flag | Effect |
|---|---|
| `HFT_RUST_ACCEL=1` (default) | Rust normalize and bid/ask scaling; `0` disables all |
| `HFT_RUST_FORCE=1` | fail instead of falling back |
| `HFT_LOB_RUST_BOOKSTATE=1` (default) | Rust book stats |
| `HFT_FUSED_NORMALIZER` | fused normalize + LOB pipeline |
| `HFT_FEATURE_ENGINE_BACKEND=rust` | Rust feature kernel (default `python`) |
| `HFT_BUS_MODE=rust_typed` | typed Rust ring buffers (default `python`) |
| `HFT_LOB_LOCKS` | per-symbol locks, default off |
| `HFT_TICK_RING_BUFFER` | deque instead of `queue.Queue` for dispatch |

Full variable reference: `config-env` skill. Verify the default in source
before relying on this table.

## Facts that change decisions

- Book depth is 0-5 levels, variable; filter on `length(bids_price)` (`70-research-data.md`).
- Python fallback price scaling uses `round()`, not `int()` (IEEE 754 truncation).
- Per-tick stats must not share one mutable object across events.
- Metrics labels cap at 200 symbols (cardinality guard) in LOBEngine and FeatureEngine.
- FeatureEngine quality flags: `GAP`, `STATE_RESET`, `STALE_INPUT`,
  `OUT_OF_ORDER`, `PARTIAL` (one-sided book). Strategies must respect them.
- Orders and quotes use separate broker facades; a quote-facade problem does
  not show up as `FeedState`. "No data" is a break only while the session is open.

## Verify

```bash
make test-file FILE=tests/unit/test_normalizer.py
make test-file FILE=tests/unit/test_lob_engine.py
make test-file FILE=tests/unit/test_feature_engine.py
make test-file FILE=tests/unit/test_market_data_service_behavior.py
make hotpath-profile
```

## Done when

Targeted tests pass for both Rust-enabled and Rust-disabled paths, scaled-int
output is unchanged (or the change is deliberate), and `make hotpath-profile`
shows no stage regression.
