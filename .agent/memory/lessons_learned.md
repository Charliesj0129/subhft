# Lessons Learned

## [GOTCHA] mypy requires explicit path config and src layout (2025-01)

**Context**: mypy failed to resolve imports when run without explicit `mypy_path` and `files` config.
**Fix**: Added `mypy_path = ["src"]`, `explicit_package_bases = true`, and listed each module in `files` within `pyproject.toml`.
**Rule**: Always configure mypy paths explicitly in monorepo/src-layout projects. Never rely on auto-discovery.
**Commits**: `0964ae1`, `8c4b061`

## [PERF] API latency must be isolated from hot path (2025-01)

**Context**: Shioaji API calls were blocking the main event loop, causing unpredictable latency spikes in feed processing.
**Fix**: Isolated all API calls behind async boundaries with dedicated latency metrics (`shioaji_latency_probe`).
**Rule**: External API calls MUST be wrapped in async tasks with timeout guards. Never call synchronous broker APIs on the event loop.
**Commits**: `e9d6a0a`, `21da1d3`

## [BUG] One-sided LOB snapshots cause normalizer crash (2025-01)

**Context**: Market data snapshots sometimes arrive with only bid or only ask side populated (e.g., pre-market, illiquid instruments). The normalizer assumed both sides exist.
**Fix**: Added guards for one-sided quotes in `sim` and synthetic side handling in normalizer.
**Rule**: Always handle `None`/empty arrays for bid or ask side independently. Never assume both sides of the book are present.
**Commits**: `683d642`, `a53dc46`

## [PERF] Rust hot-path provides 10-100x speedup over pure Python (2025-01)

**Context**: Python normalizer and stats computation were bottlenecks. Rust `pyo3` bindings for `normalize_quote` and `compute_stats` reduced latency from ~500us to ~5us.
**Fix**: Implemented Rust fast path with Python fallback. CI validates both paths.
**Rule**: Any computation on the hot path that exceeds 50us in Python should be evaluated for Rust migration. Always maintain Python fallback for testing.
**Commits**: `60570b5`, `0840d84`

## [GOTCHA] Coverage thresholds must match actual baseline (2025-01)

**Context**: Initial CI coverage gates were set too high, causing all PRs to fail. Had to lower to 60%/50% baseline and plan incremental increases.
**Fix**: Set realistic initial thresholds (`--cov-fail-under=60`, branch `--fail-under=50`) and documented a coverage roadmap.
**Rule**: Set coverage gates at current actual coverage, then ratchet up incrementally. Never set aspirational gates without a migration plan.
**Commits**: `2d0041b`, `64ad84f`

## [ARCH] Event mode must be forced under test (2025-01)

**Context**: Tests were failing intermittently because the platform defaulted to production event mode, which spawns background threads and network connections.
**Fix**: Added `HFT_EVENT_MODE` env var, forced to `test` mode in pytest fixtures and docker-compose.
**Rule**: Always use environment variables to force deterministic mode in tests. Never rely on runtime detection.
**Commits**: `32ee915`, `1f4d348`

## [BUG] Logger shadowing in main causes silent failures (2025-01)

**Context**: A local variable named `logger` shadowed the module-level structlog logger, causing log messages to disappear silently.
**Fix**: Renamed local variable to avoid shadowing.
**Rule**: Never shadow module-level `logger`. Use `structlog.get_logger()` at module level and never reassign the name.
**Commits**: `47cd0cd`

## [GOTCHA] Rust clippy warnings must be treated as errors in CI (2025-01)

**Context**: Clippy warnings accumulated silently until they became blocking. CI was not enforcing `-D warnings`.
**Fix**: Added `cargo clippy -- -D warnings` to CI and fixed all existing warnings.
**Rule**: Always run clippy with `-D warnings` in CI. Fix warnings immediately, never suppress without justification.
**Commits**: `3fe1cf7`

## [ARCH] StrategyRunner circuit breaker is 3-state FSM (2026-02)

**Context**: Strategy crashes used to disable the strategy permanently. Operators had to redeploy.
**Fix**: Implemented 3-state FSM (normal→degraded→halted) with cooldown recovery. Halted strategies auto-retry after `HFT_STRATEGY_CIRCUIT_COOLDOWN_S` (default 60s). Degraded requires N/2 consecutive successes to recover.
**Rule**: Any per-component circuit breaker must have an auto-recovery path. Never make human intervention the only recovery mechanism.

## [PERF] Typed intent tuples eliminate OrderIntent allocation on hot path (2026-02)

**Context**: Every strategy event created an `OrderIntent` dataclass object, adding GC pressure.
**Fix**: `_intent_factory()` returns a plain tuple tagged `"typed_intent_v1"` when `HFT_TYPED_INTENT_CHANNEL=1`. Gateway's `typed_frame_view()` lazily deserializes only after passing dedup+policy+exposure gates.
**Rule**: On the hot path, prefer tuples/namedtuples over dataclasses when the object's lifetime is very short and it crosses few boundaries.

## [ARCH] Recorder degrades gracefully under queue overflow (2026-02)

**Context**: When ClickHouse is slow, recorder queue fills up and market data ticks are dropped.
**Fix**: `MarketDataService` enters degraded mode after N consecutive drops (`HFT_RECORD_DEGRADE_THRESHOLD`=500). In degraded mode, ALL recording is skipped (ticks still flow). Auto-recovers when queue drops below 50%.
**Rule**: Recording must never block or drop market data. Graceful degradation > crash > data loss.

## [GOTCHA] Gateway uses deferred imports to avoid circular dependencies (2026-02)

**Context**: `GatewayService` needs `MetricsRegistry` but importing it at module level creates circular import chains through `observability → risk → gateway`.
**Fix**: All metrics access uses deferred `from hft_platform.observability.metrics import MetricsRegistry` inside methods, wrapped in `try/except` to never break the hot path.
**Rule**: In the gateway/risk/execution import triangle, always use deferred imports for observability. Never move these to top-level.

## [ARCH] AI context files must have a single source of truth (2026-02)

**Context**: Project had 4 overlapping AI context files (`CLAUDE.md`, `AGENTS.md`, `README_AI.md`, `docs/ARCHITECTURE.md`) with contradictions, broken references, and fictional content.
**Fix**: Deleted `README_AI.md` (referenced 7 nonexistent skills). Rewrote `CLAUDE.md` as the single constitution. Made `docs/ARCHITECTURE.md` an index pointing to canonical `docs/architecture/current-architecture.md`.
**Rule**: Never create a new top-level AI context file. Extend `CLAUDE.md` or add rules to `.agent/rules/`. Architecture detail goes in `.agent/library/` (auto-synced to `docs/architecture/`).

## [ARCH] Multi-broker registry uses import-time side-effect registration (2026-03)

**Context**: Adding Fubon as a second broker required a broker factory registry. The pattern chosen is module-level auto-registration: each broker `__init__.py` registers itself as a side effect on import.
**Fix**: `feed_adapter/broker_registry.py` holds `_BROKER_REGISTRY` dict. Bootstrap imports broker packages before calling `get_broker_factory()`. If the broker SDK is missing, import silently skips; failure surfaces only at `get_broker_factory()` call time.
**Rule**: Always import broker packages before calling `get_broker_factory()`. Catch `ValueError` at the call site and emit a clear error. Set `HFT_BROKER` env var (default `"shioaji"`) to select the active broker.

## [GOTCHA] Fubon prices arrive as strings — never cast via float() (2026-03)

**Context**: Fubon SDK delivers prices as decimal strings (e.g., `"523.00"`). A naive `float(price_str) * 10000` introduces floating-point error, violating the Precision Law.
**Fix**: All Fubon price ingestion uses `int(Decimal(price_str) * 10000)`. Outgoing order prices use `_scaled_int_to_price_str()` helper for the reverse conversion.
**Rule**: The Precision Law applies at every broker boundary. Never use `float()` for price string conversion — use `Decimal(str)` then scale to int.

## [ARCH] NormalizerFieldMap enables broker-agnostic Rust fast paths (2026-03)

**Context**: The Rust normalizer hot path was hardcoded to Shioaji field names. Adding Fubon required parameterising field names without regressing Shioaji performance.
**Fix**: `NormalizerFieldMap` is a frozen dataclass with Shioaji defaults. When `_is_default_map=True`, the Rust fast path is taken unchanged. Custom maps (e.g., Fubon) fall through to Python field lookups.
**Rule**: Preserve `_is_default_map=True` for Shioaji configs to keep Rust fast paths active. Only set custom field maps for non-default brokers.

## [ARCH] All broker WebSocket callbacks must use call_soon_threadsafe (2026-03)

**Context**: Both Shioaji and Fubon WebSocket/callback handlers run in a broker-owned thread, not the asyncio event loop. Calling event loop APIs directly from that thread causes silent data races or crashes.
**Fix**: Every broker callback enqueues to the event loop via `loop.call_soon_threadsafe(handler, event)`. Protocol conformance (`isinstance(facade, MarketDataProvider)`) verifies the broker implements required interfaces. Use `runtime_checkable` protocols.
**Rule**: No broker callback may touch asyncio state directly. Always use `loop.call_soon_threadsafe()`. This pattern is identical for Shioaji and Fubon — keep it consistent when adding future brokers.

## [GOTCHA] fubon-neo SDK is not on PyPI — guard all imports (2026-03)

**Context**: `fubon-neo` requires a platform-specific `.whl` file and is unavailable via `pip install`. Unconditional imports break environments without the SDK (CI, dev machines without the file).
**Fix**: All Fubon modules gate on `try: import fubon_neo except ImportError: fubon_neo = None`. The broker silently skips registration; `get_broker_factory("fubon")` then raises `ValueError` with a clear message.
**Rule**: Guard every `import fubon_neo` with a `try/except ImportError`. Note: package name uses hyphen (`fubon-neo` in `pyproject.toml`) but import uses underscore (`fubon_neo`).
