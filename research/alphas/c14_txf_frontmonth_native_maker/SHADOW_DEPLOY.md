# C14 — Shadow Deploy Playbook

Post-PROMOTE scaffold (R6 T8, 2026-04-17). This is a research artifact — it
captures the specific runbook for bringing C14 into shadow trading. It is
NOT general ops documentation.

## 0. Gate prereqs (before any shadow run)

Must all be green:

- [ ] `research/alphas/c14_txf_frontmonth_native_maker/tests/test_c14.py` passes (33/33)
- [ ] `uv run python -c "import hft_platform.strategies.c14_txf_frontmonth_maker as m; print(m.C14TxfFrontMonthMakerStrategy)"` imports without error
- [ ] `config/base/strategies.yaml` entry `C14_TXF_FRONTMONTH_MAKER` is `enabled: false`
- [ ] `config/base/strategy_limits.yaml` entry for `C14_TXF_FRONTMONTH_MAKER` exists with `daily_loss_hard_stop_ntd: 200000`
- [ ] Latency profile `sim_p95_v2026-02-26` still declared in `config/research/latency_profiles.yaml`
- [ ] Gate A/B/C/D all PASS; Gate E pending (this shadow is what produces Gate E evidence)

## 1. Environment

Minimum environment:

```bash
# Execution safety — orders NEVER reach broker
export HFT_ORDER_SHADOW_MODE=1

# Broker adapter (same as production)
export HFT_BROKER=shioaji

# Runtime mode — sim or real market data, both acceptable for shadow
export HFT_MODE=sim            # recommended: market data replay for deterministic first day
# export HFT_MODE=real         # upgrade path after first clean day in sim
export HFT_ORDER_MODE=sim      # double safety; forbids broker calls even if SHADOW_MODE drops

# Symbol subscription — front-month chain
export HFT_SYMBOLS=TXFB6,TXFC6,TXFD6

# Observability
export HFT_FEATURE_ENGINE_ENABLED=1
export HFT_MONITOR_LIVE_ENABLED=1
```

Recommended (non-blocking):

```bash
export HFT_TELEGRAM_ENABLED=1   # daily shadow summary pushed to bot
```

## 2. Capital sizing

Paper capital: **552,000 NTD minimum** = 3 × TXF margin (184K NTD/contract).
This matches the strategy's `max_pos=3` cap. Do NOT start shadow with
less — position overflows during V-shape recovery would alarm-false.

Daily-loss hard stop: **200,000 NTD** (≈ 2/3 of pessimistic OOS NTD/day
at `p_front=0.2`). If the running PnL hits this limit, the strategy flat-
out-and-disables; a human must investigate and re-enable.

## 3. Pre-launch checklist

Run in order. All must pass. Do NOT launch until a clean green.

```bash
# 3.1 Unit tests
cd /home/charlie/hft_platform
uv run pytest research/alphas/c14_txf_frontmonth_native_maker/tests/test_c14.py
# expect: 33 passed

# 3.2 Lint + typecheck (scope to new files only to avoid repo-wide blockers)
uv run ruff check \
  src/hft_platform/strategies/c14_txf_frontmonth_maker.py \
  research/alphas/c14_txf_frontmonth_native_maker/

# 3.3 Dry-run tick replay (market data only, no order flow)
HFT_ORDER_SHADOW_MODE=1 HFT_MODE=replay \
  uv run hft run sim --symbols TXFB6,TXFC6,TXFD6 --dry-run

# 3.4 SessionGovernor connectivity
uv run python -c "from hft_platform.ops.session_governor import SessionGovernor; sg = SessionGovernor(); print(sg.summary())"

# 3.5 Verify strategy loads from config (no wiring errors)
uv run python -c "
import yaml
cfg = yaml.safe_load(open('config/base/strategies.yaml'))
entry = [s for s in cfg['strategies'] if s['id'] == 'C14_TXF_FRONTMONTH_MAKER'][0]
assert entry['enabled'] is False, 'must launch disabled'
assert entry['params']['shadow_mode'] is True
print('config load ok:', entry['id'])
"
```

## 4. Launch procedure

Step 1 — toggle enabled (still shadow-safe because `HFT_ORDER_SHADOW_MODE=1`
is set):

Edit `config/base/strategies.yaml` — change `C14_TXF_FRONTMONTH_MAKER.enabled`
from `false` to `true`. Commit this change (conventional commit: `chore(c14):
enable for shadow deploy`).

Step 2 — start the engine:

```bash
HFT_ORDER_SHADOW_MODE=1 HFT_ORDER_MODE=sim HFT_SYMBOLS=TXFB6,TXFC6,TXFD6 \
  uv run hft run sim
```

Step 3 — verify first events flow through:

```bash
# In another terminal
curl -s http://localhost:9090/metrics | grep -E 'hft_c14|hft_strategy_c14'
# Expect: hft_strategy_c14_txf_frontmonth_maker_* counters incrementing
```

## 5. Daily monitoring

Every trading day during shadow:

```bash
# Prometheus metrics to watch (substitute <strategy> = c14_txf_frontmonth_maker)
#   hft_<strategy>_fills_total        — per-day fill count
#   hft_<strategy>_position           — net position per symbol
#   hft_<strategy>_pnl_pts            — running PnL in points
#   hft_<strategy>_spread_blocked     — count of quotes suppressed by spread gate
#   hft_<strategy>_slippage_ticks     — per-fill slippage (hypothetical, since orders don't reach broker)

# Recorded fills (hypothetical) in ClickHouse
docker exec clickhouse clickhouse-client --user default --password "$CLICKHOUSE_PASSWORD" \
  --query "
    SELECT toDate(exch_ts/1e9) AS d, count(), sum(qty) AS qty
    FROM hft.orders
    WHERE strategy_id = 'C14_TXF_FRONTMONTH_MAKER'
      AND toDate(exch_ts/1e9) = today()
    GROUP BY d
  "
```

Target values (shadow fidelity check — compared against T5-REVISE scorecard):

| Metric | Expected (shadow) | Alert if |
| ------ | -----------------: | -------: |
| Fills per day | 800-2,000 | < 200 or > 5,000 |
| Net PnL per day (points) | 500-1,800 | < -300 or > 3,000 |
| Max |position| at EOD | ≤ 5 | > 5 |
| Spread-blocked per day | 10,000-50,000 | < 1,000 |
| Daily-loss hard stop triggered | NEVER in shadow | if ever |

## 6. Shadow duration + exit criteria

**Minimum shadow duration: 10 trading days.**

Daily score rubric (informal, per `hft-strategy-lifecycle` skill):

| Check | Threshold |
| ----- | --------- |
| Fills per day within expected band | 8/10 days |
| No storm-related engine faults | 10/10 days |
| Daily loss never hits hard stop | 10/10 days |
| Position accounting invariants (net_pos = fills_buy - fills_sell) | 10/10 days |
| Per-contract rotation consistent with front-month selector | 10/10 days |

## 7. Live-transition criteria

Before flipping to live (`HFT_ORDER_SHADOW_MODE=0`):

1. **User approval required** (per `feedback_no_auto_deploy` memory). The
   automation does NOT flip to live by itself.
2. Shadow OOS-live Sharpe ≥ **3.0** (vs backtest 18.89). The gap between
   shadow and backtest is the main unknown; anything > 3 after 10+ days is
   acceptable, anything ≤ 2 requires a Challenger round before live.
3. Daily-loss discipline observed: hard-stop never triggered.
4. At least one rollover event observed and cleanly executed in shadow.
5. `max_pos` **must be reduced to 1** for first live day, per
   `hft-strategy-lifecycle` Phase 7 go-live checklist. Ramp to 3 over 3
   consecutive stable live days.

## 8. Rollback

If any of the following during shadow or live:

- Position accounting invariant breaks
- Fill count < 1% of expected for 2 consecutive days
- Unexplained PnL excursion exceeding 3× average daily
- Storm-related engine fault attributable to this strategy

Then:

```bash
# Immediate disable via config + restart
sed -i 's/enabled: true$/enabled: false/' \
  config/base/strategies.yaml  # (verify you only touch C14 entry)
docker compose restart hft-engine
```

Post-rollback: write incident note to `research/alphas/c14_txf_frontmonth_native_maker/INCIDENTS.md`,
fix root cause, re-run shadow day 1 from scratch (do NOT resume prior
observation window).

## 9. What this playbook does NOT cover

- **Front-month contract rotation in production.** The strategy accepts a
  literal symbol list and quotes whichever symbol produces events. A
  production rotator that dynamically resolves "front-month" to the
  current TXF contract (e.g. using the broker's expiry calendar) is a
  post-shadow engineering item — see `RELEASE_GATE.md` gate #13.
- **Live-mode fill model.** The research-side `QueuePositionStochasticFill`
  is only meaningful in backtest. Live queue position is the broker's
  problem; the shadow's "hypothetical fills" derived from market trade
  events are the closest available approximation.
- **Fee / commission accrual.** Tracked in `strategy_limits.yaml`
  `daily_loss_hard_stop_ntd` only as an aggregate; per-side cost of 0.24
  pts/side is NOT deducted from the shadow PnL counter (shadow fills are
  hypothetical, with no broker-confirmed fee). Monitor against the
  `hard_stop` bound directly.

## 10. Related files

- Live-runtime wrapper: `src/hft_platform/strategies/c14_txf_frontmonth_maker.py`
- Research impl: `research/alphas/c14_txf_frontmonth_native_maker/impl.py`
- Scorecard: `outputs/team_artifacts/alpha-research/round-6/artifacts/t5_executor_scorecard_revised.md`
- Release gate: `research/alphas/c14_txf_frontmonth_native_maker/RELEASE_GATE.md`
