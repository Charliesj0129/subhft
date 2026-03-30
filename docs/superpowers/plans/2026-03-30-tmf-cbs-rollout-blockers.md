# TMF CBS Rollout Blockers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the concrete rollout blockers for TMF CBS so session-end flattening, hard-loss halts, one-lot enforcement, concrete TMFD6 wiring, and the normalized-CBS strategy can be implemented and verified safely.

**Architecture:** The work is split into infrastructure-first slices. First make flattening executable end-to-end (`submit_intent` API + `FORCE_FLAT` through `OrderAdapter` and flatteners), then make session control real (`SessionGovernor.start()` + overnight scheduling + bootstrap wiring), then harden risk (`resulting net exposure` + global `intraday_pnl` rollout scope), then refactor CBS itself to the normalized/limit-exit design, and finally lock the runtime configuration to concrete `TMFD6` deployment wiring.

**Tech Stack:** Python 3.12, pytest, structlog, existing HFT strategy/risk/order stack, YAML config under `config/base/`

---

## File Map

- Modify: `src/hft_platform/order/adapter.py`
  Responsibility: add real `FORCE_FLAT` dispatch semantics and expose a public async submission API that flatteners can call.
- Modify: `src/hft_platform/risk/halt_flattener.py`
  Responsibility: align HALT flatten intents with `StormGuard` semantics.
- Modify: `src/hft_platform/ops/position_flattener.py`
  Responsibility: remain the platform-owned flatten path for `FORCE_FLAT` at session/halt boundaries and support the real `PositionStore.positions` shape.
- Modify: `src/hft_platform/ops/session_governor.py`
  Responsibility: schedule `OPEN/CLOSE_ONLY/FORCE_FLAT/CLOSED` transitions from wall-clock config, including overnight wrap-around, expose `start()`/`stop()`, and trigger flatten callbacks.
- Modify: `src/hft_platform/services/bootstrap.py`
  Responsibility: instantiate and wire `PositionFlattener`, `SessionGovernor`, and any position-provider plumbing required by risk/runtime startup.
- Create: `config/base/session_governor.yaml`
  Responsibility: concrete `TMFD6` day/night tracks and close/force-flat windows.
- Modify: `src/hft_platform/risk/validators.py`
  Responsibility: enforce resulting net exposure, not just per-order qty, while keeping the current rollout on global `intraday_pnl` semantics unless runtime scope support is explicitly added.
- Modify: `src/hft_platform/risk/engine.py`
  Responsibility: inject the position provider/store needed by the exposure-aware validator.
- Modify: `config/base/strategy_limits.yaml`
  Responsibility: make CBS limits runtime-effective (`max_position_lots`, `intraday_pnl` scope).
- Modify: `config/base/strategies.yaml`
  Responsibility: wire CBS to concrete `TMFD6` and its live-ready parameters.
- Modify: `src/hft_platform/strategies/cascade_bounce.py`
  Responsibility: normalized trigger, aggressive `IOC` entry-only, passive limit-exit state machine, event-driven exit bookkeeping.
- Modify: `tests/unit/test_order_adapter_force_flat.py`
  Responsibility: prove `FORCE_FLAT` dispatch actually closes inventory.
- Modify: `tests/unit/test_halt_flattener.py`
  Responsibility: prove HALT flatten emits `FORCE_FLAT`, not incompatible `NEW`.
- Modify: `tests/unit/test_position_flattener.py`
  Responsibility: prove `PositionFlattener` works with the real `PositionStore.positions` shape and calls the adapter through a public submission API.
- Modify: `tests/unit/test_session_governor.py`
  Responsibility: prove scheduled phase transitions and flatten callbacks.
- Modify: `tests/unit/test_trackgate_runner_integration.py`
  Responsibility: prove `CLOSE_ONLY` / `FORCE_FLAT` behavior remains aligned with runner filtering.
- Modify: `tests/unit/test_risk_extended_validators.py`
  Responsibility: prove resulting-position checks and intraday-loss semantics.
- Modify: `tests/unit/test_cascade_bounce.py`
  Responsibility: prove normalized trigger, `IOC` entry, exit state machine, and close-only compatibility.

## Task 1: Implement Real `FORCE_FLAT` Dispatch

**Files:**
- Modify: `src/hft_platform/order/adapter.py`
- Modify: `src/hft_platform/risk/halt_flattener.py`
- Modify: `src/hft_platform/ops/position_flattener.py`
- Modify: `tests/unit/test_order_adapter_force_flat.py`
- Modify: `tests/unit/test_halt_flattener.py`
- Modify: `tests/unit/test_position_flattener.py`

- [ ] **Step 1: Add a failing unit test for `OrderAdapter` `FORCE_FLAT` dispatch**

```python
def test_force_flat_dispatch_closes_existing_long(tmp_path):
    adapter = _make_adapter(tmp_path)
    adapter.position_store = FakePositionStore({"TMFD6": FakePos(symbol="TMFD6", net_qty=1)})
    intent = _make_intent(intent_type=IntentType.FORCE_FLAT, symbol="TMFD6", side=Side.SELL, qty=1, price=0)
    cmd = OrderCommand(cmd_id=1, intent=intent, deadline_ns=10**18, storm_guard_state=StormGuardState.HALT)

    asyncio.run(adapter._dispatch_to_api(cmd))

    adapter.client.place_order.assert_called_once()
```

- [ ] **Step 2: Run the adapter test to verify it fails**

Run: `uv run pytest --no-cov tests/unit/test_order_adapter_force_flat.py -q`
Expected: FAIL because `OrderAdapter._dispatch_to_api()` has no `FORCE_FLAT` execution branch.

- [ ] **Step 3: Add a failing halt-flattener test for HALT-compatible intent type**

```python
@pytest.mark.asyncio
async def test_on_halt_emits_force_flat():
    flattener = HaltFlattener(store, submit, enabled=True)
    await flattener.on_halt()
    intent = submit.call_args[0][0]
    assert intent.intent_type == IntentType.FORCE_FLAT
```

- [ ] **Step 4: Run the halt-flattener test to verify it fails**

Run: `uv run pytest --no-cov tests/unit/test_halt_flattener.py -q`
Expected: FAIL because current `HaltFlattener` emits `IntentType.NEW`.

- [ ] **Step 5: Add a failing `PositionFlattener` compatibility test**

```python
@pytest.mark.asyncio
async def test_flatten_track_reads_position_objects_and_uses_submit_intent():
    position_store = FakePositionStore(
        positions={"TMFD6": FakePosition(symbol="TMFD6", net_qty=1)}
    )
    adapter = AsyncMock()
    flattener = PositionFlattener(position_store=position_store, order_adapter=adapter)

    await flattener.flatten_track("futures_night", ["TMFD6"])

    adapter.submit_intent.assert_awaited_once()
```

- [ ] **Step 6: Run the flattener test to verify it fails**

Run: `uv run pytest --no-cov tests/unit/test_position_flattener.py -q`
Expected: FAIL because `PositionFlattener._get_open_positions()` assumes raw integer values and `OrderAdapter` exposes no public submission API.

- [ ] **Step 7: Implement a public async intent submission path in `OrderAdapter`**

```python
async def submit_intent(self, intent: OrderIntent) -> None:
    cmd = self._intent_to_command(intent)
    await self._dispatch_to_api(cmd)
```

This path must preserve existing guard/encoding behavior and become the supported API for flatteners.

- [ ] **Step 8: Implement minimal `FORCE_FLAT` dispatch in `OrderAdapter`**

```python
elif intent.intent_type == IntentType.FORCE_FLAT:
    net_qty = self._platform_net_position_for_symbol(intent.symbol)
    if net_qty == 0:
        return
    close_side = Side.SELL if net_qty > 0 else Side.BUY
    close_qty = abs(net_qty)
    aggressive_price = current_best_bid if close_side == Side.SELL else current_best_ask
    await self._call_api(
        "place_order",
        self.client.place_order,
        contract_code=intent.symbol,
        exchange=exchange,
        action=self._broker_codec.encode_side(close_side),
        price=self.price_codec.descale(intent.symbol, aggressive_price),
        qty=close_qty,
        order_type="IOC",
        tif="IOC",
        product_type=product_type,
        price_type=self._broker_codec.encode_price_type("LMT"),
        intent=intent,
        **order_params,
    )
```

- [ ] **Step 9: Update `HaltFlattener` to emit `FORCE_FLAT`**

```python
intent = OrderIntent(
    ...,
    intent_type=IntentType.FORCE_FLAT,
    price=0,
    qty=close_qty,
    reason="halt_flatten",
)
```

- [ ] **Step 10: Update `PositionFlattener` to read the real store shape and use `submit_intent`**

```python
def _get_open_positions(self) -> dict[str, int]:
    if hasattr(store, "positions"):
        out: dict[str, int] = {}
        for symbol, pos in store.positions.items():
            net_qty = getattr(pos, "net_qty", pos)
            if net_qty != 0:
                out[symbol] = int(net_qty)
        return out
```

Remove any fallback that depends on nonexistent `put_nowait` behavior from the runtime adapter contract.

- [ ] **Step 11: Run the focused tests to verify they pass**

Run: `uv run pytest --no-cov tests/unit/test_order_adapter_force_flat.py tests/unit/test_halt_flattener.py tests/unit/test_position_flattener.py -q`
Expected: PASS

- [ ] **Step 12: Commit**

```bash
git add src/hft_platform/order/adapter.py src/hft_platform/risk/halt_flattener.py src/hft_platform/ops/position_flattener.py tests/unit/test_order_adapter_force_flat.py tests/unit/test_halt_flattener.py tests/unit/test_position_flattener.py
git commit -m "feat: add force-flat dispatch path"
```

## Task 2: Make Session Control Real for TMFD6 Day/Night

**Files:**
- Create: `config/base/session_governor.yaml`
- Modify: `src/hft_platform/ops/session_governor.py`
- Modify: `src/hft_platform/services/bootstrap.py`
- Modify: `tests/unit/test_session_governor.py`
- Modify: `tests/unit/test_trackgate_runner_integration.py`

- [ ] **Step 1: Add a failing test for schedule-driven phase transitions**

```python
def test_governor_transitions_to_close_only_from_clock(tmp_path):
    gov = SessionGovernor(config_path=cfg_path)
    phase = gov._phase_for_wall_clock("futures_day", "13:40")
    assert phase == SessionPhase.CLOSE_ONLY
```

- [ ] **Step 2: Add a failing test for FORCE_FLAT callback on phase transition**

```python
@pytest.mark.asyncio
async def test_force_flat_phase_invokes_position_flattener(tmp_path):
    gov = SessionGovernor(config_path=cfg_path, position_flattener=flattener)
    gov.transition_track("futures_day", SessionPhase.FORCE_FLAT)
    flattener.flatten_track.assert_called_once()
```

- [ ] **Step 3: Run the governor tests to verify they fail**

Run: `uv run pytest --no-cov tests/unit/test_session_governor.py tests/unit/test_trackgate_runner_integration.py -q`
Expected: FAIL because `SessionGovernor` has no schedule evaluation loop and no flatten callback behavior.

- [ ] **Step 4: Create `config/base/session_governor.yaml` with concrete `TMFD6` tracks**

```yaml
tracks:
  futures_day:
    symbols: ["TMFD6"]
    schedule:
      - {phase: "open", time: "08:45"}
      - {phase: "close_only", time: "13:40"}
      - {phase: "force_flat", time: "13:44"}
      - {phase: "closed", time: "13:45"}
  futures_night:
    symbols: ["TMFD6"]
    schedule:
      - {phase: "open", time: "15:00"}
      - {phase: "close_only", time: "04:55"}
      - {phase: "force_flat", time: "04:59"}
      - {phase: "closed", time: "05:00"}
```

- [ ] **Step 5: Implement schedule evaluation in `SessionGovernor` with overnight wrap-around**

```python
def _phase_for_dt(self, track_name: str, dt_local: datetime) -> SessionPhase:
    # handle overnight tracks by anchoring schedule entries across day boundaries,
    # not by naive "last time <= now" lookup
```

- [ ] **Step 6: Add `start()` / `stop()` lifecycle aligned with `HFTSystem` expectations**

```python
async def start(self) -> None:
    if self._task is None:
        self._task = asyncio.create_task(self.run())
```

- [ ] **Step 7: Extend `run()` to drive track transitions from wall clock**

```python
while self._running:
    now = datetime.now(self._tz)
    for track_name in self._tracks:
        self.transition_track(track_name, self._phase_for_dt(track_name, now))
    await asyncio.sleep(1)
```

- [ ] **Step 8: Wire a real `PositionFlattener` into bootstrap + session governor**

```python
position_flattener = PositionFlattener(position_store=position_store, order_adapter=order_adapter)
session_governor = SessionGovernor(..., position_flattener=position_flattener)
```

- [ ] **Step 9: Implement `FORCE_FLAT` phase callback in `SessionGovernor`**

```python
if new_phase == SessionPhase.FORCE_FLAT and self._position_flattener is not None:
    asyncio.create_task(self._position_flattener.flatten_track(track_name, cfg.symbols))
```

- [ ] **Step 10: Re-run the governor tests**

Run: `uv run pytest --no-cov tests/unit/test_session_governor.py tests/unit/test_trackgate_runner_integration.py -q`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add config/base/session_governor.yaml src/hft_platform/ops/session_governor.py src/hft_platform/services/bootstrap.py tests/unit/test_session_governor.py tests/unit/test_trackgate_runner_integration.py
git commit -m "feat: add tmfd6 session governor scheduling"
```

## Task 3: Enforce Resulting Net Exposure and Decide `intraday_pnl` Scope

**Files:**
- Modify: `src/hft_platform/risk/validators.py`
- Modify: `src/hft_platform/risk/engine.py`
- Modify: `src/hft_platform/services/bootstrap.py`
- Modify: `config/base/strategy_limits.yaml`
- Modify: `tests/unit/test_risk_extended_validators.py`

- [ ] **Step 1: Add a failing resulting-position test**

```python
def test_rejects_order_when_resulting_position_exceeds_limit():
    validator = PositionLimitValidator(cfg, position_provider=lambda symbol, strategy_id: 1)
    intent = _make_intent(qty=1, strategy_id="CBS_TMFD6", symbol="TMFD6")
    ok, reason = validator.check(intent)
    assert ok is False
    assert "POSITION_LIMIT_EXCEEDED" in reason
```

- [ ] **Step 2: Add a failing config test for CBS `max_position_lots`**

```python
def test_cbs_tmfd6_uses_runtime_consumed_max_position_lots():
    cfg = yaml.safe_load(Path("config/base/strategy_limits.yaml").read_text())
    assert cfg["strategies"]["CBS_TMFD6"]["max_position_lots"] == 1
```

- [ ] **Step 3: Run the risk validator tests to verify they fail**

Run: `uv run pytest --no-cov tests/unit/test_risk_extended_validators.py -q`
Expected: FAIL because `PositionLimitValidator` is stateless and config still uses `max_position`.

- [ ] **Step 4: Extend `PositionLimitValidator` to accept a current-position provider**

```python
class PositionLimitValidator(RiskValidator):
    def __init__(..., position_provider: Callable[[str, str], int] | None = None):
        self._position_provider = position_provider or (lambda symbol, strategy_id: 0)

    def check(self, intent):
        current = self._position_provider(intent.symbol, intent.strategy_id)
        signed_qty = intent.qty if intent.side == Side.BUY else -intent.qty
        resulting = current + signed_qty
        if abs(resulting) > max_lots:
            return False, ...
```

- [ ] **Step 5: Inject the provider from `RiskEngine`**

```python
PositionLimitValidator(
    self.config,
    price_scale_provider,
    position_provider=self._current_strategy_symbol_net_position,
)
```

- [ ] **Step 6: Keep `intraday_pnl` rollout scope global unless runtime support is added**

```yaml
intraday_pnl:
  scope: global
  hard_limit_ntd: 8000
```

Encode the deployment precondition explicitly: if scope remains global, all other live strategies must stay disabled during CBS rollout.

- [ ] **Step 7: Inject the provider from runtime bootstrap if `RiskEngine` construction does not yet receive one**

```python
risk_engine = RiskEngine(
    ...,
    position_provider=position_store,
)
```

If `RiskEngine` cannot accept this directly, add the minimal wiring in `bootstrap.py` needed to supply the validator with current net positions.

- [ ] **Step 8: Update `config/base/strategy_limits.yaml` to use runtime-effective keys**

```yaml
strategies:
  CBS_TMFD6:
    max_position_lots: 1
    max_order_qty: 1
```

- [ ] **Step 9: Re-run risk tests**

Run: `uv run pytest --no-cov tests/unit/test_risk_extended_validators.py -q`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add src/hft_platform/risk/validators.py src/hft_platform/risk/engine.py src/hft_platform/services/bootstrap.py config/base/strategy_limits.yaml tests/unit/test_risk_extended_validators.py
git commit -m "feat: enforce resulting exposure limits"
```

## Task 4: Refactor CBS to Normalized Trigger + Passive Exit State Machine

**Files:**
- Modify: `src/hft_platform/strategies/cascade_bounce.py`
- Modify: `tests/unit/test_cascade_bounce.py`

- [ ] **Step 1: Add a failing normalized-trigger test**

```python
def test_entry_uses_sigma_over_local_vol():
    cbs = CascadeBounceStrategy(trigger_sigma=3, lookback_sec=60, ...)
    intents = cbs.handle_event(ctx, big_reversion_event)
    assert len(intents) == 1
```

- [ ] **Step 2: Add a failing test for `IOC`-only entry**

```python
def test_entry_is_always_ioc_at_touch():
    intents = cbs.handle_event(ctx, trigger_event)
    assert intents[0].tif == TIF.IOC
```

- [ ] **Step 3: Add a failing test for passive take-profit after fill**

```python
def test_on_fill_places_limit_take_profit():
    cbs.on_fill(fill_event)
    intents = cbs.handle_event(ctx, followup_event)
    assert any(i.intent_type == IntentType.NEW and i.tif == TIF.LIMIT for i in intents)
```

- [ ] **Step 4: Add a failing test for `CLOSE_ONLY` compatibility under platform-owned flattening**

```python
def test_close_only_path_only_emits_cancel_for_resting_exit():
    # CBS may cancel stale passive exits, but session-end flatten remains platform-owned
    ...
```

- [ ] **Step 5: Run the CBS tests to verify they fail**

Run: `uv run pytest --no-cov tests/unit/test_cascade_bounce.py -q`
Expected: FAIL because current CBS is bps-based, entry can use `ExecutionOptimizer`, and there is no passive-exit state machine.

- [ ] **Step 6: Implement normalized trigger fields and local-vol buffer**

```python
self._lookback_sec = lookback_sec
self._trigger_sigma = trigger_sigma
self._min_vol_samples = min_vol_samples
self._price_buf[symbol].append(_PriceEntry(now_ns, mid_x2))
local_vol_points = _rolling_rms_point_change(buf)
```

- [ ] **Step 7: Remove `ExecutionOptimizer` from CBS entry**

```python
self._place_entry(symbol, side, aggressive_price, TIF.IOC)
```

- [ ] **Step 8: Implement exit-order bookkeeping via `on_order` / `on_fill`**

```python
self._exit_order_id[symbol] = event.order_id
self._remaining_qty[symbol] = event.remaining_qty
if event.status in TERMINAL:
    self._clear_exit_state(symbol)
```

- [ ] **Step 9: Implement stop/timeout OPEN-phase exit as `CANCEL -> aggressive NEW` only while track is `OPEN`**

```python
if self._exit_order_id[symbol]:
    self.cancel(symbol, self._exit_order_id[symbol])
    self._pending_force_close[symbol] = True
```

Do not require CBS itself to emit `FORCE_FLAT` during `CLOSE_ONLY`; session-governor/position-flattener own that path.

- [ ] **Step 10: Re-run the CBS tests**

Run: `uv run pytest --no-cov tests/unit/test_cascade_bounce.py -q`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add src/hft_platform/strategies/cascade_bounce.py tests/unit/test_cascade_bounce.py
git commit -m "feat: refactor cbs for normalized live execution"
```

## Task 5: Lock Rollout Wiring to Concrete `TMFD6`

**Files:**
- Modify: `config/base/strategies.yaml`
- Modify: `config/base/strategy_limits.yaml`
- Modify: `config/base/session_governor.yaml`
- Modify: `config/symbols.yaml` (only if metadata mismatches are found)
- Test: `tests/unit/test_session_governor.py`
- Test: `tests/unit/test_cascade_bounce.py`

- [ ] **Step 1: Add a failing config assertion for concrete symbol consistency**

```python
def test_tmfd6_is_consistent_across_strategy_and_session_config():
    strategies = yaml.safe_load(Path("config/base/strategies.yaml").read_text())
    sessions = yaml.safe_load(Path("config/base/session_governor.yaml").read_text())
    assert "TMFD6" in strategies["strategies"][...]["symbols"]
    assert "TMFD6" in sessions["tracks"]["futures_day"]["symbols"]
```

- [ ] **Step 2: Run the relevant config-backed tests**

Run: `uv run pytest --no-cov tests/unit/test_session_governor.py tests/unit/test_cascade_bounce.py -q`
Expected: FAIL until all configs reference the same concrete TMFD6 symbol and session model.

- [ ] **Step 3: Update the strategy registry to concrete `TMFD6` live wiring**

```yaml
- id: "CBS_TMFD6"
  symbols: ["TMFD6"]
  params:
    ...
```

Enable only this live strategy if the hard-loss scope remains global.

- [ ] **Step 4: Verify TMFD6 metadata in `config/symbols.yaml`**

```yaml
- code: TMFD6
  product_type: future
  point_value: 10
  tick_size: 1
  price_scale: 10000
```

- [ ] **Step 5: Re-run the targeted config-backed tests**

Run: `uv run pytest --no-cov tests/unit/test_session_governor.py tests/unit/test_cascade_bounce.py tests/unit/test_risk_extended_validators.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add config/base/strategies.yaml config/base/strategy_limits.yaml config/base/session_governor.yaml config/symbols.yaml
git commit -m "chore: wire tmfd6 rollout configuration"
```

## Final Verification

**Files:**
- Verify the full touched set from Tasks 1-5

- [ ] **Step 1: Run the focused unit suite**

Run:

```bash
uv run pytest --no-cov \
  tests/unit/test_order_adapter_force_flat.py \
  tests/unit/test_halt_flattener.py \
  tests/unit/test_position_flattener.py \
  tests/unit/test_session_governor.py \
  tests/unit/test_trackgate_runner_integration.py \
  tests/unit/test_risk_extended_validators.py \
  tests/unit/test_cascade_bounce.py -q
```

Expected: all PASS

- [ ] **Step 2: Run one integration check covering HALT/session behavior if available**

Run:

```bash
uv run pytest --no-cov tests/integration/test_daily_loss_halt_flow.py -q
```

Expected: PASS or a clearly understood failure requiring fixture updates from the new `FORCE_FLAT` path

- [ ] **Step 3: Run lint on changed Python files**

Run:

```bash
uv run ruff check \
  src/hft_platform/order/adapter.py \
  src/hft_platform/risk/halt_flattener.py \
  src/hft_platform/ops/position_flattener.py \
  src/hft_platform/ops/session_governor.py \
  src/hft_platform/services/bootstrap.py \
  src/hft_platform/risk/validators.py \
  src/hft_platform/risk/engine.py \
  src/hft_platform/strategies/cascade_bounce.py \
  tests/unit/test_order_adapter_force_flat.py \
  tests/unit/test_halt_flattener.py \
  tests/unit/test_position_flattener.py \
  tests/unit/test_session_governor.py \
  tests/unit/test_trackgate_runner_integration.py \
  tests/unit/test_risk_extended_validators.py \
  tests/unit/test_cascade_bounce.py
```

Expected: no lint errors

- [ ] **Step 4: Commit the final verification touch-ups**

```bash
git add -A
git commit -m "test: verify tmf cbs rollout blockers"
```
