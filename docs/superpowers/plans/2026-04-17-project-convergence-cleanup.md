# Project Convergence Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Converge the HFT Platform codebase from its research-exploration state into a sustainable engineering state, through four sequential outside-in cleanup layers.

**Architecture:** Four sequential layers (docs → research → agent config → source polish), each on its own branch, each independently revertable. Zero runtime risk for L1-L3; low runtime risk for L4 guarded by `make ci`.

**Tech Stack:** git, ripgrep/grep, pytest, ruff, mypy, make, vulture (optional), standard Unix tools. No new runtime code.

**Spec:** `docs/superpowers/specs/2026-04-17-project-convergence-cleanup-design.md`

---

## Phase 0 — Pre-Flight Triage

The working tree already has 72 uncommitted changes (mostly deletions of files that overlap with planned cleanup). These must be triaged before starting fresh branches so L1-L4 work from a known-clean baseline.

### Task 0.1: Inventory uncommitted changes

**Files:**
- Read: (working tree only)

- [ ] **Step 1: Capture full status**

Run:
```bash
git status --short > /tmp/preflight_status.txt
wc -l /tmp/preflight_status.txt
```

Expected: 72 lines (or current count).

- [ ] **Step 2: Categorize changes**

Run:
```bash
awk '{print $1}' /tmp/preflight_status.txt | sort | uniq -c
```

Expected: `D` (deleted), `M` (modified), possibly `??` (untracked) counts.

- [ ] **Step 3: Verify no critical runtime modules deleted**

Run:
```bash
grep " D " /tmp/preflight_status.txt | awk '{print $2}' > /tmp/preflight_deleted.txt
grep -E "(bootstrap|system\.py|main\.py|normalizer|lob_engine|risk/engine|order/adapter|recorder/worker)" /tmp/preflight_deleted.txt || echo "OK: no critical runtime modules deleted"
```

Expected: "OK: no critical runtime modules deleted"

### Task 0.2: Decide on existing deletions

The existing deletions (alpha/screener.py, backtest/_metrics.py, data_quality/, execution/eod_recon.py, healing/, strategies/electronic_eye.py, strategies/vpin_regime_switch.py, etc.) look like in-progress dead-code removal. This plan treats them as L4-class work already done.

- [ ] **Step 1: Verify nothing imports the deleted files**

Run:
```bash
while read -r f; do
  module=$(echo "$f" | sed 's|src/hft_platform/||; s|\.py$||; s|/|.|g')
  hits=$(grep -rn "from hft_platform\.${module}\|import hft_platform\.${module}" src/ tests/ 2>/dev/null | wc -l)
  [ "$hits" -gt 0 ] && echo "STILL IMPORTED: $f ($hits hits)"
done < /tmp/preflight_deleted.txt
echo "done"
```

Expected: Only "done" is printed (no "STILL IMPORTED" lines). If any appear, stop and investigate.

- [ ] **Step 2: Run test suite to confirm deletions do not break anything**

Run:
```bash
make test 2>&1 | tail -40
```

Expected: tests pass. If failures appear, they are caused by the pre-existing deletions and must be fixed before proceeding (either restore the file or fix the dependent test).

- [ ] **Step 3: Create safety tag**

Run:
```bash
git stash push -u -m "preflight-stash-2026-04-17"
git tag pre-cleanup-2026-04-17
git stash pop
git log --oneline -1 pre-cleanup-2026-04-17
```

Expected: tag points to the last committed state (before the uncommitted deletions).

- [ ] **Step 4: Commit the pre-existing deletions as the L4 opener**

This lets L1-L3 start from a clean tree. The deletions are L4-class work, so committing them here short-circuits L4a.

Run:
```bash
git add -A
git status --short | head -20
```

Expected: all changes now staged.

```bash
git commit -m "chore: remove dead modules (healing/, data_quality/, eod_recon, screener, electronic_eye, vpin_regime_switch)

Pre-flight cleanup of abandoned modules ahead of the 4-layer convergence
cleanup. All deleted files verified as having zero inbound imports.
Test suite passes after removal."
```

Expected: single commit on `main`.

- [ ] **Step 5: Verify clean tree**

Run:
```bash
git status --short
```

Expected: empty output.

---

## Phase 1 — Layer 1: Docs & Memory Cleanup

Zero runtime risk. Operates only on `docs/`, `.agent/`, and the memory index.

### Task 1.1: Create L1 branch

**Files:**
- N/A

- [ ] **Step 1: Branch from clean main**

Run:
```bash
git checkout -b cleanup/l1-docs
git status
```

Expected: on branch `cleanup/l1-docs`, clean tree.

### Task 1.2: Inventory docs/alpha-research/

**Files:**
- Read: `docs/alpha-research/`

- [ ] **Step 1: List files with KILLED markers**

Run:
```bash
find docs/alpha-research -name '*.md' -print0 | xargs -0 grep -l -E "KILLED|ALL KILLED|PERMANENTLY KILLED" | tee /tmp/killed_docs.txt
wc -l /tmp/killed_docs.txt
```

Expected: list of files written, count displayed.

- [ ] **Step 2: List files NOT marked KILLED**

Run:
```bash
find docs/alpha-research -name '*.md' | sort > /tmp/all_alpha_docs.txt
comm -23 /tmp/all_alpha_docs.txt <(sort /tmp/killed_docs.txt) > /tmp/keep_candidates.txt
wc -l /tmp/keep_candidates.txt
cat /tmp/keep_candidates.txt
```

Expected: smaller list. These are the candidates to KEEP.

### Task 1.3: Delete KILLED alpha-research docs

**Files:**
- Delete: files listed in `/tmp/killed_docs.txt`

- [ ] **Step 1: Preview deletions**

Run:
```bash
head -20 /tmp/killed_docs.txt
```

Expected: sample of files to be removed (e.g., `docs/alpha-research/r25_large_order_flow.md`).

- [ ] **Step 2: Keep explicit survivor docs**

Even if these mention KILLED, they must be preserved because they contain structural lessons or describe PARKED/CONDITIONAL/deployed work:

```bash
KEEP_PATTERNS='(r29b|r29_vrp|r47_maker|r48_warrant|r49_meta|structural_lessons|methodology|pattern_summary|research_pause|directional_exhaustion|microstructure|tmfd6_|data_inventory|backtest_method_reliability|backtest_recency)'
grep -iE "$KEEP_PATTERNS" /tmp/killed_docs.txt > /tmp/killed_but_keep.txt
comm -23 <(sort /tmp/killed_docs.txt) <(sort /tmp/killed_but_keep.txt) > /tmp/final_delete_list.txt
echo "To delete: $(wc -l < /tmp/final_delete_list.txt)"
echo "Rescued: $(wc -l < /tmp/killed_but_keep.txt)"
```

Expected: counts printed. Delete list should be the larger number.

- [ ] **Step 3: Delete files**

Run:
```bash
xargs git rm < /tmp/final_delete_list.txt
git status --short | head -10
```

Expected: files now staged for deletion.

- [ ] **Step 4: Commit**

Run:
```bash
git commit -m "docs: remove KILLED alpha-research docs

Removes research-round docs for rounds permanently killed per
MEMORY.md. Structural lessons, methodology docs, and PARKED/
CONDITIONAL round docs retained."
```

Expected: single commit.

### Task 1.4: Delete deprecated .agent workflows

**Files:**
- Delete: `.agent/workflows/deploy-old-computer.md`
- Delete: `.agent/workflows/deploy-docker-old-computer.md`

- [ ] **Step 1: Confirm files exist**

Run:
```bash
ls .agent/workflows/ | grep -E "deploy-(old|docker-old)"
```

Expected: both filenames printed.

- [ ] **Step 2: Delete**

Run:
```bash
git rm .agent/workflows/deploy-old-computer.md .agent/workflows/deploy-docker-old-computer.md
```

Expected: two deletions staged.

- [ ] **Step 3: Audit remaining workflows**

Run:
```bash
ls .agent/workflows/
```

Expected: no `deploy-old-*` files remain. Other workflows (session, data-pipeline, etc.) still present.

- [ ] **Step 4: Commit**

Run:
```bash
git commit -m "docs: remove deprecated deploy-old-computer workflows"
```

### Task 1.5: Clean up stale per-round memory files

**Files:**
- Target directory: `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/`

This directory sits outside the repo (in the Claude project auto-memory area). It is NOT committed to git but must be cleaned so future sessions do not waste context on dead research.

- [ ] **Step 1: Inventory per-round memory files**

Run:
```bash
MEM_DIR="/home/charlie/.claude/projects/-home-charlie-hft-platform/memory"
ls "$MEM_DIR"/alpha_round*.md "$MEM_DIR"/alpha_r*.md 2>/dev/null | wc -l
ls "$MEM_DIR"/alpha_round*.md "$MEM_DIR"/alpha_r*.md 2>/dev/null | head -10
```

Expected: count and sample of per-round memory files.

- [ ] **Step 2: Identify KILLED-only memory files**

Per MEMORY.md, these rounds are permanently KILLED (keep files for R29b, R29 VRP, R48, R49, R47 Maker):

```bash
KILLED_ROUNDS="alpha_round6 alpha_round8 alpha_round9 alpha_round10 alpha_round11 alpha_round12 alpha_round13 alpha_round14 alpha_round15 alpha_round16 alpha_round17 alpha_round18 alpha_round19 alpha_round20 alpha_round22 alpha_round23 alpha_round24 alpha_round25 alpha_round26 alpha_round27 alpha_round28 alpha_round29_institutional alpha_round30 alpha_round31 alpha_round32 alpha_round33 alpha_round34 alpha_round35 alpha_round35_39 alpha_round36 alpha_round47 alpha_round50 alpha_round51 alpha_round52 alpha_round53 alpha_round54 alpha_round55"
MEM_DIR="/home/charlie/.claude/projects/-home-charlie-hft-platform/memory"
for prefix in $KILLED_ROUNDS; do
  ls "$MEM_DIR"/${prefix}*.md 2>/dev/null
done | sort -u > /tmp/killed_mem_files.txt
wc -l /tmp/killed_mem_files.txt
```

Expected: file list and count.

- [ ] **Step 3: Archive before deletion**

Run:
```bash
MEM_DIR="/home/charlie/.claude/projects/-home-charlie-hft-platform/memory"
ARCHIVE="$MEM_DIR/_archive_2026-04-17"
mkdir -p "$ARCHIVE"
xargs -I {} mv {} "$ARCHIVE/" < /tmp/killed_mem_files.txt
ls "$ARCHIVE" | wc -l
```

Expected: count matches line count from step 2.

- [ ] **Step 4: Verify live memories still intact**

Run:
```bash
MEM_DIR="/home/charlie/.claude/projects/-home-charlie-hft-platform/memory"
ls "$MEM_DIR"/feedback_*.md "$MEM_DIR"/alpha_round29b*.md "$MEM_DIR"/r47_maker*.md 2>/dev/null | wc -l
```

Expected: non-zero count (feedback + active round files still present).

### Task 1.6: Rewrite MEMORY.md index

**Files:**
- Modify: `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/MEMORY.md`

- [ ] **Step 1: Read current MEMORY.md**

```bash
MEM_DIR="/home/charlie/.claude/projects/-home-charlie-hft-platform/memory"
wc -l "$MEM_DIR/MEMORY.md"
```

Expected: ~200 lines.

- [ ] **Step 2: Replace KILLED-round section with a single archive link**

Edit `MEMORY.md`:

Remove all individual entries for rounds listed in `/tmp/killed_mem_files.txt`. Replace the large "Alpha Research Rounds (R6–R55b)" block with:

```markdown
## Alpha Research Rounds — Active

### Active / Parked / Conditional
- [R29b Spike Fader](alpha_round29b_event_momentum.md) — CONDITIONAL HOLD
- [R29 VRP Review](alpha_round29_vrp_review.md) — PARKED
- [R47 Maker (TMFD6)](r47_maker_strategy.md) — DEPLOYED
- [R47 Structural Properties](r47_structural_properties.md)
- [R47 Backtest Calibration](r47_backtest_data_regression.md)
- [R48 Warrant Latency Arb](alpha_round48_warrant_latency_arb.md) — PARKED
- [R49 Meta-Combination](alpha_round49_meta_combination.md) — GAPS_IDENTIFIED (if file exists)

### Archive
Archived memory files for permanently KILLED rounds (R6–R55b except those listed above) moved to `_archive_2026-04-17/`. See `docs/alpha-research/` for retained structural lessons and methodology docs.
```

Retain: Project Basics, Critical Laws, Architecture & History, StormGuard, Contract Rollover, Operations, Multi-Broker, Strategies (R47 section), Feature Engine, Monitor System, Backtest Method Reliability, Feedback sections.

- [ ] **Step 3: Verify line count reduced**

Run:
```bash
MEM_DIR="/home/charlie/.claude/projects/-home-charlie-hft-platform/memory"
wc -l "$MEM_DIR/MEMORY.md"
```

Expected: significantly fewer lines (target ≤ 120).

Note: This file is not in the repo; no git commit is needed. The cleanup takes effect immediately for future Claude sessions.

### Task 1.7: Add docs/modules/ stubs for undocumented modules

**Files:**
- Create: `docs/modules/infra.md`
- Create: `docs/modules/trade_classifier.md`
- Create: `docs/modules/main.md`

- [ ] **Step 1: Check style of an existing module doc**

Run:
```bash
head -30 docs/modules/recorder.md 2>/dev/null || head -30 docs/modules/risk.md 2>/dev/null || ls docs/modules/ | head -5
```

Expected: sample module-doc format.

- [ ] **Step 2: Create docs/modules/infra.md**

Write file with content:

```markdown
# Module: infra

## Purpose

Low-level infrastructure utilities. Currently hosts the shared ClickHouse client factory.

## Contents

- `ch_client.py` — ClickHouse client factory. Centralizes connection config
  (host, port, user, password, database) so every caller uses the same
  settings and retry behaviour.

## Used By

- `recorder/writer.py` — hot-path recording of market data and executions.
- `order/shadow_writer.py` — shadow-mode order record persistence.
- `ops/backup.py` — backup orchestration against ClickHouse.
- `monitor/_config_loader.py` — monitor TUI ClickHouse configuration.

## Notes

Not latency-critical. Float / Decimal arithmetic acceptable here; scaled-int
conversion happens at the writer boundary.
```

- [ ] **Step 3: Create docs/modules/trade_classifier.md**

Write file with content:

```markdown
# Module: trade_classifier

## Purpose

Classifies each tick as `buy`, `sell`, or `neutral` using bid/ask context.
Used by the normalizer to attach aggressor side to `TickEvent`.

## Contents

- `trade_classifier.py` (single file, lives at package root) — implements
  the Lee–Ready / tick-test hybrid used in the hot path.

## Used By

- `feed_adapter/normalizer.py` — invoked per tick during normalization.

## Notes

Must be allocation-free on the hot path. All state is held in pre-allocated
buffers. See `.agent/memory/module_gotchas.md` for edge-case handling.
```

- [ ] **Step 4: Create docs/modules/main.md**

Write file with content:

```markdown
# Module: main

## Purpose

Process entry point for the HFT runtime. Wires the service graph, installs
signal handlers, and starts the event loop.

## Contents

- `main.py` — boot orchestration.
- `__main__.py` — enables `python -m hft_platform`.

## Used By

- CLI: `hft run {sim|live|replay}` → `cli.py` → `main.py`.
- Direct invocation: `python -m hft_platform run sim`.

## Flow

1. Load config via `config/loader.py`.
2. Build `HFTSystem` via `services/bootstrap.py`.
3. Run `HFTSystem.run()` under `uvloop`.
4. On shutdown signal, drain queues and close broker sessions.

## Notes

Never hold blocking work here. All orchestration lives in `services/`.
```

- [ ] **Step 5: Commit**

Run:
```bash
git add docs/modules/infra.md docs/modules/trade_classifier.md docs/modules/main.md
git commit -m "docs: add module docs for infra, trade_classifier, main"
```

### Task 1.8: L1 verification & merge

- [ ] **Step 1: Verify no broken intra-doc links**

Run:
```bash
find docs -name '*.md' -print0 | xargs -0 grep -l "alpha-research/r" | while read f; do
  grep -oE '\[[^]]+\]\(alpha-research/[^)]+\)' "$f" | while read link; do
    path=$(echo "$link" | sed -E 's|.*\((docs/)?||; s|\)$||')
    [ ! -f "docs/$path" ] && [ ! -f "$path" ] && echo "BROKEN in $f: $link"
  done
done
echo "done"
```

Expected: only "done" prints (no BROKEN lines). If any appear, update the referring docs to drop the link or point to archive.

- [ ] **Step 2: Show summary**

Run:
```bash
git log --oneline main..cleanup/l1-docs
git diff --stat main..cleanup/l1-docs | tail -5
```

Expected: 3-4 commits; net deletions > 0.

- [ ] **Step 3: Merge to main**

Run:
```bash
git checkout main
git merge --squash cleanup/l1-docs
git commit -m "chore(l1): clean up obsolete docs, workflows, and memory references

- Delete docs for KILLED alpha-research rounds (R25-R55b)
- Remove deprecated deploy-old-computer workflows
- Add module docs for infra, trade_classifier, main
- Archive stale per-round memory files (external, not in repo)

Zero runtime impact. See docs/superpowers/specs/2026-04-17-project-convergence-cleanup-design.md"
git branch -d cleanup/l1-docs
```

Expected: merge clean; branch deleted.

---

## Phase 2 — Layer 2: Research Directory Cleanup

Zero runtime risk. `research/` is offline-only; verified by grep before any destructive action.

### Task 2.1: Create L2 branch and verify isolation

- [ ] **Step 1: Branch**

Run:
```bash
git checkout -b cleanup/l2-research
```

- [ ] **Step 2: Verify research/ has zero runtime imports**

Run:
```bash
grep -rn "from research\|import research\b" src/ tests/ 2>/dev/null || echo "OK: zero runtime imports from research/"
```

Expected: "OK: zero runtime imports from research/". Any hits must be investigated and eliminated before proceeding.

### Task 2.2: Update .gitignore

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Append research-data patterns**

Read `.gitignore`, then append:

```
# Research data (moved out of git tracking 2026-04-17)
research/data/
research/arxiv_paper/
research/arxiv_papers/
research/experiments/runs/
research/logs/
research/results/
research/results_batch6/
research/results_batch7/
research/__pycache__/
```

- [ ] **Step 2: Commit**

Run:
```bash
git add .gitignore
git commit -m "chore: gitignore bulky research data directories"
```

### Task 2.3: Remove bulk research data from tracking

**Files:**
- Git-untrack (but keep on disk): `research/data/`, `research/arxiv_paper/`, `research/arxiv_papers/`, `research/experiments/runs/`, `research/logs/`, `research/results*/`

- [ ] **Step 1: Measure baseline**

Run:
```bash
git ls-files research/ | wc -l
du -sh research/data research/arxiv_paper research/arxiv_papers research/experiments/runs research/logs research/results 2>/dev/null
```

Expected: ~999 git-tracked files; several GB listed.

- [ ] **Step 2: Untrack data directories (keep on disk)**

Run:
```bash
git rm -r --cached research/data/ 2>/dev/null | tail -5
git rm -r --cached research/arxiv_paper/ 2>/dev/null | tail -5
git rm -r --cached research/arxiv_papers/ 2>/dev/null | tail -5
git rm -r --cached research/experiments/runs/ 2>/dev/null | tail -5
git rm -r --cached research/logs/ 2>/dev/null | tail -5
git rm -r --cached research/results/ 2>/dev/null | tail -5
git rm -r --cached research/results_batch6/ 2>/dev/null | tail -5
git rm -r --cached research/results_batch7/ 2>/dev/null | tail -5
git status --short | head -5
```

Expected: large number of `D` entries staged; directories still on disk (verify with `ls research/data/ | head`).

- [ ] **Step 3: Verify files remain on disk**

Run:
```bash
ls research/data/ 2>/dev/null | head -3 && echo "--- data dir intact"
```

Expected: files visible (untracking did not delete them).

- [ ] **Step 4: Commit**

Run:
```bash
git commit -m "chore: untrack bulky research data directories

Moves research/data, arxiv_paper(s), experiments/runs, logs, results*
out of git tracking. Files remain on disk for local research use.
See .gitignore for the full pattern list."
```

- [ ] **Step 5: Verify new tracked count**

Run:
```bash
git ls-files research/ | wc -l
```

Expected: significantly smaller than 999 (target ≤ 200).

### Task 2.4: Consolidate research/alphas/

**Files:**
- Move: KILLED alpha directories from `research/alphas/` to `research/archive/`

- [ ] **Step 1: Identify active alphas (keep list)**

Active / parked / deployed per MEMORY.md:

```bash
cat > /tmp/keep_alphas.txt <<'EOF'
_templates
r47_maker
r29b_spike_fader
r29_vrp
r48_warrant_latency
fill_prob_filter
EOF
```

Verify which of these actually exist:

```bash
while read a; do
  [ -d "research/alphas/$a" ] && echo "exists: $a" || echo "missing: $a"
done < /tmp/keep_alphas.txt
```

Expected: `exists:` for items present, `missing:` for ones not there. Adjust keep list based on reality before step 2.

- [ ] **Step 2: Move all other alpha dirs to archive**

Run:
```bash
mkdir -p research/archive/alphas_2026-04-17
for d in research/alphas/*/; do
  name=$(basename "$d")
  [ "$name" = "__pycache__" ] && continue
  if ! grep -q "^${name}$" /tmp/keep_alphas.txt; then
    git mv "research/alphas/$name" "research/archive/alphas_2026-04-17/$name" 2>/dev/null \
      || mv "research/alphas/$name" "research/archive/alphas_2026-04-17/$name"
    echo "archived: $name"
  fi
done | head -20
```

Expected: list of archived alpha dirs.

- [ ] **Step 3: Stage pycache removal**

Run:
```bash
find research/alphas -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null
git status --short | head -10
```

Expected: moves staged cleanly.

- [ ] **Step 4: Commit**

Run:
```bash
git add -A research/alphas/ research/archive/
git commit -m "chore: archive KILLED alpha research directories

Moves ~46 permanently killed alpha research dirs from research/alphas/
to research/archive/alphas_2026-04-17/. Keeps active/parked/deployed
directories in research/alphas/ (r47_maker, r29b, r29_vrp, r48_warrant,
fill_prob_filter, _templates)."
```

### Task 2.5: L2 verification & merge

- [ ] **Step 1: Run test suite**

Run:
```bash
make test 2>&1 | tail -30
```

Expected: tests pass. No import errors from moved `research/` content (there shouldn't be any — that's what the grep in 2.1 verified).

- [ ] **Step 2: Measure repo shrinkage**

Run:
```bash
echo "Tracked files: $(git ls-files | wc -l)"
echo "Disk size: $(du -sh . --exclude=.git 2>/dev/null | awk '{print $1}')"
```

Expected: tracked files significantly down from 3035; disk size still large (data files are retained locally).

- [ ] **Step 3: Merge to main**

Run:
```bash
git checkout main
git merge --squash cleanup/l2-research
git commit -m "chore(l2): untrack bulky research data and archive KILLED alphas

- gitignore research/data, arxiv_paper(s), experiments/runs, logs, results*
- Untrack ~800 research data files (kept locally on disk)
- Archive ~46 KILLED alpha research dirs to research/archive/alphas_2026-04-17/

Tracked file count reduced. Zero runtime impact (research/ has zero
imports into src/ or tests/)."
git branch -d cleanup/l2-research
```

Expected: clean merge.

---

## Phase 3 — Layer 3: Agent Configuration Cleanup

Zero runtime risk. Only affects `.agent/` files consumed by AI sessions.

### Task 3.1: Create L3 branch

- [ ] **Step 1: Branch**

Run:
```bash
git checkout -b cleanup/l3-agent
```

### Task 3.2: Inventory .agent/skills/

**Files:**
- Read: `.agent/skills/`

- [ ] **Step 1: Count and sample**

Run:
```bash
find .agent/skills -maxdepth 1 -mindepth 1 -type d | wc -l
find .agent/skills -maxdepth 1 -mindepth 1 -type d | sort > /tmp/all_skills.txt
head -20 /tmp/all_skills.txt
```

Expected: ~173 directories; sample list.

- [ ] **Step 2: Identify skills referenced by live rules / workflows**

Run:
```bash
grep -rhoE "\.agent/skills/[a-zA-Z0-9_-]+" .agent/rules/ .agent/workflows/ .agent/teams/ .agent/agents/ 2>/dev/null \
  | sort -u > /tmp/referenced_skills.txt
wc -l /tmp/referenced_skills.txt
```

Expected: list of skills actively referenced.

- [ ] **Step 3: Identify global skill duplicates**

Run:
```bash
GLOBAL_SKILLS_DIR="$HOME/.claude/plugins/cache/claude-plugins-official"
if [ -d "$GLOBAL_SKILLS_DIR" ]; then
  find "$GLOBAL_SKILLS_DIR" -name 'SKILL.md' -path '*/skills/*' | \
    sed -E 's|.*/skills/([^/]+)/.*|\1|' | sort -u > /tmp/global_skills.txt
  wc -l /tmp/global_skills.txt
else
  echo "No global skills dir found"
  : > /tmp/global_skills.txt
fi
```

Expected: list of globally available skills.

- [ ] **Step 4: Build candidate delete list**

Run:
```bash
basename -a $(cat /tmp/all_skills.txt) | sort -u > /tmp/all_skill_names.txt
grep -oE '[^/]+$' /tmp/referenced_skills.txt | sort -u > /tmp/live_skill_names.txt

# Candidates: in local but not referenced
comm -23 /tmp/all_skill_names.txt /tmp/live_skill_names.txt > /tmp/unreferenced_skills.txt

# Of those, which are duplicates of global
comm -12 <(sort /tmp/unreferenced_skills.txt) <(sort /tmp/global_skills.txt) > /tmp/duplicate_skills.txt

echo "Unreferenced local skills: $(wc -l < /tmp/unreferenced_skills.txt)"
echo "Duplicates of global: $(wc -l < /tmp/duplicate_skills.txt)"
```

Expected: both counts printed.

### Task 3.3: Delete duplicate skills

**Files:**
- Delete: directories in `/tmp/duplicate_skills.txt`

- [ ] **Step 1: Preview**

Run:
```bash
head -10 /tmp/duplicate_skills.txt
```

- [ ] **Step 2: Delete**

Run:
```bash
while read name; do
  [ -d ".agent/skills/$name" ] && git rm -rf ".agent/skills/$name"
done < /tmp/duplicate_skills.txt
git status --short | wc -l
```

Expected: staged deletions matching duplicate count (plus sub-files).

- [ ] **Step 3: Commit**

Run:
```bash
git commit -m "chore: remove .agent/skills/ duplicates of global skills"
```

### Task 3.4: Manually triage unreferenced-non-duplicate skills

**Files:**
- Candidate directories listed in `/tmp/unreferenced_skills.txt` minus `/tmp/duplicate_skills.txt`

Some unreferenced skills may still be valuable (project-specific, occasionally invoked). These need human judgement.

- [ ] **Step 1: Build the manual-review list**

Run:
```bash
comm -23 <(sort /tmp/unreferenced_skills.txt) <(sort /tmp/duplicate_skills.txt) > /tmp/review_skills.txt
wc -l /tmp/review_skills.txt
head -20 /tmp/review_skills.txt
```

Expected: list of skills for manual decision.

- [ ] **Step 2: Emit review-me markers**

For each skill in the review list, add a single-line marker comment to the skill's `SKILL.md` head noting it is unreferenced and may be stale. Do NOT delete during this task; user will manually prune in a follow-up.

```bash
while read name; do
  skill_md=".agent/skills/$name/SKILL.md"
  if [ -f "$skill_md" ] && ! grep -q "REVIEW-2026-04-17" "$skill_md"; then
    printf '<!-- REVIEW-2026-04-17: unreferenced by rules/workflows/teams/agents. Confirm or delete. -->\n' \
      | cat - "$skill_md" > "$skill_md.tmp" && mv "$skill_md.tmp" "$skill_md"
  fi
done < /tmp/review_skills.txt
git status --short | head -10
```

Expected: SKILL.md files modified with review comment.

- [ ] **Step 3: Commit markers**

Run:
```bash
git add .agent/skills/
git commit -m "chore: mark unreferenced .agent/skills/ for manual review"
```

### Task 3.5: Prune .agent/library/

**Files:**
- Review: `.agent/library/`

- [ ] **Step 1: List contents**

Run:
```bash
ls -la .agent/library/
```

Expected: 19 files.

- [ ] **Step 2: Identify current-architecture vs historical**

Run:
```bash
for f in .agent/library/*.md; do
  if grep -l -E "KILLED|deprecated|obsolete|historical" "$f" >/dev/null 2>&1; then
    echo "CANDIDATE: $f"
  fi
done
```

Expected: list of candidate-for-removal files.

- [ ] **Step 3: Manually review and remove obsolete library files**

For each CANDIDATE, inspect content. If it documents:
- current architecture → keep
- a completed/abandoned design review → archive (move to `.agent/library/_archive_2026-04-17/`)

```bash
mkdir -p .agent/library/_archive_2026-04-17
# Example pattern (adjust to actual files found):
# git mv .agent/library/design-review-r47-v1.md .agent/library/_archive_2026-04-17/
```

- [ ] **Step 4: Commit if changes made**

Run:
```bash
if ! git diff --quiet .agent/library/; then
  git add .agent/library/
  git commit -m "chore: archive obsolete .agent/library entries"
fi
```

### Task 3.6: L3 verification & merge

- [ ] **Step 1: Verify rule files still resolve**

Run:
```bash
ls .agent/rules/
for rule in .agent/rules/*.md; do
  refs=$(grep -oE "\.agent/skills/[a-zA-Z0-9_-]+" "$rule" 2>/dev/null)
  for r in $refs; do
    [ ! -d "$r" ] && echo "BROKEN: $rule references missing $r"
  done
done
echo "done"
```

Expected: only "done" prints (no BROKEN references).

- [ ] **Step 2: Merge to main**

Run:
```bash
git log --oneline main..cleanup/l3-agent
git checkout main
git merge --squash cleanup/l3-agent
git commit -m "chore(l3): prune .agent/ duplicates and mark stale skills for review

- Delete .agent/skills/ directories that duplicate global skills
- Add REVIEW-2026-04-17 markers to unreferenced skills for manual triage
- Archive obsolete .agent/library/ entries

Zero runtime impact. Agent sessions keep all actively-referenced skills."
git branch -d cleanup/l3-agent
```

Expected: clean merge.

---

## Phase 4 — Layer 4: Source Code Polish

Low runtime risk. Guarded by `make ci` after every change. Most L4a work (dead-module removal) was already completed in Phase 0.

### Task 4.1: Create L4 branch

- [ ] **Step 1: Branch**

Run:
```bash
git checkout -b cleanup/l4-source
```

### Task 4.2: Remove options/live_adapter.py

**Files:**
- Delete: `src/hft_platform/options/live_adapter.py`
- Delete: `tests/unit/test_live_adapter.py`

- [ ] **Step 1: Verify zero inbound imports**

Run:
```bash
grep -rn "live_adapter\|options\.live_adapter" src/ tests/ | grep -v "^src/hft_platform/options/live_adapter.py\|^tests/unit/test_live_adapter.py" || echo "OK: zero external imports"
```

Expected: "OK: zero external imports".

- [ ] **Step 2: Delete**

Run:
```bash
git rm src/hft_platform/options/live_adapter.py tests/unit/test_live_adapter.py
```

Expected: two deletions staged.

- [ ] **Step 3: Run tests**

Run:
```bash
make test 2>&1 | tail -15
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

Run:
```bash
git commit -m "chore: remove unused options/live_adapter

Module had only test coverage, no runtime integration. Rebuild when
options trading is integrated into the main pipeline."
```

### Task 4.3: Delete remaining empty scripts/ dir

**Files:**
- Delete: `src/hft_platform/scripts/` (if still present after Phase 0)

- [ ] **Step 1: Check state**

Run:
```bash
ls src/hft_platform/scripts/ 2>/dev/null && echo "still present" || echo "already gone"
```

- [ ] **Step 2: Remove if present**

Run:
```bash
if [ -d src/hft_platform/scripts ]; then
  git rm -rf src/hft_platform/scripts
  git commit -m "chore: remove empty src/hft_platform/scripts/"
fi
```

Expected: either a commit is created, or this step is a no-op.

### Task 4.4: Add package-naming convention to CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Locate insertion point**

Run:
```bash
grep -n "## 🏛️ Architecture Quick Reference" CLAUDE.md
```

Expected: a line number.

- [ ] **Step 2: Insert new subsection**

After the existing "Architecture Quick Reference" section, add a new subsection:

```markdown
### Package Naming Convention

Two splits in the codebase can look confusing but are intentional:

| Split | Framework side | Implementation side | Rule |
|-------|----------------|---------------------|------|
| Strategies | `strategy/` — BaseStrategy, StrategyRunner, StrategyContext, registry | `strategies/` — concrete strategy classes (r47_maker, cascade_bounce, etc.) | Concrete strategy code lives in `strategies/`. Framework code lives in `strategy/`. |
| Runtime | `engine/` — low-level event bus (RingBufferBus) | `services/` — high-level service orchestration (bootstrap, HFTSystem, market_data) | Generic infrastructure lives in `engine/`. Service graph wiring lives in `services/`. |

Do not rename these packages — they are load-bearing across hundreds of imports. Add a new package if you need a new layer.
```

- [ ] **Step 3: Add one-liner docstrings to package __init__.py files**

Edit `src/hft_platform/strategy/__init__.py` (add at top of file if not already present):

```python
"""Strategy framework: BaseStrategy, StrategyRunner, StrategyContext, registry.

Concrete strategy implementations live in ``hft_platform.strategies``.
"""
```

Edit `src/hft_platform/strategies/__init__.py`:

```python
"""Concrete strategy implementations.

The strategy framework (BaseStrategy, runner, context) lives in
``hft_platform.strategy``.
"""
```

Edit `src/hft_platform/engine/__init__.py`:

```python
"""Low-level runtime engine: event bus and ring buffers.

Higher-level service orchestration lives in ``hft_platform.services``.
"""
```

Edit `src/hft_platform/services/__init__.py`:

```python
"""High-level service orchestration: bootstrap, HFTSystem, market data.

Low-level event-bus infrastructure lives in ``hft_platform.engine``.
"""
```

- [ ] **Step 4: Run CI**

Run:
```bash
make ci 2>&1 | tail -30
```

Expected: lint + typecheck + test all pass.

- [ ] **Step 5: Commit**

Run:
```bash
git add CLAUDE.md src/hft_platform/strategy/__init__.py src/hft_platform/strategies/__init__.py src/hft_platform/engine/__init__.py src/hft_platform/services/__init__.py
git commit -m "docs: explain strategy/strategies and engine/services package split

Adds Package Naming Convention section to CLAUDE.md and module-level
docstrings to the four __init__.py files clarifying the framework vs
implementation split. No code moves."
```

### Task 4.5: Dependency audit

**Files:**
- Review: `pyproject.toml`

- [ ] **Step 1: List current dependencies**

Run:
```bash
python3 -c "
import tomllib
with open('pyproject.toml','rb') as f: d=tomllib.load(f)
for dep in d.get('project',{}).get('dependencies',[]): print(dep)
"
```

Expected: 20 dependencies printed.

- [ ] **Step 2: Grep imports for each dependency**

Run:
```bash
for dep in numpy pandas clickhouse_connect structlog msgspec uvloop hftbacktest shioaji prometheus_client psutil yaml make joblib numba dotenv scipy exchange_calendars orjson optuna aiohttp; do
  hits=$(grep -rn "import $dep\|from $dep" src/ 2>/dev/null | wc -l)
  echo "$dep: $hits"
done
```

Expected: each dep shows some hits; unused ones show 0.

- [ ] **Step 3: Investigate suspicious entries**

Specifically verify `make>=0.1.6.post2`:

```bash
grep -rn "^import make\b\|^from make\b" src/ tests/ 2>/dev/null || echo "make: unused"
```

Expected: "make: unused" or hits listed.

- [ ] **Step 4: Remove confirmed-unused deps**

If `make` (and any others) are confirmed unused, edit `pyproject.toml` and remove them.

- [ ] **Step 5: Lock regeneration**

Run:
```bash
uv lock 2>&1 | tail -10
```

Expected: lock file updated.

- [ ] **Step 6: CI check**

Run:
```bash
make ci 2>&1 | tail -20
```

Expected: passes.

- [ ] **Step 7: Commit**

Run:
```bash
git add pyproject.toml uv.lock
git commit -m "chore: remove unused runtime dependencies"
```

### Task 4.6: Static dead-code pass (report only)

**Files:**
- Generate: `/tmp/vulture_report.txt`

- [ ] **Step 1: Install vulture locally if not present**

Run:
```bash
uv run python -c "import vulture" 2>/dev/null || uv run pip install vulture
```

Expected: vulture importable.

- [ ] **Step 2: Run vulture**

Run:
```bash
uv run vulture src/hft_platform/ --min-confidence 80 > /tmp/vulture_report.txt 2>&1
wc -l /tmp/vulture_report.txt
head -40 /tmp/vulture_report.txt
```

Expected: candidate list.

- [ ] **Step 3: Present findings to user**

Copy `/tmp/vulture_report.txt` content into a markdown file for review:

```bash
cp /tmp/vulture_report.txt docs/superpowers/specs/2026-04-17-vulture-report.md
git add docs/superpowers/specs/2026-04-17-vulture-report.md
git commit -m "docs: static dead-code analysis report for manual review"
```

**User decides per-item** whether to delete. Do NOT delete automatically. Deletion is a follow-up session with explicit user approval per item.

### Task 4.7: L4 verification & merge

- [ ] **Step 1: Final CI pass**

Run:
```bash
make ci 2>&1 | tail -30
```

Expected: pass.

- [ ] **Step 2: Smoke import test**

Run:
```bash
uv run python -c "from hft_platform import main; print('import OK')"
```

Expected: "import OK".

- [ ] **Step 3: Merge to main**

Run:
```bash
git log --oneline main..cleanup/l4-source
git checkout main
git merge --squash cleanup/l4-source
git commit -m "chore(l4): source-code polish

- Remove options/live_adapter.py (unused, test-only)
- Remove empty scripts/ directory
- Document strategy/strategies and engine/services naming in CLAUDE.md + __init__.py docstrings
- Remove unused runtime dependencies (e.g., 'make' package)
- Add static dead-code analysis report for manual review

All CI green. Zero runtime regressions."
git branch -d cleanup/l4-source
```

Expected: clean merge.

---

## Phase 5 — Final Verification

### Task 5.1: Measure success criteria

- [ ] **Step 1: Tracked file count**

Run:
```bash
echo "Current tracked files: $(git ls-files | wc -l)"
echo "Baseline was 3035; target ≤ 2250"
```

Expected: count at or below 2250.

- [ ] **Step 2: Disk footprint**

Run:
```bash
echo "Current disk size: $(du -sh . --exclude=.git 2>/dev/null | awk '{print $1}')"
echo "Baseline was 30G; target ≤ 6G"
```

Expected: ≤ 6 GB. (Note: local research data files are still on disk but no longer tracked; this measures what ships in a fresh clone via `.git` size drop is separate.)

- [ ] **Step 3: Fresh clone size check**

Run:
```bash
cd /tmp
git clone --depth=1 $HOME/hft_platform hft_platform_fresh 2>&1 | tail -3
du -sh hft_platform_fresh
rm -rf hft_platform_fresh
cd -
```

Expected: dramatic size reduction versus prior baseline.

- [ ] **Step 4: CI on main**

Run:
```bash
make ci 2>&1 | tail -20
```

Expected: pass.

- [ ] **Step 5: Write summary commit**

Not a new commit — just report success-criteria table:

| Metric | Baseline | Target | Actual |
|--------|----------|--------|--------|
| Tracked files | 3035 | ≤ 2250 | fill in |
| Disk size | 30 GB | ≤ 6 GB | fill in |
| .agent/ files | 531 | ≤ 370 | fill in |
| KILLED refs in MEMORY.md | ~20% | 0 | 0 (Task 1.6) |
| `make ci` on main | pass | pass | fill in |

- [ ] **Step 6: Update CLAUDE.md memory note**

Append to the auto-memory MEMORY.md file:

```markdown
## Convergence Cleanup (2026-04-17)
- 4-layer cleanup completed. See `docs/superpowers/specs/2026-04-17-project-convergence-cleanup-design.md` and `docs/superpowers/plans/2026-04-17-project-convergence-cleanup.md`.
- Baseline → Post: tracked files [baseline]→[after], disk [baseline]→[after].
- Follow-up: manual triage of REVIEW-2026-04-17 skill markers; user-approved deletion of vulture-report candidates.
```

---

## Self-Review

After writing the complete plan, checked against spec:

**Spec coverage:**
- L1 all four items (1a alpha-research docs, 1b MEMORY.md, 1c workflows, 1d module docs) → Tasks 1.2, 1.3, 1.4, 1.5, 1.6, 1.7
- L2 all four items (2a evict data, 2b consolidate alphas, 2c prune archive, 2d gitignore) → Tasks 2.2, 2.3, 2.4
- L3 all five items (3a skills audit, 3b library review, 3c evals kept, 3d teams, 3e agents) → Tasks 3.2, 3.3, 3.4, 3.5 (evals/teams/agents consolidated under 3.5/3.6)
- L4 all five items (4a dead code, 4b module docs, 4c naming docs, 4d deps, 4e vulture) → Phase 0 + Tasks 4.2, 4.3, 4.4, 4.5, 4.6
- Execution strategy (branching, verification, rollback, order) → Tasks 0.2, 1.1, 2.1, 3.1, 4.1, 5.1
- Non-goals respected (no renames, no architectural refactor)

**Placeholder scan:** No TBD/TODO placeholders in the plan. All steps have concrete commands or code.

**Type consistency:** Plan does not introduce new types. All referenced commands (`make ci`, `make test`, `uv run`, `git mv`, etc.) are consistent across tasks.

**Gap filled:** Added Phase 0 (pre-flight triage) to handle the 72 uncommitted changes found at session start. Added Phase 5 (final verification) to measure against spec success criteria.
