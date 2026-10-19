# Runbook: Strategy Rollback

## Scope

This runbook covers rolling back a promoted alpha strategy when it exceeds guardrail thresholds in the live canary phase. It covers automatic trigger conditions, manual rollback procedure, re-promotion steps, and config version traceability.

## Automatic Trigger Conditions

Each promotion YAML (`config/strategy_promotions/<date>/<alpha_id>.yaml`) contains a `rollback.trigger` block:

```yaml
rollback:
  trigger:
    live_slippage_bps_gt: 3.0
    live_drawdown_contribution_gt: 0.02
    execution_error_rate_gt: 0.01
  action:
    set_weight_to_zero: true
    open_incident: true
```

The canary monitor (`hft_platform.alpha.canary.CanaryMonitor`) evaluates these thresholds when `hft alpha canary evaluate` is called. If any threshold is exceeded, the action block is applied.

## Metrics to Watch

These are `live_metrics` keys handed to `CanaryMonitor.evaluate` (`alpha/canary.py:118-120`), not Prometheus series. `canary_scheduler.py:178-190` fills them from ClickHouse or from stored values.

| `live_metrics` key | Rolls back when | Source |
|---|---|---|
| `slippage_bps` | > `rollback.trigger.live_slippage_bps_gt` (else `guardrails.max_live_slippage_bps`, default 3.0) | canary scheduler |
| `drawdown_contribution` | > `rollback.trigger.live_drawdown_contribution_gt` (else `guardrails.max_live_drawdown_contribution`, default 0.02) | canary scheduler |
| `execution_error_rate` | > `rollback.trigger.execution_error_rate_gt` (else `guardrails.max_execution_error_rate`, default 0.01) | canary scheduler |

Missing data rolls back. When ClickHouse returns nothing and the promotion YAML has no `live_metrics` block, `_build_metrics` supplies worst-case values (slippage 999, drawdown 1.0, error rate 1.0), so the next evaluation rolls the canary back (`canary_scheduler.py:42-44`, `:248-267`).

The weight itself is the `weight` key of the promotion YAML (`alpha/promotion.py:1100`); a rollback writes `weight: 0.0` and `enabled: false` there (`CanaryMonitor.apply_decision`, `alpha/canary.py:203-205`). `canary_weight` appears only in the one-time `promotion_decision.json` written at promotion (`alpha/promotion.py:292`, `:297`) and is not updated by a rollback. The action taken is counted in `alpha_canary_actions_total{alpha_id,action}` (`action` = `hold` / `escalated` / `rolled_back` / `graduated`).

## Automatic Rollback Flow

1. Canary monitor detects threshold breach via `hft alpha canary evaluate`.
2. `apply_decision()` sets `weight: 0` and `enabled: false` in the promotion YAML.
3. `StrategyRunner` picks up weight change on next config reload.
4. `open_incident: true` triggers an alerting webhook (if configured).

## Manual Rollback Procedure

If the automatic rollback did not fire or you need to force-rollback:

```bash
# 1. Evaluate current canary status and apply decision
uv run hft alpha canary evaluate \
    --alpha-id <alpha_id> \
    --slippage-bps <observed_bps> \
    --dd-contrib <observed_drawdown> \
    --error-rate <observed_error_rate> \
    --sessions <live_sessions> \
    --apply

# 2. Verify weight is now 0
uv run hft alpha canary status

# 3. If promotion YAML needs manual edit
# Open config/strategy_promotions/<date>/<alpha_id>.yaml and set:
#   weight: 0.0
#   enabled: false
```

## Re-Promotion Steps

After root cause has been identified and fixed:

1. **Fix root cause** — update strategy logic, risk parameters, or execution config.

2. **Re-validate** — run Gate C scorecard again:
   ```bash
   uv run hft alpha validate --alpha-id <alpha_id> --data <data_paths>
   ```

3. **A/B compare** — verify improvement over the previous failing run:
   ```bash
   uv run hft alpha ab-compare <old_run_id> <new_run_id>
   ```

4. **Re-promote with version lineage:**
   ```bash
   uv run hft alpha promote \
       --alpha-id <alpha_id> \
       --owner <your_name> \
       --config-version v2 \
       --parent-config-version v1 \
       --shadow-sessions <sessions> \
       <...gate thresholds...>
   ```

5. **Start a new canary session** with lower initial weight (e.g., `--canary-weight 0.02`).

## Config Version Traceability

Each promotion YAML includes version lineage fields:

```yaml
config_version: v2
parent_config_version: v1
source_commit: abc1234
```

- `config_version` — the semantic version of this promotion (set via `--config-version`).
- `parent_config_version` — the version this supersedes (set via `--parent-config-version`).
- `source_commit` — the git commit at promotion time.

This lineage allows auditors to trace the full promotion history:
`v1 (initial) → v2 (after rollback fix) → ...`

To find all promotions for an alpha:
```bash
ls config/strategy_promotions/*/<alpha_id>.yaml
```
