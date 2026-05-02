# Agent Config Hygiene Report (2026-04-19)

Read-only audit of `/home/charlie/hft_platform/.agent/skills/`. No changes made.

Scope: 171 skill directories + 1 top-level `db-schema.skill.md` file = **172 skills** (the CLAUDE.md "170" count excluded `00-index.md` and `README.md`; this audit does the same but also includes the legacy `.skill.md` file).

## Summary
- Total skills: 172
- Missing SKILL.md: 1
- Missing YAML frontmatter: 12 (10 use legacy XML `<skill>` format, 2 have broken YAML)
- Stub skills (<20 body lines): 12
- Duplicate descriptions: 0 pairs
- Generic descriptions (no "when to use" trigger): 73

## Findings

### Missing SKILL.md
- `hft-remote-data-ops` — directory exists but contains no `SKILL.md` (already known)

### Missing or broken frontmatter

Ten skills use the older `<skill><name>...</name><description>...</description><instructions>...</instructions></skill>` XML format instead of YAML `---` frontmatter. Claude Code's skill loader expects YAML frontmatter; these need migration.

| Skill | Issue |
|-------|-------|
| `auto-fix` | XML `<skill>` format (no YAML frontmatter) |
| `background-manager` | XML `<skill>` format |
| `clickhouse-optimized` | XML `<skill>` format |
| `context-loader` | XML `<skill>` format |
| `delegate` | XML `<skill>` format |
| `doc-updater` | XML `<skill>` format |
| `git-parallel` | XML `<skill>` format |
| `planner` | XML `<skill>` format |
| `scaffold-project` | XML `<skill>` format |
| `sequential-thinking` | XML `<skill>` format |
| `shioaji-contracts` | YAML frontmatter present but uses `skill:` key instead of `name:` |
| `skill-stocktake` | YAML frontmatter has `description:` but no `name:` field |

### Stub skills (<20 body lines)

Body-line count (non-empty lines only) after frontmatter/header. 12 stubs found.

| Skill | Body lines | Description quality |
|-------|-----------|---------------------|
| `auto-fix` | 11 | generic |
| `background-manager` | 14 | generic |
| `clickhouse-optimized` | 7 | generic |
| `clickhouse-queries` | 12 | concrete |
| `context-loader` | 10 | generic |
| `delegate` | 12 | generic |
| `doc-updater` | 11 | generic |
| `eightctl` | 17 | generic |
| `fix` | 8 | concrete |
| `git-parallel` | 9 | generic |
| `pr-status-triage` | 16 | concrete |
| `sequential-thinking` | 14 | generic |

Note: 9 of 12 stubs also appear in the "missing frontmatter" list — the XML-format skills are both structurally outdated and content-thin.

### Duplicate descriptions

None. After whitespace normalisation and lowercasing, no two skills share an identical description. (Near-duplicates, e.g. generic "patterns for X framework" phrasings, are not flagged by this exact-match check — see Generic descriptions below.)

### Generic descriptions (no "when to use" trigger)

Heuristic: description lacks phrases like `use when`, `when you`, `when the user`, `trigger when`, `invoke when`, `for when`, `before`, `after`, `whenever`, `examples:`, `使用時機`, etc. Descriptions that only describe what the skill does (but not when it should fire) are flagged.

73 skills flagged. First 30:

- `agent-md-refactor` — "Refactor bloated AGENTS.md, CLAUDE.md, or similar agent instruction files to follow progressive disclosure pri..."
- `api-design` — "REST API design patterns including resource naming, status codes, pagination, filtering, error responses, vers..."
- `auto-fix` — "Automatically formats and lints the code. Equivalent to a \"Save Hook\" in an IDE."
- `backend-patterns` — "Backend architecture patterns, API design, database optimization, and server-side best practices for Node.js, ..."
- `background-manager` — "Manages long-running background processes (backtests, model training). Allows async workflows."
- `bear-notes` — "Create, search, and manage Bear notes via grizzly CLI."
- `cc-skill-clickhouse-io` — "ClickHouse database patterns, query optimization, analytics, and data engineering best practices for high-perf..."
- `clickhouse-optimized` — "Token-efficient ClickHouse client. Executes queries without exposing full schema metadata to context."
- `coding-standards` — "Universal coding standards, best practices, and patterns for TypeScript, JavaScript, React, and Node.js develo..."
- `config-env` — "Configure HFT platform environment variables, YAML config files, and runtime settings. Covers the full config ..."
- `configure-ecc` — "Interactive installer for Everything Claude Code — guides users through selecting and installing skills and ru..."
- `content-hash-cache-pattern` — "Cache expensive file processing results using SHA-256 content hashes — path-independent, auto-invalidating, wi..."
- `context-loader` — "Implements \"Progressive Retrieval\". Loads specialized documentation or code maps only when requested."
- `continuous-learning` — "Automatically extract reusable patterns from Claude Code sessions and save them as learned skills for future u..."
- `continuous-learning-v2` — "Instinct-based learning system that observes sessions via hooks, creates atomic instincts with confidence scor..."
- `cost-aware-llm-pipeline` — "Cost optimization patterns for LLM API usage — model routing by task complexity, budget tracking, retry logic,..."
- `cpp-testing` — "Use only when writing/updating/fixing C++ tests, configuring GoogleTest/CTest, diagnosing failing or flaky tes..." *(borderline — "Use only when..." does trigger intent; flagged because no discrete trigger keyword matched)*
- `data-flow-verify` — "Verify the HFT platform data flow pipeline end-to-end, covering hot path (feed to strategy) and recording path..."
- `database-migrations` — "Database migration best practices for schema changes, data migrations, rollbacks, and zero-downtime deployment..."
- `database-schema-designer` — "Design robust, scalable database schemas for SQL and NoSQL databases. Provides normalization guidelines, index..."
- `db-schema (file)` — "資料庫設計規範 Skill。命名規範、索引策略、多租戶設計、Migration 規範。 任務涉及「資料庫」、「Schema」、「ERD」、「Migration」關鍵字時自動載入。"
- `delegate` — "Orchestrate specialized sub-agents. Loads persona context dynamically to solve specific problems."
- `dependency-updater` — "Smart dependency management for any language. Auto-detects project type, applies safe updates automatically, p..."
- `deploy-docker` — "Deploy and manage the HFT platform Docker stack (hft-engine, ClickHouse, Redis, Prometheus, Grafana, Alertmana..."
- `deployment-patterns` — "Deployment workflows, CI/CD pipeline patterns, Docker containerization, health checks, rollback strategies, an..."
- `design-system-starter` — "Create and evolve design systems with design tokens, component architecture, accessibility guidelines, and doc..."
- `discord` — "Discord ops via the message tool (channel=discord)."
- `django-patterns` — "Django architecture patterns, REST API design with DRF, ORM best practices, caching, signals, middleware, and ..."
- `django-security` — "Django security best practices, authentication, authorization, CSRF protection, SQL injection prevention, XSS ..."
- `django-tdd` — "Django testing strategies with pytest-django, TDD methodology, factory_boy, mocking, coverage, and testing Dja..."

... 43 more. Remaining entries:

`doc-updater`, `docker-patterns`, `domain-name-brainstormer`, `e2e-testing`, `eightctl`, `eval-harness`, `feishu-perm`, `feishu-wiki`, `frontend-patterns`, `gh-issues`, `git-parallel`, `gog`, `golang-patterns`, `golang-testing`, `himalaya`, `iterative-retrieval`, `java-coding-standards`, `jpa-patterns`, `mcporter`, `naming-analyzer`, `notion`, `nutrient-document-processing`, `obsidian`, `performance-profiling`, `postgres-patterns`, `project-guidelines-example`, `python-patterns`, `python-pro`, `python-testing`, `qa-test-planner`, `reducing-entropy`, `regex-vs-llm-structured-text`, `rust-pro`, `rust_feature_engineering`, `scaffold-project`, `security-scan`, `sequential-thinking`, `session-manager`, `springboot-security`, `strategic-compact`, `swift-actor-persistence`, `swift-protocol-di-testing`, `verification-loop`.

## Methodology

- Source data: `ls /home/charlie/hft_platform/.agent/skills/` (171 dirs + 1 legacy `.skill.md` file).
- Parser: `/tmp/audit_skills.py` — handles YAML frontmatter (with leading `<!-- HTML comment -->` tolerance), detects XML `<skill>` wrapper format, counts non-empty body lines.
- Generic heuristic: description is flagged generic if it lacks any of 20+ "when to use" trigger keywords (English + Chinese). This is a permissive heuristic — some flagged descriptions (e.g. `cpp-testing` "Use only when...") are borderline and may read as concrete.
- Raw JSON results: `/tmp/skill_audit_results.json` (not committed; regenerable from `/tmp/audit_skills.py`).

## Cross-reference notes (for downstream triage, not this audit)

- 9 of 12 stubs also lack YAML frontmatter — likely legacy XML-format skills retained from an earlier generation of `.agent/skills/`.
- `auto-fix`, `doc-updater`, `delegate`, `planner`, `context-loader`, `sequential-thinking`, `session-manager`, `scaffold-project`, `background-manager` all share the XML-format + generic-description pattern — these look like an imported agent-persona bundle.
- `shioaji-contracts` has a valid, concrete description but the YAML key is `skill:` rather than `name:`; trivial mechanical fix.

## SP4 Resolution (2026-04-19)

**Archived** (moved to `.agent/skills/_archive-2026-04-19/`, 13 skills — zero external refs + XML-format + stubs):

`auto-fix`, `background-manager`, `clickhouse-optimized`, `context-loader`, `delegate`, `eightctl`, `pr-status-triage`, `scaffold-project`, `shioaji-contracts`, `skill-stocktake`, `hft-remote-data-ops`, `fix`, `clickhouse-queries`.

**Fixed in place** (XML → YAML frontmatter with concrete "when to use" triggers; 4 skills, all externally referenced):

- `planner` — cited in `research/SOP.md`, `.agent/rules/ecc/common/agents.md`, `docs/runbooks/release-convergence.md`.
- `doc-updater` — cited in `scripts/release_converge.py`, `scripts/release_first_ops_gate.py`, `.agent/rules/ecc/common/agents.md`, `docs/runbooks/release-convergence.md`.
- `git-parallel` — cited in `.agent/rules/30-git.md`.
- `sequential-thinking` — cited in `docs/architecture/ecc-shortform-guide.md`.

**Not addressed** (intentional): the 73 generic descriptions. These are quality-of-discovery issues, not load-bearing blockers; improving them is follow-up work for skill owners, not this pass.

**Index updated:** `/home/charlie/hft_platform/.agent/skills/00-index.md` removes the 13 archived rows and refreshes the 4 fixed rows.

**Rollback primitive:** `mv .agent/skills/_archive-2026-04-19/<name> .agent/skills/` restores any archived skill (tree is untracked by git, so no revert needed).

## SP5 Resolution (2026-04-19)

### `.agent/agents/` audit (11 entries)

Cross-referenced each name against CLAUDE.md, `.agent/rules/`, `.agent/workflows/`, `.agent/library/`, `.agent/skills/` (excluding archive), `docs/`, and `scripts/`.

**Archived (1):** `data-steward` → `.agent/agents/_archive-2026-04-19/data-steward.md` — zero external references anywhere in the audited paths.

**Kept (10):** all other agents have real external citations — `architect`, `build-error-resolver`, `code-reviewer`, `database-reviewer`, `doc-updater`, `e2e-runner`, `planner`, `refactor-cleaner`, `security-reviewer`, `tdd-guide`.

Heaviest references: ECC common rule files (`.agent/rules/ecc/common/agents.md`, `performance.md`, `security.md`, `testing.md`) and release scripts (`scripts/release_converge.py`, `scripts/release_first_ops_gate.py`, `docs/runbooks/release-convergence.md`).

### `.agent/commands/` audit (29 entries)

**Archived: 0.** All 29 commands are user-facing `/slash-command` entry points. The user invoked this session with `/update-docs` and `/skill-create`; those plus `/plan`, `/verify`, `/tdd`, `/e2e`, `/test-coverage`, `/learn`, `/eval`, `/sessions`, `/checkpoint`, `/orchestrate`, `/evolve`, `/instinct-*`, `/learn-eval`, `/multi-*`, `/go-test`, `/build-fix`, `/setup-pm`, `/python-review`, `/refactor-clean`, `/code-review`, `/update-codemaps` are all valid entry points regardless of internal grep hits.

**Rollback primitive:** `mv .agent/agents/_archive-2026-04-19/data-steward.md .agent/agents/` restores the archived agent.
