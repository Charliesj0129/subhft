# Shioaji 1.5.6 Quote-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a fully tested Shioaji 1.5.6 quote-only deployment candidate that subscribes all configured symbols while making broker order submission unreachable.

**Architecture:** Bootstrap treats `HFT_ORDER_MODE=disabled` as a first-class fail-closed mode, creates quote clients with CA disabled, and supplies a no-op order client. System startup exposes health before broker login, skips order-specific startup work when disabled, and refuses to continue after a failed live order login. Health readiness derives required broker/task checks from the order mode.

**Tech Stack:** Python 3.12, asyncio, pytest, uv, Shioaji 1.5.6 Rust/ABI3 wheel, Docker candidate packaging.

---

### Task 1: Capture the 1.5.6 SDK surface

**Files:**
- Create: `tests/golden/shioaji_sdk/surface_1.5.6.json`
- Create: `tests/golden/shioaji_sdk/diff_1.5.5_to_1.5.6.json`
- Modify: `docs/runbooks/shioaji-version-diff.md`

- [x] Bootstrap the isolated 1.5.6 harness under `/tmp` and confirm repo hashes do not change.
- [x] Capture the 1.5.6 surface using the existing structured capture tool.
- [x] Generate and review the 1.5.5-to-1.5.6 diff against official release notes.
- [x] Run the real-wheel Phase 1 adapter and performance harness.

### Task 2: Add order-mode semantics with TDD

**Files:**
- Modify: `src/hft_platform/services/bootstrap.py`
- Modify: `tests/unit/test_bootstrap_order_mode_guard.py`
- Modify: `tests/unit/test_bootstrap_broker_selection.py`

- [x] Add failing tests for `disabled`, unknown values, no order facade, and CA-disabled market-data facades.
- [x] Run focused tests and confirm failures are caused by missing behavior.
- [x] Implement canonical mode parsing and quote/order client construction.
- [x] Re-run focused tests to green.

### Task 3: Harden startup with TDD

**Files:**
- Modify: `src/hft_platform/services/system.py`
- Modify: `tests/unit/test_system_service_behavior.py`

- [x] Add failing tests proving health starts before login, disabled mode skips order login/callback/recovery, and failed live login stops startup.
- [x] Confirm each test fails for the intended reason.
- [x] Implement the minimal startup branching and remove duplicate health startup.
- [x] Re-run focused tests and perform a break-probe.

### Task 4: Make readiness mode-aware with TDD

**Files:**
- Modify: `src/hft_platform/observability/health.py`
- Modify: `tests/unit/test_health_endpoint.py`

- [x] Add a failing quote-only readiness test requiring market data and recorder/order task health without requiring order broker login.
- [x] Implement mode-aware broker and order-path checks.
- [x] Re-run health tests and the break-probe.

### Task 5: Enforce CA isolation inside the quote pool

**Files:**
- Modify: `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`
- Modify: `tests/unit/test_quote_connection_pool.py`

- [x] Add a failing test proving every facade receives `activate_ca=False` despite a true base setting.
- [x] Override the per-facade config and retain current shard behavior.
- [x] Run quote-pool tests and static type checking for the changed file.

### Task 6: Move the dependency pin

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [x] Apply the approved Shioaji 1.5.6 pin and minimum security fixes.
- [x] Regenerate the lock with uv and inspect all transitive changes.
- [x] Sync the environment and assert `shioaji.__version__ == "1.5.6"`.
- [x] Run `pip-audit`/the repo security gate and report any unresolved advisory.

### Task 7: Candidate verification and review

**Files:**
- Review every changed path; no additional production files without a new risk check.

- [x] Run all focused bootstrap/system/health/quote-pool tests.
- [x] Run adapter protocol tests and `make shioaji-guard`.
- [x] Run break-probes for startup and quote-only safety behavior.
- [x] Run `make check`.
- [x] Run full `make ci`.
- [x] Perform Tier-3 adversarial diff review and resolve confirmed findings.
- [x] Package a deployment candidate with hashes; do not deploy or restart.
