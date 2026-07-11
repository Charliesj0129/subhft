# .agent/ Manifest — Subsystem Lifecycle

`.agent/` has accumulated several generations of agent frameworks. This
manifest is the single answer to "which subsystem is live?" for any agent or
human landing here. Labels:

- **ACTIVE** — part of Agent System v2 (`CLAUDE.md` → `AGENTS.md` →
  rules/skills/memory). Read it, maintain it.
- **DEPRECATED** — superseded by v2. Do not follow it for new work; kept in
  place for reference. Any deletion or move is a separate, per-case,
  user-approved operation.
- **ARCHIVE-CANDIDATE** — runtime output or dead-generation data with no
  reference value in place; first in line for relocation out of `.agent/`
  (still user-approved, per case).

A new `.agent/` subdirectory requires a row here at creation time.

## Inventory (audited 2026-07-10; DEPRECATED/ARCHIVE-CANDIDATE entries removed 2026-07-11 — see §Removed)

| Path | Tracked | Status | Judgement |
|---|---|---|---|
| `rules/` | tracked | ACTIVE | v2 guardrails; indexed by `rules/00-index.md`; mandatory reads per `CLAUDE.md` |
| `skills/` | 84 files | ACTIVE | v2 procedures; indexed by `skills/00-index.md` |
| `memory/` | 12 files (force-added; dir is gitignored) | ACTIVE | v2 ledgers; routing table in `memory/README.md` |
| `templates/` | 1 tracked (force-added) | ACTIVE | `ADR_TEMPLATE.md` — required by governance change control for authority/tier changes (`rules/60-agent-workflow-governance.md`) |
| `CHANGELOG.md` | tracked (force-added) | ACTIVE | one-line-per-change governance history; updated in every `docs(agents):` commit |
| `reports/` | untracked | ACTIVE (destination) | target directory for periodic meta-audit reports (proposal #15); existing 2026-03 files are historical evidence |
| `library/` | 5 tracked / 17 on disk | ACTIVE (with known drift) | reference shelf (broker/API/architecture docs); at least one skill reference points to a missing file (`library/design-review-artifacts.md` cited by `hft-architect`) — to be caught by the agent-docs checker (proposal #2) |
| `hooks/` | untracked | ACTIVE | `verify_health.sh` (called by the `healthcheck` skill) + `README.md` |
| `evals/` | 5 tracked / 9 on disk | ACTIVE (golden intake cases) | `golden-intake-tasks.md` = routing regression cases run after routing-relevant governance changes (#8); legacy 2026-02/03 module-eval specs retained as historical reference |
| `teams/alpha-research/rounds/` | untracked | EVIDENCE | research round artifacts (R56) — append-only, never rewrite; the surrounding team framework was removed 2026-07-11 |
| `agent-docs-known-drift.txt` | tracked (force-added) | ACTIVE | ratchet baseline for `scripts/check_agent_docs.py` (`make agent-docs-check`) |

## Removed (2026-07-11, user-approved cleanup)

Deleted after the 2026-07-10 audit above confirmed zero live references; tag
`pre-cleanup-2026-07-11` marks the pre-deletion state, tracked files remain
recoverable from git history (commit hash recoverable from `git log` by this
date). Untracked entries had no git history and are gone permanently — that
trade-off was explicitly approved.

- `rules/ecc/` (tracked, 16 files) — ECC-generation rule set
- `contexts/` (tracked, 3 files) — ECC-generation context presets
- `workflows/` (1 tracked + opsx-* on disk) — ECC-generation workflows
- `teams/alpha-research/` framework (tracked 9 + untracked role/hook strays; `rounds/` evidence kept)
- `agents/`, `commands/`, `pixiu/`, `logs/`, `mcp/`, `extensions/`, `project_context.json`, `hooks/ecc_hooks.json` (all untracked)
- `.claude/commands/alpha-research.md` (tracked) — drove the removed team framework

## Method

Judgements grounded in two scans (2026-07-10): reference count from v2
governing docs (`rg --hidden "\.agent/<dir>" CLAUDE.md AGENTS.md Makefile
.agent/rules .agent/skills .agent/memory docs/MODULES_REFERENCE.md`) and
tracked-state from `git ls-files .agent`. "Untracked" means the content is
invisible to any clone of this repo — it exists only on this machine.
