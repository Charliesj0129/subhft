# Feature Plane Operations Runbook (Prototype)

## Scope
- Feature profile validation, activation, preflight compatibility, shadow rollout checks.

## Commands
- `hft feature profiles --json`
- `hft feature validate`
- `hft feature preflight --strategies config/base/strategies.yaml`

## Rollout (Prototype)
1. Validate profile file.
2. Run preflight compatibility.
3. Enable `HFT_FEATURE_ENGINE_ENABLED=1` and selected `HFT_FEATURE_PROFILE_ID`.
4. Observe metrics: `feature_plane_updates_total`, `feature_quality_flags_total`, `feature_shadow_parity_*`.
5. Rollback by switching `HFT_FEATURE_PROFILE_ID` or disabling feature engine.

## TODO
- Dashboard links
- Automated canary decision policy
