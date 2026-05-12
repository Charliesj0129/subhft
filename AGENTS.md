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
