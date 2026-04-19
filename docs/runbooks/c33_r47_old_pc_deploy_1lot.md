# C33 + R47 Old-PC Deploy Notes (1 Lot Each)

目的：整理舊電腦連線方式、目前遠端實況，以及在不破壞現有 R47 live 的前提下，評估 `R47_MAKER_TMF=1 lot` 與 `C33_TXFD6_SOLO_MAKER=1 lot` 的部署可行性。

## Remote Target

- Host alias: `THESHOW`
- SSH target: `charl@100.91.176.126`
- Project root: `/home/charl/subhft`

Connection smoke test:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=8 charl@100.91.176.126 \
  "echo HOST=$(hostname); date -Iseconds; test -d /home/charl/subhft && echo PROJECT_OK"
```

## Current Remote State (observed 2026-04-18)

- `hft-engine` is up and healthy on `THESHOW`.
- Only `R47_MAKER_TMF` is enabled remotely.
- Remote `.env` is currently:
  - `HFT_MODE=live`
  - `HFT_ORDER_MODE=live`
  - `HFT_ORDER_SHADOW_MODE=0`
  - `HFT_GATEWAY_ENABLED=1`
- Remote `R47_MAKER_TMF` already runs with `max_pos: 1`.
- Remote `strategy_limits.yaml` also caps `R47_MAKER_TMF.max_position_lots: 1`.
- Remote worktree is dirty; do not assume `git pull` is safe in-place.

## Hard Blockers

### 1. Remote host does not yet have C33 scaffold

The remote checkout inspected on 2026-04-18 does not include the `C33_TXFD6_SOLO_MAKER` entry nor its runbook/artifacts. A deploy must first bring the remote codebase up to a revision that contains:

- `src/hft_platform/strategies/c33_txfd6_solo_maker.py`
- `config/base/strategies.yaml` C33 entry
- `config/base/strategy_limits.yaml` C33 entry
- `research/alphas/c33_txfd6_solo_passive_maker/`

### 2. C33 is governance-gated to shadow first

Per `research/alphas/c33_txfd6_solo_passive_maker/RELEASE_GATE.md` and `SHADOW_DEPLOY.md`:

- remote deployment is manual
- agents do not execute the shadow/live flip
- C33 must qualify through shadow before live

### 3. Shadow mode is global, not per-strategy

`ShadowOrderSink` is controlled by global `HFT_ORDER_SHADOW_MODE=1`. On the current architecture, enabling shadow for C33 on the single `hft-engine` would also intercept R47 orders. There is no per-strategy shadow toggle in the order adapter.

### 4. Single Runtime Principle blocks a second parallel broker runtime

The platform only allows one runtime to hold the Shioaji session. So a second engine/container cannot be spun up on the same host to shadow C33 while keeping R47 live.

## What Is Safe Right Now

### Safe fact pattern

- `R47_MAKER_TMF` live at `1 lot`
- `C33_TXFD6_SOLO_MAKER` not remotely deployed yet
- `R47 live + C33 shadow on the same engine` is **not feasible** with current architecture
- `R47 live + C33 live` is **not policy-compliant** because C33 has not completed shadow qualification

## Recommended Path

### Option A — Strictly policy-compliant

1. Sync the remote host to a commit that contains C33.
2. Temporarily pause live deployment changes to R47.
3. Run a dedicated C33 shadow period on the old PC with conservative `max_pos=1`.
4. After shadow qualification, promote C33 to live.
5. Only then run both live with:
   - `R47_MAKER_TMF.max_pos = 1`
   - `C33_TXFD6_SOLO_MAKER.max_pos = 1`
   - global `max_position_lots = 2`

### Option B — User wants both live immediately

This requires knowingly bypassing the C33 shadow gate. That is a governance exception, not a normal deployment.

If that exception is intentionally taken, the minimum conservative controls are:

- `R47_MAKER_TMF.params.max_pos = 1`
- `R47_MAKER_TMF` risk limit `max_position_lots = 1`
- `C33_TXFD6_SOLO_MAKER.params.max_pos = 1`
- `C33_TXFD6_SOLO_MAKER` risk limit `max_position_lots = 1`
- `global_defaults.max_position_lots = 2`
- keep `max_order_qty = 1` for both strategies

## Manual Commands

These commands are for the user to run manually on the old PC workflow.

### 1. Verify remote before any deploy

```bash
ssh charl@100.91.176.126 "
  cd /home/charl/subhft &&
  hostname &&
  git rev-parse --short HEAD &&
  git status --short &&
  docker compose ps
"
```

### 2. If you want to shadow C33 (policy-compliant path)

Warning: this shadows the whole engine, including R47, because shadow mode is global.

```bash
ssh charl@100.91.176.126 "
  cd /home/charl/subhft &&
  export HFT_ORDER_SHADOW_MODE=1 &&
  export HFT_ORDER_MODE=sim &&
  export HFT_GATEWAY_ENABLED=0 &&
  docker compose restart hft-engine
"
```

Then manually enable only the C33 strategy in `config/base/strategies.yaml` on the remote host and keep TXF-conflicting strategies disabled.

### 3. If you intentionally run both live after qualification or exception approval

Required remote config target:

```yaml
# config/base/strategies.yaml
- id: "R47_MAKER_TMF"
  enabled: true
  params:
    max_pos: 1

- id: "C33_TXFD6_SOLO_MAKER"
  enabled: true
  params:
    max_pos: 1
    spread_threshold_pts: 5
    inventory_skew_tenths: 2
    shadow_mode: false
    queue_share: 0.05
    variant: "R47-minimal"
```

```yaml
# config/base/strategy_limits.yaml
global_defaults:
  max_position_lots: 2

strategies:
  R47_MAKER_TMF:
    max_position_lots: 1
    max_order_qty: 1

  C33_TXFD6_SOLO_MAKER:
    max_position_lots: 1
    max_order_qty: 1
```

Restart:

```bash
ssh charl@100.91.176.126 "
  cd /home/charl/subhft &&
  docker compose restart hft-engine &&
  docker compose logs --tail=100 hft-engine
"
```

## Verification Targets

After restart, verify:

```bash
ssh charl@100.91.176.126 "
  curl -fsS http://localhost:9090/metrics | rg 'R47_MAKER_TMF|C33_TXFD6_SOLO_MAKER|max_position|shadow_mode_active'
"
```

Also inspect:

- `docker compose logs --tail=200 hft-engine`
- `hft_strategy_position_current{strategy_id="R47_MAKER_TMF"}`
- `hft_strategy_position_current{strategy_id="C33_TXFD6_SOLO_MAKER"}`

## Bottom Line

On the current code and remote state, the only clean answer is:

- old PC connection is known and working
- R47 is already running at 1 lot
- deploying C33 "like R47" on the same host is blocked by both:
  - remote code drift / missing C33 scaffold on the host
  - architecture + governance (global shadow mode, single runtime, C33 shadow-first rule)

If a governance exception is desired, treat it as an explicit manual live rollout with `1 + 1 = 2 lots` hard caps, not as a standard C33 promotion path.
