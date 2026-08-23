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

## [BUG] Non-reentrant `threading.Lock` self-deadlocks when writers emit metrics via public reader API (2026-04)

**Context**: Wave 3 of debug-team concurrency fixes added `with self._fill_lock:` to `PositionStore.get_drawdown_pct` to snapshot the (peak, current) PnL pair atomically against writers. But `_on_fill_python` and `_on_fill_rust` both call `self.get_drawdown_pct()` to push the `portfolio_drawdown_pct` Prometheus gauge from inside their own `with self._fill_lock:` block. `threading.Lock` is non-reentrant → every fill timed out at 30s, breaking 13 unit tests.
**Fix**: Split into a private `_get_drawdown_pct_locked()` (assumes caller holds lock) and a public `get_drawdown_pct()` that acquires-then-delegates. Writer call sites switched to the `_locked` variant.
**Rule**: When promoting a public reader to acquire a non-reentrant lock, audit ALL writer call sites for self-recursion via metric emission, observability hooks, or any "tell me my own state" pattern. The locked/unlocked split is the canonical fix — don't reach for `RLock` (it hides reentrance bugs in adjacent code).
**Commits**: `f2126f96` (R3-6 introduced bug), `63cac7ec` (R3-6 hole fix).

## [ARCH] Slice A — Promotion Gate Hardening (2026-05)

**Context**: Gate C was advisory-only — every sub-gate (`SharpeThresholdGate`, `MaxDrawdownGate`, `WinningDayPctGate`, `FillQualityGate`, `FillRateValidationGate`, `ICEvaluationGate`) was registered but its result did not affect `passed`. R47-OE1 (39 fills over 31 days, 96.9% of PnL from a single day, 2026-04-02) passed Gate C and reached `R47_MAKER_TMF enabled: true` in production. Live result on 2026-04-21: −1,722 NTD vs +7,701 NTD instant-RTT backtest prediction.
**Fix**:
- Ship `config/research/profiles/vm_ul6_strict.yaml` carrying `is_strict: true` plus a `blocking_sub_gates: [...]` list of 13 gate names. `ValidationProfile` is a frozen dataclass loaded by `_validation_profile.load_profile()`; the loader rejects any name that is not registered.
- Add 7 small-sample sub-gates (`min_sample_size`, `single_day_dominance`, `loo_day_sensitivity`, `outlier_trade_removal`, `day_bootstrap_ci`, `stationary_block_bootstrap`, `deflated_sharpe_maker`) sharing the existing `SubGate` Protocol. R47-OE1 fingerprint fails on `min_sample_size`, `single_day_dominance`, `loo_day_sensitivity` (verified via integration test).
- Replace `_invoke_sub_gates_advisory()` with `_invoke_sub_gates(*, profile)` returning `(advisory, blocking)`. Aggregator ANDs `blocking["passed"]` into Gate C's `passed`. Loose path (`profile=None`) is bit-for-bit unchanged; backward-compat wrapper preserved.
- `promote_alpha()` raises `PromotionError("strict profile required for Gate D entry; got profile=...")` when `config.validation_profile is None or not is_strict`. Test fixtures inject a minimal strict profile.
**Rule**:
1. Any artifact entering Gate D MUST run with `--validation-profile vm_ul6_strict` (or another `is_strict: true` profile that lists at least the 7 small-sample gates as blocking). Loose `make research` runs do NOT need a profile.
2. When wiring a new aggregated sub-call into an existing Gate function, place the call BEFORE the `passed = ...` consumer line, not after. Tests that bypass the full Gate function (calling `_invoke_sub_gates` directly) will not catch the order-of-statements bug; ruff F821 will. (We hit this exact bug in Slice A `_gate_c.py`; unit/integration tests stayed green because they call `_invoke_sub_gates` directly, but `ruff check` flagged unbound `maker_blocking`/`taker_blocking`.)
3. Sub-gate `name` strings are snake_case (`min_sample_size`, not `MinSampleSize`); profile YAML must match the canonical `.name` attribute exactly.
**Commits**: `6f9ef772` (plan) → `69758563` (CI sweep). Branch `slice-a/promotion-gate-hardening`, 19 commits.

## [PROCESS] Gate exit codes must never be read through a pipe (2026-07)

**Context**: `bash scripts/check_git_preconditions.sh --narrow-commit | tail -2 && git commit ...` committed straight through a BLOCKED gate: `&&` saw `tail`'s exit 0, not the gate's exit 1. Same failure mode as the 2026-07-10 agent-docs gate incident (7d3b2475 shipped red) — the lesson existed in private memory and was still repeated.
**Fix**: Run the gate bare and branch on `$?`, or redirect to a log file (`gate ... > gate.log 2>&1; gate=$?`) when output must be captured. Undo any commit that slipped through (`git reset --soft HEAD^`) and re-run the gate before recommitting.
**Rule**: A gate's exit code is the gate. Any pipeline segment after it (tail, tee, grep) replaces that exit code under `&&`/`if` unless you use `PIPESTATUS[0]` — so don't pipe gates at all.

## [HARNESS] Claude Code hooks hot-load; agent defs do not (2026-07-14)

**Context**: v3 W1 hook probe. Planned "live-fire owed next session" like the
.claude/agents/ defs (which register at session start only) — but a probe hook
added to .claude/settings.json mid-session fired on the SAME session's next
tool call.
**Facts** (probe evidence, hook stdin JSON): subagent tool calls carry
`agent_type` (e.g. "hft-docs") + `agent_id`; main-session calls carry neither;
`session_id` is SHARED between a session and its subagents (session-identity
comparison cannot discriminate). `permission_mode`, `tool_use_id`, `effort`,
`prompt_id`, `transcript_path`, `cwd` also present.
**Rule**: settings.json hook changes are testable immediately in-session;
.claude/agents/ changes still need a fresh session. When a harness behavior
matters, probe it (log the event JSON) — don't assume from docs or analogy.

## [PROCESS] Enforcement code needs adversarial review more than feature code (2026-07-14)

**Context**: First hft-reviewer run on the W1 hooks commit returned
REQUEST-CHANGES with 8 findings, 2 HIGH: a text-scanning git guard that
false-denied any command MENTIONING git (it blocked the reviewer's own greps
3 times, live), and a scope guard that auto-allowed the guarded subagent to
rewrite the guard's own marker file. Author-written tests all passed — they
tested intent, not bypasses (marker self-rewrite, /tmp/claude-x/../ traversal,
prefix siblings).
**Rule**: (1) Guards parse command POSITIONS (shell segments), never scan
command text. (2) A guard's control files are excluded from what the guarded
party may touch — check the bypass direction explicitly. (3) Normalize paths
before any prefix-based allow. (4) Route every new guard/gate through
independent adversarial review before relying on it; its failure mode is
silent false confidence.

## [OPS] Production-host Tailscale node keys expire — disable expiry, or lose remote access with no fallback (2026-07-19)

**Context**: THESHOW (old host, production engine) became unreachable
mid-operations: local Windows Tailscale was logged out AND the old host's
node key had expired ("peer's node key has expired"). The old host is
Tailscale-ONLY — a full /24 LAN sweep found no SSH path, and on-site action
looked required. Disabling key expiry for the node in the admin console
revived it without re-auth (tailscaled was still running with its old key).
**Rule**: (1) Every production node gets "Disable key expiry" in the
Tailscale admin console the day it is provisioned — an expiry mid-incident
or mid-market is unrecoverable remotely. (2) When a peer is unreachable,
`tailscale ping` distinguishes peer-side key expiry from local auth issues;
check BOTH ends before assuming one. (3) Try admin-console disable-key-expiry
BEFORE traveling to the machine — it can revive an expired-but-running node.

## [OPS] `sed -i` on a single-file Docker bind mount silently detaches the container's view (2026-07-19)

**Context**: Patched `config/monitoring/prometheus.yml` (single-file bind
mount) on THESHOW with `sed -i`, then SIGHUP'd prometheus — reload "worked"
but the container still served the OLD config. `sed -i` writes a new file
and renames it, so the host path gets a NEW inode while the bind mount stays
attached to the old one; every later in-place write is invisible too.
**Rule**: After replacing a single-file bind-mounted file (sed -i, mv, rename
— anything that swaps the inode), a SIGHUP/reload is a no-op; the container
must be RESTARTED to re-resolve the mount. Verify by reading the config back
through the app's own API (e.g. Prometheus /api/v1/targets scrapeUrl), never
by reading the host file.

## [OPS] Agent shell `cd` into a dir without `.claude/hooks/` locks out ALL tools (2026-07-20)

**Context**: During disk cleanup the agent `cd`'d into
`.claude/worktrees/shioaji155-eval` to inspect it. This repo's hooks in
`.claude/settings.json` use cwd-relative paths (`python3
.claude/hooks/git_guard.py`), so from that cwd every hook failed to resolve
→ every Bash AND Write/Edit call was pre-blocked, including `cd` back.
Subagent rescue was classifier-denied; unblocking required Charlie manually
copying the hook scripts to the stuck cwd's relative path.
**Rule**: (1) Never `cd` the persistent shell away from repo root in this
repo — use `git -C` / absolute paths for everything. (2) If stuck anyway:
copy the four hook scripts to `<cwd>/.claude/hooks/` (guards keep enforcing;
only path resolution is fixed), cd back, remove the copies. (3) Long-term
fix: prefix hook commands with `$CLAUDE_PROJECT_DIR/` in settings.json.
(4) Related user-shell gotcha: a leading `!` pasted into a PLAIN bash shell
negates the first pipeline's exit code, so `! cmd && next` silently skips
`next` — the `!` prefix is Claude Code's input feature, not bash's.

## [RESEARCH] Two unrelated `sharpe_oos` fields exist in the repo — do not blind-rename (2026-07-23)

**Context**: `research/combinatorial/search_engine.py::SearchResult.sharpe_oos`
was a misleading name — that module has no train/test split, so the value
was selection-set data mislabeled as out-of-sample. Fixed by renaming it to
`selection_sharpe` (Phase 0 of the SubHFT Alpha Mining v2 spec). But
`research/backtest/types.py::BacktestResult.sharpe_oos` is a *different,
real* field — an actual OOS Sharpe from real IS/OOS-split backtests,
consumed by Gate C/D, promotion, canary, and paper-trade batching (~300
references across `src/hft_platform/alpha/*`, `research/factory.py`,
`research/registry/*`, ~40 test files). A repo-wide find-and-replace on
`sharpe_oos` would have silently corrupted the real gate metric.
**Rule**: Before any repo-wide rename of a common-sounding field name,
`rg` every call site and check the type/class it belongs to, not just the
string — two dataclasses (or a dataclass and a mock) can legitimately share
a field name with completely different meanings. Scope renames to the
specific class's construction/read sites, confirmed by grep, not a blanket
sed/replace_all.

## [OPS/FIXED] `make test` crashed with exit 70 at ~98% — root cause found and fixed: leaked `LoopStallWatchdog` threads from `test_system_service_behavior.py` (2026-07-23, root-caused + fixed 2026-07-24)

**Original symptom**: Full `make test` reproducibly crashed with `exit 70` at
~98% collection (~14089/14295 tests), always near
`tests/unit/test_wal_coverage.py`, with zero FAILED/ERROR before it. Isolated
runs of the apparently-crashing file always passed. The crash point drifted
by 1-2 tests between runs, which earlier investigation (2026-07-23,
2026-07-24 first pass) misread as evidence for a resource-accumulation
theory (fd/thread exhaustion from ~14K preceding tests) — plausible-sounding
but never actually confirmed, and wrong.

**Actual root cause**: `src/hft_platform/services/loop_watchdog.py` defines
`LoopStallWatchdog`, a real background daemon thread the production engine
(`HFTSystem.run()`, `services/system.py:574`) starts to force-`os._exit(70)`
the process if the event loop stops beating for `HFT_LOOP_STALL_KILL_S`
seconds (default 60, real wall clock, real `os._exit`) — a deliberate
production safety valve for the 2026-06-15 THESHOW stall incident.
`STALL_KILL_EXIT_CODE = 70` — the literal number in "Error 70" is this
constant, not a pytest/coverage/uv exit code.

Two tests in `tests/unit/test_system_service_behavior.py` —
`test_run_disabled_order_mode_skips_order_startup_work` and
`test_run_disabled_order_mode_never_starts_order_risk_strategy_plane` —
construct a real `HFTSystem` via `__new__` and call the real
`HFTSystem.run(sys_obj)`, which starts a real watchdog thread. Both tests
mock `sys_obj._supervise` to raise immediately (right after the watchdog
starts) **and** mock `sys_obj.stop_async` entirely, so the `finally` block's
cleanup never reaches the real `watchdog.stop()`. The result: every test run
that exercises this file leaks 1-2 live daemon threads with a genuine
60-second countdown to `os._exit(70)` — timing out and killing the *entire*
pytest process however many tests later happen to be running at that
wall-clock moment (which is why the "crash point" looked like it drifted
with test count, when it was actually pinned to elapsed wall-clock time
since this file ran). `os._exit()` skips buffer flushes, and the watchdog's
own diagnostic `sys.stderr.write()` lands inside pytest's per-test capture
buffer for whatever test is running at kill time (never flushed, since the
process dies before pytest's normal reporting runs) — this is why captured
stdout/stderr showed nothing but a bare exit code, no traceback, no warning.

**Confirmed empirically**: a throwaway `pytest_sessionfinish` hook checking
`threading.enumerate()` showed exactly 2 `loop-stall-watchdog` threads alive
after running `test_system_service_behavior.py` in isolation, attributable
to exactly those 2 tests (verified individually). `grep -rn
"LoopStallWatchdog(" src/ tests/` confirms `services/system.py:574` is the
only production construction site and there is no other leak source.

**Fix** (test-only, 2 lines): both tests now
`monkeypatch.setenv("HFT_LOOP_STALL_KILL_S", "0")` before calling
`HFTSystem.run(sys_obj)` — `stall_kill_s <= 0` makes `LoopStallWatchdog`
disabled (`start()` becomes a no-op), matching the precedent already used by
`test_startup_event_fixes.py::test_system_run_executes_startup_fill_backfill_before_recon`
for the identical hazard. Post-fix: `threading.enumerate()` shows zero
watchdog threads after the file runs, and a full `make test` completed
end-to-end in 17m54s (14281 passed, 19 skipped, 1 unrelated pre-existing
failure in `test_cli_extended_advanced.py::test_cmd_alpha_list` — a
`research/alphas/` discovery mismatch tied to already-dirty working-tree
state, not this fix).
**Rule**: any test that calls the real `HFTSystem.run()` (not just
`stop_async()`/`stop()` in isolation) MUST either mock `stop_async` with a
side effect that still calls `sys_obj._loop_watchdog.stop()`, or — simpler,
preferred — `monkeypatch.setenv("HFT_LOOP_STALL_KILL_S", "0")` before the
call. `grep -rn "HFTSystem\.run(" tests/` to find all call sites if adding a
new one; as of this fix there are 5 (4 in `test_system_service_behavior.py`,
1 in `test_startup_event_fixes.py`), all now safe.

## [ARCH] `research/candidate_loop/` is loop_v1 (FROZEN live registry) — SubHFT Alpha Mining v2 deliberately kept decoupled (2026-07-23)

**Context**: While designing Phase 1 ("Trial Ledger & Data Partition") of the
new "SubHFT Alpha Mining v2" spec, exploration found `research/candidate_loop/`
already has a mature day/symbol train/validation/test splitter
(`splits.py::DaySymbol`, `config/research/candidate_loop/split_definition_v1.yaml`,
pinned to one specific historical TXF dataset) and its own CH+jsonl result
writer (`ch_writer.py::ResultWriter`). This is almost certainly "loop_v1" —
the system CLAUDE.md calls FROZEN (`Live registry FROZEN under loop_v1 L11,
locked to r47_tmf_v1`). The Mining v2 spec text never mentions this module at
all.
**Rule**: Mining v2's new partitioning/ledger code
(`research/combinatorial/partitioning.py`, `research/combinatorial/ledger.py`)
takes a generic per-row session-id array and has zero imports from
`candidate_loop` — this was a deliberate, user-confirmed design choice, not an
oversight. Don't later "helpfully" merge the two systems or point Mining v2 at
`candidate_loop`'s DaySymbol/NPZ conventions — that would couple new
(actively-changing) mining infrastructure to the frozen live-registry system,
which is exactly the kind of change that needs to stay isolated per the
Alpha Governance section of CLAUDE.md.

## [ARCH] Mining v2 Phase 1 trial identity is a reduced, deliberately-overcounting formula (2026-07-23)

**Context**: The Mining v2 spec's full trial-identity formula (§7.2) is
`canonical-AST-hash : dataset-fingerprint : partition-manifest-hash :
target-definition : feature-schema-version : evaluator-profile : config`. No
typed AST / canonicalizer exists yet (that's Phase 2), so Phase 1's
`TrialLedger.trial_id_for()` (`research/combinatorial/ledger.py`) uses only
`sha256(normalized_expression_text : dataset_fingerprint :
partition_manifest_hash : algorithm)` — raw expression text, not a canonical
AST hash.
**Rule**: This is intentionally conservative, not a shortcut to fix later
under time pressure: semantically-identical-but-textually-different
expressions (e.g. `add(a,b)` vs `add(b,a)`) count as separate trials today,
which can only *overcount*, never *undercount*, the number of trials fed into
the (existing, Phase-0-hardened) Deflated Sharpe multiple-testing correction.
Don't mistake today's `trial_id_for()` output for the eventual Phase 2
semantics, and don't "simplify" it further — the overcounting-safety direction
only holds as long as the formula never *drops* a component that could
distinguish two truly-different trials.

**Update (2026-07-23, Phase 2 landed)**: `research/combinatorial/canonical_ast.py`
now exists and `TrialLedger.candidate_id_for`/`trial_id_for` use
`canonical_ast.canonical_hash()` (falling back to the old normalized-text hash
only when the expression is malformed) — the "reduced formula" described above
is now historical for well-formed expressions. See the entry below for what
Phase 2 actually built and a real bug it found along the way.

## [BUG] `evaluate_expression()`'s compile-failure try/except didn't cover `.evaluate()` — uncaught runtime TypeError on bad arity (2026-07-23, fixed in Phase 2)

**Context**: While building Mining v2 Phase 2 (typed AST / canonicalizer /
semantic dedup, `research/combinatorial/canonical_ast.py`), found that
`expression_lang.py`'s grammar validation (`_validate_tree`) checked operator
*names* against `OPERATORS` but never argument *count* — e.g. `ts_mean(x)`
(missing the required `window` arg) compiled successfully and only failed at
runtime as a raw Python `TypeError` inside `OPERATORS[name](*args)`. Worse,
`search_engine.py::evaluate_expression`'s try/except (which feeds
`TrialLedger.record_compile_failure`) only wrapped `compile_expression(...)`,
not the subsequent `compiled.evaluate(self.features)` call — so this kind of
error, or any other evaluate-time error (e.g. a `KeyError` for a feature name
not present in the engine's `features` dict, which also can't be caught at
compile time since it depends on what's passed to `.evaluate()`), would raise
**uncaught** out of `evaluate_expression()`, capable of crashing
`random_search`/`genetic_search` mid-loop instead of being recorded as a clean
ledger failure.
**Rule**: Fixed two ways, both now in place — (1) `compile_expression()` gained
a `type_check: bool = True` kwarg that calls `canonical_ast.to_typed_ast()`
for its arity-checking side effect, turning bad-arity expressions into a
compile-time `ValueError` (see `OPERATOR_ARITY` in `canonical_ast.py`); (2)
`evaluate_expression()`'s try/except was widened to also cover
`compiled.evaluate(...)`, so any remaining evaluate-time error (unknown
feature name, etc.) is still recorded and re-raised cleanly instead of
crashing uncaught. Don't narrow that try/except back to compile-only — the
whole point is that "compile succeeded" doesn't imply "evaluate will succeed."

## [ARCH] Mining v2 Phase 3 role-typed crossover closes an int(ndarray) crash hazard (2026-07-24)

**Context**: `search_engine.py::genetic_search()` was rewritten from
single-parent string-token mutation to real tournament selection + typed
subtree crossover + elitism. Every `ts_*`/`decay_linear`/`zscore` operator in
`operator_library.py` does `int(window)` on its last positional arg at
runtime; grammar/arity checks (`OPERATOR_ARITY` in `canonical_ast.py`) verify
operator name and argument *count* only, never argument *kind*. A naive
(untyped) subtree-swap crossover could therefore construct e.g.
`ts_mean(x, ts_sum(y, 5))` — swapping a nested `Call`/`Name` subtree into a
window-arg slot — which compiles fine (grammar has no opinion on it) and then
raises an uncaught `TypeError: only length-1 arrays can be converted to
Python scalars` mid-search-loop. Nothing before Phase 3 could produce this:
manual templates and the legacy string-token mutation are role-safe by
construction (digit tokens only ever swap with digit tokens).
**Rule**: `canonical_ast.OPERATOR_ARG_ROLES` classifies every operator's
positional args as `"signal"` (array-valued, any subtree) or `"window"` (must
stay a scalar constant); `canonical_ast.crossover()` only swaps same-role
subtrees, so a window arg can never receive a non-constant donor. **If you add
a new operator to `OPERATOR_ARITY`/`OPERATORS`, you must also add it to
`OPERATOR_ARG_ROLES`** — `_collect_role_positions()` raises `ValueError` on a
missing entry rather than silently defaulting a role, but that only helps if
someone notices the crossover path exercising the new operator at all.
Crossover always operates on **freshly-parsed** trees (`to_typed_ast()`
called directly, never `canonicalize()`'s output) — `canonicalize()` rebuilds
`OpNode` wrappers for commutative-operator operand sorting, which would break
the `is`-identity-based node selection `crossover()`/`_replace_by_identity()`
depend on to disambiguate structurally-equal duplicate subtrees (e.g.
`add(x, x)`).

## [BUG] `_mutate_expression` always fell back to a fresh random expression — parens/commas never restored after tokenization (found + fixed 2026-07-24 during Phase 3)

**Context**: While building Phase 3's deterministic `parent_ids` test
(`tests/unit/test_combinatorial_search.py`), direct probing showed
`AlphaSearchEngine._mutate_expression()` failed on **every** call, 100% of the
time, across all seeds tried. Root cause: it tokenized via
`expression.replace("(", " ").replace(")", " ").replace(",", " ").split()` —
this discarded `(`, `)`, `,` as characters entirely (turned them into spaces,
then `.split()` dropped the resulting empty fields), so `tokens`/`out` never
contained those characters at all. The subsequent cleanup line
(`rebuilt.replace(" ,", ",").replace("( ", "(").replace(" )", ")")`) was a
no-op since none of those substrings could ever occur in `rebuilt` — there was
no step that reinserted parens/commas. `compile_expression(rebuilt)` therefore
always raised `SyntaxError` (e.g. `"sign(ts_delta(ofi, 3))"` mutated to the
unparenthesized `"sign ts_delta ofi 3"`), and the method's own except-branch
silently called `self._random_expression()` instead — meaning **every call
site that believed it was using mutation (the legacy `genetic_search` in
Phases 0-2, and the new tournament-selected mutation branch in Phase 3) had
always actually been doing plain random reinitialization**, undetected
because no existing test asserted anything about mutation's actual output,
only about `genetic_search`'s overall tagging/logging behavior. Predates
Phase 3; found only as a side effect of the deterministic `parent_ids` test's
probing.
**Rule**: Fixed by tokenizing with `_MUTATION_TOKEN_RE =
re.compile(r"\(|\)|,|[^\s(),]+")` instead of stripping — `(`, `)`, `,` each
become their own token so they survive into the rebuilt string; `" ".join`
with extra whitespace around them is valid Python syntax (the tokenizer
doesn't care), so the old brittle `.replace(" ,", ",")...` cleanup was deleted
outright rather than patched. Regression test:
`test_mutate_expression_produces_genuine_mutations_not_a_silent_random_fallback`
asserts `_mutation_failures == 0` and that at least one of 30 mutations of a
fixed parent actually differs from it. If you touch this function again,
don't reintroduce a tokenizer that discards syntax-load-bearing characters —
verify with a direct call + `compile_expression()` round-trip, not just via
`genetic_search()`'s tagging, which this bug proved insufficient to catch.

## [ARCH] GP streaming-adapter buffer sizing must be additive across nested windows, not max() — and two operators can't be streamed at all (2026-07-24, Phase 4)

**Context**: Phase 4 (`research/combinatorial/gp_alpha_adapter.py`) bridges
`CompiledExpression.evaluate()` (batch: whole-array in, whole-array out — how
the search engine scores/selects candidates) into `AlphaProtocol.update()`
(streaming: one tick in, one float out — how the real pipeline runs a
signal, via `alpha_strategy_bridge.py`'s `self._alpha.update(**payload)`).
The approved plan's original design sized each variable's rolling buffer as
"the largest window constant found anywhere in the expression"
(`max(w1, w2, ...)`). Hand-verification before writing any code caught two
separate, independent correctness bugs in that design:

1. **Nested windowed operators need additive, not max, buffer sizing.** For
   `zscore(ts_delta(x, w1), w2)`, `max(w1, w2)` under-sizes the buffer —
   `zscore` needs its last `w2` `ts_delta` outputs correct, and *each of
   those* itself needs an `x` value `w1` samples further back, so the true
   requirement is `w1 + w2 - 1` trailing `x` samples. Verified numerically:
   `x=[0,0,0,0,100,0,0,0,10]`, `w1=2, w2=3` — the correct (batch) output at
   the last index is `≈0.805`; a buffer sized `max(2,3)=3` silently produces
   `≈1.414` instead, a wrong answer with no error raised. Fixed via
   `gp_alpha_adapter._required_history()`, a recursive walk (reusing Phase
   3's `OPERATOR_ARG_ROLES`) that accumulates required trailing history
   additively down the tree, per variable.
2. **`ts_delta` needs `window`, not `window - 1`, extra history** — unlike
   every other windowed operator (`ts_mean`/`ts_std`/`ts_sum`/`ts_rank`/
   `decay_linear`/`ts_corr`/windowed `zscore`), which are true rolling
   windows (`arr[i-w+1:i+1]`, needing `w-1` extra trailing samples per
   output), `ts_delta[i] = arr[i] - arr[i-w]` is a fixed-offset difference
   that needs the sample exactly `w` bars back, one more than the
   rolling-window formula. `gp_alpha_adapter._extra_history()` special-cases
   this (`_OFFSET_WINDOW_OPS = {"ts_delta"}`) rather than applying one
   uniform formula to every operator with a window arg.
3. **`rank(x)` and 1-arg `zscore(x)` cannot be streamed via any bounded
   buffer at all** — `operator_library.rank` computes a whole-array
   cross-sectional percentile via a single `np.argsort` over the *entire*
   input; `target[i]` depends on every other element, including ones after
   `i`. 1-arg `zscore` (window=None) normalizes against the whole-array
   mean/std, same look-ahead shape. Both are grammar-valid
   (`OPERATOR_ARITY["zscore"] = (1, 2)`), and the search engine's own
   `_random_expression` "volume" family generates bare `rank(...)` by
   default — not a hypothetical edge case.

**Rule**: `max_window_for_expression()` raises `ValueError` for expressions
using `rank(...)` or 1-arg `zscore(...)` (checked before any buffer-sizing
work), and computes every other expression's required buffer length via the
additive/per-operator-formula walk above — never `max()` of window
constants. **If you add a new windowed operator to `operator_library.py`,
you must classify it correctly here too**: default assumption is "true
rolling window" (`extra = window - 1`); only add it to
`_OFFSET_WINDOW_OPS` if, like `ts_delta`, it reads a *fixed offset* rather
than averaging/reducing over a trailing span. If you add a new *whole-array*
operator (reads beyond a bounded trailing window, e.g. any future
cross-sectional/full-series primitive), add it to
`_WHOLE_ARRAY_LOOKAHEAD_OPS` — do not let it silently fall through to the
windowed-buffer path, since no finite buffer can reproduce it correctly.
Streaming/batch equivalence is verified directly, index-for-index, in
`tests/unit/test_combinatorial_gp_alpha_adapter.py` — treat any change to
`operator_library.py`'s per-index read pattern as requiring a re-check of
that test, not just a re-run of it.

## [ARCH] Phase 4 pipeline integration deliberately leaves `TrialLedger`'s schema and `research/candidate_loop/` untouched (2026-07-24)

**Context**: Phase 4 needed lineage (parent candidate ids, discovery score)
for a promoted GP candidate's README/manifest provenance block.
`SearchResult.metadata` (`search_engine.py`) doesn't carry `candidate_id`/
`parent_ids`; only `TrialLedger` rows do, keyed by a semantic-identity hash
(`TrialLedger.candidate_id_for()`) — the ledger never stores raw expression
text. It was tempting to "fix" this by adding an expression-text field or an
index to the ledger schema. Separately, `research/candidate_loop/` (a
different, already-shipped, LLM/JSONL-based candidate system) has the exact
same unsolved "no Gate A bridge" problem Phase 4 solves for
`research/combinatorial` — and its own spec
(`docs/research/alpha_candidate_loop_v1_spec.md` §18) explicitly defers that
integration as a v1 non-goal.
**Rule**: Phase 4 does neither. Lineage lookup in `promote.py` recomputes
`TrialLedger.candidate_id_for(expression)` and filters `read_trials()` for a
matching row — a pure additive read, empty-safe (returns `None`/`()`
fields, not an error) when no matching row exists (e.g. a hand-typed
`--expression` never run through the search engine). `research/combinatorial/
partitioning.py:15-17` documents `combinatorial` and `candidate_loop` as
*deliberately* decoupled systems, not an oversight — Phase 4 does not touch
`candidate_loop` at all. Don't unify these without an explicit ask; the
decoupling is a documented design choice, not tech debt.

## [BUG] When you fix a defect in one implementation of a pattern, grep for the others (2026-08-22, PRs #445/#446)

`OrderAdapter.order_id_map` and `ExecutionRouter.fill_dedup` are the same
checkpoint pattern written twice: snapshot state, write to a temp file, fsync,
rename, throttled by an interval. Three defects were found in the fill-dedup
copy, and **two of them had already been found and fixed in the order_id_map
copy** — the fix was never carried across, and nothing in either file pointed
at the other.

1. `fsync` running inline on the event loop from `ExecutionRouter.run()`
   (Law 3). The twin had been offloaded to the executor for exactly this
   reason.
2. Leading-edge-only throttling with no trailing flush: the last event before a
   crash reached disk only if another event happened to arrive after it. Same
   defect, same prior fix on the twin.
3. A throttle keyed on `timebase.now_ns()` (wall clock) under a comment that
   said `# noqa: monotonic timestamp`. An NTP step backwards stalls
   checkpointing for the length of the step; a restart inside that window
   re-applies fills that were already applied — double-counted positions.

Both fixes also had to add a completion signal (`flush_order_id_map()` /
`flush_fill_dedup()`): the original code dropped the executor future on the
floor, so a caller could only *guess* how long a write took. The "flaky"
integration test that started this whole arc was asserting `os.path.exists()`
immediately after a write it had no way to wait for — the test was racing
production's missing signal, not a scheduler.

**How to apply:** a durability assertion that has to guess a duration is
evidence the write has no completion signal. And before closing a checkpoint
defect, `rg` for the other implementations of the same pattern and check each
one for the same three failure modes.

## [PROCESS] A `# noqa` can be load-bearing, and a comment is a worse place for a unit than the name (2026-08-22, PR #446)

`_fill_dedup_last_persist_s: float = 0.0  # noqa: monotonic timestamp` passed
CI's "Detect float in financial paths" gate *only* because of the comment —
and the same comment was describing the code incorrectly, since the field was
being set from a wall clock. Deleting the wrong comment turned the lint job
red.

The fix was to rename the field `_fill_dedup_last_persist_seconds`, which the
gate's allowlist matches on the `_seconds` suffix. The unit now lives in the
identifier, where it cannot drift away from the code, instead of in a comment
nobody is obliged to keep true.

Second trap in the same change: `0.0` is a sane "long ago" sentinel under a
wall clock (1970) and a **bug** under `time.monotonic()`, which counts from
boot — on a host whose uptime is shorter than the configured interval, the very
first checkpoint is throttled away. Use `float("-inf")`. CI caught this;
the local run could not, because the dev box's uptime exceeded the interval.

## [PROCESS] A working-tree review reports the diff of whatever branch it is standing on (2026-08-23)

Two automated review passes in two days were both distorted by the checkout
they ran against, in opposite directions:

- 2026-08-22: the tree was parked 82 commits behind `origin/main`, and **all 5
  findings** were defects that had already been fixed upstream.
- 2026-08-23: the tree was on `refactor/boundary-extractions-b2`, which held
  `pip 26.1.2` while `origin/main` held `26.2.1`. The reviewer read the diff
  backwards and reported the **merged security bump** (#438, PYSEC-2026-3721)
  as unapproved dependency drift. Acting on that advice would have reverted the
  fix.

The second failure is worse than the first: a stale checkout does not only
manufacture already-closed findings, it can invert the sign of a real change.

**How to apply:** before accepting any "this change introduces X" from a
working-tree review, run `git rev-list --left-right --count origin/main...HEAD`
and read the specific hunk against `origin/main` yourself. Also note what a
clean review does *not* prove: the same day's cloud review returned `[]` while
never seeing the focus note it was launched with.

## [RESEARCH] Gitignoring a regenerable tree removes it from every staleness check (2026-08-23)

`research/combinatorial/results/` was gitignored in PR #435 because it dirtied
every `git status`. Every file in it is dated **2026-07-25**. The
self-correlation guard (`has_self_correlation`, `search_engine.py:30`) landed
**2026-08-19** in `6ce84168`.

So `WINNER_DAILY.json` still sits on disk promoting
`sign(ts_corr(mid, mid, 50))` — a tautology, +1 in every window where `mid`
varies, hence permanently long — at `day_sharpe` 13.4, while its own
`tick_selection_sharpe` is 0.0025. `WINNER_BARS` and `WINNER_OOS` are clean.
Self-correlated expressions also survive in `daily_hunt_qualifying.json` and
`oos_hunt_qualifying.json`.

Nothing was ever going to catch this: no diff, no CI gate, no `git status`, and
the artifact carries no record of which generator version produced it.

**How to apply:** a fix that invalidates previously-emitted artifacts is not
done until those artifacts are regenerated or quarantined — say which, in the
PR. Stamp the generator commit into every emitted artifact. And when a note
says an artifact set is "clean", name the defect it is clean *of*: this set was
clean of the reset leak and simultaneously tautological.
