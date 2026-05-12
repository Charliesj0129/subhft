# AGENTS.md Onboarding Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `AGENTS.md` into a hybrid onboarding guide that gives agents compact mandatory rules plus task-specific routing to canonical project docs, rules, and skills.

**Architecture:** This is a documentation-only change. `AGENTS.md` remains the thin entry point; detailed architecture, rules, skills, and runbooks remain in their existing source files to avoid drift.

**Tech Stack:** Markdown, local repo docs, `.agent/rules`, `.agent/skills`, git.

---

## File Structure

- Modify: `AGENTS.md`
  - Responsibility: top-level agent onboarding, safety contract, and reference router.
- Reference only: `docs/superpowers/specs/2026-05-12-agents-md-onboarding-design.md`
  - Responsibility: approved design source.
- Reference only: `docs/MODULES_REFERENCE.md`
  - Responsibility: canonical compressed module map and hot-path overview.
- Reference only: `.agent/rules/00-index.md`, `.agent/rules/01-core-laws.md`, `.agent/rules/10-hft-performance.md`, `.agent/rules/50-testing.md`
  - Responsibility: rule index, HFT laws, performance rules, testing rules.
- Reference only: `.agent/skills/00-index.md`
  - Responsibility: task-to-skill routing source.
- Reference only: `docs/architecture/`
  - Responsibility: canonical architecture docs when architecture is involved.

No code files, tests, rule files, or skill files should be modified.

## Task 1: Verify Current References

**Files:**
- Read: `AGENTS.md`
- Read: `docs/superpowers/specs/2026-05-12-agents-md-onboarding-design.md`
- Read: `docs/MODULES_REFERENCE.md`
- Read: `.agent/rules/00-index.md`
- Read: `.agent/skills/00-index.md`

- [ ] **Step 1: Inspect current AGENTS.md**

Run:

```bash
sed -n '1,220p' AGENTS.md
```

Expected: current file is the short generated Pixiu Agent document and includes the stale `docs/AI_DEVELOPER_CHEAT_SHEET.md` path.

- [ ] **Step 2: Inspect the approved design**

Run:

```bash
sed -n '1,260p' docs/superpowers/specs/2026-05-12-agents-md-onboarding-design.md
```

Expected: design describes the hybrid command-center structure.

- [ ] **Step 3: Verify replacement reference paths exist**

Run:

```bash
test -f docs/MODULES_REFERENCE.md
test -f .agent/rules/00-index.md
test -f .agent/rules/01-core-laws.md
test -f .agent/rules/10-hft-performance.md
test -f .agent/rules/50-testing.md
test -f .agent/skills/00-index.md
test -d docs/architecture
```

Expected: all commands exit successfully.

## Task 2: Rewrite AGENTS.md

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Replace the generated body**

Use `apply_patch` to replace the existing `AGENTS.md` content with the maintained onboarding guide below.

```markdown
# AGENTS.md

> 重要：本專案是 Python HFT 平台。處理任何任務時，請優先使用基於檢索的推理（retrieval-led reasoning）：先讀本 repo 的文件、規則、技能與相關原始碼，再做判斷或修改。

## Project Context

| Key | Value |
| --- | --- |
| Name | `hft_platform` |
| Domain | High-frequency trading platform |
| Primary tech | Python, Rust/PyO3 fast paths, ClickHouse/Redis integrations |
| Risk profile | Latency-sensitive, money-facing, safety-critical |

## Mandatory First Reads

開始工作前先讀這些目前存在的索引與地圖，不要依賴記憶猜測專案規則：

1. `docs/MODULES_REFERENCE.md` - module map, hot-path modules, contracts, runtime planes.
2. `.agent/rules/00-index.md` - project rule index.
3. `.agent/skills/00-index.md` - task-to-skill routing.

任務涉及架構或模組邊界時，再讀 `docs/architecture/` 中相關文件。任務涉及已知 gotcha 時，檢查 `.agent/memory/module_gotchas.md`（若存在且相關）。

若引用的文件不存在：明確說出缺少的路徑，用 `rg --files` 搜尋最接近的現行文件，改讀目前 repo 內的 canonical source；不要憑預訓練記憶補規則。

## Hard HFT Laws

完整規則以 `.agent/rules/01-core-laws.md` 與 `.agent/rules/10-hft-performance.md` 為準。摘要如下：

1. **Allocator Law**: hot path / tick loop 不得配置 heap 物件；使用預配置 buffer、ring buffer、object pool 或 Rust。
2. **Cache Law**: 優先 cache-local / packed data；避免 pointer chasing。
3. **Async Law**: event loop 不得做 blocking IO 或超過 1 ms 的同步計算。
4. **Precision Law**: 價格與金額使用 scaled int（專案慣例 x10000）或明確安全型別；hot path 禁用 float price math。
5. **Boundary Law**: Python/Rust 邊界避免大資料 copy；優先 zero-copy buffer、shared memory 或明確的 FFI contract。

Hot-path 檔案包含 market data ingestion、LOB、feature engine、event bus、strategy dispatch、risk、order/execution gateway 等；修改前必讀對應 rule/skill。

## Task Routing

先讀 `.agent/skills/00-index.md`，再只打開當前任務直接相關的 `SKILL.md`。

| Task | First references |
| --- | --- |
| Market data, normalizer, LOB, feature engine | `.agent/skills/hft-market-data/SKILL.md`, `.agent/skills/hft-hot-path-dev/SKILL.md` |
| Strategy changes | `.agent/skills/hft-strategy-dev/SKILL.md`, `.agent/skills/hft-strategy-sdk/SKILL.md` |
| Alpha research, backtest, promotion gates | `.agent/skills/hft-alpha-research/SKILL.md`, `.agent/skills/research-factory/SKILL.md`, `.agent/skills/validation-gate/SKILL.md` |
| Execution, fills, positions, TCA | `.agent/skills/hft-execution/SKILL.md` |
| Recorder, WAL, ClickHouse | `.agent/skills/hft-recorder/SKILL.md`, `.agent/skills/clickhouse-io/SKILL.md` |
| Ops, sessions, alerts, health | `.agent/skills/hft-ops/SKILL.md`, `.agent/skills/troubleshoot-metrics/SKILL.md` |
| Rust/PyO3 changes | `.agent/skills/rust-pro/SKILL.md`, `.agent/skills/hft-rust-exports/SKILL.md` |
| Tests and verification | `.agent/rules/50-testing.md`, `.agent/skills/hft-test-hft/SKILL.md`, `.agent/skills/python-testing-patterns/SKILL.md` |
| Docs, codemaps, generated references | `.agent/skills/doc-updater/SKILL.md`, `docs/MODULES_REFERENCE.md` |
| Architecture decisions | `docs/architecture/`, `.agent/skills/hft-architect/SKILL.md`, `.agent/rules/25-architecture-governance.md` |
| Broker abstraction / multi-broker work | `.agent/skills/broker-abstraction/SKILL.md`, `.agent/skills/multi-broker-ops/SKILL.md`, `.agent/rules/26-multi-broker-governance.md` |
| Config and environment variables | `.agent/skills/config-env/SKILL.md`, `.agent/skills/hft-env-vars/SKILL.md` |

## Workflow Expectations

- Read relevant docs/skills before implementation.
- Use `rg`/source inspection before making behavioral claims.
- Keep edits scoped to the user request; do not perform unrelated refactors.
- Treat hot-path changes as high-risk and verify allocator, precision, latency, and async behavior.
- Preserve user work in dirty worktrees. Do not revert unrelated changes.
- Prefer structured parsers/APIs over ad hoc string handling when changing structured data.
- When changing docs that list paths, verify the paths exist.

## Testing and Verification

Verification should match blast radius:

- Narrow docs-only change: inspect rendered/readable Markdown, verify referenced paths, review `git diff`.
- Bug fix: add or update a focused regression test before/with the fix.
- Shared contract or hot-path change: run targeted unit tests plus relevant HFT-specific tests for scaled ints, monotonic time, fail-closed behavior, state transitions, and latency-sensitive paths.
- Architecture or workflow change: update related docs/codemaps and verify no stale references remain.

Do not claim tests pass unless the command was run and the result is known.

## Safety and Git Hygiene

- Never expose secrets, API keys, broker credentials, account identifiers, or production tokens in logs, docs, tests, commits, or chat.
- Do not run production-impacting commands, live trading commands, destructive filesystem operations, or destructive git commands unless explicitly requested.
- Commit only intentional files. If the worktree is dirty, inspect/stage narrowly.
- Avoid modifying generated or cached artifacts unless the task explicitly requires it.
- If instructions conflict, follow the most specific current user instruction that does not violate safety, project rules, or higher-priority system/developer instructions.

---

_Maintained as the agent entry point for `hft_platform`; detailed rules live in `.agent/rules/`, skills in `.agent/skills/`, and architecture docs in `docs/architecture/`._
```

- [ ] **Step 2: Inspect the rewritten file**

Run:

```bash
sed -n '1,260p' AGENTS.md
```

Expected: file is readable, bilingual where useful, and no longer references `docs/AI_DEVELOPER_CHEAT_SHEET.md`.

## Task 3: Validate References and Diff

**Files:**
- Read: `AGENTS.md`
- Read: referenced docs/rules/skills

- [ ] **Step 1: Verify all referenced concrete paths exist**

Run:

```bash
for path in \
  docs/MODULES_REFERENCE.md \
  .agent/rules/00-index.md \
  .agent/skills/00-index.md \
  docs/architecture \
  .agent/rules/01-core-laws.md \
  .agent/rules/10-hft-performance.md \
  .agent/skills/hft-market-data/SKILL.md \
  .agent/skills/hft-hot-path-dev/SKILL.md \
  .agent/skills/hft-strategy-dev/SKILL.md \
  .agent/skills/hft-strategy-sdk/SKILL.md \
  .agent/skills/hft-alpha-research/SKILL.md \
  .agent/skills/research-factory/SKILL.md \
  .agent/skills/validation-gate/SKILL.md \
  .agent/skills/hft-execution/SKILL.md \
  .agent/skills/hft-recorder/SKILL.md \
  .agent/skills/clickhouse-io/SKILL.md \
  .agent/skills/hft-ops/SKILL.md \
  .agent/skills/troubleshoot-metrics/SKILL.md \
  .agent/skills/rust-pro/SKILL.md \
  .agent/skills/hft-rust-exports/SKILL.md \
  .agent/rules/50-testing.md \
  .agent/skills/hft-test-hft/SKILL.md \
  .agent/skills/python-testing-patterns/SKILL.md \
  .agent/skills/doc-updater/SKILL.md \
  .agent/skills/hft-architect/SKILL.md \
  .agent/rules/25-architecture-governance.md \
  .agent/skills/broker-abstraction/SKILL.md \
  .agent/skills/multi-broker-ops/SKILL.md \
  .agent/rules/26-multi-broker-governance.md \
  .agent/skills/config-env/SKILL.md \
  .agent/skills/hft-env-vars/SKILL.md; do \
  test -e "$path" || { echo "missing: $path"; exit 1; }; \
done
```

Expected: no output, exit code 0.

- [ ] **Step 2: Verify stale path is gone**

Run:

```bash
rg 'AI_DEVELOPER_CHEAT_SHEET|Pixiu Agent' AGENTS.md
```

Expected: no matches.

- [ ] **Step 3: Review scoped diff**

Run:

```bash
git diff -- AGENTS.md
```

Expected: only `AGENTS.md` changed for the implementation.

## Task 4: Commit Implementation

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Confirm only intended implementation file is staged**

Run:

```bash
git status --short AGENTS.md
```

Expected: ` M AGENTS.md`.

- [ ] **Step 2: Stage only AGENTS.md**

Run:

```bash
git add AGENTS.md
```

Expected: command succeeds.

- [ ] **Step 3: Commit**

Run:

```bash
git commit -m "docs: rewrite AGENTS onboarding guide"
```

Expected: commit succeeds and includes only `AGENTS.md`.

- [ ] **Step 4: Report final state**

Run:

```bash
git log --oneline -1
git status --short
```

Expected: latest commit is `docs: rewrite AGENTS onboarding guide`; unrelated pre-existing dirty files may remain unstaged.

## Review Notes

Subagent review is recommended by the writing-plans workflow, but this session's active tool policy only permits subagents when the user explicitly authorizes delegated/parallel agent work. If no such authorization is given, perform a local review by checking:

- The plan matches the approved design spec.
- Only `AGENTS.md` is in implementation scope.
- The proposed content avoids stale paths.
- Every referenced concrete path exists.
