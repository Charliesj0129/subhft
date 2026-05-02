# Agent Config Best-Practices Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trim every-turn context loads (CLAUDE.md, `.agent/rules/`, global `~/.claude/rules/common/`) and move reference content to on-demand skills, per Claude Code's "積極管理 context" best-practice.

**Architecture:** Five sequential sub-projects, each landing as its own commit for bisect/revert. On-demand reference content goes to `.claude/skills/<name>/SKILL.md`. Reversibility via `pre-agent-config-cleanup-2026-04-19` tag + `_archive-2026-04-19/` subfolders.

**Tech Stack:** Markdown, shell (git, grep, wc), no code changes.

**Spec:** `docs/superpowers/specs/2026-04-19-agent-config-best-practices-alignment-design.md`

**Note on worktree:** This plan edits both project files AND user-home files (`~/.claude/rules/common/`). A git worktree can't isolate the user-home edits, so execute on `main`. The baseline tag + per-SP commits are the rollback primitive.

---

## Task 0: Baseline & verification

### Task 0.1: Create baseline tag

**Files:** (none — tagging only)

- [ ] **Step 1: Create the baseline git tag**

Run:
```bash
cd /home/charlie/hft_platform
git tag pre-agent-config-cleanup-2026-04-19 -m "Baseline before agent config cleanup (best-practices alignment)"
git tag --list pre-agent-config-cleanup-2026-04-19
```

Expected output: `pre-agent-config-cleanup-2026-04-19`

- [ ] **Step 2: Record baseline line counts**

Run:
```bash
cd /home/charlie/hft_platform
echo "=== CLAUDE.md ===" && wc -l CLAUDE.md
echo "=== .agent/rules/ ===" && wc -l .agent/rules/*.md | tail -1
echo "=== ~/.claude/rules/common/ ===" && wc -l ~/.claude/rules/common/*.md | tail -1
echo "=== .agent/skills/ count ===" && ls .agent/skills/ | wc -l
echo "=== .agent/agents/ count ===" && ls .agent/agents/ | wc -l
echo "=== .agent/commands/ count ===" && ls .agent/commands/ | wc -l
```

Save this output — it's the "before" snapshot referenced in commits.

### Task 0.2: Verify `.claude/skills/` auto-discovery

**Files:**
- Create: `/home/charlie/hft_platform/.claude/skills/hft-config-probe/SKILL.md`

- [ ] **Step 1: Create probe skill**

Create `/home/charlie/hft_platform/.claude/skills/hft-config-probe/SKILL.md`:

```markdown
---
name: hft-config-probe
description: Probe skill to verify .claude/skills/ auto-discovery. Safe to delete after verification.
---

# HFT Config Probe

This is a verification skill created during agent-config cleanup (SP0). If this skill appears in the session's available-skills list after a Claude Code restart, then `.claude/skills/` auto-discovery works and SP1 extraction can proceed as designed.

Safe to delete after verification.
```

Run:
```bash
mkdir -p /home/charlie/hft_platform/.claude/skills/hft-config-probe
```

- [ ] **Step 2: Commit probe**

Run:
```bash
cd /home/charlie/hft_platform
git add .claude/skills/hft-config-probe/SKILL.md
git commit -m "chore(agent-config): add .claude/skills/ auto-discovery probe (SP0)"
```

- [ ] **Step 3: Manual verification gate (user action)**

**STOP and ask the user to start a fresh Claude Code session in this repo. Check the system-reminder's "available skills" list in the new session for `hft-config-probe`.**

- If `hft-config-probe` appears → `.claude/skills/` auto-discovery works. Proceed with SP1 as designed (extract tables to `.claude/skills/<name>/`).
- If `hft-config-probe` does NOT appear → Fallback path: skip `.claude/skills/` extraction, instead store extracted content at `.agent/library/` and use `@path` imports in CLAUDE.md. Note the fallback in each affected SP task.

**Record the outcome in the commit message of Task 1.1 by adding `[probe: PASS]` or `[probe: FAIL]`.**

- [ ] **Step 4: Delete probe after verification**

Run:
```bash
cd /home/charlie/hft_platform
rm -rf .claude/skills/hft-config-probe
git add -A .claude/skills/
git commit -m "chore(agent-config): remove .claude/skills/ probe after verification"
```

---

## Sub-Project 1 (SP1) — CLAUDE.md slim-down

**Goal:** 261 → ≤120 lines. Extract reference tables to on-demand skills.

### Task 1.1: Extract Rust exports table to skill

**Files:**
- Create: `/home/charlie/hft_platform/.claude/skills/hft-rust-exports/SKILL.md`
- Modify: `/home/charlie/hft_platform/CLAUDE.md` (remove lines 154–180)

> **If probe FAILED:** create at `.agent/library/rust-exports-reference.md` instead and add `- **Rust exports reference**: @.agent/library/rust-exports-reference.md` to CLAUDE.md's Architecture Quick Reference section.

- [ ] **Step 1: Create the skill file**

Create `/home/charlie/hft_platform/.claude/skills/hft-rust-exports/SKILL.md`:

```markdown
---
name: hft-rust-exports
description: Reference table of Rust (`rust_core` via PyO3) exports — ring buffers, LOB scaling, normalizers, risk validators, alpha kernels, feature engines, ClickHouse mappers, shared memory primitives. Consult when invoking or reviewing Rust-backed Python APIs in the HFT platform.
---

# Rust Boundary (`rust_core` via PyO3)

Compiled extension at `src/hft_platform/rust_core.cpython-*.so`.

| Export                                                                                                                                                               | Purpose                                        |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `FastRingBuffer`, `EventBus`, `FastTickRingBuffer`, `FastBidAskRingBuffer`, `FastLOBStatsRingBuffer`                                                                 | Lock-free event routing / typed ring buffers   |
| `scale_book`, `scale_book_seq`, `scale_book_pair`, `scale_book_pair_stats`, `scale_book_pair_stats_np`, `compute_book_stats`, `get_field`                            | LOB scaling and book stats hot path            |
| `normalize_tick_tuple`, `normalize_bidask_tuple`, `normalize_bidask_tuple_np`, `normalize_bidask_tuple_with_synth`, `normalize_tick_v2`, `normalize_bidask_v2`       | Tick/BidAsk normalization (Python + v2 paths)  |
| `LimitOrderBook`                                                                                                                                                     | Full limit order book state                    |
| `RustBookState`                                                                                                                                                      | Lightweight LOB snapshot state                 |
| `RustPositionTracker`                                                                                                                                                | O(1) position accounting                       |
| `FastGate`, `RustRiskValidator`, `RustExposureStore`, `RustCircuitBreaker`, `RustStormGuardValidator`                                                                | Risk gate, validator, exposure tracking, breaker, storm guard |
| `RustDedupStore`                                                                                                                                                     | Idempotency / order deduplication              |
| `LobFeatureKernelV1`, `RustFeaturePipelineV1`, `RustFeatureEngineV2`                                                                                                | LOB feature kernels and feature engine         |
| `AlphaDepthSlope`, `AlphaOFI`, `AlphaRegimePressure`, `AlphaRegimeReversal`, `AlphaTransientReprice`, `AlphaMarkovTransition`, `MatchedFilterTradeFlow`, `MetaAlpha` | Alpha signal generators                        |
| `AlphaStrategy`                                                                                                                                                      | Rust-native strategy executor                  |
| `RustColumnarBuffer`                                                                                                                                                 | Columnar data buffer for batch recording       |
| `RustMetricsSampler`                                                                                                                                                 | Low-overhead Prometheus metrics sampler        |
| `to_ch_price_scaled`, `map_tick_record`, `map_bidask_record`, `map_order_record`, `map_fill_record`                                                                  | ClickHouse record mapping                      |
| `coerce_ns_int`, `coerce_ns_float`                                                                                                                                   | Timestamp coercion utilities                   |
| `ShmRingBuffer`, `ShmSnapshotTable`                                                                                                                                  | Shared memory IPC and snapshot table           |
| `SymbolInternTable` *(Wave 4)*                                                                                                                                       | Symbol string interning (O(1) lookup)          |
| `FastTypedRingBuffer` *(Wave 4)*                                                                                                                                     | Typed, cache-friendly ring buffer              |
| `RustGatewayFusedCheck` *(Wave 4)*                                                                                                                                   | Fused gateway risk check (zero-copy)           |
| `RustNormalizerLobFused` *(Wave 4)*                                                                                                                                  | Fused normalizer + LOB pipeline                |
| `RustNormalizerFeatureFusedV1` *(Wave 4)*                                                                                                                            | Fused normalizer + LOB + feature pipeline      |
```

- [ ] **Step 2: Remove Rust exports section from CLAUDE.md**

Delete lines 154–180 (the entire `## 🦀 Rust Boundary` section) from `/home/charlie/hft_platform/CLAUDE.md`.

Replace with this single line in the "Architecture Quick Reference" list (after the existing `- **Detailed governance rules**` bullet):

```
- **Rust exports reference**: `.claude/skills/hft-rust-exports/` (on-demand skill)
```

- [ ] **Step 3: Verify line count reduction**

Run:
```bash
cd /home/charlie/hft_platform
wc -l CLAUDE.md
```

Expected: CLAUDE.md is roughly 235 lines (261 - 27 + 1 new reference line).

- [ ] **Step 4: Commit**

Run:
```bash
cd /home/charlie/hft_platform
git add .claude/skills/hft-rust-exports/SKILL.md CLAUDE.md
git commit -m "refactor(claude-md): extract Rust exports table to hft-rust-exports skill [probe: <PASS|FAIL from Task 0.2>]"
```

### Task 1.2: Extract env-vars table to skill

**Files:**
- Create: `/home/charlie/hft_platform/.claude/skills/hft-env-vars/SKILL.md`
- Modify: `/home/charlie/hft_platform/CLAUDE.md` (trim the `## 🌐 Critical Environment Variables` section)

> **If probe FAILED:** use `.agent/library/env-vars-reference.md` instead.

- [ ] **Step 1: Create the skill file**

Create `/home/charlie/hft_platform/.claude/skills/hft-env-vars/SKILL.md`:

```markdown
---
name: hft-env-vars
description: Complete reference table of HFT platform environment variables — runtime mode, broker selection, ClickHouse, monitor, reconnect, storm guard, backup, startup reconciliation, Telegram. Consult when configuring a runtime, setting up a new deployment, or debugging why a feature isn't activating.
---

# HFT Platform Environment Variables (Complete Reference)

> For the everyday-essential short list (`HFT_MODE`, `HFT_ORDER_MODE`, `HFT_STRICT_PRICE_MODE`, `HFT_BROKER`), see CLAUDE.md. The full table below is for deep configuration.

| Variable                   | Default     | Purpose                                   |
| -------------------------- | ----------- | ----------------------------------------- |
| `HFT_MODE`                 | `sim`       | Runtime mode: `sim` / `real` / `replay`   |
| `HFT_ORDER_MODE`           | `sim`       | Order execution: `sim` / `live` (LIVE = real money) |
| `HFT_SYMBOLS`              | —           | Comma-separated symbol list override      |
| `HFT_QUOTE_VERSION`        | `auto`      | Shioaji quote protocol version            |
| `HFT_STRICT_PRICE_MODE`    | `0`         | `1` = reject float prices with TypeError  |
| `HFT_GATEWAY_ENABLED`      | `0`         | `1` = enable CE-M2 order/risk gateway     |
| `HFT_RECORDER_MODE`        | `direct`    | `wal_first` = WAL-only write path (CE-M3) |
| `HFT_CLICKHOUSE_HOST`      | `localhost` | ClickHouse host                           |
| `HFT_EXPOSURE_MAX_SYMBOLS` | `10000`     | ExposureStore cardinality bound           |
| `HFT_BROKER`               | `shioaji`   | Broker backend: `shioaji` / `fubon`       |
| `HFT_FEATURE_ENGINE_ENABLED` | `1`         | `0` = disable FeatureEngine in runtime pipeline (default: v3 with 27 features) |
| `HFT_FUSED_NORMALIZER`     | `0`         | `1` = enable fused Rust normalizer+LOB pipeline |
| `HFT_FEATURE_ENGINE_BACKEND` | `python`  | Backend for FeatureEngine: `python` / `rust`    |
| `HFT_FUBON_CERT_PATH`      | —           | Fubon API certificate file path           |
| `HFT_FUBON_ACCOUNT`        | —           | Fubon trading account ID                  |
| `HFT_FUBON_PASSWORD`       | —           | Fubon account password (use secret mgr)   |
| `HFT_MONITOR_SOURCE`       | `clickhouse`| Monitor data source: `clickhouse`/`redis`/`hybrid` |
| `HFT_MONITOR_LIVE_ENABLED` | `0`         | `1` = enable Redis live publisher in MarketDataService |
| `HFT_MONITOR_REDIS_HOST`   | `localhost` | Redis host for monitor live cache         |
| `HFT_MONITOR_REDIS_PORT`   | `6379`      | Redis port for monitor live cache         |
| `HFT_MONITOR_REDIS_PASSWORD`| —          | Redis password for monitor live cache     |
| `HFT_MONITOR_DATA_SOURCE`  | `auto`      | Data source layer: `ch`/`shm`/`auto`     |
| `HFT_RECONNECT_HOURS`     | `08:30-13:35`| Trading hours window for auto-reconnect  |
| `HFT_RECONNECT_HOURS_2`   | —           | Secondary trading hours window            |
| `HFT_RECONNECT_COOLDOWN`  | `60`        | Reconnect cooldown seconds                |
| `HFT_RECONNECT_BACKOFF_S` | `5`         | Initial reconnect backoff delay seconds   |
| `HFT_RECONNECT_BACKOFF_MAX_S`| `120`    | Maximum reconnect backoff delay seconds   |
| `HFT_QUOTE_FLAP_THRESHOLD`| `5`         | Quote flap detection: max flaps in window |
| `HFT_QUOTE_FLAP_WINDOW_S` | `60`        | Quote flap detection window seconds       |
| `HFT_QUOTE_FLAP_COOLDOWN_S`| `300`      | Quote flap cooldown before re-subscribe   |
| `HFT_STORMGUARD_FEED_GAP_STORM_S`| `1.0` | Feed gap threshold (seconds) to trigger STORM. Feed gap alone cannot trigger HALT. |
| `HFT_STORMGUARD_FEED_GAP_HALT_S`| `30`  | **Deprecated** alias for `_STORM_S`. Maps to STORM (not HALT). |
| `HFT_BACKUP_ENABLED`        | `0`                    | `1` = enable automated daily ClickHouse backup |
| `HFT_BACKUP_RETAIN_DAYS`    | `30`                   | Number of daily backups to retain               |
| `CH_BACKUP_PATH`            | `./backups/clickhouse`  | Host path for ClickHouse backup volume mount    |
| `HFT_STARTUP_RECON_ENABLED`              | `1`   | Enable startup position recovery            |
| `HFT_STARTUP_RECON_QTY_THRESHOLD`        | `10`  | Stock discrepancy auto-correct threshold    |
| `HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD`| `2`   | Futures discrepancy auto-correct threshold  |
| `HFT_CHECKPOINT_ENABLED`                 | `1`   | Enable periodic position checkpoint writing |
| `HFT_ORDER_SHADOW_MODE`  | `0`         | `1` = shadow order interception (orders never reach broker) |
| `HFT_RECONNECT_DAYS`    | `mon,tue,wed,thu,fri` | Weekdays for auto-reconnect              |
| `HFT_RECONNECT_TZ`      | `Asia/Taipei`| Timezone for reconnect hours             |
| `HFT_ARCHIVE_RETENTION_DAYS` | `3`    | WAL archive retention days                |
| `HFT_TELEGRAM_ENABLED`  | `0`         | `1` = enable Telegram notification bot    |
| `HFT_TELEGRAM_BOT_TOKEN`| —           | Telegram bot token (use secret mgr)       |
| `HFT_TELEGRAM_CHAT_ID`  | —           | Telegram chat ID for alerts               |
```

- [ ] **Step 2: Replace the env-vars section in CLAUDE.md**

In `/home/charlie/hft_platform/CLAUDE.md`, replace the entire `## 🌐 Critical Environment Variables` section (originally lines 194–243, but line numbers will have shifted after Task 1.1) with this short version:

```markdown
## 🌐 Critical Environment Variables

Essential runtime/safety vars (full reference in `.claude/skills/hft-env-vars/`):

| Variable                | Default     | Purpose                                   |
| ----------------------- | ----------- | ----------------------------------------- |
| `HFT_MODE`              | `sim`       | Runtime mode: `sim` / `real` / `replay`   |
| `HFT_ORDER_MODE`        | `sim`       | Order execution: `sim` / `live` (LIVE = real money) |
| `HFT_STRICT_PRICE_MODE` | `0`         | `1` = reject float prices with TypeError  |
| `HFT_BROKER`            | `shioaji`   | Broker backend: `shioaji` / `fubon`       |
| `HFT_GATEWAY_ENABLED`   | `0`         | `1` = enable CE-M2 order/risk gateway     |
| `HFT_FEATURE_ENGINE_ENABLED` | `1`    | `0` = disable FeatureEngine in runtime pipeline |
```

- [ ] **Step 3: Verify line count reduction**

Run:
```bash
cd /home/charlie/hft_platform
wc -l CLAUDE.md
```

Expected: CLAUDE.md dropped by ~44 lines (from ~235 to ~191).

- [ ] **Step 4: Commit**

Run:
```bash
cd /home/charlie/hft_platform
git add .claude/skills/hft-env-vars/SKILL.md CLAUDE.md
git commit -m "refactor(claude-md): extract env-vars table to hft-env-vars skill (keep 6 essentials)"
```

### Task 1.3: Extract data contracts table to skill

**Files:**
- Create: `/home/charlie/hft_platform/.claude/skills/hft-data-contracts/SKILL.md`
- Modify: `/home/charlie/hft_platform/CLAUDE.md` (trim `## 📦 Key Data Contracts` section)

> **If probe FAILED:** use `.agent/library/data-contracts-reference.md` instead.

- [ ] **Step 1: Create the skill file**

Create `/home/charlie/hft_platform/.claude/skills/hft-data-contracts/SKILL.md`:

```markdown
---
name: hft-data-contracts
description: HFT platform data contract field reference — OrderIntent, OrderCommand, FillEvent, PositionDelta, TickEvent, BidAskEvent, LOBStatsEvent. Consult when designing strategies, modifying risk/execution code, or writing tests that construct these events. All price fields are scaled int (x10000).
---

# Key Data Contracts (Scaled Int Convention)

All price fields are `int` scaled by **x10000** (configurable per symbol in `symbols.yaml`).

```
OrderIntent → (Risk) → RiskDecision → OrderCommand → (Broker) → FillEvent → PositionDelta
```

| Contract        | File                     | Key Fields                                                          |
| --------------- | ------------------------ | ------------------------------------------------------------------- |
| `OrderIntent`   | `contracts/strategy.py`  | `price: int`, `qty: int`, `side: Side`, `idempotency_key`, `ttl_ns` |
| `OrderCommand`  | `contracts/strategy.py`  | `cmd_id`, `deadline_ns`, `storm_guard_state`                        |
| `FillEvent`     | `contracts/execution.py` | `price: int`, `fee: int`, `tax: int` (all x10000)                   |
| `PositionDelta` | `contracts/execution.py` | `net_qty`, `avg_price: int`, `realized_pnl: int`                    |
| `TickEvent`     | `events.py`              | `price: int` (x10000), `volume`, `meta: MetaData`                   |
| `BidAskEvent`   | `events.py`              | `bids/asks: np.ndarray` shape (N,2), `stats: tuple`                 |
| `LOBStatsEvent` | `events.py`              | `mid_price_x2: int`, `spread_scaled: int`, `imbalance: float`       |
```

- [ ] **Step 2: Replace the data contracts section in CLAUDE.md**

Replace the entire `## 📦 Key Data Contracts` section in CLAUDE.md with:

```markdown
## 📦 Key Data Contracts

All prices are scaled int (x10000). Contract flow: `OrderIntent → RiskDecision → OrderCommand → FillEvent → PositionDelta`. Field reference: `.claude/skills/hft-data-contracts/`.
```

- [ ] **Step 3: Commit**

Run:
```bash
cd /home/charlie/hft_platform
git add .claude/skills/hft-data-contracts/SKILL.md CLAUDE.md
git commit -m "refactor(claude-md): extract data contracts table to hft-data-contracts skill"
```

### Task 1.4: Delete derivable sections

**Files:**
- Modify: `/home/charlie/hft_platform/CLAUDE.md` (remove Runtime Planes table, Non-Hot-Path Services table, Config Priority Chain, Alpha Governance Gate table)

- [ ] **Step 1: Remove Runtime Planes (7) table**

In CLAUDE.md, delete the entire `### Runtime Planes (7)` subsection (the 7-row table). The canonical pipeline diagram above (lines ~46-49 originally) already gives the orientation; the full planes table is in `docs/architecture/current-architecture.md`.

Replace with: (nothing — it's redundant)

- [ ] **Step 2: Remove Non-Hot-Path Services (Cold Plane) table**

Delete the entire `### Non-Hot-Path Services (Cold Plane)` subsection including its table. It's documented module-by-module in `docs/modules/<module>.md` already.

Replace with: (nothing)

- [ ] **Step 3: Remove Config Priority Chain section**

Delete the entire `## ⚙️ Config Priority Chain` section. It's a simple concept trivially visible by reading `config/loader.py`.

- [ ] **Step 4: Remove Alpha Governance Pipeline table**

Delete the Alpha Governance gate table. Keep a two-line reference:

```markdown
## 🧬 Alpha Governance Pipeline

Research → Gates A/B/C/D/E → Canary. Implementation in `src/hft_platform/alpha/`. Research artifacts: `research/alphas/<alpha_id>/`.
```

- [ ] **Step 5: Verify line count**

Run:
```bash
cd /home/charlie/hft_platform
wc -l CLAUDE.md
```

Expected: CLAUDE.md is now ≤120 lines. If over, re-inspect for boilerplate like "these are the laws" or reduce the Red Flags checklist.

- [ ] **Step 6: Commit**

Run:
```bash
cd /home/charlie/hft_platform
git add CLAUDE.md
git commit -m "refactor(claude-md): remove derivable content (planes table, cold services, config chain, gate table)"
```

### Task 1.5: SP1 verification

- [ ] **Step 1: Run test suite**

Run:
```bash
cd /home/charlie/hft_platform
make lint 2>&1 | tail -20
```

Expected: no errors. Lint is docs-agnostic, so this just confirms nothing else was broken.

- [ ] **Step 2: Grep for broken references**

Run:
```bash
cd /home/charlie/hft_platform
grep -rn "Rust Boundary" CLAUDE.md .agent/rules/ docs/ || echo "OK: no stale references"
grep -rn "Critical Environment Variables" CLAUDE.md .agent/rules/ docs/ || echo "OK: no stale references"
```

If anywhere else in the repo references the old CLAUDE.md sections by name, update those references to point at the skills or leave a terse pointer.

- [ ] **Step 3: SP1 summary commit**

Run:
```bash
cd /home/charlie/hft_platform
echo "SP1 complete: CLAUDE.md $(wc -l < CLAUDE.md) lines (target ≤120)" >&2
```

**STOP — ask user to review SP1 commits before proceeding to SP2.**

---

## Sub-Project 2 (SP2) — `.agent/rules/` consolidation

**Goal:** 1430 → ≤800 lines. No duplication with CLAUDE.md.

### Task 2.1: Merge 30-git-workflow.md + 35-git-hygiene.md

**Files:**
- Modify: `/home/charlie/hft_platform/.agent/rules/30-git-workflow.md` (rename to `30-git.md`, absorb 35 content)
- Delete: `/home/charlie/hft_platform/.agent/rules/35-git-hygiene.md`

- [ ] **Step 1: Read both files**

Run:
```bash
cd /home/charlie/hft_platform
cat .agent/rules/30-git-workflow.md .agent/rules/35-git-hygiene.md | wc -l
```

Record the line count.

- [ ] **Step 2: Merge content**

Read both files, then rewrite `.agent/rules/30-git-workflow.md` as a single merged file. Keep:
- Conventional commit prefixes list (from 30)
- Branch strategy (from 30)
- DO NOT commit list (from 30)
- Pre-commit checklist (from 30)
- Git hygiene rules section (from 35 — worktree, branch, stash discipline, session-end cleanup)

De-dup where both files describe the same thing. Target final size: ≤60 lines.

- [ ] **Step 3: Rename file**

Run:
```bash
cd /home/charlie/hft_platform
git mv .agent/rules/30-git-workflow.md .agent/rules/30-git.md
git rm .agent/rules/35-git-hygiene.md
```

- [ ] **Step 4: Update index**

In `.agent/rules/00-index.md`, replace the two rows for `30-git-workflow.md` and `35-git-hygiene.md` with one row:

```markdown
| `30-git.md`                     | Commits, branches, pre-commit, hygiene              | ~60   |
```

- [ ] **Step 5: Commit**

Run:
```bash
cd /home/charlie/hft_platform
git add .agent/rules/30-git.md .agent/rules/00-index.md
git commit -m "refactor(rules): merge 30-git-workflow + 35-git-hygiene into 30-git.md"
```

### Task 2.2: De-dup rules against CLAUDE.md

**Files:**
- Modify: various `.agent/rules/*.md`

- [ ] **Step 1: Identify overlaps**

Run:
```bash
cd /home/charlie/hft_platform
# Content from CLAUDE.md that also appears in rules: Laws, Red Flags, Package Naming, Latency Realism Guard
grep -l "Allocator Law\|Precision Law\|Red Flags\|Package Naming\|Latency Realism" .agent/rules/*.md
```

- [ ] **Step 2: Remove duplicated Constitution content**

`01-core-laws.md` is the canonical source for the 5 Laws. CLAUDE.md should link to it, not duplicate. Read `.agent/rules/01-core-laws.md` — if CLAUDE.md's Laws text is identical or near-identical, replace CLAUDE.md's Laws section with:

```markdown
## 🛡️ Critical HFT Laws

Five non-negotiable laws (details in `.agent/rules/01-core-laws.md`):
1. **Allocator** — no heap allocs on hot path
2. **Cache** — Structure of Arrays, not Array of Structures
3. **Async** — no blocking IO or compute >1ms on main loop
4. **Precision** — never float for prices; use scaled int (x10000)
5. **Boundary** — zero-copy Python↔Rust
```

If `01-core-laws.md` is actually thinner than CLAUDE.md's version, expand it and keep CLAUDE.md terse. The canonical version lives in `01-core-laws.md`.

- [ ] **Step 3: Commit**

Run:
```bash
cd /home/charlie/hft_platform
git add CLAUDE.md .agent/rules/01-core-laws.md
git commit -m "refactor(rules): make 01-core-laws.md the single source for the 5 Laws"
```

### Task 2.3: Trim 55-enforcement.md

**Files:**
- Modify: `/home/charlie/hft_platform/.agent/rules/55-enforcement.md`

- [ ] **Step 1: Read the file**

Run:
```bash
cd /home/charlie/hft_platform
cat .agent/rules/55-enforcement.md
```

- [ ] **Step 2: Apply lean-content test**

For each paragraph/bullet, apply the test: *"Would removing this line cause Claude to make a mistake?"* Delete lines where the answer is "no" — Claude already knows generic coding practices.

Target size: ≤45 lines (from 84).

Keep only: things unique to this project's enforcement (pre-commit hooks that actually exist, CI gates that actually block, specific tooling commands).

- [ ] **Step 3: Verify**

Run:
```bash
cd /home/charlie/hft_platform
wc -l .agent/rules/55-enforcement.md
```

Expected: ≤45 lines.

- [ ] **Step 4: Commit**

Run:
```bash
cd /home/charlie/hft_platform
git add .agent/rules/55-enforcement.md
git commit -m "refactor(rules): trim 55-enforcement.md (84→≤45 lines), remove generic content"
```

### Task 2.4: Trim 60-agent-workflow-governance.md

**Files:**
- Modify: `/home/charlie/hft_platform/.agent/rules/60-agent-workflow-governance.md`

- [ ] **Step 1: Read the file**

Run:
```bash
cd /home/charlie/hft_platform
cat .agent/rules/60-agent-workflow-governance.md
```

- [ ] **Step 2: Identify load-bearing AWG rules**

The file currently is 252 lines with AWG-01 through AWG-10+ rules. Keep the *rule statements* (one line each) and the minimum context. Remove verbose examples, historical justifications, and illustrations. Target: ≤120 lines.

For each rule: keep the numbered rule title and a 1-3 line crisp statement. Move examples to inline comments in the relevant source files if critical, or delete.

- [ ] **Step 3: Verify**

Run:
```bash
cd /home/charlie/hft_platform
wc -l .agent/rules/60-agent-workflow-governance.md
```

Expected: ≤120 lines.

- [ ] **Step 4: Commit**

Run:
```bash
cd /home/charlie/hft_platform
git add .agent/rules/60-agent-workflow-governance.md
git commit -m "refactor(rules): trim 60-agent-workflow-governance.md (252→≤120 lines)"
```

### Task 2.5: Trim remaining rule files

**Files:**
- Modify: each of `05-project-structure.md`, `10-hft-performance.md`, `15-security.md`, `20-data-flow.md`, `25-architecture-governance.md`, `26-multi-broker-governance.md`, `40-ops.md`, `50-testing.md`, `70-research-data.md`

- [ ] **Step 1: Apply lean test to each**

For each file, apply the same test as Task 2.3/2.4. Per-file targets from the spec:

| File | Current | Target |
|---|---|---|
| `05-project-structure.md` | 53 | 35 |
| `10-hft-performance.md` | 61 | 45 |
| `15-security.md` | 31 | 25 |
| `20-data-flow.md` | 47 | 40 |
| `25-architecture-governance.md` | 137 | 90 |
| `26-multi-broker-governance.md` | 67 | 55 |
| `40-ops.md` | 61 | 45 |
| `50-testing.md` | 35 | 30 |
| `70-research-data.md` | 175 | 120 |

Trim one file at a time. Read the file, identify generic / derivable / example-heavy content, trim.

- [ ] **Step 2: Update index**

Update line-count estimates in `.agent/rules/00-index.md` to reflect new sizes.

- [ ] **Step 3: Verify total**

Run:
```bash
cd /home/charlie/hft_platform
wc -l .agent/rules/*.md | tail -1
```

Expected: total ≤800 lines.

- [ ] **Step 4: Commit**

Run:
```bash
cd /home/charlie/hft_platform
git add .agent/rules/
git commit -m "refactor(rules): trim remaining rule files to per-file targets (total ≤800 lines)"
```

### Task 2.6: SP2 verification

- [ ] **Step 1: Run test suite**

Run:
```bash
cd /home/charlie/hft_platform
make lint 2>&1 | tail -5
make test 2>&1 | tail -20
```

Expected: no new failures.

- [ ] **Step 2: Grep for broken references**

Run:
```bash
cd /home/charlie/hft_platform
grep -rn "35-git-hygiene\|30-git-workflow" CLAUDE.md .agent/ docs/ 2>/dev/null || echo "OK: no stale references"
```

If any stale references, update them to `30-git.md`.

- [ ] **Step 3: SP2 summary**

Run:
```bash
cd /home/charlie/hft_platform
wc -l .agent/rules/*.md | tail -1
```

**STOP — ask user to review SP2 commits before proceeding to SP3.**

---

## Sub-Project 3 (SP3) — Global `~/.claude/rules/common/` audit

**Goal:** 8 files → 1–3 files, ≤100 lines total.

### Task 3.1: Archive the originals

**Files:**
- Create: `~/.claude/rules/common/_archive-2026-04-19/` with copies of all existing files

- [ ] **Step 1: Create archive directory and copy originals**

Run:
```bash
mkdir -p ~/.claude/rules/common/_archive-2026-04-19
cp ~/.claude/rules/common/*.md ~/.claude/rules/common/_archive-2026-04-19/
ls -la ~/.claude/rules/common/_archive-2026-04-19/
```

Expected: 8 `.md` files copied.

- [ ] **Step 2: Verify archive integrity**

Run:
```bash
diff <(ls ~/.claude/rules/common/*.md | xargs -n1 basename | sort) \
     <(ls ~/.claude/rules/common/_archive-2026-04-19/*.md | xargs -n1 basename | sort)
```

Expected: empty diff (archive contains all originals).

### Task 3.2: Grep-check referenced agents

**Files:** (read-only check)

- [ ] **Step 1: List agents referenced in `agents.md`**

Run:
```bash
grep -E "^\|" ~/.claude/rules/common/agents.md | awk -F'|' '{print $2}' | tr -d ' ' | grep -v '^$\|---\|Agent'
```

- [ ] **Step 2: Check if each agent exists in the platform**

For each agent listed (planner, architect, tdd-guide, code-reviewer, security-reviewer, build-error-resolver, e2e-runner, refactor-cleaner, doc-updater), check:

```bash
ls ~/.claude/agents/ 2>/dev/null
ls /home/charlie/hft_platform/.agent/agents/ 2>/dev/null
```

Note which exist and which don't. Agents that don't exist anywhere are fiction and get deleted from `agents.md`.

### Task 3.3: Decide per-file disposition

**Files:** (planning — no edits yet)

- [ ] **Step 1: Read each file**

```bash
for f in ~/.claude/rules/common/*.md; do
  echo "=== $f ==="
  wc -l "$f"
done
```

- [ ] **Step 2: Apply lean-content test to each**

Per spec disposition table:

| File | Disposition | Reason |
|---|---|---|
| `performance.md` | Trim to ≤20 lines | Keep only model-selection hints truly not obvious |
| `hooks.md` | Merge into `core.md` | Short to begin with |
| `git-workflow.md` | Delete | Generic — every project has its own |
| `patterns.md` | Delete | Generic design patterns |
| `agents.md` | Trim or delete | Depends on Task 3.2 results |
| `security.md` | Trim to ≤15 lines | Keep only non-obvious |
| `coding-style.md` | Trim to ≤20 lines | Keep only non-default (immutability, file-org) |
| `testing.md` | Merge into `coding-style.md` | Small |

### Task 3.4: Execute consolidation

**Files:**
- Create: `~/.claude/rules/common/core.md`
- Delete: all other `.md` files in `~/.claude/rules/common/` (except the archive folder)

- [ ] **Step 1: Write consolidated `core.md`**

Create `~/.claude/rules/common/core.md` — a single consolidated file with ≤80 lines. Structure:

```markdown
# Common Rules (Cross-Project)

## Coding
- Prefer immutability; create new objects instead of mutating.
- Many small files > few large files. ~200-400 lines typical, 800 max.
- Validate all inputs at system boundaries. Never trust external data.
- Handle errors explicitly; don't silently swallow.

## Testing
- New code: ≥80% coverage. Hot-path / financial logic: ≥90%.
- Test-driven default: failing test → minimal impl → refactor.

## Git
- Conventional commits: `feat:`, `fix:`, `refactor:`, `perf:`, `docs:`, `test:`, `chore:`, `ci:`.
- Never commit secrets. Never use `--no-verify` without explicit user ask.
- Never force-push to main/master.

## Security
- Never hardcode secrets. Use env vars or secret manager.
- Never log credential values. Log identifiers only (`api_key_prefix=ABC***`).
- Never commit `.env` files.

## Agent usage
- Delegate broad codebase exploration (>3 queries) to Explore agent.
- Use Plan agent for non-trivial refactors before touching code.
- Run code-reviewer agent after significant changes.
```

Adjust size/content based on what the `_archive-2026-04-19/` files actually contained — but keep total ≤80 lines.

- [ ] **Step 2: Write agents reference (conditional)**

If `agents.md` Task 3.2 showed most agents DO exist, create `~/.claude/rules/common/agents.md` (≤20 lines) with just a lean reference table. Otherwise skip this step.

- [ ] **Step 3: Delete old files**

Run:
```bash
cd ~/.claude/rules/common
# Delete all top-level .md files EXCEPT the ones we just wrote
for f in performance.md hooks.md git-workflow.md patterns.md agents.md security.md coding-style.md testing.md; do
  # Skip agents.md if we just wrote it
  if [ "$f" = "agents.md" ] && grep -q "^# Agent" agents.md 2>/dev/null && [ "$(head -1 agents.md)" = "# Agent Reference" ]; then
    continue
  fi
  [ -f "$f" ] && rm "$f" && echo "Deleted: $f"
done
ls ~/.claude/rules/common/
```

Expected: `core.md` (+ optionally `agents.md`) and `_archive-2026-04-19/` remain.

- [ ] **Step 4: Verify total line count**

Run:
```bash
wc -l ~/.claude/rules/common/*.md | tail -1
```

Expected: ≤100 lines.

- [ ] **Step 5: No git commit**

Note: `~/.claude/rules/common/` is NOT in a git repo (it's in the user's home dir). The `_archive-2026-04-19/` folder IS the rollback mechanism.

### Task 3.5: SP3 verification

- [ ] **Step 1: Start a fresh Claude Code session in any project (not just hft_platform)**

Verify the consolidated rules load correctly at session start. If Claude complains about a missing rule, restore from archive:

```bash
cp ~/.claude/rules/common/_archive-2026-04-19/<file>.md ~/.claude/rules/common/
```

- [ ] **Step 2: Commit nothing (user-home is not git-tracked)**

**STOP — ask user to review SP3 state before proceeding to SP4.**

---

## Sub-Project 4 (SP4) — `.agent/skills/` dead-skill pass

**Goal:** Detect and archive/delete broken, duplicate, or stub skills from the 174 entries in `.agent/skills/`.

### Task 4.1: Audit frontmatter

**Files:**
- Create: `/home/charlie/hft_platform/docs/superpowers/specs/2026-04-19-agent-config-hygiene-report.md`

- [ ] **Step 1: Create hygiene report scaffold**

Create the report file:

```markdown
# Agent Config Hygiene Report — 2026-04-19

**Scope:** SP4 (`.agent/skills/`) and SP5 (`.agent/agents/` + `.agent/commands/`).

## SP4 Findings

### Skills missing frontmatter

(to be filled by Task 4.1)

### Duplicate descriptions

(to be filled by Task 4.2)

### Stub skills (<20 lines non-frontmatter content)

(to be filled by Task 4.3)

### Dispositions

| Skill | Issue | Action |
|---|---|---|

## SP5 Findings

### Agents

(to be filled by Task 5.x)

### Commands

(to be filled by Task 5.x)
```

- [ ] **Step 2: Scan for missing frontmatter**

Run:
```bash
cd /home/charlie/hft_platform
for dir in .agent/skills/*/; do
  skill_name=$(basename "$dir")
  md_file="${dir}SKILL.md"
  if [ ! -f "$md_file" ]; then
    echo "MISSING_SKILL_MD: $skill_name"
    continue
  fi
  head -5 "$md_file" | grep -q '^name:' || echo "NO_NAME: $skill_name"
  head -10 "$md_file" | grep -q '^description:' || echo "NO_DESCRIPTION: $skill_name"
done
```

Record findings in the report under "Skills missing frontmatter".

- [ ] **Step 3: Commit scaffold + findings**

Run:
```bash
cd /home/charlie/hft_platform
git add docs/superpowers/specs/2026-04-19-agent-config-hygiene-report.md
git commit -m "docs: add agent config hygiene report scaffold + SP4 frontmatter audit"
```

### Task 4.2: Detect duplicate descriptions

- [ ] **Step 1: Extract descriptions**

Run:
```bash
cd /home/charlie/hft_platform
for dir in .agent/skills/*/; do
  skill_name=$(basename "$dir")
  md_file="${dir}SKILL.md"
  if [ -f "$md_file" ]; then
    desc=$(awk '/^description:/ {sub(/^description:\s*/, ""); print; exit}' "$md_file")
    echo "$desc|$skill_name"
  fi
done | sort | awk -F'|' '
{
  if ($1 == prev_desc && $1 != "") {
    if (!printed[$1]) { print "DUP: \"" $1 "\"" ; printed[$1]=1 }
    print "  - " prev_skill
    print "  - " $2
  }
  prev_desc = $1
  prev_skill = $2
}'
```

Append findings to the hygiene report under "Duplicate descriptions".

### Task 4.3: Detect stub skills

- [ ] **Step 1: Find skills with <20 lines of real content**

Run:
```bash
cd /home/charlie/hft_platform
for dir in .agent/skills/*/; do
  skill_name=$(basename "$dir")
  md_file="${dir}SKILL.md"
  if [ -f "$md_file" ]; then
    # Count non-frontmatter, non-blank, non-comment lines
    real_lines=$(awk '
      /^---/ { in_fm = !in_fm; next }
      in_fm { next }
      /^[[:space:]]*$/ { next }
      /^[[:space:]]*#/ { count++; next }  # headings count
      { count++ }
      END { print count+0 }
    ' "$md_file")
    if [ "$real_lines" -lt 20 ]; then
      echo "STUB ($real_lines lines): $skill_name"
    fi
  fi
done
```

Append to hygiene report under "Stub skills".

### Task 4.4: Apply dispositions

**Files:**
- Create: `/home/charlie/hft_platform/.agent/skills/_archive-2026-04-19/`
- Delete: obvious duplicates and broken skills

- [ ] **Step 1: Fill in Dispositions table**

For each flagged skill, assign an action in the hygiene report:
- `hard-delete` — exact duplicate (same body as another skill)
- `archive` — stub, borderline, or unreferenced-but-unique
- `keep` — false positive (actually useful, just happens to be short)

- [ ] **Step 2: Execute hard deletes**

For each skill marked `hard-delete`:
```bash
cd /home/charlie/hft_platform
git rm -rf .agent/skills/<skill-name>
```

- [ ] **Step 3: Execute archives**

```bash
cd /home/charlie/hft_platform
mkdir -p .agent/skills/_archive-2026-04-19
for skill in <list_of_archived_skills>; do
  git mv .agent/skills/$skill .agent/skills/_archive-2026-04-19/
done
```

- [ ] **Step 4: Update `.agent/skills/00-index.md` and `README.md`**

Read both files; remove entries for deleted/archived skills.

- [ ] **Step 5: Commit**

```bash
cd /home/charlie/hft_platform
git add .agent/skills/
git commit -m "chore(skills): archive/delete broken, duplicate, stub skills (SP4)"
```

### Task 4.5: SP4 verification

- [ ] **Step 1: Re-run audits**

Re-run Task 4.1 Step 2, Task 4.2 Step 1, Task 4.3 Step 1 scripts. Confirm all flagged entries are resolved (either deleted, archived, or explicitly marked `keep` with justification).

- [ ] **Step 2: Update hygiene report**

In the report file, add a "SP4 complete" timestamp and final counts.

**STOP — ask user to review SP4 commits before proceeding to SP5.**

---

## Sub-Project 5 (SP5) — `.agent/agents/` + `.agent/commands/` inventory

**Goal:** Remove dead entries. Validate frontmatter.

### Task 5.1: Audit `.agent/agents/`

- [ ] **Step 1: Check frontmatter**

Run:
```bash
cd /home/charlie/hft_platform
for f in .agent/agents/*.md; do
  name=$(basename "$f" .md)
  head -5 "$f" | grep -q '^name:' || echo "NO_NAME: $name"
  head -10 "$f" | grep -q '^description:' || echo "NO_DESCRIPTION: $name"
done
```

Record findings.

- [ ] **Step 2: Check for references across repo + user home**

For each agent file, check if its name is invoked anywhere:

```bash
cd /home/charlie/hft_platform
for f in .agent/agents/*.md; do
  name=$(basename "$f" .md)
  count=$(grep -rln "$name" .agent/ CLAUDE.md docs/ src/ ~/.claude/ 2>/dev/null | grep -v "^.agent/agents/$name.md$" | wc -l)
  echo "$name: $count references"
done
```

Agents with 0 references outside their own definition are candidates for archive.

### Task 5.2: Audit `.agent/commands/`

- [ ] **Step 1: Check frontmatter and references**

Run:
```bash
cd /home/charlie/hft_platform
for f in .agent/commands/*.md; do
  name=$(basename "$f" .md)
  head -5 "$f" | grep -q '^name:' || echo "NO_NAME: $name"
  # Commands get invoked as /name; grep for slash-name usages
  count=$(grep -rln "/$name" .agent/ CLAUDE.md docs/ src/ 2>/dev/null | wc -l)
  echo "$name: $count slash-references"
done
```

Commands with 0 references may be dead.

### Task 5.3: Apply dispositions

- [ ] **Step 1: Update hygiene report SP5 section**

Fill in agents and commands dispositions.

- [ ] **Step 2: Execute archives/deletes**

```bash
cd /home/charlie/hft_platform
mkdir -p .agent/agents/_archive-2026-04-19 .agent/commands/_archive-2026-04-19
# For each dead agent/command:
git mv .agent/agents/<name>.md .agent/agents/_archive-2026-04-19/
git mv .agent/commands/<name>.md .agent/commands/_archive-2026-04-19/
```

- [ ] **Step 3: Commit**

```bash
cd /home/charlie/hft_platform
git add .agent/agents/ .agent/commands/ docs/superpowers/specs/2026-04-19-agent-config-hygiene-report.md
git commit -m "chore(agents,commands): archive dead entries (SP5)"
```

### Task 5.4: SP5 verification

- [ ] **Step 1: Run test suite**

```bash
cd /home/charlie/hft_platform
make lint && make test
```

- [ ] **Step 2: Final summary**

Run:
```bash
cd /home/charlie/hft_platform
echo "=== After state ==="
echo "CLAUDE.md: $(wc -l < CLAUDE.md) lines (baseline: 261, target ≤120)"
echo ".agent/rules/ total: $(wc -l .agent/rules/*.md | tail -1 | awk '{print $1}') lines (baseline: 1430, target ≤800)"
echo "~/.claude/rules/common/ total: $(wc -l ~/.claude/rules/common/*.md 2>/dev/null | tail -1 | awk '{print $1}') lines (baseline ~300, target ≤100)"
echo ".agent/skills/ count: $(ls .agent/skills/ | grep -v '^_archive' | wc -l)"
echo ".agent/agents/ count: $(ls .agent/agents/ | grep -v '^_archive' | wc -l)"
echo ".agent/commands/ count: $(ls .agent/commands/ | grep -v '^_archive' | wc -l)"
```

Paste this into the hygiene report as "Final state".

- [ ] **Step 3: Create post-cleanup tag**

```bash
cd /home/charlie/hft_platform
git tag post-agent-config-cleanup-2026-04-19 -m "After agent config best-practices alignment"
```

- [ ] **Step 4: Final commit**

```bash
cd /home/charlie/hft_platform
git add docs/superpowers/specs/2026-04-19-agent-config-hygiene-report.md
git commit -m "docs: finalize agent config hygiene report with final state"
```

---

## Self-Review (run before finishing)

### Spec coverage check

- [x] SP1 (CLAUDE.md slim-down) → Tasks 1.1–1.5
- [x] SP2 (rules consolidation) → Tasks 2.1–2.6
- [x] SP3 (global rules audit with archive) → Tasks 3.1–3.5
- [x] SP4 (skills hygiene) → Tasks 4.1–4.5
- [x] SP5 (agents/commands inventory) → Tasks 5.1–5.4
- [x] R1 mitigation (`.claude/skills/` probe) → Task 0.2
- [x] Baseline tag → Task 0.1

### Placeholder scan

- `<list_of_archived_skills>` in Task 4.4 Step 3 is a placeholder — but it's a dynamic output from Task 4.4 Step 1's disposition table. Mark it as "fill in from Step 1 output, not plan-time."
- Task 2.5 does not hand-write each file's new content — this is intentional (trim is judgment-based), but the "keep only non-generic" criteria is given.

### Type consistency

- Tag names: `pre-agent-config-cleanup-2026-04-19` (Task 0.1) and `post-agent-config-cleanup-2026-04-19` (Task 5.4 Step 3) — consistent.
- Archive path convention: `_archive-2026-04-19/` — consistent across SP3, SP4, SP5.
- Report file path: `docs/superpowers/specs/2026-04-19-agent-config-hygiene-report.md` — referenced from SP4 task 4.1 and SP5 task 5.3. Consistent.

### Gaps

None found. All 5 sub-projects have tasks. Reversibility (tag + archive) is built in.
