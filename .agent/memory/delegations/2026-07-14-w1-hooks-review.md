# 2026-07-14 · hft-reviewer smoke + real review of W1 hooks commit

Delegation type: independent review (hft-reviewer binding, sync,
`run_in_background: false`). Purpose: (1) the smoke spawn owed from
2026-07-14's harness-binding session — verify tool-enforced read-only + verdict
delivery; (2) a real diff-scoped review of commit 1e8619d1.

## 1. Packet (verbatim)

DIFF: commit 1e8619d1 only. Get it with: git show 1e8619d1
CONTEXT: This commit adds a Claude Code hook enforcement floor: 4 stdlib-only
Python hooks under .claude/hooks/ (scope_guard, git_guard fail-closed;
discipline_feedback, commit_audit fail-open/advisory), wired in
.claude/settings.json, with 12 behavior tests in tests/unit/test_agent_hooks.py.
Design intent: hooks enforce EXISTING policy from AGENTS.md (executor packet
allowlists; subagents never mutate git) — no new policy.
GOVERNING RULES (read only these): AGENTS.md "Roles" + "Task Routing (task
types)"; .agent/rules/50-testing.md
CHECKS (priority order; cut checks, not the verdict): 1. fail-closed/fail-open
split matches docstrings 2. git_guard allowlist gaps both directions
3. scope_guard path matching (abs/rel, scratchpad, orchestrator bypass)
4. tests assert behavior; would they fail if guard logic inverted?
BUDGET: diff + the two rule files; no repo-wide exploration. Verdict MANDATORY
before ending: APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES / ESCALATE.
Read-only: no file edits, no git state changes.

## 2. Executor final report

N/A (review delegation — the review itself is the deliverable; see section 3).
Reviewer ran 14 subprocess probes + the test suite; was itself falsely denied
3 times by the live git_guard (F1 evidence captured in situ).

## 3. Review verdict

REQUEST-CHANGES — 8 findings: F1 HIGH git_guard parses quoted text as git
commands (live false-denials during the review); F2 HIGH scope_guard
auto-allowed .agent/runtime/* to the guarded subagent (marker self-rewrite);
F3 MED `git -C/-c` read-only forms wrongly blocked; F4 MED scratchpad
traversal escape (no normpath); F5 LOW unreadable-stdin fails open; F6 LOW
exact-file patterns granted prefix siblings; F7 LOW shell-indirection bypass
(accepted floor limit, docstring); F8 LOW test gaps (abs paths, scratchpad,
positive discipline path, quiet commit_audit path).

Outcome: smoke criteria PASS (read-only tool set held; sync verdict delivered
in format; delegation budget respected — ~59K tokens / 14 tool uses).
Orchestrator verified F1/F2/F3/F4/F6 with failing tests, fixed same session
(11 new tests, 23 total green); F5/F7 documented as docstring caveats.
