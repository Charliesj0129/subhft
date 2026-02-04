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
