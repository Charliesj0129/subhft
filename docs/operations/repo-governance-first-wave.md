# Repo Governance First Wave

This document records the first versioned governance controls added to the repository.

## Versioned Controls In Repo

- `CODEOWNERS` defines default and high-risk ownership.
- `Dependency Boundary` CI job enforces Python import contracts from `importlinter.ini`.
- `Semgrep` CI workflow enforces governance/security rules from `semgrep/rules/`.
- `CodeQL (python)` CI workflow runs GitHub code scanning on the Python codebase.
- PR template requires an `AI Participation` disclosure section.
- `merge_group` is enabled in CI/security/governance workflows so merge queue can replay required checks.

## Manual GitHub Settings To Turn On

The following controls must be configured in GitHub repository settings because they are not fully versionable in the repository.

1. Create a branch ruleset for `main`.
2. Require pull requests before merge.
3. Require merge queue.
4. Require code owner review.
5. Disable bypass for administrators unless there is an explicit break-glass policy.
6. Require these status checks:
   - `🔍 Lint & Format`
   - `Dependency Boundary`
   - `🔬 Type Check`
   - `🧪 Tests & Coverage`
   - `🔗 Integration Tests`
   - `🔒 Security Scan`
   - `Code Quality Checks`
   - `Semgrep`
   - `CodeQL (python)`

## Initial Import Contracts

- `hft_platform.contracts` must stay independent from runtime services.
- `hft_platform.events` must stay independent from strategy/execution/services.
- `hft_platform.testing` must not be imported by production packages.

## Initial Semgrep Scope

- Silent exception swallowing.
- Production imports of test helpers.
- Broker SDK imports outside approved packages.
- Direct DB client imports in decision/execution paths.

## Expansion Candidates

- Tighten Import Linter to cover more runtime layers once current imports are cleaned up.
- Add more Semgrep rules for fallback masking, secret handling, and dangerous query construction.
- Promote CodeQL custom queries/model packs after baseline alert triage stabilizes.
