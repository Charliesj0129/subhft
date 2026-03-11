# Skills Index

> **150 skills** from HFT Platform + [everything-claude-code](https://github.com/affaan-m/everything-claude-code) and other community repos
> Each skill has a `SKILL.md`. Read it with `view_file` before using.

## HFT Core

| Skill | Description |
| --- | --- |
| `clickhouse-io` | ClickHouse patterns and query optimization _(ECC)_ |
| `hft-alpha-research` | Use when creating, validating, or promoting alpha factors; navigating Gate A→E governance; scaffolding research artifacts; or working with the research factory CLI, VM-UL data tiers, latency profiles, or synthetic LOB generation. |
| `hft-architect` | Use when designing new platform features, reviewing architecture decisions, or understanding the 6-plane runtime, Rust/PyO3 boundary, or Shioaji client decomposition (SessionRuntime/QuoteRuntime/ContractsRuntime). |
| `hft-backtest` | Use when writing, debugging, or running backtests and strategies based on the hftbacktest framework. |
| `hft-backtester` | Use when writing or debugging hftbacktest-based backtests via HftBacktestAdapter; modeling Shioaji broker latency (P95 ~36ms, submit_steps=28 base/61 stress); or validating alpha signals under realistic TWSE execution conditions. |
| `hft-helper` | HFT Platform task assistance |
| `hft-strategy-dev` | Use when implementing or modifying trading strategies, handling market data events, emitting orders, integrating the FeatureEngine for LOB-derived signals (HFT_FEATURE_ENGINE_ENABLED=1), or navigating the alpha lifecycle from research to production canary. |
| `shioaji-contracts` | Shioaji API contract handling |
| `symbols-sync` | Symbol universe sync |
| `troubleshoot-metrics` | Active diagnostics for HFT platform |
| `validation-gate` | Alpha validation gates |
| `fubon-tradeapi` | Fubon TradeAPI reference: auth, SDK, endpoints, order types, env vars, failure modes |
| `multi-broker-ops` | Multi-broker operational procedures: broker switching, failover, dual-broker routing |
| `soak-report-analysis` | Analyze soak test reports, identify degradation trends, and generate operational insights. Trigger on "soak report", "daily report", "health check", "operational status". |

## Python & Rust

| Skill | Description |
| --- | --- |
| `async-python-patterns` | Master Python asyncio, concurrent programming, and async/await patterns for high-performance applications. Use when building async APIs, concurrent systems, or I/O-bound applications requiring non-blocking operations. |
| `coding-standards` | Universal coding standards _(ECC)_ |
| `cpp-coding-standards` | C++ Core Guidelines _(ECC)_ |
| `cpp-testing` | GoogleTest, CTest, sanitizers _(ECC)_ |
| `python-patterns` | Pythonic idioms, PEP 8, type hints _(ECC)_ |
| `python-pro` | Python 3.12+ modern features, async |
| `python-testing` | pytest TDD, fixtures, mocking _(ECC)_ |
| `python-testing-patterns` | Comprehensive pytest strategies |
| `rust-pro` | Rust 1.75+ async, advanced types |
| `rust_feature_engineering` | Rust+PyO3 feature engineering workflow |

## Frontend & UI Design

| Skill | Description |
| --- | --- |
| `cache-components` |  |
| `design-system-starter` | Create and evolve design systems with design tokens, component architecture, accessibility guidelines, and documentation templates. Ensures consistent, scalable, and accessible UI across products. |
| `frontend-patterns` | React, Next.js patterns _(ECC)_ |
| `mui` | Material-UI v7 component library patterns including sx prop styling, theme integration, responsive design, and MUI-specific hooks. Use when working with MUI components, styling with sx prop, theme customization, or MUI utilities. |
| `react-dev` | This skill should be used when building React components with TypeScript, typing hooks, handling events, or when React TypeScript, React 19, Server Components are mentioned. Covers type-safe patterns for React 18-19 including generic components, proper event typing, and routing integration (TanStack Router, React Router). |
| `react-useeffect` | React useEffect best practices from official docs. Use when writing/reviewing useEffect, useState for derived values, data fetching, or state synchronization. Teaches when NOT to use Effect and better alternatives. |

## Backend & Architecture

| Skill | Description |
| --- | --- |
| `api-design` | REST API design patterns _(ECC)_ |
| `backend-patterns` | Backend architecture (Node.js, Express) _(ECC)_ |
| `backend-to-frontend-handoff-docs` | Create API handoff documentation for frontend developers. Use when backend work is complete and needs to be documented for frontend integration, or user says 'create handoff', 'document API', 'frontend handoff', or 'API documentation'. |
| `c4-architecture` | Generate architecture documentation using C4 model Mermaid diagrams. Use when asked to create architecture diagrams, document system architecture, visualize software structure, create C4 diagrams, or generate context/container/component/deployment diagrams. Triggers include "architecture diagram", "C4 diagram", "system context", "container diagram", "component diagram", "deployment diagram", "document architecture", "visualize architecture". |
| `data-flow-verify` | No description |
| `deployment-patterns` | CI/CD, health checks, rollback _(ECC)_ |
| `docker-patterns` | Docker/Compose patterns _(ECC)_ |
| `frontend-to-backend-requirements` | Document frontend data needs for backend developers. Use when frontend needs to communicate API requirements to backend, or user says 'backend requirements', 'what data do I need', 'API requirements', or is describing data needs for a UI. |
| `mermaid-diagrams` | Comprehensive guide for creating software diagrams using Mermaid syntax. Use when users need to create, visualize, or document software through diagrams including class diagrams (domain modeling, object-oriented design), sequence diagrams (application flows, API interactions, code execution), flowcharts (processes, algorithms, user journeys), entity relationship diagrams (database schemas), C4 architecture diagrams (system context, containers, components), state diagrams, git graphs, pie charts, gantt charts, or any other diagram type. Triggers include requests to "diagram", "visualize", "model", "map out", "show the flow", or when explaining system architecture, database design, code structure, or user/application flows. |
| `openapi-to-typescript` | Converts OpenAPI 3.0 JSON/YAML to TypeScript interfaces and type guards. This skill should be used when the user asks to generate types from OpenAPI, convert schema to TS, create API interfaces, or generate TypeScript types from an API specification. |

## Data & Storage

| Skill | Description |
| --- | --- |
| `cc-skill-clickhouse-io` | ClickHouse database patterns, query optimization, analytics, and data engineering best practices for high-performance analytical workloads. |
| `clickhouse-optimized` | No description |
| `clickhouse-queries` | Deep Analyzer. Performs statistical analysis on ClickHouse data. Returns P50/P99 latency and throughput metrics in JSON. Use for Performance Verification and Darwin Gate checks. |
| `content-hash-cache-pattern` | SHA-256 content hash caching _(ECC)_ |
| `database-migrations` | Zero-downtime migrations (Postgres, MySQL, ORMs) _(ECC)_ |
| `database-schema-designer` | Design robust, scalable database schemas for SQL and NoSQL databases. Provides normalization guidelines, indexing strategies, migration patterns, constraint design, and performance optimization. Ensures data integrity, query performance, and maintainable data models. |
| `postgres-patterns` | PostgreSQL query optimization _(ECC)_ |

## Testing & Quality

| Skill | Description |
| --- | --- |
| `e2e-testing` | Playwright E2E patterns _(ECC)_ |
| `eval-harness` | Eval-driven development framework _(ECC)_ |
| `naming-analyzer` | Suggest better variable, function, and class names based on context and conventions. |
| `qa-test-planner` | Generate comprehensive test plans, manual test cases, regression test suites, and bug reports for QA engineers. Includes Figma MCP integration for design validation. |
| `security-review` | Security checklist and patterns _(ECC)_ |
| `security-scan` | AgentShield config scanning _(ECC)_ |
| `skill-stocktake` | Audit skills quality _(ECC)_ |
| `tdd-workflow` | Test-driven development workflow _(ECC)_ |
| `verification-loop` | Comprehensive verification system _(ECC)_ |

## AI & LLM

| Skill | Description |
| --- | --- |
| `agent-md-refactor` | Refactor bloated AGENTS.md, CLAUDE.md, or similar agent instruction files to follow progressive disclosure principles. Splits monolithic files into organized, linked documentation. |
| `codex` | Use when the user asks to run Codex CLI (codex exec, codex resume) or references OpenAI Codex for code analysis, refactoring, or automated editing. Uses GPT-5.2 by default for state-of-the-art software engineering. |
| `coding-agent` | Delegate coding tasks to Codex, Claude Code, or Pi agents via background process. Use when: (1) building/creating new features or apps, (2) reviewing PRs (spawn in temp dir), (3) refactoring large codebases, (4) iterative coding that needs file exploration. NOT for: simple one-liner fixes (just edit), reading code (use read tool), or any work in ~/clawd workspace (never spawn agents here). Requires a bash tool that supports pty:true. |
| `configure-ecc` | Interactive ECC installer _(ECC)_ |
| `context-loader` | No description |
| `continuous-learning` | Extract patterns from sessions _(ECC)_ |
| `continuous-learning-v2` | Instinct-based learning system _(ECC)_ |
| `cost-aware-llm-pipeline` | LLM cost optimization, model routing _(ECC)_ |
| `gepetto` | Creates detailed, sectionized implementation plans through research, stakeholder interviews, and multi-LLM review. Use when planning features that need thorough pre-implementation analysis. |
| `iterative-retrieval` | Progressive context retrieval _(ECC)_ |
| `lesson-learned` | Analyze recent code changes via git history and extract software engineering lessons. Use when the user asks 'what is the lesson here?', 'what can I learn from this?', 'engineering takeaway', 'what did I just learn?', 'reflect on this code', or wants to extract principles from recent work. |
| `regex-vs-llm-structured-text` | Regex vs LLM decision framework _(ECC)_ |
| `search-first` | Research-before-coding workflow _(ECC)_ |
| `session-handoff` | Creates comprehensive handoff documents for seamless AI agent session transfers. Triggered when: (1) user requests handoff/memory/context save, (2) context window approaches capacity, (3) major task milestone completed, (4) work session ending, (5) user says 'save state', 'create handoff', 'I need to pause', 'context is getting full', (6) resuming work with 'load handoff', 'resume from', 'continue where we left off'. Proactively suggests handoffs after substantial work (multiple file edits, complex debugging, architecture decisions). Solves long-running agent context exhaustion by enabling fresh agents to continue with zero ambiguity. |
| `skill-judge` | Evaluate Agent Skill design quality against official specifications and best practices. Use when reviewing, auditing, or improving SKILL.md files and skill packages. Provides multi-dimensional scoring and actionable improvement suggestions. |
| `strategic-compact` | Manual context compaction _(ECC)_ |

## Django / Spring Boot

| Skill | Description |
| --- | --- |
| `django-patterns` | Django + DRF patterns _(ECC)_ |
| `django-security` | Django security best practices _(ECC)_ |
| `django-tdd` | Django testing with pytest-django _(ECC)_ |
| `django-verification` | Django verification loop _(ECC)_ |
| `java-coding-standards` | Java coding standards _(ECC)_ |
| `jpa-patterns` | JPA/Hibernate patterns _(ECC)_ |
| `springboot-patterns` | Spring Boot architecture _(ECC)_ |
| `springboot-security` | Spring Security best practices _(ECC)_ |
| `springboot-tdd` | Spring Boot TDD (JUnit 5) _(ECC)_ |
| `springboot-verification` | Spring Boot verification loop _(ECC)_ |

## Go

| Skill | Description |
| --- | --- |
| `golang-patterns` | Idiomatic Go patterns _(ECC)_ |
| `golang-testing` | Go testing, benchmarks, fuzzing _(ECC)_ |

## Swift

| Skill | Description |
| --- | --- |
| `swift-actor-persistence` | Swift actor-based persistence _(ECC)_ |
| `swift-protocol-di-testing` | Swift protocol-based DI _(ECC)_ |

## Document Processing

| Skill | Description |
| --- | --- |
| `defuddle` | Extract clean markdown content from web pages using Defuddle CLI, removing clutter and navigation to save tokens. Use instead of WebFetch when the user provides a URL to read or analyze, for online documentation, articles, blog posts, or any standard web page. |
| `excalidraw` | Use when working with *.excalidraw or *.excalidraw.json files, user mentions diagrams/flowcharts, or requests architecture visualization - delegates all Excalidraw operations to subagents to prevent context exhaustion from verbose JSON (single files: 4k-22k tokens, can exceed read limits) |
| `humanizer` |  |
| `json-canvas` | Create and edit JSON Canvas files (.canvas) with nodes, edges, groups, and connections. Use when working with .canvas files, creating visual canvases, mind maps, flowcharts, or when the user mentions Canvas files in Obsidian. |
| `marp-slide` | Create professional Marp presentation slides with 7 beautiful themes (default, minimal, colorful, dark, gradient, tech, business). Use when users request slide creation, presentations, or Marp documents. Supports custom themes, image layouts, and "make it look good" requests with automatic quality improvements. |
| `nutrient-document-processing` | PDF/DOCX/XLSX processing via Nutrient _(ECC)_ |
| `project-guidelines-example` | Project-specific skill template _(ECC)_ |
| `web-to-markdown` | Use ONLY when the user explicitly says: 'use the skill web-to-markdown ...' (or 'use a skill web-to-markdown ...'). Converts webpage URLs to clean Markdown by calling the local web2md CLI (Puppeteer + Readability), suitable for JS-rendered pages. |

## Dev Workflow & Ops

| Skill | Description |
| --- | --- |
| `auto-fix` | No description |
| `background-manager` | No description |
| `command-creator` | This skill should be used when creating a Claude Code slash command. Use when users ask to "create a command", "make a slash command", "add a command", or want to document a workflow as a reusable command. Essential for creating optimized, agent-executable slash commands with proper structure and best practices. |
| `commit-work` | Create high-quality git commits: review/stage intended changes, split into logical commits, and write clear commit messages (including Conventional Commits). Use when the user asks to commit, craft a commit message, stage changes, or split work into multiple commits. |
| `config-env` | No description |
| `crafting-effective-readmes` | Use when writing or improving README files. Not all READMEs are the same — provides templates and guidance matched to your audience and project type. |
| `dependency-updater` | Smart dependency management for any language. Auto-detects project type, applies safe updates automatically, prompts for major versions, diagnoses and fixes dependency issues. |
| `doc-updater` | Documentation updating |
| `fix` | Lint/format error fixing |
| `flags` | Feature flag management |
| `healthcheck` | Host security hardening |
| `performance-profiling` | Profiling and optimization |
| `planner` | Planning assistance |
| `plugin-forge` | Create and manage Claude Code plugins with proper structure, manifests, and marketplace integration. Use when creating plugins for a marketplace, adding plugin components (commands, agents, hooks), bumping plugin versions, or working with plugin.json/marketplace.json manifests. |
| `pr-status-triage` | PR status triage |
| `reducing-entropy` | Manual-only skill for minimizing total codebase size. Only activate when explicitly requested by user. Measures success by final code amount, not effort. Bias toward deletion. |
| `requirements-clarity` | Clarify ambiguous requirements through focused dialogue before implementation. Use when requirements are unclear, features are complex (>2 days), or involve cross-team coordination. Ask two core questions - Why? (YAGNI check) and Simpler? (KISS check) - to ensure clarity before coding. |
| `runtime-debug` | Runtime debugging |
| `scaffold-project` | Project scaffolding |
| `sequential-thinking` | Step-by-step reasoning |
| `session-manager` | Session management |
| `skill-lookup` | Skill discovery |
| `writing-skills` | Skill creation and editing |

## External Integrations

| Skill | Description |
| --- | --- |
| `bear-notes` | Create, search, and manage Bear notes via grizzly CLI. |
| `datadog-cli` | Datadog CLI for searching logs, querying metrics, tracing requests, and managing dashboards. Use this when debugging production issues or working with Datadog observability. |
| `delegate` | Task delegation |
| `deploy-docker` | Docker deploy |
| `discord` | Discord operations |
| `draw-io` | draw.io diagram creation, editing, and review. Use for .drawio XML editing, PNG conversion, layout adjustment, and AWS icon usage. |
| `eightctl` | Eight Sleep pod control |
| `feishu-perm` | Feishu permissions |
| `feishu-wiki` | Feishu wiki |
| `gemini` | Gemini CLI integration |
| `gh-issues` | GitHub issues → PRs pipeline |
| `git-parallel` | Git parallel workflows |
| `github` | GitHub CLI operations |
| `gog` | Google Workspace CLI |
| `himalaya` | Email via IMAP/SMTP |
| `jira` | Use when the user mentions Jira issues (e.g., "PROJ-123"), asks about tickets, wants to create/view/update issues, check sprint status, or manage their Jira workflow. Triggers on keywords like "jira", "issue", "ticket", "sprint", "backlog", or issue key patterns. |
| `mcporter` | MCP server management |
| `notion` | Notion API |
| `obsidian` | Obsidian vault automation |
| `obsidian-bases` | Create and edit Obsidian Bases (.base files) with views, filters, formulas, and summaries. Use when working with .base files, creating database-like views of notes, or when the user mentions Bases, table views, card views, filters, or formulas in Obsidian. |
| `obsidian-cli` | Interact with Obsidian vaults using the Obsidian CLI to read, create, search, and manage notes, tasks, properties, and more. Also supports plugin and theme development with commands to reload plugins, run JavaScript, capture errors, take screenshots, and inspect the DOM. Use when the user asks to interact with their Obsidian vault, manage notes, search vault content, perform vault operations from the command line, or develop and debug Obsidian plugins and themes. |
| `obsidian-markdown` | Create and edit Obsidian Flavored Markdown with wikilinks, embeds, callouts, properties, and other Obsidian-specific syntax. Use when working with .md files in Obsidian, or when the user mentions wikilinks, callouts, frontmatter, tags, embeds, or Obsidian notes. |
| `perplexity` | Web search and research using Perplexity AI. Use when user says "search", "find", "look up", "ask", "research", or "what's the latest" for generic queries. NOT for library/framework docs (use Context7) or workspace questions. |
| `portainer` | Portainer CE management |
| `slack` | Slack control |

## Productivity & Communication

| Skill | Description |
| --- | --- |
| `daily-meeting-update` | Interactive daily standup/meeting update generator. Use when user says 'daily', 'standup', 'scrum update', 'status update', 'what did I do yesterday', 'prepare for meeting', 'morning update', or 'team sync'. Pulls activity from GitHub, Jira, and Claude Code session history. Conducts 4-question interview (yesterday, today, blockers, discussion topics) and generates formatted Markdown update. |
| `difficult-workplace-conversations` | Structured approach to workplace conflicts, performance discussions, and challenging feedback using preparation-delivery-followup framework. Use when preparing for tough conversations, addressing conflicts, giving critical feedback, or navigating sensitive workplace discussions. |
| `domain-name-brainstormer` | Generates creative domain name ideas for your project and checks availability across multiple TLDs (.com, .io, .dev, .ai, etc.). Saves hours of brainstorming and manual checking. |
| `feedback-mastery` | Navigate difficult conversations and deliver constructive feedback using structured frameworks. Covers the Preparation-Delivery-Follow-up model and Situation-Behavior-Impact (SBI) feedback technique. Use when preparing for difficult conversations, giving feedback, or managing conflicts. |
| `game-changing-features` | Find 10x product opportunities and high-leverage improvements. Use when user wants strategic product thinking, mentions '10x', wants to find high-impact features, or says 'what would make this 10x better', 'product strategy', or 'what should we build next'. |
| `meme-factory` | Generate memes using the memegen.link API. Use when users request memes, want to add humor to content, or need visual aids for social media. Supports 100+ popular templates with custom text and styling. |
| `professional-communication` | Guide technical communication for software developers. Covers email structure, team messaging etiquette, meeting agendas, and adapting messages for technical vs non-technical audiences. Use when drafting professional messages, preparing meeting communications, or improving written communication. |
| `ship-learn-next` | Transform learning content (like YouTube transcripts, articles, tutorials) into actionable implementation plans using the Ship-Learn-Next framework. Use when user wants to turn advice, lessons, or educational content into concrete action steps, reps, or a learning quest. |
| `writing-clearly-and-concisely` | Use when writing prose humans will read—documentation, commit messages, error messages, explanations, reports, or UI text. Applies Strunk's timeless rules for clearer, stronger, more professional writing. |

