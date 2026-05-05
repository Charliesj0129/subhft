# Slice B — Maker Realism (per-slice plan)

> Phase 2 of the alpha-promotion overhaul (master blueprint: `/home/charlie/.claude/plans/curried-launching-unicorn.md`). Follows Slice A (`66f3eb8a`, PR #337) and Slice C (`5861730d`, PR #339, 2026-05-05). Branch: `slice-b/maker-realism`. Single PR, ~16-18 tasks, ~2000-2800 LoC including tests + calibration data fixtures.

## 0. Spec reference

The blueprint's Slice B section (`/home/charlie/.claude/plans/curried-launching-unicorn.md` §"Slice B — Maker Realism") with Codex 2026-05-04 review applied (verdict ACCEPT-WITH-FIXES). Anchors verified on `main` at `5861730d`:

| Anchor | File | Line |
|---|---|---|
| `class MakerEngine` | `research/backtest/maker_engine.py` | 219 |
| Day loop (calls `_run_day`, then `_compute_fifo_pnl`) | `research/backtest/maker_engine.py` | 265-288 |
| `_run_day(strategy, events) → (fills, position)` | `research/backtest/maker_engine.py` | 364, returns at 502 |
| `_compute_fifo_pnl(fills) → (gross, trips, wins)` (realized only) | `research/backtest/maker_engine.py` | 505 |
| `class QueueDepletionFill` (`queue_fraction=0.5` literal) | `research/backtest/fill_models.py` | 42-77 |
| `class BacktestResult` (frozen dataclass; has `daily_pnl: list[dict] \| None`, `final_pos` already in row) | `research/backtest/types.py` | 43-80 |
| `class MakerStrategyBridge(BaseStrategy)` (no `on_session_end`) | `src/hft_platform/backtest/maker_bridge.py` | 41 |
| `vm_ul6_strict.yaml :: blocking_sub_gates` (currently lists 7 entries incl. `replay_parity`) | `config/research/profiles/vm_ul6_strict.yaml` | 49-66 |
| `class SubGate(Protocol)` + `SubGateResult(name, passed, metrics, details)` frozen | `src/hft_platform/alpha/_sub_gates/registry.py` | 17, 36-50 |
| `_evaluate_gate_d` (Slice C added `replay_parity_audit` at 339-345) | `src/hft_platform/alpha/promotion.py` | 327-345 |
| `PromotionConfig` (Slice C added `min_replay_parity_match_pct=95.0`) | `src/hft_platform/alpha/promotion.py` | 39-91 |
| `v2026-04-24_measured` Shioaji broker latency profile (place P95 395ms, cancel P95 59ms, 6.7× asymmetric) | `config/research/latency_profiles.yaml` | 71+ |

## 1. Goal

The MakerEngine backtest must (a) mark the open-position residual carried into the next session to market so the realized-only FIFO PnL stops over-stating profit; (b) replace `QueueDepletionFill`'s hard-coded `queue_fraction=0.5` with a calibrated `q_hat(symbol, hour, depth)` table proven within ±15% of CK-replay actual fill rates; (c) make the place/cancel latency P95 a hard Gate-D blocker at the `v2026-04-24_measured` profile (no silent fall-through when the profile is missing); (d) close out residual on the live side by calling FORCE_FLAT through `MakerStrategyBridge.on_session_end()`; (e) two new sub-gates `inventory_mtm_gate` and `cost_uncertainty_gate` blocking under `vm_ul6_strict`. **DoD-B5 lock-in:** Slice C's `replay_parity_gate` MUST still pass on the modified MakerEngine (parity preserved end-to-end).

## 2. Why

Per `docs/incidents/2026-04-24-r47-backtest-credibility-audit.md:530-540`, the R47 deployed-config baseline is `+2,398 NTD / 39 fills` *under the corrected 395/59ms latency profile across 31 days* — and 96.9% of that comes from a single day (2026-04-02). The realized-only FIFO PnL, the literal `queue_fraction=0.5`, and the missing P95 latency hard-blocker are jointly responsible: they let small-sample / single-day-dominated alphas reach Gate D with a positive headline number that the cost floor and queue model would not have endorsed if computed honestly. R47-OE1's live PnL of −1,722 NTD on 2026-04-21 against a +7,701 NTD same-day backtest is the consequence; Slice C catches the cancel-path short-circuit, Slice B catches the realism gap that allows the headline number itself to be wrong.

## 3. Pre-flight

Already executed before drafting this plan:

1. **Slice C merged** — PR #339 squashed to `5861730d` on main; tag `slice-c-merged-2026-05-05` corrected.
2. **WIP-park** — no in-flight tracked changes worth parking on `main` at `5861730d` (working tree had only `uv.lock` drift; reverted). The earlier loop_v1 work that contaminated the slice-c branch is preserved on `origin/loop-v1/convergence` (branch tip `606fb5ef`); not touched by Slice B.
3. **Branch** — `slice-b/maker-realism` cut off `5861730d`.

Pre-flight to execute at the start of Task 1:

4. **Pre-B baseline capture (DoD-B1 evidence)** — before any maker_engine.py edit, run R47 backtest under current `main` MakerEngine on the **canonical 31-day TMFD6 fixture** with `latency_profile=r47_maker_shioaji_p95_v2026-04-24_measured`. Persist the artifact at `tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_pre_b.json` with fields `{pnl_ntd, fills, daily_pnl[]}`. Expected: `pnl_ntd ≈ +2,398, fills = 39`. This file is the post-B comparison anchor.
5. **Cost-floor anchor capture** — record the cost-floor figure as documented in `docs/incidents/2026-04-24-r47-backtest-credibility-audit.md` (cite the exact line) so DoD-B1's "post-B PnL ≤ cost_floor × 39 fills" is checkable from the plan.

## 4. Inheritance from Slice A and Slice C

1. **Profile-driven blocking** — every new sub-gate is registered as `(result, config, thresholds) → SubGateResult`. The two new gates (`inventory_mtm`, `cost_uncertainty`) get added to `vm_ul6_strict.yaml :: blocking_sub_gates`. Loose defaults stay loose; `make research` exploratory runs unaffected.
2. **Subagent-driven execution** — fresh subagent per task with two-stage review (`superpowers:subagent-driven-development`). Same RED→GREEN→commit cadence as Slice C.
3. **`make ci` green = done** — lint + typecheck + coverage all pass before merge. No `--no-verify`. Domain-weighted coverage floors apply (PR #328).
4. **Float exception lives in `research/`** — `25-architecture-governance.md` §11 explicitly permits `float` in `src/hft_platform/alpha/` and `research/`. MakerEngine residual MtM math stays float. Live-side `MakerStrategyBridge.on_session_end()` writes scaled int / Decimal money values, routed through the existing risk/order pipeline (no new precision violations).
5. **Replay parity preserved** — DoD-B5 explicitly re-runs Slice C's `replay_parity_gate` end-to-end on the new MakerEngine and asserts no regression.

## 5. File structure

### New files

| Path | Purpose |
|---|---|
| `research/backtest/calibrate_queue_fill.py` | Calibration harness — produces `q_hat(symbol, hour, depth)` table from CK-replay |
| `research/backtest/q_hat_table.py` | Lookup type + `load(path) → QHatTable` + `lookup(symbol, hour, depth) → float` with explicit fallback to 0.5 |
| `research/backtest/q_hat_data/tmfd6_q_hat.parquet` | Calibrated fixture for TMFD6 (committed) |
| `research/backtest/q_hat_data/txfd6_q_hat.parquet` | Calibrated fixture for TXFD6 (committed) |
| `research/backtest/q_hat_data/txo_q_hat.parquet` | Calibrated fixture for TXO (committed; smaller sample acceptable) |
| `src/hft_platform/alpha/_sub_gates/inventory_mtm.py` | `InventoryMtMGate(applies_to={"maker"})` |
| `src/hft_platform/alpha/_sub_gates/cost_uncertainty.py` | `CostUncertaintyGate(applies_to={"maker","taker"})` |
| `tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_pre_b.json` | Pre-B baseline artifact (DoD-B1) |
| `tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_post_b.json` | Post-B artifact (DoD-B1 comparison) |
| `tests/fixtures/maker_engine_pre_mtm_baseline/robust_alpha_synthetic.json` | Synthetic robust-alpha fixture for DoD-B2 PASS path (mirrors Slice A's clean-fixture pattern) |
| `tests/integration/test_inventory_mtm_e2e.py` | DoD-B1 + DoD-B2 end-to-end |
| `tests/integration/test_queue_calibration.py` | DoD-B3 end-to-end (predicted vs CK-replay actual within ±15% across 5 days) |
| `tests/integration/test_session_end_force_flat.py` | DoD-B4 (live FORCE_FLAT on residual) |
| `tests/integration/test_replay_parity_post_slice_b.py` | DoD-B5 lock-in (Slice C's gate still passes) |
| `tests/unit/backtest/test_residual_mtm.py` | Unit RED→GREEN for `_compute_residual_mtm` |
| `tests/unit/backtest/test_q_hat_table.py` | Unit RED→GREEN for `q_hat_table.lookup` and fallback |
| `tests/unit/alpha/test_inventory_mtm_gate.py` | Unit gate fail/pass coverage |
| `tests/unit/alpha/test_cost_uncertainty_gate.py` | Unit gate fail/pass coverage |
| `tests/unit/alpha/test_latency_audit_strict_failclosed.py` | Unit harden P95 fail-closed when profile missing |
| `tests/unit/backtest/test_maker_bridge_on_session_end.py` | Unit force-flat residual close-out |
| `docs/runbooks/maker-realism-gate.md` | Operator runbook (mirror Slice C runbook structure) |
| `tests/fixtures/q_hat_calibration/<symbol>_<date>_replay_actual.parquet` | CK-replay actual fill rates per symbol×day (committed; 5 days each for TMFD6, TXFD6, TXO) |

### Modified files

| Path | Modification |
|---|---|
| `research/backtest/maker_engine.py` | Add `_compute_residual_mtm(open_pos: int, mark_price: int, mark_method: str) → float` static method; insert call in day-loop after `_run_day` returns at line 270 (before `_compute_fifo_pnl`); fold `residual_mtm_pts` into `daily_pnl` row at line 286; track `residual_qty` and `mark_method` per row. Update `MakerEngine.run` (line 245) to thread `mark_method` through (default `"last_mid"`, optional `"worse_of_mid_last_trade"` per profile). |
| `research/backtest/fill_models.py` | `QueueDepletionFill.__init__` gains optional `q_hat_table: QHatTable \| None = None`; if provided, `post_quote(side, price, book_qty)` looks up `q_hat(symbol, hour, depth)` instead of using `self._qf`. Keep `0.5` fallback preserved. New `__init__` kw `symbol: str = ""` and `clock: Callable[[], int] \| None = None` so the model knows the hour-of-day. |
| `research/backtest/types.py` | `BacktestResult` gains `residual_mtm_pts: float = 0.0`, `residual_qty: int = 0`, `mark_method: str = ""`. Daily-pnl rows additionally carry `residual_mtm_pts` and `mark_method` keys. |
| `src/hft_platform/alpha/_sub_gates/__init__.py` | Register `InventoryMtMGate()` and `CostUncertaintyGate()` in `ensure_builtin_sub_gates_registered()`. |
| `config/research/profiles/vm_ul6_strict.yaml` | Add `cost_floor_per_fill_pts: 0.5` and `cost_uncertainty_p95_lower_bound_min_pts: 0.0` under both maker (line 47) and taker (line 17) thresholds. Append `- inventory_mtm` and `- cost_uncertainty` to `blocking_sub_gates`. |
| `src/hft_platform/alpha/promotion.py` | `PromotionConfig` gains `min_inventory_mtm_safety_margin_pct: float = 5.0`, `min_cost_uncertainty_p95_lower_bound_pts: float = 0.0`. `_evaluate_gate_d` extends the audits dict with `inventory_mtm_audit` and `cost_uncertainty_audit` mirroring `latency_audit` / `replay_parity_audit`. |
| `src/hft_platform/alpha/latency_audit.py` | Harden P95 enforcement: when `place_ns/cancel_ns` profile entries are absent, the audit now FAILS CLOSED (currently logs and returns advisory). New strict-mode toggle `strict: bool = False` on the audit signature; `_evaluate_gate_d` passes `strict=True` when profile is `vm_ul6_strict`. |
| `src/hft_platform/backtest/maker_bridge.py` | Add `def on_session_end(self) → list[OrderIntent]:` returning `[OrderIntent(side=opposite_of_residual, qty=abs(residual_qty), price=cur_mid, kind="MARKET", reason="session_end_force_flat")]`. Wire from supervisor's session-end signal (existing hook in `services/bootstrap.py` if present; otherwise add minimal hook). |
| `docs/operations/env-vars-reference.md` | Add `HFT_MAKER_MARK_METHOD` (default `last_mid`; alternative `worse_of_mid_last_trade`) and `HFT_QUEUE_CALIBRATION_TABLE_PATH` (default `research/backtest/q_hat_data/<HFT_SYMBOLS_PRIMARY>_q_hat.parquet`). |
| `docs/architecture/current-architecture.md` | Append §7B "Slice B — Maker Realism" mirroring §7A's seven-row surface table. |

## 6. Tasks

> RED→GREEN→commit cadence per task. Fresh subagent per task. Two-stage review between tasks. Each task self-contained: any subagent can pick it up cold.

### Task 1 — Pre-B baseline artifact (DoD-B1 evidence)

**Goal.** Run R47 backtest under current main MakerEngine on the canonical 31-day TMFD6 fixture with `r47_maker_shioaji_p95_v2026-04-24_measured` and persist the artifact.

**Files.** New `tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_pre_b.json`. Optional helper script `scripts/capture_pre_b_baseline.py` (allowed in `scripts/`, not committed to `src/`).

**Code sketch.**
```python
# scripts/capture_pre_b_baseline.py
from research.backtest.maker_engine import MakerEngine
from research.backtest.fill_models import QueueDepletionFill
from research.tools.fixtures import load_canonical_fixture

result = MakerEngine(
    ck_source=load_canonical_fixture("tmfd6_31d"),
    fill_model=QueueDepletionFill(queue_fraction=0.5),
    cost_model=R47CostModel(),
    latency_profile="r47_maker_shioaji_p95_v2026-04-24_measured",
).run(strategy=R47MakerPivot(), instrument="TMFD6", dates=...)

artifact = {
    "pnl_ntd": result.equity_curve[-1] * POINT_VALUE,
    "fills": int(result.daily_pnl_total_fills()),
    "daily_pnl": result.daily_pnl,
    "fixture_id": "tmfd6_31d_2026_03_to_2026_04",
    "latency_profile": "r47_maker_shioaji_p95_v2026-04-24_measured",
    "captured_at": "2026-05-05",
    "captured_by": "slice-b-task-1",
}
Path("tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_pre_b.json").write_text(
    json.dumps(artifact, sort_keys=True, indent=2)
)
```

**Verification.**
- `pnl_ntd` ≈ `+2,398` (within ±50 NTD of credibility-audit incident doc).
- `fills` == `39`.
- Artifact roundtrips through `json.loads` cleanly.

**Commit.** `feat(backtest): pre-B baseline artifact for R47/TMFD6/31d (Slice B task 1)` — single artifact file.

### Task 2 — `_compute_residual_mtm` static method + unit tests

**Goal.** Add the residual-MtM helper. RED first: write a failing unit test that exercises mark methods and edge cases, then GREEN.

**Files.**
- Modify `research/backtest/maker_engine.py` (add static method at end of `MakerEngine` class, near `_compute_fifo_pnl`).
- New `tests/unit/backtest/test_residual_mtm.py`.

**Code sketch.**
```python
@staticmethod
def _compute_residual_mtm(
    open_pos: int,
    mark_price: int,
    avg_entry_price: int,
    mark_method: str = "last_mid",
) -> float:
    """Mark-to-market the un-FIFO'd residual position to a chosen mark.

    open_pos > 0 → long; open_pos < 0 → short; 0 → no residual.
    All prices are scaled int (x10000); returned PnL is in points (float).
    """
    if open_pos == 0:
        return 0.0
    pnl_int = open_pos * (mark_price - avg_entry_price)
    return pnl_int / 10000.0  # scaled-int → points
```

**RED test cases.**
1. `open_pos=0` → returns `0.0` regardless of mark.
2. `open_pos=+1, mark=avg+50pts` → `+50.0`.
3. `open_pos=-1, mark=avg+50pts` → `-50.0` (short loses).
4. `mark_method="worse_of_mid_last_trade"` with mid=100, last_trade=99 long: should pick 99.
5. Float-precision regression: `open_pos=+10, mark-avg = 1` → exactly `0.001 * 10 = 0.01` (no `9.999...e-3` artifacts).

**Verification.** `uv run pytest tests/unit/backtest/test_residual_mtm.py -q` → 5 PASS, 0 FAIL.

**Commit.** `feat(backtest): residual MtM helper (Slice B task 2)`.

### Task 3 — Wire `_compute_residual_mtm` into the day loop

**Goal.** Insert the call after `_run_day` returns, fold `residual_mtm_pts` into `daily_pnl[]` rows, fold into `total_gross` so `equity_curve` reflects MtM-aware cumulative PnL.

**Files.** Modify `research/backtest/maker_engine.py:270-288`.

**Diff sketch.**
```python
day_fills, day_position = self._run_day(strategy, events)
day_gross, day_trips, day_wins = self._compute_fifo_pnl(day_fills)

# NEW (Slice B):
day_residual_mtm = self._compute_residual_mtm(
    open_pos=day_position,
    mark_price=self._last_mid,
    avg_entry_price=self._last_avg_entry,
    mark_method=self._mark_method,
)
day_residual_qty = abs(day_position)
day_gross_mtm_aware = day_gross + day_residual_mtm
day_net = self._cost_model.apply(day_gross_mtm_aware, len(day_fills))
total_gross += day_gross_mtm_aware
total_residual_mtm += day_residual_mtm

equity_points.append(equity_points[-1] + day_net)
daily_pnl.append({
    "date": date,
    "pnl_pts": round(day_net, 2),
    "gross_pts": round(day_gross_mtm_aware, 2),
    "fills": len(day_fills),
    "trips": day_trips,
    "wins": day_wins,
    "final_pos": day_position,
    "residual_mtm_pts": round(day_residual_mtm, 2),  # NEW
    "residual_qty": day_residual_qty,                # NEW
    "mark_method": self._mark_method,                # NEW
})
```

`MakerEngine.__init__` gains `mark_method: str = "last_mid"`. `_last_mid` and `_last_avg_entry` tracking added to `_run_day` (return tuple grows to `(fills, position, last_mid, last_avg_entry)` — update return-callsite accordingly).

**RED test.** Augment `tests/unit/backtest/test_residual_mtm.py` with one MakerEngine-level test running 1 day with intentional unmatched residual; assert `daily_pnl[0]["residual_mtm_pts"] != 0.0` and `equity_curve[-1]` includes the residual.

**Verification.**
- Unit test passes.
- Existing maker_engine tests still pass (`uv run pytest tests/unit/backtest/ -q --no-cov`).
- Run pre-B baseline script again → numbers DIFFER from Task 1's artifact (this is expected; Task 15 will re-pin as post-B).

**Commit.** `feat(backtest): MtM-aware day loop folds residual into daily_pnl (Slice B task 3)`.

### Task 4 — Extend `BacktestResult` with residual fields

**Goal.** Frozen-dataclass field additions; default values for backward compat.

**Files.** Modify `research/backtest/types.py:43-80`.

**Diff sketch.**
```python
@dataclass(frozen=True)
class BacktestResult:
    ...
    # --- Maker-specific (None for taker) ---
    maker_scorecard: dict | None = None
    per_spread_breakdown: dict | None = None
    queue_fraction: float | None = None
    # --- Slice B: Maker Realism (added 2026-05-05) ---
    residual_mtm_pts: float = 0.0
    residual_qty: int = 0
    mark_method: str = ""
    # --- Daily detail ---
    daily_pnl: list[dict] | None = None
```

**Verification.** `uv run pytest tests/unit/backtest/ -q --no-cov` → all green.

**Commit.** `feat(backtest): BacktestResult residual fields (Slice B task 4)`.

### Task 5 — `q_hat_table.py` lookup type + fallback semantics

**Goal.** Define the typed lookup with explicit fallback.

**Files.** New `research/backtest/q_hat_table.py`. New `tests/unit/backtest/test_q_hat_table.py`.

**Code sketch.**
```python
from dataclasses import dataclass
from pathlib import Path
import pyarrow.parquet as pq


@dataclass(frozen=True)
class QHatTable:
    """Calibrated queue_fraction lookup keyed by (symbol, hour, depth_bucket).

    depth_bucket is the LOB depth tier ("shallow" if depth < 5, "deep" otherwise).
    """
    _data: dict[tuple[str, int, str], float]
    fallback: float = 0.5

    @classmethod
    def load(cls, path: Path | str) -> "QHatTable":
        table = pq.read_table(str(path))
        records = table.to_pylist()
        data = {(r["symbol"], int(r["hour"]), r["depth_bucket"]): float(r["q_hat"]) for r in records}
        return cls(_data=data)

    def lookup(self, symbol: str, hour: int, depth: int) -> float:
        bucket = "shallow" if depth < 5 else "deep"
        return self._data.get((symbol, hour, bucket), self.fallback)
```

**RED tests.**
1. Empty table → `lookup(...)` returns `fallback` (0.5).
2. Loaded table with TMFD6 hour=9 shallow → returns calibrated value.
3. Unknown symbol → returns `fallback`.
4. Unknown hour → returns `fallback`.
5. Boundary `depth=5` → uses `"deep"` bucket.

**Verification.** `uv run pytest tests/unit/backtest/test_q_hat_table.py -q` → 5 PASS.

**Commit.** `feat(backtest): QHatTable lookup with explicit 0.5 fallback (Slice B task 5)`.

### Task 6 — Calibration harness `calibrate_queue_fill.py`

**Goal.** Generate `q_hat(symbol, hour, depth)` from CK-replay actual fill rates.

**Files.** New `research/backtest/calibrate_queue_fill.py`. New `tests/unit/backtest/test_calibrate_queue_fill.py` (small synthetic CK fixture).

**API sketch.**
```python
def calibrate(
    symbol: str,
    dates: list[str],
    out_path: Path,
    *,
    ck_source: ChDataSource,
) -> QHatTable:
    """For each (hour, depth_bucket) cell:
       q_hat = mean over `dates` of (#fills / #post_quote_attempts)
       where post_quote_attempt = a quote was placed AND tracked AND
       at least one trade arrived through the price.
       Cells with n < 30 attempts are dropped (fallback applies).
    """
```

**RED test.** Inject synthetic CK fixture with known fill rates; assert calibration recovers them within ±0.02.

**Verification.** `uv run pytest tests/unit/backtest/test_calibrate_queue_fill.py -q` → green.

**Commit.** `feat(backtest): queue-fill calibration harness (Slice B task 6)`.

### Task 7 — Generate and commit `q_hat_data/` fixtures (TMFD6, TXFD6, TXO)

**Goal.** Run `calibrate_queue_fill.py` against 5+ CK-replay days per symbol; commit the parquet outputs.

**Files.** New `research/backtest/q_hat_data/{tmfd6,txfd6,txo}_q_hat.parquet`. New `tests/fixtures/q_hat_calibration/<symbol>_<date>_replay_actual.parquet` (the 5 days × 3 symbols = 15 small fixtures used as the calibration input — committed for reproducibility).

**Sample-size note.** TXO calibration may produce ≥5 fewer hour×depth cells than TMFD6 because options activity is concentrated near expiry; cells with `n < 30` legitimately fall through to 0.5.

**Verification.**
- All three parquet outputs load via `QHatTable.load(...)`.
- `tests/integration/test_queue_calibration.py` (Task 8) verifies prediction error.

**Commit.** `data(backtest): commit q_hat fixtures for TMFD6/TXFD6/TXO (Slice B task 7)`.

### Task 8 — Replace `QueueDepletionFill` literal `qf=0.5` with table lookup

**Goal.** Wire `QHatTable` into `QueueDepletionFill.post_quote`. Keep `queue_fraction=0.5` as fallback for unknown cells / missing tables.

**Files.** Modify `research/backtest/fill_models.py:42-77`. Augment `tests/unit/backtest/test_q_hat_table.py` with QueueDepletionFill integration cases.

**Diff sketch.**
```python
class QueueDepletionFill:
    __slots__ = ("_qf", "_q_hat_table", "_symbol", "_clock")

    def __init__(
        self,
        queue_fraction: float = 0.5,
        *,
        q_hat_table: QHatTable | None = None,
        symbol: str = "",
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._qf = queue_fraction
        self._q_hat_table = q_hat_table
        self._symbol = symbol
        self._clock = clock or (lambda: 0)

    def post_quote(self, side: str, price: int, book_qty: int) -> QueuePosition:
        qf = self._qf
        if self._q_hat_table is not None:
            hour = self._clock() // 3600 % 24
            qf = self._q_hat_table.lookup(self._symbol, hour, book_qty)
        queue_ahead = max(1, int(book_qty * qf))
        return QueuePosition(side=side, price=price, queue_ahead=queue_ahead)
```

**RED test.**
1. Without table → behaves identically to pre-B (regression check on existing tests).
2. With table → queue_ahead reflects looked-up `qf`.
3. Unknown cell → falls back to `queue_fraction=0.5`.

**DoD-B3 integration test.** New `tests/integration/test_queue_calibration.py`:
```python
def test_predicted_fill_rate_matches_ck_replay_within_15pct():
    table = QHatTable.load("research/backtest/q_hat_data/tmfd6_q_hat.parquet")
    for date in TMFD6_5DAY_FIXTURE:
        actual = ck_replay_actual_fill_rate(symbol="TMFD6", date=date)
        predicted = simulate_with_table(table, symbol="TMFD6", date=date)
        assert abs(predicted - actual) / actual < 0.15
```

**Verification.** `uv run pytest tests/unit/backtest/ tests/integration/test_queue_calibration.py -q` → all green.

**Commit.** `feat(backtest): QueueDepletionFill uses calibrated q_hat table (Slice B task 8)`.

### Task 9 — `InventoryMtMGate` sub-gate

**Goal.** Implement the gate: fail if `daily_pnl_realized + residual_mtm < cost_floor * n_fills`.

**Files.** New `src/hft_platform/alpha/_sub_gates/inventory_mtm.py`. New `tests/unit/alpha/test_inventory_mtm_gate.py`.

**Code sketch.**
```python
from .registry import SubGate, SubGateResult


class InventoryMtMGate:
    name: str = "inventory_mtm"
    applies_to: frozenset[str] = frozenset({"maker"})

    def evaluate(self, result, config, thresholds) -> SubGateResult:
        cost_floor = thresholds.get("cost_floor_per_fill_pts")
        n_fills = sum(d.get("fills", 0) for d in (result.daily_pnl or []))
        realized = sum(d.get("pnl_pts", 0.0) for d in (result.daily_pnl or []))
        residual_mtm = sum(d.get("residual_mtm_pts", 0.0) for d in (result.daily_pnl or []))
        net_after_residual = realized + residual_mtm
        cost_floor_total = cost_floor * n_fills if cost_floor is not None else None
        passed = (
            cost_floor_total is None
            or net_after_residual >= cost_floor_total
        )
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "realized_pts": realized,
                "residual_mtm_pts": residual_mtm,
                "net_pts": net_after_residual,
                "cost_floor_total_pts": cost_floor_total,
                "n_fills": n_fills,
            },
            details=(
                "Net PnL (realized + residual MtM) is below cost floor."
                if not passed else "OK"
            ),
        )
```

**RED tests.**
1. R47 fixture with realized=`+2,398/10` pts, residual=`-2,500/10` pts → FAIL.
2. Synthetic robust fixture (realized > cost_floor × n_fills, residual ≈ 0) → PASS.
3. Missing `cost_floor_per_fill_pts` threshold → PASS (advisory, not fail-closed).
4. Empty `daily_pnl` → PASS (no fills, no claim).

**Verification.** `uv run pytest tests/unit/alpha/test_inventory_mtm_gate.py -q` → 4 PASS.

**Commit.** `feat(alpha): InventoryMtMGate sub-gate (Slice B task 9)`.

### Task 10 — `CostUncertaintyGate` sub-gate

**Goal.** Fail when the P95 cost-band lower bound is ≤ 0 — i.e., the alpha's edge is statistically indistinguishable from cost noise.

**Files.** New `src/hft_platform/alpha/_sub_gates/cost_uncertainty.py`. New `tests/unit/alpha/test_cost_uncertainty_gate.py`.

**API sketch.**
```python
class CostUncertaintyGate:
    name: str = "cost_uncertainty"
    applies_to: frozenset[str] = frozenset({"maker", "taker"})

    def evaluate(self, result, config, thresholds) -> SubGateResult:
        # P95 cost-band lower bound = pnl_pts - 1.645 * cost_sensitivity_ratio * mean_cost_per_fill * n_fills
        threshold = thresholds.get("cost_uncertainty_p95_lower_bound_min_pts", 0.0)
        ...
```

Reuses `Scorecard.cost_sensitivity_ratio` at `research/registry/scorecard.py:44-48`.

**RED tests.**
1. R47 fixture (cost-sensitive) → FAIL.
2. Synthetic robust fixture (high edge / cost ratio) → PASS.
3. Missing `cost_sensitivity_ratio` field → fail-closed under strict; advisory under loose.

**Verification.** `uv run pytest tests/unit/alpha/test_cost_uncertainty_gate.py -q` → green.

**Commit.** `feat(alpha): CostUncertaintyGate sub-gate (Slice B task 10)`.

### Task 11 — Register both gates and add to `vm_ul6_strict.yaml`

**Goal.** Wire into `ensure_builtin_sub_gates_registered()` and `blocking_sub_gates`.

**Files.**
- Modify `src/hft_platform/alpha/_sub_gates/__init__.py` (add `InventoryMtMGate()` and `CostUncertaintyGate()` to candidates list — mirror Slice C `replay_parity` registration).
- Modify `config/research/profiles/vm_ul6_strict.yaml`:
  - Append `cost_floor_per_fill_pts: 0.5` and `cost_uncertainty_p95_lower_bound_min_pts: 0.0` under both maker and taker thresholds.
  - Append `- inventory_mtm` and `- cost_uncertainty` to `blocking_sub_gates`.
- Modify `src/hft_platform/alpha/_gate_c.py` (passthrough block for the new metrics so the dispatcher's `_invoke_sub_gates` can read them — mirror `replay_parity_report` block from Slice C).

**Verification.** `uv run pytest tests/unit/alpha/test_strict_profile_e2e.py -q` (existing Slice A/C aggregator test) → still green; new gates discoverable.

**Commit.** `feat(alpha): register inventory_mtm + cost_uncertainty in strict profile (Slice B task 11)`.

### Task 12 — Harden `latency_audit.py` to fail-closed when profile missing under strict

**Goal.** Currently logs and returns advisory; under `vm_ul6_strict`, missing `place_ns/cancel_ns` profile becomes a hard FAIL.

**Files.** Modify `src/hft_platform/alpha/latency_audit.py`. Modify `src/hft_platform/alpha/promotion.py:_evaluate_gate_d` to pass `strict=True` when profile is `vm_ul6_strict`. New `tests/unit/alpha/test_latency_audit_strict_failclosed.py`.

**Diff sketch.** `latency_audit(result, profile, strict=False)`:
```python
def latency_audit(
    result: BacktestResult,
    profile: dict,
    *,
    strict: bool = False,
) -> dict:
    place_p95 = profile.get("place_p95_ns")
    cancel_p95 = profile.get("cancel_p95_ns")
    if place_p95 is None or cancel_p95 is None:
        if strict:
            return {"passed": False, "reason": "place_ns/cancel_ns missing under strict profile"}
        return {"passed": True, "reason": "advisory: profile missing"}
    ...
```

**RED tests.**
1. Profile missing + strict=True → `passed: False`, reason cites field.
2. Profile missing + strict=False → `passed: True`, advisory string in reason.
3. Profile present (place_p95 ≤ budget) → `passed: True`.
4. Profile present (place_p95 > budget) → `passed: False`.
5. Asymmetric latency regression: `v2026-04-24_measured` profile (place P95 395ms, cancel P95 59ms — `place_ns > 6× cancel_ns`) is the canonical Shioaji broker profile; assert that this profile is the one the test fixture cites by ID. The asymmetry itself is informational, not an automatic gate fail; only the explicit P95 budget check applies.

**Verification.** `uv run pytest tests/unit/alpha/test_latency_audit_strict_failclosed.py -q` → 5 PASS. Existing `latency_audit` tests stay green.

**Commit.** `fix(alpha): latency_audit fails closed under strict when profile missing (Slice B task 12)`.

### Task 13 — `MakerStrategyBridge.on_session_end()` FORCE_FLAT

**Goal.** Live-side residual close-out at session end.

**Files.** Modify `src/hft_platform/backtest/maker_bridge.py`. New `tests/unit/backtest/test_maker_bridge_on_session_end.py`. New `tests/integration/test_session_end_force_flat.py`.

**API.**
```python
class MakerStrategyBridge(BaseStrategy):
    def on_session_end(self, ctx: StrategyContext) -> list[OrderIntent]:
        residual_qty = ctx.position.net_qty
        if residual_qty == 0:
            return []
        opposite_side = "sell" if residual_qty > 0 else "buy"
        cur_mid = ctx.lob.mid_price()  # scaled int x10000
        return [
            OrderIntent(
                side=opposite_side,
                qty=abs(residual_qty),
                price=cur_mid,
                kind="MARKET",
                reason="session_end_force_flat",
                strategy_id=self.strategy_id,
            )
        ]
```

The supervisor wiring lives in `services/bootstrap.py` — Task 13 follows the existing session-end signal chain (already used for halt-cleanup) and adds a `bridge.on_session_end(ctx)` call site, draining returned intents through the standard risk pipeline.

**RED tests.**
1. Residual = 0 → returns `[]`.
2. Residual = +1 → returns one MARKET sell intent.
3. Residual = -2 → returns one MARKET buy intent qty=2.
4. Integration: drive a 1-day session with intentional unfilled residual, verify `position.net_qty == 0` after `on_session_end()` is called and intent flushed through risk gateway.

**Verification.** `uv run pytest tests/unit/backtest/test_maker_bridge_on_session_end.py tests/integration/test_session_end_force_flat.py -q` → all green.

**Commit.** `feat(maker): on_session_end FORCE_FLAT for residual (Slice B task 13)`.

### Task 14 — DoD-B5 lock-in: replay-parity regression check on modified MakerEngine

**Goal.** Re-run Slice C's `replay_parity_gate` end-to-end on the new MakerEngine. Assert no parity regression — the cancel-path short-circuit fixture from Slice C still kills, and the clean echo still passes.

**Files.** New `tests/integration/test_replay_parity_post_slice_b.py`.

**Test cases.**
1. R47 synthetic divergence fixture (94/100 with 6 CANCEL omissions at idx 13/25/37/49/61/73 — Slice C fixture path) under MtM-aware MakerEngine → `replay_parity_gate` FAILS at `match_pct ≈ 94%`.
2. Clean 1-tick echo fixture under MtM-aware MakerEngine → `replay_parity_gate` PASSES at 100%.
3. Smoke check: the maker_realism gates (`inventory_mtm`, `cost_uncertainty`) and parity gate co-fire as expected on the R47 fixture.

**Verification.** `uv run pytest tests/integration/test_replay_parity_post_slice_b.py -q` → 3 PASS.

**Commit.** `test(slice-b): replay parity holds end-to-end on MtM-aware MakerEngine (Slice B task 14)`.

### Task 15 — DoD-B1 evidence: post-B baseline vs pre-B baseline

**Goal.** Run R47 backtest **on the same canonical 31-day TMFD6 fixture and same `v2026-04-24_measured` profile** under the new MakerEngine + new q_hat table; persist post-B artifact at `tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_post_b.json`. Assert: `post_b.pnl_ntd ≤ cost_floor × 39`.

**Files.** New `tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_post_b.json`. New `tests/integration/test_inventory_mtm_e2e.py` (DoD-B2 + DoD-B1 in one test file).

**DoD-B1 assertion.**
```python
def test_dod_b1_post_b_pnl_below_cost_floor():
    pre_b = json.loads(Path("tests/fixtures/.../r47_tmfd6_31d_pre_b.json").read_text())
    post_b = json.loads(Path("tests/fixtures/.../r47_tmfd6_31d_post_b.json").read_text())
    cost_floor_per_fill_ntd = COST_FLOOR_PER_FILL_NTD  # cite credibility-audit doc
    n_fills = post_b["fills"]
    assert post_b["pnl_ntd"] <= cost_floor_per_fill_ntd * n_fills, (
        f"post-B PnL {post_b['pnl_ntd']} exceeds cost floor "
        f"{cost_floor_per_fill_ntd} × {n_fills} = {cost_floor_per_fill_ntd * n_fills}"
    )
    assert pre_b["fills"] == post_b["fills"], "fill count must be invariant under MtM"
    # Document the magnitude shift for memory:
    print(f"R47 TMFD6 31d: pre_b={pre_b['pnl_ntd']:+.0f}, post_b={post_b['pnl_ntd']:+.0f}")
```

**DoD-B2 assertion.** Same test file:
```python
def test_dod_b2_inventory_mtm_gate_fires_on_r47_passes_on_robust():
    r47_result = build_result_from_artifact("r47_tmfd6_31d_post_b.json")
    robust_result = build_result_from_artifact("robust_alpha_synthetic.json")
    strict_thresholds = load_yaml("config/research/profiles/vm_ul6_strict.yaml")["strategy_types"]["maker"]
    r47_verdict = _invoke_sub_gates("maker", r47_result, strict_thresholds, profile="vm_ul6_strict")
    robust_verdict = _invoke_sub_gates("maker", robust_result, strict_thresholds, profile="vm_ul6_strict")
    assert r47_verdict["inventory_mtm"].passed is False
    assert r47_verdict["cost_uncertainty"].passed is False
    assert robust_verdict["inventory_mtm"].passed is True
    assert robust_verdict["cost_uncertainty"].passed is True
```

**Verification.** `uv run pytest tests/integration/test_inventory_mtm_e2e.py -q` → 2 PASS.

**Commit.** `test(slice-b): DoD-B1 + DoD-B2 evidence on R47/robust fixtures (Slice B task 15)`.

### Task 16 — Loose-profile non-regression

**Goal.** Verify the new gates stay advisory under `make research` (loose / no profile) — exploratory runs unaffected.

**Files.** Append to `tests/integration/test_inventory_mtm_e2e.py` (or new `tests/integration/test_loose_profile_post_slice_b.py`).

**Test case.**
```python
def test_loose_profile_does_not_block_on_inventory_mtm_or_cost_uncertainty():
    r47_result = build_result_from_artifact("r47_tmfd6_31d_post_b.json")
    loose_thresholds = {}  # explicit absence of strict gates
    verdict = _invoke_sub_gates("maker", r47_result, loose_thresholds, profile=None)
    # Sub-gate results may be advisory FAIL, but the aggregator MUST NOT block:
    assert verdict.get("inventory_mtm") is None or verdict["inventory_mtm"].passed is True
    assert verdict.get("cost_uncertainty") is None or verdict["cost_uncertainty"].passed is True
```

**Verification.** Test passes under both pre-B and post-B engines.

**Commit.** `test(alpha): inventory_mtm + cost_uncertainty stay advisory under loose profile (Slice B task 16)`.

### Task 17 — Runbook + architecture map

**Goal.** Operator-facing documentation.

**Files.**
- New `docs/runbooks/maker-realism-gate.md` (mirror Slice C's runbook structure: Goal / Activation / Tuning / Mark-method choice / FORCE_FLAT semantics / Failure modes / Recovery / Slack alert template).
- Modify `docs/architecture/current-architecture.md` to append §7B "Slice B — Maker Realism" with a 7-row surface table (engine MtM hook, q_hat path, two gates, latency strict mode, on_session_end, env vars).
- Modify `docs/operations/env-vars-reference.md` (add `HFT_MAKER_MARK_METHOD`, `HFT_QUEUE_CALIBRATION_TABLE_PATH`).

**Verification.** `make env-vars-guard` → `pass`. `make ci` lint section green.

**Commit.** `docs(maker): runbook + arch map + env-vars for Slice B (Slice B task 17)`.

### Task 18 — `make ci` final + format/lint fixup

**Goal.** Mirror Slice C task 16 — final `ruff format` + `ruff check --fix` sweep on Slice B touched files; full `make ci` green.

**Files.** Whatever the formatter/linter touches. Coverage rerun under `make ci`.

**Verification.**
- `uv run ruff format --check .` → all formatted.
- `uv run ruff check .` → all checks passed.
- `uv run mypy src/` → 0 errors.
- `make ci` final sweep → 12,700+ tests pass with 87%+ coverage.

**Commit.** `style(slice-b): ruff format + import ordering fixup (Slice B task 18)`.

## 7. Self-review (apply before Codex submission and again before Task 1)

Use the same checklist Slice A and Slice C applied:

- **Spec coverage.** Every DoD-B1..B6 maps to a task in §6.
- **Anchor stability.** All file:line references checked on `main` at `5861730d` (this plan's pre-flight). Re-validate at the start of Task 1 — if any anchor shifted, update the plan before the subagent picks it up.
- **Type/name consistency.** `InventoryMtMGate` and `CostUncertaintyGate` follow Slice C's `ReplayParityGate` shape (frozen `name`, `applies_to`, `evaluate(result, config, thresholds) → SubGateResult`).
- **Placeholders.** None — every `...` in code sketches is intentional ellipsis where the implementation is straightforward and the subagent can fill in. No TODO without a `#NNN` ticket reference.
- **Out-of-scope drift.** No alpha factory MVP work (Slice D). No DSL design. No new latency profile (we depend on `v2026-04-24_measured` which already exists).
- **Float exception sanity.** All money math in `research/backtest/maker_engine.py` and `fill_models.py` uses float per `25-architecture-governance.md` §11. The new `MakerStrategyBridge.on_session_end()` returns `OrderIntent` with `price` as scaled int (live precision).
- **Risk-register entries (carried forward from blueprint §"Risk register"):**
  - Inventory MtM regression flips other backtests' historical numbers → **mitigated** by Task 1's pre-B baseline capture for R47/TMFD6/31d. Future regressions are diff-able.
  - Asymmetric latency hidden by P95-only wording → **mitigated** by Task 12's regression test that names `v2026-04-24_measured` by ID.
  - HFT-P004 trip on Slice B's research-side floats → **not applicable** per §11.
  - Slice C parity regression by Slice B → **mitigated** by Task 14 (DoD-B5).

## 8. Codex adversarial review (before Task 1)

Submit this plan to a Codex subagent with the prompt:

> Adversarial review of `docs/superpowers/plans/2026-05-05-slice-b-maker-realism.md` against `main` at `5861730d`. Verify every file:line anchor is correct; flag any DoD that is not measurable; flag any task whose RED test does not actually fail before its GREEN implementation; flag any out-of-scope creep relative to the master blueprint at `/home/charlie/.claude/plans/curried-launching-unicorn.md`. Verdict format: ACCEPT / ACCEPT-WITH-FIXES / REJECT. Cite file:line for every finding.

Resolve all findings before Task 1 dispatch. Append the Codex verdict and findings list to this plan as §9 (post-review delta).

## 9. Post-review delta (Codex adversarial review applied 2026-05-05)

**Verdict.** ACCEPT-WITH-FIXES.

**Review process.** Codex `task-mosdl3vv-ptq19b` (rescue dispatch, thread `019df749-3ad5-7c93-83e0-b8932c5da2b1`, started 2026-05-05T08:37:38Z) ran an adversarial sweep of the plan against `main@5861730d`. The Codex process surfaced two anchor defects in its preliminary pass and then exited mid-flight (process gone at 08:45 UTC, 80 jsonl entries, no final verdict written). Cross-cutting checks 1-7 were completed in-line on the foreground session against the same commit. All defects below are anchored to `git show 5861730d:<path>` reads.

### Findings

**[HIGH] §0 anchor row for `_evaluate_gate_d` is off by ~44 lines.**
- Plan claim: `src/hft_platform/alpha/promotion.py:327-345`, `replay_parity_audit at 339-345`.
- Code evidence on `5861730d`: `_evaluate_gate_d` defined at line **283**; `replay_parity_audit` block at lines **340-348** (driven by `min_match_pct = getattr(config, "min_replay_parity_match_pct", 95.0)` at line 339).
- Defect: subagents running Task 12 will land on the wrong code section first; mid-task drift risk.
- Recommended fix in §0 row: `_evaluate_gate_d (Slice C added replay_parity_audit at 340-348) | promotion.py | 283-373`.

**[HIGH] §0 anchor row for `PromotionConfig` overstates the range.**
- Plan claim: `promotion.py:39-91`.
- Code evidence on `5861730d`: `class PromotionConfig` starts at line **40**; `min_replay_parity_match_pct: float = 95.0` at line **64**.
- Recommended fix in §0 row: `PromotionConfig (Slice C added min_replay_parity_match_pct=95.0 at line 64) | promotion.py | 40-135`.

**[HIGH] Task 13 mis-cites session-end signal source.**
- Plan claim (lines 93, 556): "supervisor wiring lives in `services/bootstrap.py` ... existing session-end signal chain (already used for halt-cleanup)".
- Code evidence on `5861730d`: `services/bootstrap.py` only handles runtime shutdown (`shutdown()` at line 174, `teardown()` at line 526) and Redis session lease (`session_lease_*`) — there is **no** in-trading-hour session-end broadcast there. The actual `SessionPhase` state machine (`PRE_OPEN / OPEN / CLOSE_ONLY / FORCE_FLAT`) lives in `src/hft_platform/services/system.py` (referenced at lines 963-966) and the consumer-side phase guards live in `src/hft_platform/strategy/runner.py` (at lines 1605, 1607, 1623 for OPEN / CLOSE_ONLY / FORCE_FLAT branches; `IntentType.FORCE_FLAT` at runner.py:1597).
- Defect: Task 13 wiring step would land in the wrong file. The signal exists; the citation is wrong.
- Recommended fix in Task 13 + §5 file table: wiring point is the `SessionPhase` transition into `CLOSE_ONLY` (or `FORCE_FLAT`) inside `services/system.py` (the producer); `MakerStrategyBridge.on_session_end(ctx)` is invoked from the strategy-runner consumer side when `SessionPhase` transitions are observed (mirror existing FORCE_FLAT-phase handling at `runner.py:1623`). Returned intents flow through the standard risk pipeline.

**[MEDIUM] §0 row asserts `blocking_sub_gates` "currently lists 7 entries" — actually 14.**
- Plan claim (line 18): "currently lists 7 entries incl. `replay_parity`".
- Code evidence on `5861730d` (`config/research/profiles/vm_ul6_strict.yaml:49-66`): 14 entries — `sharpe_threshold, max_drawdown, winning_day_pct, fill_quality, fill_rate_validation, ic_evaluation, min_sample_size, single_day_dominance, loo_day_sensitivity, outlier_trade_removal, day_bootstrap_ci, stationary_block_bootstrap, deflated_sharpe_maker, replay_parity`.
- Defect: cosmetic; risk is a subagent miscounting "what is already there" when adding `inventory_mtm` and `cost_uncertainty` (Task 11) and over-removing existing entries.
- Recommended fix in §0 row: "currently lists 14 entries (Slice A 13 + Slice C `replay_parity`)".

**[MEDIUM] §0 row reverses the symbol order for `registry.py`.**
- Plan claim (line 19): "`class SubGate(Protocol)` + `SubGateResult(name, passed, metrics, details)` frozen | registry.py | 17, 36-50".
- Code evidence on `5861730d`: `SubGateResult(frozen)` is defined at lines **17-31**; `SubGate(Protocol)` at lines **36-50**. The line ranges are correct, but the symbol order in the description reads them backwards.
- Recommended fix in §0 row: "`SubGateResult(frozen, name/passed/metrics/details)` at 17 + `SubGate(Protocol)` at 36-50".

**[LOW] Task 12 should explicitly cite the existing `latency_profile is not None` check it is replacing.**
- Plan Task 12 (line 495+): hardens `latency_audit.py` to fail-closed when profile missing under strict.
- Code evidence on `5861730d`: `_evaluate_gate_d` already includes a `latency_profile` check at lines 320-333 with `pass: latency_profile is not None`. Task 12 hardening is layered on top — the new behavior is "under strict, also require `place_ns/cancel_ns` P95 fields populated, not just profile-name set".
- Recommended fix in Task 12 §: explicit "Existing behaviour" callout naming `promotion.py:320-333`, then "Strict-mode delta" naming the new fail-closed conditions (P95 fields populated, profile-id matches `v2026-04-24_measured` family).

### Cross-cutting checks not flagged

- **Anchor rows verified clean**: `class MakerEngine` at 219, `_run_day` at 364→502, `_compute_fifo_pnl` at 505, `QueueDepletionFill` at 42-77, `BacktestResult` at 43-80, `MakerStrategyBridge` at 41, `vm_ul6_strict.yaml` line 49-66 for `blocking_sub_gates`, `latency_profiles.yaml` `v2026-04-24_measured` at 71+. No shift.
- **DoD measurability** (B1-B6): all DoDs cite concrete fixtures, commands, or numeric thresholds. DoD-B1 names `+2,398 NTD / 39 fills` baseline and the `v2026-04-24_measured` profile; DoD-B5 names the existing 94/100 R47 synthetic divergence fixture.
- **OrderIntent shape for `on_session_end()` MARKET force-flat**: `IntentType.FORCE_FLAT` already exists in `strategy/runner.py:1597`, and `runner.py:858` confirms the price-scaling contract. No speculative new contract surface.
- **Risk path acceptance of FORCE_FLAT in CLOSE_ONLY phase**: covered by `runner.py:1587-1588` ("During CLOSE_ONLY: allow CANCEL, FORCE_FLAT, and position-reducing IOC orders"). No risk-engine reject.
- **Out-of-scope creep**: no DSL, kill ledger, correlation clustering, new latency profile, recorder topic, or replay infrastructure changes in any of Tasks 1-18. Scope holds.
- **q_hat depth-bucket cutoff (depth<5 = "shallow")**: Task 5 documents it as a CONFIGURABLE constant; the n<30 cell-drop rule is justified inline as a minimum-power threshold. Acceptable.

### Top 3 residual risks if shipped without further review

1. **Task 13 wiring still requires manual cross-check**: even with the corrected `services/system.py` SessionPhase signal source, the integration test must drive the actual `SessionPhase.CLOSE_ONLY → FORCE_FLAT` transition end-to-end (not just call `on_session_end()` directly). Test Task 13 lands one cycle of integration before declaring it done.
2. **q_hat fixtures may require larger CK windows**: ±15% backtest-vs-replay tolerance assumes ≥5 trading days per (symbol, hour, depth_bucket) cell with n≥30; Task 7 should report the actual cell occupancy and flag any cell that falls back to `0.5` literal so the calibration coverage is auditable.
3. **`min_replay_parity_match_pct` default of 95.0 was set in Slice C and never re-verified post-merge**: if Task 14 regression test on the 94/100 R47 fixture passes but the headroom is razor-thin (e.g. 95.0% exactly), a single intent-stream nondeterminism could flip future runs to FAIL. Task 14 should report the actual match_pct and flag if it is within 1pp of the 95% threshold.

### Recommended follow-up edits (separate commit)

The §0 anchor table rows, §5 modified-files row for `maker_bridge.py`, and §6 Task 12 / Task 13 wording should be patched against the recommended fixes above before Task 1 dispatch. They are not yet applied to the plan body — the §9 record above is authoritative for the subagent that will execute Slice B (it should reconcile §0/§5/§6 wording against §9 "Recommended fix" lines on first read).

## 10. Execution handoff

Subagent-driven development pattern (`superpowers:subagent-driven-development`):

```
for task_n in 1..18:
    spawn fresh subagent with:
        - this plan file
        - pointer to Slice C memory (`/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/slice_c_replay_parity_gate.md`)
        - the specific Task N section as the "your task" payload
    subagent runs RED → GREEN → uv run pytest <touched-paths> -q --no-cov → git commit
    review subagent run (pass / regress / drift) before dispatching task_{n+1}
end
final make ci → open PR → tag slice-b-merged-YYYY-MM-DD → memory update
```

PR body: copy Slice C PR body (`#339`) structure exactly — `## Summary`, `## Goal`, `## Why`, `## What changed`, `## AI Participation`, `## Test Plan`, `## HFT Design Review` (allocation/latency/threading/data-layout/failure-mode), `## Out of scope`. The `## Summary` section is **mandatory** for the PR governance check (`pr-review-checklist.yml:Check PR template governance sections`); Slice C's PR initially failed this check until I added it.

---

**End of Slice B per-slice plan.** Ready for Codex adversarial review.
