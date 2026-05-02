# Agent Config Best-Practices Alignment — Design

**Date:** 2026-04-19
**Author:** Claude (brainstorming session)
**Reference:** [Claude Code 最佳實踐 — 積極管理 context](https://code.claude.com/docs/zh-TW/best-practices#%E7%A9%8D%E6%A5%B5%E7%AE%A1%E7%90%86-context)
**Baseline tag:** `pre-agent-config-cleanup-2026-04-19` (to be created before SP1)

## Goal

Align `CLAUDE.md`, `.agent/`, and global `~/.claude/rules/` with the "積極管理 context" best-practice: keep every-turn context lean; put reference content on-demand via skills.

## Motivation

Current state violates the lean-context principle:

| Surface | Size | Load frequency |
|---|---|---|
| `CLAUDE.md` | 261 lines | Every conversation turn |
| `.agent/rules/*.md` (16 files) | 1430 lines | Every conversation turn (via auto-load) |
| `~/.claude/rules/common/*.md` (8 files) | ~300 lines | Every session across ALL projects |

The best-practices doc's single strongest rule: *"對於每一行，問自己：『刪除這一行會導致 Claude 犯錯誤嗎？』如果不會，刪除它。臃腫的 CLAUDE.md 文件會導致 Claude 忽略您的實際指令！"*

## Success Criteria

- `CLAUDE.md` ≤ 120 lines (from 261)
- `.agent/rules/` ≤ 800 lines (from 1430), no content duplicated in CLAUDE.md
- Global `~/.claude/rules/common/` ≤ 100 lines total (from ~300), 1–3 files
- Extracted reference tables accessible as `.claude/skills/<name>/SKILL.md` (fallback: `@path` imports)
- Baseline git tag `pre-agent-config-cleanup-2026-04-19` exists
- `make lint && make test` green after every sub-project
- No broken `@path/to/file` imports

## Policy Decisions (user-approved)

- **Global-rules handling:** back up to `~/.claude/rules/common/_archive-2026-04-19/` before trim (reversible).
- **Extraction destination:** `.claude/skills/<name>/SKILL.md` (Claude Code official path). Fallback to `.agent/library/` + `@path` imports if skills don't auto-discover.
- **Deletion aggressiveness:** hybrid — hard-delete obvious duplicates and stubs (<20 lines of real content); soft-archive borderline to `_archive-2026-04-19/` subfolders.

## Five Sub-Projects

Each sub-project lands as its own commit for bisect-ability and independent revert.

### SP1 — CLAUDE.md slim-down

**Target:** 261 → ≤120 lines.

**Keep** (load-bearing every turn):
- 🛡️ The 5 Critical HFT Laws (Allocator, Cache, Async, Precision, Boundary)
- 🏛️ Runtime pipeline diagram (single-glance orientation)
- 🚩 Red Flags checklist (reject criteria)
- Package Naming Convention (`strategy/` vs `strategies/`; `engine/` vs `services/`) — load-bearing, non-obvious
- Latency Realism Guard (mandatory research policy)
- Broker selection (`HFT_BROKER` env var) — one-liner
- Shell commands (setup, test, run, docker)
- Critical env-var short list (`HFT_MODE`, `HFT_ORDER_MODE`, `HFT_STRICT_PRICE_MODE`, `HFT_BROKER`) only

**Extract to skills** (reference content, load on demand):
- Rust export table (~30 lines) → `.claude/skills/hft-rust-exports/SKILL.md`
- Full env-var table (~50 lines) → `.claude/skills/hft-env-vars/SKILL.md`
- Data contracts table → `.claude/skills/hft-data-contracts/SKILL.md`

**Delete** (derivable from code):
- 7-plane runtime table (redundant with pipeline diagram + `docs/architecture/current-architecture.md`)
- Non-hot-path services table (in `docs/modules/` stubs already)
- Config priority chain (trivial from reading `config/loader.py`)
- Alpha governance gate table (in `src/hft_platform/alpha/` module docstrings)

### SP2 — `.agent/rules/` consolidation

**Target:** 1430 → ≤800 lines.

**Actions:**
1. Merge `30-git-workflow.md` + `35-git-hygiene.md` → single `30-git.md` (currently split artificially).
2. De-dup against CLAUDE.md: remove lines that also appear in CLAUDE.md (CLAUDE.md wins for laws + red flags; rules wins for operational detail).
3. Trim `55-enforcement.md` (84 lines) and `60-agent-workflow-governance.md` (252 lines) — over-specified; target 50% reduction.
4. Keep `00-index.md` as navigation; update it after trim.
5. Verify each remaining line passes "would removing this cause a mistake?".

**Per-file target sizes (approx):**

| File | Current | Target |
|---|---|---|
| `00-index.md` | 26 | 26 |
| `01-core-laws.md` | 31 | 31 |
| `05-project-structure.md` | 53 | 35 |
| `10-hft-performance.md` | 61 | 45 |
| `15-security.md` | 31 | 25 |
| `20-data-flow.md` | 47 | 40 |
| `25-architecture-governance.md` | 137 | 90 |
| `26-multi-broker-governance.md` | 67 | 55 |
| `30-git.md` (merged) | 77+32 | 60 |
| `40-ops.md` | 61 | 45 |
| `50-testing.md` | 35 | 30 |
| `55-enforcement.md` | 84 | 45 |
| `60-agent-workflow-governance.md` | 252 | 120 |
| `70-research-data.md` | 175 | 120 |
| **Total** | **1430** | **~770** |

### SP3 — Global `~/.claude/rules/common/` audit

**Target:** 8 files, ~300 lines → 1–3 files, ≤100 lines.

**Pre-step:** `mkdir ~/.claude/rules/common/_archive-2026-04-19 && cp ~/.claude/rules/common/*.md ~/.claude/rules/common/_archive-2026-04-19/` — keeps archive alongside originals so recovery is trivial.

**Per-file disposition (to be refined in planning):**

| File | Current content | Disposition |
|---|---|---|
| `performance.md` | Model selection, context window, extended thinking | Trim to terse bullet list |
| `hooks.md` | TodoWrite best practices, auto-accept | Trim / merge |
| `git-workflow.md` | Generic commit/PR workflow | Likely delete (per-project overrides this) |
| `patterns.md` | Generic repository/API patterns | Likely delete |
| `agents.md` | Lists `planner`, `architect`, `tdd-guide` etc. | Grep each agent name across projects; keep lean reference only if referenced, else delete |
| `security.md` | Pre-commit security checklist | Trim to 3 bullets |
| `coding-style.md` | Immutability, file org, error handling | Trim |
| `testing.md` | 80% coverage mandate, TDD | Trim / merge with coding-style |

**End state target:**
- `~/.claude/rules/common/core.md` — consolidated essentials (≤60 lines)
- `~/.claude/rules/common/agents.md` — reference (≤40 lines), only if agents are actively used
- Everything else deleted (archive has originals)

### SP4 — `.agent/skills/` dead-skill pass

**Scope:** detection + removal only; no rewrites.

**Checks:**
1. Missing `name:` or `description:` frontmatter → broken, archive
2. Duplicate `description:` across folders → flag and merge or delete
3. `SKILL.md` < 20 lines of non-frontmatter content → stub, archive
4. Folder without `SKILL.md` at all → broken, archive

**Output:** `docs/superpowers/specs/2026-04-19-agent-config-hygiene-report.md` with triage list per skill.

**Deletion policy (hybrid):**
- Hard-delete: exact duplicates (same frontmatter + same body)
- Archive to `.agent/skills/_archive-2026-04-19/`: stubs, borderline, unique-but-unreferenced

### SP5 — `.agent/agents/` + `.agent/commands/` inventory

**Scope:** 11 agents + 28 commands.

**Checks:**
1. Grep codebase + plugin index for references to each agent/command name
2. Zero references → candidate for archive
3. Validate frontmatter (`name:`, `description:`, optional `tools:`, `model:`)

**Output:** appended to same hygiene report from SP4.

**Deletion policy (hybrid):** same as SP4.

## Risks and Mitigations

### R1 — `.claude/skills/` may not auto-discover

The session's available-skills list shows plugin-namespaced skills (`superpowers:brainstorming`, `shioaji:shioaji`) but nothing from `.agent/skills/`, implying `.agent/skills/` is documentation-only. `.claude/skills/` has no precedent here either — needs verification.

**Mitigation:** Before extracting CLAUDE.md content, create one small test skill at `.claude/skills/hft-config-test/SKILL.md` with a distinctive `name:` and restart session / check available-skills list. If it appears: proceed with SP1 extraction. If not: fallback to `.agent/library/` + `@path` imports from CLAUDE.md.

### R2 — Aggressive trimming removes a silent-bug-preventer

**Mitigation:**
- Baseline git tag `pre-agent-config-cleanup-2026-04-19` before SP1
- Each SP is an independent commit (bisect-friendly, per-SP revert)
- Hybrid deletion policy archives borderline content
- `make lint && make test` after every SP

### R3 — Scope drift across 5 sub-projects

**Mitigation:**
- Per-SP commit + user review pause before next SP
- Single hygiene report file (one place to audit all the dispositions)
- Writing-plans skill drives tight execution loop

### R4 — Global rules changes affect other projects

**Mitigation:** Archive is co-located (`~/.claude/rules/common/_archive-2026-04-19/`). Any other project that loses a rule can recover from archive in one copy.

## Out of Scope

- Migrating 174 `.agent/skills/` entries to `.claude/skills/` — they're documentation by convention.
- Adding new hooks (best-practices doc recommends them; separate design).
- Editing plugin-installed skills (superpowers, shioaji, codex) — not project-owned.
- Touching `.agent/teams/`, `.agent/library/`, `.agent/memory/`, `.agent/workflows/` — not every-turn loads.
- `.claude/settings.local.json` permissions audit — separate concern.

## Execution Order

1. Create baseline tag.
2. Verify `.claude/skills/` auto-discovery (R1 mitigation).
3. SP1 → commit → pause for review.
4. SP2 → commit → pause.
5. SP3 → commit → pause.
6. SP4 → commit → pause.
7. SP5 → commit → final review.
8. Tag `post-agent-config-cleanup-2026-04-19`.

## Verification

After each SP:
- `make lint && make test`
- Grep for broken `@path` imports in CLAUDE.md and rules
- Spot-check: open a new Claude Code session, ask a question that should hit the extracted content; verify the skill loads or the `@path` resolves
- Line-count diff in commit message
