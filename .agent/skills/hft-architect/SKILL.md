---
name: hft-architect
description: "Platform architecture (runtime planes, boundaries, queue and degradation design, Python-Rust split) plus the data-contract field reference. Use when changing runtime boundaries, adding a hot-path stage, or touching contracts or events.py. Not for broker details or strategy logic."
---

# HFT architecture

The flow diagram, contract chain, and boundary rules are in `AGENTS.md`. The
canonical baseline is `docs/architecture/current-architecture.md`; the module
index is `docs/MODULES_REFERENCE.md`; `docs/architecture/pipeline-chains.md` traces every chain.

## Planes

| Plane | Where | Responsibility |
|---|---|---|
| Control | `services/` (bootstrap, system, registry), `config/` | queues, service graph, supervision, HALT enforcement |
| Market data | `feed_adapter/`, normalizer, `lob_engine.py` | ingest, normalize, LOB state, Rust fast paths |
| Feature | `feature/` | feature plane, Python/Rust parity |
| Decision | `strategy/`, `risk/` (validators, `storm_guard.py`) | dispatch, risk validation, StormGuard |
| Execution | `order/`, `execution/` | order dispatch, fill routing, positions, reconciliation |
| Persistence | `recorder/` | ClickHouse, WAL, batcher, loader |
| Observability | `observability/`, `notifications/` | Prometheus metrics, alerts |
| Ops (cross-cutting) | `ops/` | `session_governor`, `autonomy_monitor`, `position_flattener` |

Keep a change inside one plane when you can; a change that crosses planes
needs the design checklist below.

## Design checklist

1. Allocation on the hot path? Latency budget consumed (loop budget 1 ms)?
2. Does it block the event loop or the quote-callback path?
3. Locality preserved; FFI copies avoided?
4. How does it degrade or recover when the component fails (fail-closed)?
5. Unbounded state? Maps declare max cardinality and eviction (exposure cap 10,000).
6. Does a new hot-path stage use a bounded queue with an explicit overflow policy?
7. Does the 5-gate design review in `docs/architecture/design-review-artifacts.md` apply?

High-risk areas: broker adapters (protocol boundary, SDK import guards),
recorder durability (WAL replay, dedup, schema evolution), feature-plane parity,
Python-Rust interfaces (hidden fallbacks, copies, panics), unbounded maps
(order id map, exposure store, metric labels), ops phase transitions (a HALT
recovery needs a manual rearm).

## Verify and update

```bash
uv run maturin develop --manifest-path rust_core/Cargo.toml   # rebuild Rust
make check && make hotpath-profile
```

When responsibilities move, update `docs/architecture/current-architecture.md`,
`docs/MODULES_REFERENCE.md`, and `docs/CODEMAPS/`.

## Done when

The checklist answers are written down in the PR or report, boundary tests
pass (`make dependency-boundary`), and the canonical docs match the new shape.

## References

- `references/data-contracts.md` — read when constructing or reviewing events, intents, or fills in code or tests.
