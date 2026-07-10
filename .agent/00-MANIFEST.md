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

## Inventory (audited 2026-07-10)

| Path | Tracked | Status | Judgement |
|---|---|---|---|
| `rules/` | 27 files | ACTIVE | v2 guardrails; indexed by `rules/00-index.md`; mandatory reads per `CLAUDE.md` |
| `rules/ecc/` | subset of above | DEPRECATED | ECC-generation rule set duplicating v2 rules; absent from `rules/00-index.md` |
| `skills/` | 84 files | ACTIVE | v2 procedures; indexed by `skills/00-index.md` |
| `memory/` | 12 files (force-added; dir is gitignored) | ACTIVE | v2 ledgers; routing table in `memory/README.md` |
| `templates/` | untracked | ACTIVE | `ADR_TEMPLATE.md` — used by governance change-control (institutionalization proposal #3) |
| `reports/` | untracked | ACTIVE (destination) | target directory for periodic meta-audit reports (proposal #15); existing 2026-03 files are historical evidence |
| `library/` | 5 tracked / 17 on disk | ACTIVE (with known drift) | reference shelf (broker/API/architecture docs); at least one skill reference points to a missing file (`library/design-review-artifacts.md` cited by `hft-architect`) — to be caught by the agent-docs checker (proposal #2) |
| `hooks/` | untracked | ACTIVE (`verify_health.sh` only) | `verify_health.sh` is called by the `healthcheck` skill; `ecc_hooks.json` is ECC-generation and DEPRECATED (not wired into `.claude/settings.json`) |
| `agents/` | untracked | DEPRECATED | role definitions superseded by `AGENTS.md` §Roles; sole remaining reference (`research-factory` skill table) cites paths that do not exist as written |
| `commands/` | untracked | DEPRECATED | ECC-generation command library (28 files); zero references from v2 docs |
| `contexts/` | 3 files | DEPRECATED | ECC-generation context presets; zero references from v2 docs |
| `extensions/` | untracked | DEPRECATED | single ECC-generation file (`opus-advanced.md`); zero references |
| `mcp/` | untracked | DEPRECATED | MCP server config unreferenced by v2 docs; confirm no external tooling reads it before archiving |
| `evals/` | 4 tracked / 8 on disk | DEPRECATED (repurpose planned) | stale module-eval specs; slated to become golden intake-task cases per proposal #8 |
| `teams/alpha-research/` | 9 tracked | DEPRECATED (evidence retained) | team framework unused since candidate-loop v1 became research mainline (2026-06-12); `rounds/` artifacts are research evidence — append-only, never rewrite |
| `workflows/` | 1 tracked / 7 on disk | DEPRECATED | `opsx-*` are ECC-generation; `multi-broker-setup.md` is tracked but unreferenced — promote into a skill if still wanted, else archive |
| `pixiu/` | untracked | ARCHIVE-CANDIDATE | dead framework: registries plus runtime reports/logs (token/scan histories) |
| `logs/` | untracked | ARCHIVE-CANDIDATE | runtime log output (`pixiu.log`); does not belong in a knowledge base |
| `project_context.json` | untracked | DEPRECATED | ECC-generation project descriptor; zero references |
| `agent-docs-known-drift.txt` | tracked (force-added) | ACTIVE | ratchet baseline for `scripts/check_agent_docs.py` (`make agent-docs-check`) |

## Method

Judgements grounded in two scans (2026-07-10): reference count from v2
governing docs (`rg --hidden "\.agent/<dir>" CLAUDE.md AGENTS.md Makefile
.agent/rules .agent/skills .agent/memory docs/MODULES_REFERENCE.md`) and
tracked-state from `git ls-files .agent`. "Untracked" means the content is
invisible to any clone of this repo — it exists only on this machine.
