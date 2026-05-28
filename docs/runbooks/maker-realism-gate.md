# Maker-Realism Gate Runbook

## Why this gate exists

R47-style maker strategies repeatedly produced backtests that looked
profitable in points but were not robust to realistic execution
costs and end-of-day inventory. Three concrete failure modes recurred:

1. **Un-FIFO'd residual ignored.** The backtest closed the day with
   open inventory and never marked it to market — the equity curve
   lied by the residual P&L.
2. **Optimistic fills.** `QueueDepletionFill` assumed `queue_fraction
   = 0.5` for every passive resting order. Live queues are far slower
   and far more variable.
3. **Cost uncertainty hidden.** Mean P&L exceeded the cost floor on
   paper, but the bottom decile of daily P&L did not — promotion
   cleared on a centroid that the strategy could not consistently
   reach.

Slice B introduces three blocking sub-gates plus a strict latency
audit that close this loop. A backtest that does not survive this
gate cannot pass `vm_ul6_strict` and therefore cannot promote.

Companion incidents and rationale: `docs/architecture/feature-engine-lob-research-unification-spec.md`,
[`docs/runbooks/replay-parity-gate.md`](replay-parity-gate.md) (Slice
C — the structural template for this runbook).

## Goal

The Slice B gate enforces four invariants on every promotion-grade
backtest:

| Invariant                         | Source                                                                      | Enforcement                          |
| --------------------------------- | --------------------------------------------------------------------------- | ------------------------------------ |
| Residual MtM included in daily PnL | `MakerEngine._compute_residual_mtm`                                         | Engine itself; no gate needed        |
| Queue calibration realism          | `QueueDepletionFill` consults `QHatTable` instead of a flat `0.5` fraction  | `inventory_mtm_audit` (indirectly)   |
| Cost uncertainty bound             | `CostUncertaintyGate` — P95 lower bound of daily P&L ≥ floor                | `cost_uncertainty` sub-gate          |
| Strict latency provenance          | `latency_audit` — fail-closed when `place_ns` / `cancel_ns` are missing     | `latency_audit` (strict mode)        |
| End-of-session residual close-out  | `MakerStrategyBridge.on_session_end` emits FORCE_FLAT MARKET intent         | Live-side bridge; verified in tests  |

Together these guarantee that a "PASS" decision is grounded in a
realistic equity curve, not a cherry-picked centroid.

## Activation

Slice B sub-gates are registered globally but their blocking status
is profile-controlled.

- **Strict** (`config/research/profiles/vm_ul6_strict.yaml`) — both
  `inventory_mtm` and `cost_uncertainty` appear in
  `blocking_sub_gates`. `latency_audit` runs in **strict mode** and
  fails closed on missing P95 entries.
- **Loose** (`config/research/profiles/vm_ul6.yaml`) — both gates are
  registered but advisory; non-blocking. `latency_audit` runs in
  legacy advisory mode (warns and returns rather than failing).

```bash
# Strict (promotion-grade)
uv run python -m hft_platform.alpha promote \
  --alpha-id r47_maker \
  --profile vm_ul6_strict

# Loose (research / exploration)
uv run python -m hft_platform.alpha promote \
  --alpha-id r47_maker \
  --profile vm_ul6
```

The two profiles share schema; the only differences are the
`blocking_sub_gates` list and the strict `latency_audit` toggle.

## Tuning

The four operator-facing knobs that control gate stringency live in
the profile YAML and `PromotionConfig`. All four are floats in
**points** (the platform's canonical maker-spread unit).

| Knob                                              | Defined in                                | Default                       | Effect                                                                                                            |
| ------------------------------------------------- | ----------------------------------------- | ----------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `cost_floor_per_fill_pts`                         | profile YAML (per side)                   | `0.5`                         | Per-fill cost charged inside `InventoryMtMGate`. Lower → easier to clear. Used to derive `cost_floor_total_pts`. |
| `cost_uncertainty_p95_lower_bound_min_pts`        | profile YAML (per side)                   | `0.0`                         | Minimum P95 lower-bound the strategy must clear. Strict default of `0.0` allows zero-PnL strategies but blocks negative-tail ones. |
| `min_inventory_mtm_safety_margin_pct`             | `PromotionConfig`                         | `5.0`                         | Required margin between `realized + residual_mtm` and `cost_floor_total`. Below this the gate fails.            |
| `min_cost_uncertainty_p95_lower_bound_pts`        | `PromotionConfig`                         | `0.0`                         | Hard floor applied in addition to the per-profile threshold. Useful for cross-profile minimums.                  |

Two related runtime overrides (operator escape hatches) are documented
in `docs/operations/env-vars-reference.md`:

- `HFT_MAKER_MARK_METHOD` — selects the residual MtM method.
- `HFT_QUEUE_CALIBRATION_TABLE_PATH` — overrides the `q_hat` parquet
  used by `QueueDepletionFill`.

## Mark-method choice

`MakerEngine._compute_residual_mtm(open_pos, mark_price, mark_method)`
supports two mark methods:

| `mark_method`              | Reference price                                                  | When to use                                                                                                                |
| -------------------------- | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `last_mid` (default)       | Final mid-price on the session                                    | Standard equity-curve practice. Symmetric, low-bias.                                                                       |
| `worse_of_mid_last_trade`  | `min(mid, last_trade)` for longs, `max(mid, last_trade)` for shorts | Conservative skew when last-trade prints diverge from mid. Use for thinly-traded products or end-of-session imbalance days. |

The default `last_mid` matches the historical R47 backtest convention
and keeps comparability with pre-Slice-B baselines (see
`tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_pre_b.json`
captured in Task 1). Switch to `worse_of_mid_last_trade` when the
final book is one-sided or when the operator wants worst-of-two
provisioning before promotion.

The setting is plumbed through the `MakerEngine.run(mark_method=...)`
parameter and overridable at the operator boundary via
`HFT_MAKER_MARK_METHOD`.

## FORCE_FLAT semantics

`MakerStrategyBridge.on_session_end(ctx)` is the live-side analogue of
the engine's residual MtM accounting:

1. The runtime calls it on every `SessionPhase` transition into
   `CLOSE_ONLY` / `FORCE_FLAT` (producer side: `services/system.py`
   around lines 963–966; consumer-side hook in
   `strategy/runner.py:1623`).
2. If `ctx.position` is non-zero, the bridge returns one
   `OrderIntent` with `intent_type=IntentType.FORCE_FLAT`,
   `kind="MARKET"`, `qty=abs(residual)`, `side=opposite_of_residual`,
   and `price=cur_mid`. The `FORCE_FLAT` intent type is an
   always-allowed safety intent, so it is not blocked by halt /
   reduce-only gating.
3. If flat, the bridge returns an empty list — no spurious market
   order is emitted.

This guarantees that the **live** strategy never carries inventory
across the session boundary, which keeps the live equity curve
reconcilable against the engine-side `_compute_residual_mtm`.

The unit and integration coverage is in
`tests/unit/test_maker_bridge.py` and the e2e tests under
`tests/integration/test_slice_b_*.py`.

## q_hat calibration

`QHatTable` is a (symbol, hour, depth_bucket) lookup of empirical
queue fractions. `QueueDepletionFill.post_quote(...)` consults the
table when one is provided and falls back to the historical `0.5`
constant when no table is configured. The table is calibrated
offline from CK and committed as parquet.

### Committed fixtures

Three fixtures ship with the platform in
`research/backtest/q_hat_data/`:

| Fixture path                                              | Symbol         | Cells observed (out of 42*) | Fallback rate |
| --------------------------------------------------------- | -------------- | --------------------------- | ------------- |
| `research/backtest/q_hat_data/tmfd6_q_hat.parquet`        | TMFD6          | 42 / 42                     | 0 (full grid) |
| `research/backtest/q_hat_data/txfd6_q_hat.parquet`        | TXFD6          | 42 / 42                     | 2 cells fall through to `0.5` |
| `research/backtest/q_hat_data/txo35000q6_q_hat.parquet`   | TXO35000Q6     | 35 / 42                     | 7 cells fall through to `0.5` |

\* 42 = 6 trading hours × 7 depth buckets.

### Recalibration

Run `scripts/generate_q_hat_fixtures.py` against a fresh CK time
window when the regime shifts (e.g., a contract roll, a tick-size
change, or a liquidity regime break). Inputs: a CK source range and
the symbol(s) to cover. Output: a parquet at the same path.

```bash
PYTHONPATH=. uv run python scripts/generate_q_hat_fixtures.py \
  --symbol TMFD6 \
  --window-start 2026-04-01 \
  --window-end   2026-04-30 \
  --output research/backtest/q_hat_data/tmfd6_q_hat.parquet
```

The harness records its calibration metadata (window, snapshot count,
n_cells_with_data) inline in the parquet so downstream consumers can
verify provenance.

To use a non-default table at backtest time, set
`HFT_QUEUE_CALIBRATION_TABLE_PATH` to the parquet path before running
the backtest. The default resolves to
`research/backtest/q_hat_data/<HFT_SYMBOLS_PRIMARY>_q_hat.parquet`.

## Failure modes

The four common failure surfaces, in roughly descending frequency:

| Symptom                                                            | Likely root cause                                                                                                              |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `inventory_mtm` FAIL with `safety_margin_pct < 5.0`                | Residual MtM was non-trivial and the strategy's realized P&L did not absorb the cost floor (`n_fills × cost_floor_per_fill_pts`). Strategy is fundamentally not maker-realistic. |
| `cost_uncertainty` FAIL with `p95_lower_bound_pts < min_pts`        | Daily P&L distribution has a fat left tail. Mean is positive but the worst 5% of days drag the P95 lower bound below floor.   |
| `latency_audit` FAIL strict-mode with "missing P95 latency entry"   | Profile bound to a stale or partial latency profile (e.g., `place_ns` present but `cancel_ns` missing). Strict mode refuses to promote on incomplete data. |
| Replay-parity drift after recalibrating q_hat                       | Fresh `q_hat` fixture changed `QueueDepletionFill` outputs → backtest no longer matches the recorded intent stream. (Slice C gate fires.) |

A FAIL on any of the three sub-gates blocks promotion under
`vm_ul6_strict`. Under `vm_ul6` the same sub-gates emit advisory
warnings only — useful for research iteration but not for promotion.

## Recovery

Operator playbook by failure mode. Each path leads to either a fix
or an explicit "non-promotion" decision; **none** of these is a hot
patch on production.

### `inventory_mtm` blocks

1. Inspect the audit JSON: `realized_pts`, `residual_mtm_pts`,
   `cost_floor_total_pts`, `safety_margin_pct`. Confirm the residual
   was material vs. the realized series.
2. If the residual is large, the strategy is leaving inventory on the
   table. Either add an explicit close-out at session end (the live
   side already has `on_session_end`; the research side may not be
   calling `_compute_residual_mtm` correctly) or accept the verdict.
3. If the residual is small but `cost_floor_total_pts` is large, the
   strategy is over-trading. Compute `n_fills × cost_floor_per_fill_pts`
   and decide whether the per-fill cost is mis-tuned or the fill rate
   is structurally too high.
4. **Do not** lower `cost_floor_per_fill_pts` to clear the gate — the
   floor is anchored to TAIFEX RT cost reality (see
   `feedback_taifex_fee_structure`).

### `cost_uncertainty` blocks

1. Inspect `daily_pnl_p95_lower_bound_pts` vs the threshold.
2. Plot the daily-P&L distribution — confirm a left-tail / fat-tail
   pathology. Single-day-dominance (one extreme day driving the
   centroid) is a known maker-strategy failure mode (see
   `r47_revalidation_2026_04_24`).
3. Either lengthen the backtest panel (more days dilute single-day
   dominance), structurally bound the strategy's per-day exposure,
   or accept the verdict.
4. Strict profile threshold (`0.0` minimum P95 lower bound) is the
   floor — never set negative.

### `latency_audit` strict-fails

1. Verify which latency profile is bound (`promotion_audit.profile_id`).
2. Confirm it is `v2026-04-24_measured` and not stale. The
   `2026-04-24` profile has the canonical Shioaji asymmetric P95s
   (place 395 ms, cancel 59 ms) — earlier profiles often miss
   `cancel_ns` or use mean rather than P95.
3. If the profile is current but missing entries, regenerate it from
   recent CK latency_spans data.
4. Do **not** disable strict mode to clear the failure. Strict mode
   exists precisely to refuse promotion on incomplete latency data.

### q_hat needs regeneration

1. Run `scripts/generate_q_hat_fixtures.py` for the affected symbol
   on a fresh window.
2. Verify n_cells_with_data improved (look at the parquet metadata).
3. Re-run the backtest.
4. Be prepared to re-run the Slice C replay-parity gate as well — a
   different `q_hat` produces a different intent stream.

## Slack alert template

When a promotion run fails one of the Slice B gates in CI or in an
automated nightly evaluation, post the following to the
`#hft-alerts` Slack channel (or local equivalent). Numeric values in
the snapshot are **synthetic illustrative placeholders** — populate
from the actual audit JSON.

```
:rotating_light: Slice B promotion gate FAIL

Alpha:        r47_maker
Profile:      vm_ul6_strict
Failed gate:  inventory_mtm  (or cost_uncertainty / latency_audit)
Run ID:       <promotion run id>
Backtest:     <backtest result id or path>

Snapshot (synthetic example values):
  realized_pts:           +12.4
  residual_mtm_pts:       -18.7
  cost_floor_total_pts:   +6.0
  safety_margin_pct:     -42.1   <-- threshold 5.0
  n_fills:                12

Audit JSON:   <link to promotion_audit JSON in artifact store>
Dashboard:    <link to maker-realism panel in Grafana>
Runbook:      docs/runbooks/maker-realism-gate.md
Operator:     <on-call DRI>
Decision:     pending — no auto-rollback
```

Include the audit JSON link so the on-call DRI can drop into the
canonical fields without re-running the harness. The dashboard panel
should highlight `safety_margin_pct`, `p95_lower_bound_pts`, and the
strict latency profile id.

## Operator quick-reference

| Knob                                                  | Effect                                                                          |
| ----------------------------------------------------- | ------------------------------------------------------------------------------- |
| `--profile vm_ul6_strict`                             | Slice B sub-gates blocking; `latency_audit` strict.                            |
| `--profile vm_ul6` (default loose)                    | Slice B sub-gates advisory; `latency_audit` legacy.                            |
| `cost_floor_per_fill_pts` (profile YAML)              | Per-fill cost floor. Anchored to TAIFEX RT.                                    |
| `cost_uncertainty_p95_lower_bound_min_pts` (profile)  | Minimum P95 lower bound of daily P&L.                                          |
| `min_inventory_mtm_safety_margin_pct` (PromotionConfig) | Margin required between realized+residual and cost floor (default 5.0%).      |
| `min_cost_uncertainty_p95_lower_bound_pts` (PromotionConfig) | Cross-profile P95 lower-bound floor.                                       |
| `HFT_MAKER_MARK_METHOD=last_mid`                      | Default residual mark method.                                                  |
| `HFT_MAKER_MARK_METHOD=worse_of_mid_last_trade`       | Conservative residual mark method.                                             |
| `HFT_QUEUE_CALIBRATION_TABLE_PATH=...`                | Override `q_hat` parquet path for the backtest.                                |

| Audit field                                           | Consumer                                                                        |
| ----------------------------------------------------- | ------------------------------------------------------------------------------- |
| `BacktestResult.residual_mtm_pts`                     | `InventoryMtMGate.evaluate()` and `inventory_mtm_audit` in Gate D.              |
| `BacktestResult.daily_pnl_p95_lower_bound_pts`        | `CostUncertaintyGate.evaluate()` and `cost_uncertainty_audit` in Gate D.        |
| `BacktestResult.mark_method`                          | Audit provenance — recorded for reproducibility.                                |
| `MakerStrategyBridge.on_session_end()` return value   | Strategy runner FORCE_FLAT branch (`strategy/runner.py:1623`).                  |
