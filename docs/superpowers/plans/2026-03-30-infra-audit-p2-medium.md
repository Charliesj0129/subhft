# Infrastructure Audit P2 — Structural Improvements

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 16 MEDIUM items: CI pipeline efficiency (8), monitoring coverage (3), config cleanup (3), ops fixes (2).

**Architecture:** CI composite actions, workflow YAML, Grafana JSON, config file cleanup, Python deprecation comments, shell script fixes.

**Tech Stack:** GitHub Actions composite actions, Prometheus YAML, Grafana JSON, Python, shell.

**Spec:** `docs/superpowers/specs/2026-03-30-infrastructure-audit-design.md` §4

**Depends on:** P0 + P1 complete.

---

### Task 1: CI-06 + CI-07 — Extract composite actions + fix canary pip

Consolidate repeated setup boilerplate into reusable composite actions. Fix canary-deploy.yml to use uv.

### Task 2: CI-08 + CI-09 + CI-12 + CI-13 — CI quick fixes

Add latency-gate Makefile target, consolidate print/float checks, add concurrency, create dependabot.yml.

### Task 3: CI-10 + CI-11 — CodeQL + license compliance

Add CodeQL workflow and pip-licenses check.

### Task 4: M-15 — Orphaned metrics triage

Cross-reference metrics.py vs alert rules + dashboards, document intentionally unmonitored.

### Task 5: M-17 + M-18 — Alert runbook URLs + alertmanager config decision

Add runbook_url to 9 matching alerts, remove unused production alertmanager config.

### Task 6: C-06 + C-07 + C-08 — Config cleanup

Delete orphan configs, add wizard-generated comments, add deprecation timeline, remove duplicate env dirs.

### Task 7: O-05 + O-06 — Ops fixes

Delete broken daily-backup.sh, fix post_market_check.py WAL path.
