# Forced Promotion (Research-Only Escape Hatch)

> **Status:** loop_v1 L9 — landed on `loop-v1/convergence` 2026-05-05.
>
> **Scope:** governs `promote_alpha(force=True, force_reason=...)` artifacts.

## Why this runbook exists

`promote_alpha()` enforces strict gates by default. The `force=True`
override exists for operational research scenarios — exploratory
overrides that need a recorded artifact without representing an
approved live-eligible promotion.

Before L9, a forced promotion wrote a YAML to
`config/strategy_promotions/<date>/<id>.yaml` with `forced: true` and
`enabled: true`. There was no CI guard preventing that file from being
copied into a live-config path. **A forced artifact could therefore
travel into production by accident.**

L9 closes the gap with two enforcement layers:

1. **Artifact placement** — `_write_promotion_config()` routes forced
   artifacts to `research/forced_promotions/<YYYYMMDD>/<alpha_id>.yaml`,
   stamps `live_promotion_eligible: false`, and forces `enabled: false`
   and `weight: 0.0` on disk. The non-forced path
   (`config/strategy_promotions/...`) is unchanged.

2. **CI guard** — `.github/workflows/ci.yml` job
   `verify-no-forced-live-config` greps `config/live/**` for
   `research/forced_promotions` or `forced: true` and fails the PR on
   match. The job is a no-op until `config/live/` is created in L10.

## What "forced" means in artifacts

A forced YAML has these stamped fields (synthetic example):

```yaml
alpha_id: r47_demo
enabled: false
live_promotion_eligible: false
weight: 0.0
owner: research
expiry_review_date: "2026-06-04"
forced: true
source_commit: <git sha>
```

`enabled` and `weight` are deliberately zeroed regardless of the
in-memory `approved=True` override, because the on-disk artifact must
not advertise itself as live-eligible.

## What forced promotion is NOT

- Not a path to live deployment. Live promotion requires a non-forced
  artifact under `config/strategy_promotions/<date>/<alpha_id>.yaml`
  that comes from a passing strict gate run.
- Not a way to silence Gate D/E/F. The strict-profile checks still run
  and their failure is recorded in `promotion_decision.json`.
- Not a label-only bypass. Both the artifact location and CI guard are
  enforced.

## Operator workflow when force is genuinely required

1. Document the reason in the runbook for the originating incident.
2. Run with `force=True, force_reason="<concrete reason, signer>"`.
3. Verify the produced YAML lives under `research/forced_promotions/`
   (not `config/strategy_promotions/`).
4. Do not copy or symlink the artifact into `config/live/`. The CI
   guard will catch it; treat that as a stop signal.
5. If the override is truly load-bearing for live, withdraw the force
   path and rerun `promote_alpha` after fixing the underlying gate
   failure (cost model, latency profile, sample-size shortfall, etc.).

## Tests

- `tests/unit/test_promotion_force_research_only.py` locks the
  redirect, the `live_promotion_eligible: false` stamp, and that the
  non-forced path is unchanged.
- `tests/unit/test_alpha_promotion_force.py` covers `force_reason`
  validation (must be non-empty/non-whitespace).

## CI guard mechanics

The job is `verify-no-forced-live-config` in
`.github/workflows/ci.yml`. It depends on `lint` and runs in <2 min.
Logic:

```bash
if [ ! -d config/live ]; then exit 0; fi
grep -rEn '(research/forced_promotions|forced:[[:space:]]*true)' config/live/ \
  && exit 1 || exit 0
```

Until `config/live/` is created (L10), the guard is intentionally a
no-op so it cannot flap on PRs that touch unrelated config paths.

## Related plan steps

- **L1** — `loop_id` schema + strict mode (single source of truth for
  production strategy).
- **L6** — strict-only validation + equity-source classifier (refuses
  to promote synthetic-equity artifacts).
- **L9** (this runbook).
- **L10** — moves killed/revoked/disabled strategies out of live
  registry; introduces `config/live/strategies.yaml`. The CI guard
  becomes load-bearing once L10 lands.
- **L11** — freeze + stabilization: CODEOWNERS / environment
  protection / Dependabot allowlist layered on top of this guard.
