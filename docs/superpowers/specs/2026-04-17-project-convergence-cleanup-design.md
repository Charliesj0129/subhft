# Project Convergence Cleanup — Design Spec

**Date**: 2026-04-17
**Author**: charlie.sj502@gmail.com
**Status**: Approved, pending implementation plan

## Goal

Converge the HFT Platform codebase from its current "research exploration" state into a "sustainable engineering" state. Enable fast iteration, easy maintenance, and eventual productization for Taiwan retail quant traders.

The pain points we are addressing, in priority order:

1. **Dead code / abandoned research artifacts** — 52 alpha research directories, many referencing KILLED work
2. **Blurry architecture boundaries** — package naming creates confusion for new developers (even when the split is technically correct)
3. **Documentation & agent memory bloat** — 379 docs files, 531 `.agent/` files, 200-line MEMORY.md index with ~20% stale entries

The chosen approach is **gradual, incremental cleanup** with CI verification at every step. No big-bang refactor. No architectural changes.

## Non-Goals (Explicit)

- No renaming of packages (`strategy/` vs `strategies/`, `engine/` vs `services/` stay as-is)
- No architectural refactor or module reorganization
- No test suite consolidation (test bloat is not a current pain point)
- No Docker / CI pipeline changes
- No pip packaging (that is a later productization step)
- No migration to a different license / open-source release
- No splitting into multiple repos

## Baseline Audit Findings

| Dimension | Value |
|-----------|-------|
| Python source | ~84K LOC, 24 packages |
| Rust extensions | ~7.5K LOC |
| Tests | ~211K LOC, 816 files |
| Research dirs | 52 alpha directories |
| Agent files | 531 |
| Docs files | 379 |
| Git-tracked files | 3,035 |
| Disk size | 30 GB |
| Runtime dependencies | 20 |

**Source code assessment**: mostly clean. Only `src/hft_platform/scripts/` is empty. The `strategy/strategies` and `engine/services` splits are architecturally correct.

**Real bloat sources**:
- `research/`: **5.9 GB**, 3,030 files, 999 git-tracked
  - `research/data/`: 5.1 GB of parquet
  - `research/arxiv_paper*/`: 526 MB of PDFs
  - `research/experiments/`: 268 MB
- `docs/alpha-research/`: 150 files, ~70% KILLED
- `.agent/skills/`: 391 files, 4.5 MB
- `MEMORY.md`: ~20% stale KILLED references

## Architecture: Outside-In Layers

Clean from lowest-risk outer ring to highest-risk inner ring. Each layer is one PR, CI verified, independently revertable.

```
┌──────────────────────────────────────────────┐
│ L1: docs/ + MEMORY.md + .agent/workflows/    │  zero runtime risk
├──────────────────────────────────────────────┤
│ L2: research/ artifacts + .gitignore         │  zero runtime risk
├──────────────────────────────────────────────┤
│ L3: .agent/skills/ + library/ + agents/      │  zero runtime risk
├──────────────────────────────────────────────┤
│ L4: source code polish (scripts/, docs gaps) │  low runtime risk
└──────────────────────────────────────────────┘
```

## Layer 1 — Docs & Memory Cleanup

Zero runtime risk. Only touches documentation and agent config files.

### 1a. `docs/alpha-research/` — Archive KILLED rounds

150 files, ~70% reference KILLED research.

**Delete** docs for rounds confirmed KILLED in MEMORY.md:
- R25, R25b, R26, R27, R28, R29 (institutional), R30, R31, R32a, R32b, R34, R35, R35b, R35-R39 cohort, R36, R47 (options combo), R50, R51, R52, R53, R54, R55, R55b

**Keep**:
- R29b (CONDITIONAL HOLD)
- R29 VRP (PARKED)
- R48 (PARKED)
- R49 (GAPS_IDENTIFIED)
- R47 Maker (deployed strategy)
- Structural lessons, methodology docs, pattern summaries

### 1b. MEMORY.md index cleanup

- Remove entries for all KILLED rounds; replace with a single "Research Archive (R25-R55b KILLED)" line linking to a consolidated summary
- Remove individual memory files that only describe KILLED work
- Keep: feedback memories, architecture memories, ops memories, deployed-strategy memories

### 1c. `.agent/workflows/` cleanup

- Delete `deploy-old-computer.md` and `deploy-docker-old-computer.md` (deprecated)
- Audit remaining workflows; keep only active procedures

### 1d. `docs/modules/` — Fill gaps

Add minimal module docs for the 3 currently undocumented modules:
- `docs/modules/infra.md` — ClickHouse client utilities
- `docs/modules/trade_classifier.md` — tick trade classification
- `docs/modules/main.md` — entry point orchestration

Match the style of existing module docs.

**Estimated impact**: –100+ files, significantly cleaner context for humans and AI agents.

## Layer 2 — Research Directory Cleanup

Zero runtime risk. `research/` is offline-only; not imported by the live trading pipeline. Verified by `grep -r "from research\|import research" src/ tests/` returning zero hits before any deletion.

### 2a. Evict bulk data from git

Add to `.gitignore` and remove from tracking (move to `~/research_data/` or an external volume):

```
research/data/              # 5.1 GB parquet data
research/arxiv_paper/       # 440 MB PDFs
research/arxiv_papers/      # 86 MB PDFs
research/experiments/runs/  # 268 MB experiment outputs
research/logs/
research/__pycache__/
research/results/
research/results_batch6/
research/results_batch7/
```

### 2b. Consolidate `research/alphas/` (52 directories)

Classify each directory using MEMORY.md as source of truth:

- **KEEP** in `research/alphas/` (active / parked / deployed):
  - `r47_maker`, `r29b`, `r29_vrp`, `r48_warrant`, currently-referenced kernels
- **ARCHIVE** — move to `research/archive/`: all KILLED rounds
- **DELETE** — empty stubs, template-only folders that were never filled

### 2c. Prune `research/archive/` itself

The existing `research/archive/` (7 MB) may have obsolete content. Audit and prune further.

### 2d. `.gitignore` hygiene

The `.gitignore` additions above become the new policy. All future research data files belong outside the tracked tree.

**Estimated impact**: git-tracked files 3,035 → ~2,100 (–30%); disk 30 GB → ~5 GB; clone time and `git status` latency drop dramatically.

## Layer 3 — Agent Configuration Cleanup

Zero runtime risk. Only affects `.agent/` files used by AI assistants.

### 3a. `.agent/skills/` audit (391 files, 4.5 MB — biggest single offender)

- Inventory all 173 skill subdirectories
- Classify each as:
  - **LIVE**: referenced in active workflows, recently invoked, or listed in `.agent/rules/`
  - **DUPLICATE**: overlaps with global skills at `~/.claude/plugins/` or `~/.claude/skills/`
  - **STALE**: references KILLED research, deprecated tooling, or old architecture
- **Action**: Delete DUPLICATE and STALE; keep LIVE. Target ~50% reduction (~200 files).

### 3b. `.agent/library/` review (19 files, 260 KB)

- Keep: `c4-model-current.md`, `cluster-evolution-backlog.md`, current architecture refs
- Prune: design-review artifacts for completed/abandoned work, outdated API references
- Cross-check against `docs/architecture/` (the authoritative source per CLAUDE.md)

### 3c. `.agent/evals/` — keep as-is

All 8 component evals are valuable regression anchors.

### 3d. `.agent/teams/` review (7 files)

Keep alpha-research team config. Remove any one-off experiment team configs.

### 3e. `.agent/agents/` review (12 files)

Audit role definitions. Remove any that duplicate global agents available in the harness.

**Estimated impact**: –200+ files, faster agent context load, less confusion for future AI sessions.

## Layer 4 — Source Code Polish

Low runtime risk. Touches actual code only in safe, well-bounded ways. CI runs after every change.

### 4a. Delete truly dead code

- `src/hft_platform/scripts/` — empty directory, delete
- `src/hft_platform/options/live_adapter.py` — unused (test-only, research phase). Delete; rebuild later when options trading is actually integrated. Also delete the orphan test file that covers it.

### 4b. Fill module documentation gaps

Add minimal module docs for:
- `src/hft_platform/infra/`
- `src/hft_platform/trade_classifier.py`
- `src/hft_platform/main.py`

### 4c. Naming clarity via documentation (no renames)

The `strategy/` vs `strategies/` split is technically correct (framework vs implementations), as is `engine/` vs `services/` (low-level event bus vs high-level service orchestration). Renaming would churn hundreds of imports for marginal clarity gain.

**Action**: Add a clear "Package Naming Convention" section to `CLAUDE.md` and a one-liner comment in each `__init__.py` explaining the split. No code moves.

### 4d. Dependency audit

Review all 20 entries in `pyproject.toml`:
- Is each still imported? (grep)
- Is the version pin still appropriate?
- `make>=0.1.6.post2` — suspicious, likely unused placeholder; verify and remove

Remove unused dependencies.

### 4e. Static dead-code pass

Run `vulture` (or equivalent) across `src/hft_platform/`. Report candidates to the user. User approves each deletion before committing. No automatic deletion from the static analysis.

**Estimated impact**: minor file reduction, material clarity boost. Risk is low because `make ci` runs after each change.

## Execution Strategy

### Branch & commit strategy

- One branch per layer: `cleanup/l1-docs`, `cleanup/l2-research`, `cleanup/l3-agent`, `cleanup/l4-source`
- Each layer = one PR to `main` after verification
- Within a layer, split into small commits (one logical change per commit) so `git revert` stays surgical

### Verification per layer

| Layer | Verification |
|-------|--------------|
| L1 | Visual review of index coherence; no runtime impact |
| L2 | `grep -r "from research\|import research" src/ tests/` returns zero before any deletion; `make test` passes |
| L3 | Manual agent session to confirm active skills still load; no runtime impact |
| L4 | `make ci` (lint + typecheck + test); smoke import test: `python -c "from hft_platform import main"` |

### Rollback safety

- Before L1: tag current state as `pre-cleanup-2026-04-17`
- Each layer's PR is a single squash merge — revertable as one commit
- Keep `research/archive/` as the graveyard for anything uncertain; do not delete from the graveyard for at least 2 weeks

### Order & timing

Strictly sequential, one layer per session:

1. **L1** first (docs/memory) — fastest, zero risk, immediate clarity
2. **L2** next (research) — biggest disk & git savings
3. **L3** (agent config) — quality-of-life for future AI sessions
4. **L4** (source code) — last because it is the only layer that touches runtime

## Success Criteria

After all four layers are merged:

- Git-tracked files reduced by ≥ 25% (3,035 → ~2,250 or lower)
- Disk size reduced by ≥ 80% (30 GB → ≤ 6 GB)
- `.agent/` total files reduced by ≥ 30%
- `docs/alpha-research/` retains only active/parked rounds
- `MEMORY.md` contains zero references to KILLED-and-permanently-archived rounds
- `make ci` passes on `main` at every layer merge
- Zero runtime regressions
- A new developer (or Claude session) reading the repo cold can identify current architecture without wading through obsolete research
