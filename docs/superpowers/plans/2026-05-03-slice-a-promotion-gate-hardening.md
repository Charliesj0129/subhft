# Slice A — Promotion Gate Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the alpha promotion path (Gate D) refuse R47-style small-sample / single-day-dominated alphas by introducing a strict `vm_ul6_strict` validation profile that turns selected sub-gates from advisory to blocking and adds seven new small-sample-rejection sub-gates.

**Architecture:** Profile-driven blocking. Sub-gates remain pure `(result, config, thresholds) → SubGateResult` callables. A new `ValidationProfile` loaded from `config/research/profiles/vm_ul6_strict.yaml` carries threshold overrides plus a `blocking_sub_gates: [...]` list. The Gate C aggregator (in `_gate_c.py`) consults the profile and ANDs the named gates' results into the existing `passed` boolean. `ValidationConfig` defaults stay loose so exploratory `make research` runs are unaffected; `promote_alpha()` rejects any artifact that did not run under a strict profile.

**Tech Stack:** Python 3.12, `pytest`, `numpy`, `PyYAML`, existing `_sub_gates` registry pattern, existing `_stat_tests._bh_correction()`, existing `_param_opt` deflated-Sharpe formula.

---

## Spec reference

Source spec (plan-mode): `/home/charlie/.claude/plans/charliesj0129-subhft-trade-project-read-crispy-karp.md`. Three locked decisions:

1. Loose `ValidationConfig` defaults preserved; ship strict profile and require it for promotion.
2. Profile-driven blocking list (sub-gates stay pure, profile YAML carries `blocking_sub_gates`).
3. γ-bundle of new sub-gates: `MinSampleSizeGate`, `SingleDayDominanceGate`, `LOODaySensitivityGate`, `OutlierTradeRemovalGate`, `DayLevelBootstrapCIGate`, `StationaryBlockBootstrapGate`, `DeflatedSharpeForMakerGate`.

Existing convention to follow (from `src/hft_platform/alpha/_sub_gates/common.py`, `maker.py`, `taker.py`):

- Each gate is a class with `name: str` (snake_case), `applies_to: set[str]`, `evaluate(result, config, thresholds) → SubGateResult`.
- Gates auto-register via `ensure_builtin_sub_gates_registered()` in `_sub_gates/__init__.py`.
- `result` is a `BacktestResult` (built in `_invoke_sub_gates_advisory`) with `daily_pnl`, `pnl_per_fill`, `adverse_fill_pct`, `n_fills`, `n_trading_days`, `equity_curve`.
- Helpers `_daily_pnl_points()` and `_daily_pnl_sequence()` already handle the dict-or-float entry shape.

## Pre-flight

Codex adversarial review (run on the current working tree) flagged two **pre-existing** issues that are unrelated to Slice A but will block Slice A's verification step (`make ci`):

- HIGH: `src/hft_platform/alpha/factor_compiler.py:139-147` leaks rolling state across `FormulaContext` boundaries (separate Slice; not in scope here).
- MEDIUM: `tests/unit/test_v2_types_smoke.py:193-207` fails because `gate_policy.yaml` now hard-requires `risk_review.slippage_in_edge_calculation`, which the test fixture omits (separate fix; not in scope here).

**Pre-flight task before starting Task 1:** stash or fix the unrelated working-tree changes so `make ci` is green on `main` before Slice A work begins.

```bash
cd /home/charlie/hft_platform
git status --short --untracked-files=all
# Either:
#   (a) commit / stash the untracked factor_compiler / validation_v2 / etc. changes on a different branch, or
#   (b) verify with the user that those pre-existing fixes are out of scope for Slice A and address them in a separate plan first.
git switch -c slice-a-promotion-gate-hardening   # work branch
make ci                                          # must be green before Task 1
```

If `make ci` is not green for reasons unrelated to Slice A, **stop and surface to the user** rather than starting Task 1.

## File structure

### New files (all under `/home/charlie/hft_platform`)

| Path | Responsibility |
|---|---|
| `config/research/profiles/vm_ul6_strict.yaml` | Strict promotion profile: thresholds + `blocking_sub_gates` list |
| `src/hft_platform/alpha/_validation_profile.py` | `ValidationProfile` dataclass + `load_profile(path)` loader + `ProfileValidationError` |
| `src/hft_platform/alpha/_resampling.py` | Pure resampling primitives: `leave_one_day_out`, `drop_top_trades`, `day_bootstrap`, `stationary_block_bootstrap` |
| `src/hft_platform/alpha/_sub_gates/min_sample_size.py` | `MinSampleSizeGate` |
| `src/hft_platform/alpha/_sub_gates/single_day_dominance.py` | `SingleDayDominanceGate` |
| `src/hft_platform/alpha/_sub_gates/loo_day_sensitivity.py` | `LOODaySensitivityGate` |
| `src/hft_platform/alpha/_sub_gates/outlier_trade_removal.py` | `OutlierTradeRemovalGate` |
| `src/hft_platform/alpha/_sub_gates/day_bootstrap_ci.py` | `DayLevelBootstrapCIGate` |
| `src/hft_platform/alpha/_sub_gates/stationary_block_bootstrap.py` | `StationaryBlockBootstrapGate` |
| `src/hft_platform/alpha/_sub_gates/deflated_sharpe_maker.py` | `DeflatedSharpeForMakerGate` |
| `tests/unit/alpha/test_resampling.py` | Resampling primitive tests |
| `tests/unit/alpha/test_validation_profile.py` | Profile loader tests |
| `tests/unit/alpha/test_sub_gate_min_sample_size.py` | per-gate tests |
| `tests/unit/alpha/test_sub_gate_single_day_dominance.py` | per-gate tests |
| `tests/unit/alpha/test_sub_gate_loo_day_sensitivity.py` | per-gate tests |
| `tests/unit/alpha/test_sub_gate_outlier_trade_removal.py` | per-gate tests |
| `tests/unit/alpha/test_sub_gate_day_bootstrap_ci.py` | per-gate tests |
| `tests/unit/alpha/test_sub_gate_stationary_block_bootstrap.py` | per-gate tests |
| `tests/unit/alpha/test_sub_gate_deflated_sharpe_maker.py` | per-gate tests |
| `tests/integration/test_strict_profile_e2e.py` | R47-kill + robust-pass + loose-parity tests |

### Modified files

| Path | Change |
|---|---|
| `src/hft_platform/alpha/_validation_types.py` | Add optional `profile: Any \| None = None` field to `ValidationConfig` |
| `src/hft_platform/alpha/_sub_gates/__init__.py` | Register the 7 new gates in `ensure_builtin_sub_gates_registered()` |
| `src/hft_platform/alpha/_gate_c.py` | Replace `_invoke_sub_gates_advisory()` with `_invoke_sub_gates()` returning both advisory list + blocking aggregate; modify maker (line 232) and taker (lines 515-528) aggregators to AND blocking aggregate into `passed` |
| `src/hft_platform/alpha/promotion.py` | Add strict-profile enforcement in `promote_alpha()` (around line 126) |
| `tests/unit/test_alpha_promotion.py` | Add `test_promotion_requires_strict_profile` |

---

## Task 1: Resampling primitives — `_resampling.py`

Pure utilities used by 4 of the 7 new sub-gates. Built first because everything downstream depends on it.

**Files:**
- Create: `src/hft_platform/alpha/_resampling.py`
- Test: `tests/unit/alpha/test_resampling.py`

- [ ] **Step 1.1: Write the failing tests**

```python
# tests/unit/alpha/test_resampling.py
"""Tests for hft_platform.alpha._resampling primitives."""
from __future__ import annotations

import math

import numpy as np
import pytest

from hft_platform.alpha._resampling import (
    day_bootstrap,
    drop_top_trades,
    leave_one_day_out,
    stationary_block_bootstrap,
)


class TestLeaveOneDayOut:
    def test_drops_each_day_once(self) -> None:
        daily = [1.0, 2.0, 3.0]
        out = list(leave_one_day_out(daily))
        assert len(out) == 3
        assert sorted(sum(s) for s in out) == [3.0, 4.0, 5.0]

    def test_empty_input_returns_empty(self) -> None:
        assert list(leave_one_day_out([])) == []

    def test_single_day_yields_one_empty_slice(self) -> None:
        out = list(leave_one_day_out([5.0]))
        assert len(out) == 1 and list(out[0]) == []


class TestDropTopTrades:
    def test_drops_top_pct_by_magnitude(self) -> None:
        trades = [10.0, -50.0, 1.0, 2.0, 3.0]  # |.| sorted desc => -50, 10, 3, 2, 1
        kept = drop_top_trades(trades, pct=0.4)  # drop top 2
        assert sorted(kept) == [1.0, 2.0, 3.0]

    def test_zero_pct_keeps_all(self) -> None:
        assert drop_top_trades([1.0, 2.0, 3.0], pct=0.0) == [1.0, 2.0, 3.0]

    def test_pct_clamped_below_one(self) -> None:
        with pytest.raises(ValueError, match="pct must be in"):
            drop_top_trades([1.0], pct=1.5)


class TestDayBootstrap:
    def test_returns_n_resamples_of_correct_length(self) -> None:
        daily = [1.0, 2.0, 3.0, 4.0]
        samples = day_bootstrap(daily, n_resamples=100, rng_seed=42)
        assert samples.shape == (100, 4)

    def test_ci_lower_above_threshold_for_clearly_positive(self) -> None:
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=10.0, scale=1.0, size=100)).tolist()
        samples = day_bootstrap(daily, n_resamples=2000, rng_seed=42)
        means = samples.mean(axis=1)
        ci_low = float(np.quantile(means, 0.05))
        assert ci_low > 5.0

    def test_raises_when_too_few_days(self) -> None:
        with pytest.raises(ValueError, match="insufficient"):
            day_bootstrap([1.0], n_resamples=10, rng_seed=42)


class TestStationaryBlockBootstrap:
    def test_returns_n_resamples_of_correct_length(self) -> None:
        daily = [float(i) for i in range(50)]
        samples = stationary_block_bootstrap(
            daily, block_size=5, n_resamples=200, rng_seed=42
        )
        assert samples.shape == (200, 50)

    def test_preserves_first_moment_in_expectation(self) -> None:
        daily = [1.0] * 100
        samples = stationary_block_bootstrap(
            daily, block_size=5, n_resamples=500, rng_seed=42
        )
        assert math.isclose(samples.mean(), 1.0, abs_tol=1e-6)

    def test_raises_when_block_size_too_small(self) -> None:
        with pytest.raises(ValueError, match="block_size"):
            stationary_block_bootstrap(
                [1.0, 2.0], block_size=0, n_resamples=10, rng_seed=42
            )
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_resampling.py -v --no-cov --tb=short
```
Expected: FAIL — `ModuleNotFoundError: hft_platform.alpha._resampling`.

- [ ] **Step 1.3: Create `__init__.py` for `tests/unit/alpha/`**

```bash
test -f tests/unit/alpha/__init__.py || touch tests/unit/alpha/__init__.py
```

- [ ] **Step 1.4: Implement the resampling primitives**

```python
# src/hft_platform/alpha/_resampling.py
"""Resampling primitives for small-sample sub-gates.

Pure functions over `daily_pnl: list[float]` or `trades: list[float]`.
No I/O, no logging, no global state. Used by:
- LOODaySensitivityGate    -> leave_one_day_out
- OutlierTradeRemovalGate  -> drop_top_trades
- DayLevelBootstrapCIGate  -> day_bootstrap
- StationaryBlockBootstrapGate -> stationary_block_bootstrap
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Sequence

import numpy as np


def leave_one_day_out(daily_pnl: Sequence[float]) -> Iterator[list[float]]:
    """Yield N slices of daily PnL with the i-th day removed.

    Order of yielded slices matches the order of the input.
    Returns immediately if the input is empty.
    """
    n = len(daily_pnl)
    if n == 0:
        return iter([])
    return ([daily_pnl[j] for j in range(n) if j != i] for i in range(n))


def drop_top_trades(trades: Sequence[float], *, pct: float) -> list[float]:
    """Return trades with the top ``pct`` fraction (by |value|) removed.

    Args:
        trades: per-trade signed PnL.
        pct: fraction in [0, 1) to drop. ``pct=0.05`` drops the top 5%.

    Raises:
        ValueError: if pct is not in [0, 1).
    """
    if not 0.0 <= pct < 1.0:
        raise ValueError(f"pct must be in [0, 1), got {pct}")
    if not trades:
        return []
    n_drop = int(len(trades) * pct)
    if n_drop == 0:
        return list(trades)
    order = sorted(range(len(trades)), key=lambda i: abs(trades[i]), reverse=True)
    drop_idx = set(order[:n_drop])
    return [t for i, t in enumerate(trades) if i not in drop_idx]


def day_bootstrap(
    daily_pnl: Sequence[float],
    *,
    n_resamples: int,
    rng_seed: int,
) -> np.ndarray:
    """Day-level non-overlapping bootstrap.

    Returns an array of shape ``(n_resamples, len(daily_pnl))`` where each row
    is a sample-with-replacement of the input days.

    Raises:
        ValueError: if fewer than 2 days are provided.
    """
    n = len(daily_pnl)
    if n < 2:
        raise ValueError(f"insufficient days for bootstrap: n={n}, need >= 2")
    rng = np.random.default_rng(rng_seed)
    arr = np.asarray(daily_pnl, dtype=float)
    idx = rng.integers(low=0, high=n, size=(n_resamples, n))
    return arr[idx]


def stationary_block_bootstrap(
    daily_pnl: Sequence[float],
    *,
    block_size: int,
    n_resamples: int,
    rng_seed: int,
) -> np.ndarray:
    """Politis-Romano stationary block bootstrap.

    Geometric block lengths with mean ``block_size``; concatenates blocks
    sampled with replacement from the original series until the desired
    sample length is reached. Returns ``(n_resamples, len(daily_pnl))``.

    Raises:
        ValueError: if block_size <= 0 or input shorter than block_size.
    """
    n = len(daily_pnl)
    if block_size <= 0:
        raise ValueError(f"block_size must be > 0, got {block_size}")
    if n < block_size:
        raise ValueError(f"input length {n} < block_size {block_size}")

    rng = np.random.default_rng(rng_seed)
    arr = np.asarray(daily_pnl, dtype=float)
    p = 1.0 / float(block_size)

    samples = np.empty((n_resamples, n), dtype=float)
    for r in range(n_resamples):
        out = np.empty(n, dtype=float)
        i = 0
        while i < n:
            start = int(rng.integers(low=0, high=n))
            length = int(rng.geometric(p=p))
            for j in range(length):
                if i >= n:
                    break
                out[i] = arr[(start + j) % n]
                i += 1
        samples[r] = out
    return samples
```

- [ ] **Step 1.5: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_resampling.py -v --no-cov --tb=short
```
Expected: PASS — 9 tests green.

- [ ] **Step 1.6: Commit**

```bash
git add src/hft_platform/alpha/_resampling.py tests/unit/alpha/__init__.py tests/unit/alpha/test_resampling.py
git commit -m "feat(alpha): add resampling primitives for small-sample sub-gates"
```

---

## Task 2: `ValidationProfile` loader — `_validation_profile.py`

A frozen dataclass + `load_profile()` + structural validation against the live registry.

**Files:**
- Create: `src/hft_platform/alpha/_validation_profile.py`
- Test: `tests/unit/alpha/test_validation_profile.py`

- [ ] **Step 2.1: Write the failing tests**

```python
# tests/unit/alpha/test_validation_profile.py
"""Tests for hft_platform.alpha._validation_profile."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hft_platform.alpha._validation_profile import (
    ProfileValidationError,
    ValidationProfile,
    load_profile,
)


_VALID_BODY = {
    "name": "test_strict",
    "is_strict": True,
    "thresholds": {
        "maker": {"sharpe_oos_min": 1.0, "min_fills": 300},
        "taker": {"sharpe_oos_min": 1.5},
    },
    "blocking_sub_gates": [
        "sharpe_threshold",
        "max_drawdown",
        "winning_day_pct",
    ],
}


def _write_yaml(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


class TestLoadProfile:
    def test_loads_valid_profile(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, _VALID_BODY)
        prof = load_profile(p)
        assert isinstance(prof, ValidationProfile)
        assert prof.name == "test_strict"
        assert prof.is_strict is True
        assert prof.thresholds["maker"]["sharpe_oos_min"] == 1.0
        assert "sharpe_threshold" in prof.blocking_sub_gates

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_profile(tmp_path / "nope.yaml")

    def test_unregistered_gate_raises(self, tmp_path: Path) -> None:
        bad = dict(_VALID_BODY)
        bad["blocking_sub_gates"] = ["sharpe_threshold", "totally_made_up_gate"]
        p = _write_yaml(tmp_path, bad)
        with pytest.raises(ProfileValidationError, match="totally_made_up_gate"):
            load_profile(p)

    def test_strict_without_blocking_gates_raises(self, tmp_path: Path) -> None:
        bad = dict(_VALID_BODY)
        bad["blocking_sub_gates"] = []
        p = _write_yaml(tmp_path, bad)
        with pytest.raises(ProfileValidationError, match="strict profile must list"):
            load_profile(p)

    def test_non_strict_with_empty_blocking_is_ok(self, tmp_path: Path) -> None:
        body = dict(_VALID_BODY)
        body["is_strict"] = False
        body["blocking_sub_gates"] = []
        p = _write_yaml(tmp_path, body)
        prof = load_profile(p)
        assert prof.is_strict is False

    def test_thresholds_for_returns_per_gate_view(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, _VALID_BODY)
        prof = load_profile(p)
        merged = prof.thresholds_for(strategy_type="maker")
        assert merged["sharpe_oos_min"] == 1.0
        assert merged["min_fills"] == 300
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_validation_profile.py -v --no-cov --tb=short
```
Expected: FAIL — `ModuleNotFoundError: hft_platform.alpha._validation_profile`.

- [ ] **Step 2.3: Implement the profile module**

```python
# src/hft_platform/alpha/_validation_profile.py
"""Profile-driven blocking for Gate C sub-gates.

A `ValidationProfile` carries threshold overrides plus a list of sub-gate
names that must pass for Gate C to mark a run as ``passed``. Loose runs
(profile=None) preserve the existing advisory-only behavior bit-for-bit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger("alpha.validation_profile")


class ProfileValidationError(ValueError):
    """Raised when a profile is structurally invalid (e.g. references an unregistered gate)."""


@dataclass(frozen=True)
class ValidationProfile:
    """Promotion-eligibility profile."""

    name: str
    is_strict: bool
    thresholds: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocking_sub_gates: tuple[str, ...] = ()

    def thresholds_for(self, *, strategy_type: str) -> dict[str, Any]:
        """Return thresholds for the given strategy type (maker|taker)."""
        return dict(self.thresholds.get(strategy_type, {}))


def load_profile(path: str | Path) -> ValidationProfile:
    """Load and validate a profile YAML file.

    Validation:
        - Every name in `blocking_sub_gates` must be present in the live
          sub-gate registry.
        - A profile with `is_strict: true` must list at least one blocking
          sub-gate.

    Raises:
        FileNotFoundError: if `path` does not exist.
        ProfileValidationError: on any structural validation failure.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"profile not found: {p}")

    body = yaml.safe_load(p.read_text()) or {}
    name = str(body.get("name", p.stem))
    is_strict = bool(body.get("is_strict", False))
    thresholds = dict(body.get("thresholds") or {})
    blocking = tuple(body.get("blocking_sub_gates") or ())

    from hft_platform.alpha._sub_gates import (
        ensure_builtin_sub_gates_registered,
        get_registered_sub_gates,
    )

    ensure_builtin_sub_gates_registered()
    known_names = {g.name for g in get_registered_sub_gates()}
    unknown = [n for n in blocking if n not in known_names]
    if unknown:
        raise ProfileValidationError(
            f"profile {name!r}: blocking_sub_gates references unregistered gate(s): {unknown}"
        )

    if is_strict and not blocking:
        raise ProfileValidationError(
            f"profile {name!r}: strict profile must list at least one blocking_sub_gate"
        )

    logger.info(
        "validation_profile_loaded",
        name=name,
        is_strict=is_strict,
        blocking_gate_count=len(blocking),
    )
    return ValidationProfile(
        name=name,
        is_strict=is_strict,
        thresholds=thresholds,
        blocking_sub_gates=blocking,
    )
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_validation_profile.py -v --no-cov --tb=short
```
Expected: PASS — 6 tests green.

- [ ] **Step 2.5: Commit**

```bash
git add src/hft_platform/alpha/_validation_profile.py tests/unit/alpha/test_validation_profile.py
git commit -m "feat(alpha): add ValidationProfile loader with registry-aware validation"
```

---

## Task 3: Extend `ValidationConfig` with optional `profile` field

**Files:**
- Modify: `src/hft_platform/alpha/_validation_types.py:7-71`
- Test: `tests/unit/alpha/test_validation_profile.py` (extend)

- [ ] **Step 3.1: Append the failing test to the existing file**

```python
# Append to tests/unit/alpha/test_validation_profile.py
class TestValidationConfigProfileField:
    def test_default_is_none(self) -> None:
        from hft_platform.alpha._validation_types import ValidationConfig

        cfg = ValidationConfig(alpha_id="x", data_paths=[])
        assert cfg.profile is None

    def test_accepts_profile_object(self, tmp_path: Path) -> None:
        from hft_platform.alpha._validation_types import ValidationConfig

        p = _write_yaml(tmp_path, _VALID_BODY)
        prof = load_profile(p)
        cfg = ValidationConfig(alpha_id="x", data_paths=[], profile=prof)
        assert cfg.profile is prof
```

- [ ] **Step 3.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_validation_profile.py::TestValidationConfigProfileField -v --no-cov --tb=short
```
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'profile'`.

- [ ] **Step 3.3: Add the field to `ValidationConfig`**

In `src/hft_platform/alpha/_validation_types.py`, after the existing field `gate_c_tier` (line 70), append:

```python
    profile: Any | None = None
```

(`Any` is already imported at line 4. No other changes.)

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_validation_profile.py -v --no-cov --tb=short
```
Expected: PASS — all 8 tests green.

- [ ] **Step 3.5: Commit**

```bash
git add src/hft_platform/alpha/_validation_types.py tests/unit/alpha/test_validation_profile.py
git commit -m "feat(alpha): add optional profile field to ValidationConfig"
```

---

## Task 4: `MinSampleSizeGate`

**Files:**
- Create: `src/hft_platform/alpha/_sub_gates/min_sample_size.py`
- Test: `tests/unit/alpha/test_sub_gate_min_sample_size.py`

- [ ] **Step 4.1: Write the failing tests**

```python
# tests/unit/alpha/test_sub_gate_min_sample_size.py
"""Tests for MinSampleSizeGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.min_sample_size import MinSampleSizeGate


@dataclass
class _FakeResult:
    n_fills: int = 0
    n_trading_days: int = 0
    daily_pnl: list[Any] = field(default_factory=list)


class TestMinSampleSizeGate:
    def test_passes_when_both_above_threshold(self) -> None:
        gate = MinSampleSizeGate()
        r = _FakeResult(n_fills=400, n_trading_days=70)
        out = gate.evaluate(r, config=None, thresholds={"min_fills": 300, "min_days": 60})
        assert out.passed is True
        assert out.metrics["n_fills"] == 400.0

    def test_fails_when_fills_below(self) -> None:
        gate = MinSampleSizeGate()
        r = _FakeResult(n_fills=39, n_trading_days=70)
        out = gate.evaluate(r, config=None, thresholds={"min_fills": 300, "min_days": 60})
        assert out.passed is False
        assert "39" in out.details

    def test_fails_when_days_below(self) -> None:
        gate = MinSampleSizeGate()
        r = _FakeResult(n_fills=400, n_trading_days=31)
        out = gate.evaluate(r, config=None, thresholds={"min_fills": 300, "min_days": 60})
        assert out.passed is False
        assert "31" in out.details

    def test_uses_defaults_when_thresholds_absent(self) -> None:
        gate = MinSampleSizeGate()
        r = _FakeResult(n_fills=10, n_trading_days=2)
        out = gate.evaluate(r, config=None, thresholds={})
        assert out.passed is True

    def test_applies_to_includes_maker_and_taker(self) -> None:
        gate = MinSampleSizeGate()
        assert "maker" in gate.applies_to
        assert "taker" in gate.applies_to
```

- [ ] **Step 4.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_min_sample_size.py -v --no-cov --tb=short
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 4.3: Implement `MinSampleSizeGate`**

```python
# src/hft_platform/alpha/_sub_gates/min_sample_size.py
"""Minimum-sample-size sub-gate (fills + trading days)."""
from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


class MinSampleSizeGate:
    """Reject runs with too few fills or too few trading days.

    Targets the R47-OE1 fingerprint: 39 fills over 31 days.
    """

    name = "min_sample_size"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        n_fills = int(getattr(result, "n_fills", 0) or 0)
        n_days = int(getattr(result, "n_trading_days", 0) or 0)
        min_fills = int(thresholds.get("min_fills", 0))
        min_days = int(thresholds.get("min_days", 0))

        passed = n_fills >= min_fills and n_days >= min_days
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "n_fills": float(n_fills),
                "n_days": float(n_days),
                "min_fills": float(min_fills),
                "min_days": float(min_days),
            },
            details=(
                f"fills={n_fills} (min {min_fills}), days={n_days} (min {min_days})"
            ),
        )
```

- [ ] **Step 4.4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_min_sample_size.py -v --no-cov --tb=short
```
Expected: PASS — 5 tests green.

- [ ] **Step 4.5: Commit**

```bash
git add src/hft_platform/alpha/_sub_gates/min_sample_size.py tests/unit/alpha/test_sub_gate_min_sample_size.py
git commit -m "feat(alpha): add MinSampleSizeGate sub-gate"
```

---

## Task 5: `SingleDayDominanceGate`

Catches the R47-OE1 pathology (96.9% of PnL from one day).

**Files:**
- Create: `src/hft_platform/alpha/_sub_gates/single_day_dominance.py`
- Test: `tests/unit/alpha/test_sub_gate_single_day_dominance.py`

- [ ] **Step 5.1: Write the failing tests**

```python
# tests/unit/alpha/test_sub_gate_single_day_dominance.py
"""Tests for SingleDayDominanceGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.single_day_dominance import SingleDayDominanceGate


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestSingleDayDominanceGate:
    def test_passes_when_distribution_is_balanced(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[10.0] * 10)
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 25.0})
        assert out.passed is True

    def test_fails_when_one_day_dominates(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[100.0] + [1.0] * 30)
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 25.0})
        assert out.passed is False
        assert "top_day_contribution_pct" in out.metrics
        assert out.metrics["top_day_contribution_pct"] > 25.0

    def test_uses_signed_contribution_in_aggregate(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[100.0, -1.0, -1.0, -1.0])
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 50.0})
        assert out.passed is False  # 100 / (100+1+1+1)*100 ~= 97%

    def test_handles_negative_total_pnl(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[0.0, 0.0, 0.0])
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 25.0})
        assert out.passed is True
        assert "no measurable PnL" in out.details

    def test_dict_entries_are_supported(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[{"pnl_pts": 100.0}, {"pnl_pts": 1.0}])
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 50.0})
        assert out.passed is False  # 100/101 ~= 99%
```

- [ ] **Step 5.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_single_day_dominance.py -v --no-cov --tb=short
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 5.3: Implement `SingleDayDominanceGate`**

```python
# src/hft_platform/alpha/_sub_gates/single_day_dominance.py
"""Single-day-dominance sub-gate.

Rejects runs where the top-magnitude day contributes more than
``outlier_day_contribution_max_pct`` percent of the absolute total
daily PnL.
"""
from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class SingleDayDominanceGate:
    """Reject when one day's |PnL| / sum(|daily PnL|) exceeds threshold."""

    name = "single_day_dominance"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
        max_pct = float(thresholds.get("outlier_day_contribution_max_pct", 100.0))

        if not daily:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"top_day_contribution_pct": 0.0, "threshold_pct": max_pct},
                details="no daily pnl to evaluate",
            )

        abs_total = sum(abs(d) for d in daily)
        if abs_total <= 0.0:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={"top_day_contribution_pct": 0.0, "threshold_pct": max_pct},
                details="no measurable PnL — gate skipped",
            )

        top = max(abs(d) for d in daily)
        pct = top / abs_total * 100.0
        passed = pct <= max_pct
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "top_day_contribution_pct": float(pct),
                "threshold_pct": float(max_pct),
                "n_days": float(len(daily)),
            },
            details=f"top_day={pct:.1f}% of |total| (max {max_pct:.1f}%)",
        )
```

- [ ] **Step 5.4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_single_day_dominance.py -v --no-cov --tb=short
```
Expected: PASS — 5 tests green.

- [ ] **Step 5.5: Commit**

```bash
git add src/hft_platform/alpha/_sub_gates/single_day_dominance.py tests/unit/alpha/test_sub_gate_single_day_dominance.py
git commit -m "feat(alpha): add SingleDayDominanceGate sub-gate"
```

---

## Task 6: `LOODaySensitivityGate`

**Files:**
- Create: `src/hft_platform/alpha/_sub_gates/loo_day_sensitivity.py`
- Test: `tests/unit/alpha/test_sub_gate_loo_day_sensitivity.py`

- [ ] **Step 6.1: Write the failing tests**

```python
# tests/unit/alpha/test_sub_gate_loo_day_sensitivity.py
"""Tests for LOODaySensitivityGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.loo_day_sensitivity import LOODaySensitivityGate


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestLOODaySensitivityGate:
    def test_passes_when_sign_is_robust(self) -> None:
        gate = LOODaySensitivityGate()
        r = _FakeResult(daily_pnl=[10.0] * 30)
        out = gate.evaluate(r, config=None, thresholds={"loo_day_sign_preserved": True})
        assert out.passed is True

    def test_fails_when_dropping_top_day_flips_sign(self) -> None:
        gate = LOODaySensitivityGate()
        r = _FakeResult(daily_pnl=[100.0] + [-2.0] * 30)
        out = gate.evaluate(r, config=None, thresholds={"loo_day_sign_preserved": True})
        assert out.passed is False
        assert out.metrics["worst_loo_pnl"] < 0.0

    def test_disabled_threshold_passes(self) -> None:
        gate = LOODaySensitivityGate()
        r = _FakeResult(daily_pnl=[100.0] + [-2.0] * 30)
        out = gate.evaluate(r, config=None, thresholds={"loo_day_sign_preserved": False})
        assert out.passed is True

    def test_insufficient_days_fails_with_explicit_detail(self) -> None:
        gate = LOODaySensitivityGate()
        r = _FakeResult(daily_pnl=[1.0])
        out = gate.evaluate(r, config=None, thresholds={"loo_day_sign_preserved": True})
        assert out.passed is False
        assert "insufficient days" in out.details
```

- [ ] **Step 6.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_loo_day_sensitivity.py -v --no-cov --tb=short
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 6.3: Implement `LOODaySensitivityGate`**

```python
# src/hft_platform/alpha/_sub_gates/loo_day_sensitivity.py
"""Leave-one-day-out sensitivity sub-gate.

When the sign of total PnL flips after removing any single day, the
edge is single-day-dominated and the gate fails.
"""
from __future__ import annotations

from typing import Any

from hft_platform.alpha._resampling import leave_one_day_out
from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class LOODaySensitivityGate:
    """Sign of total PnL must survive any single-day removal."""

    name = "loo_day_sensitivity"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        require = bool(thresholds.get("loo_day_sign_preserved", False))
        if not require:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={},
                details="loo_day_sign_preserved=False — gate skipped",
            )

        daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
        if len(daily) < 2:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily))},
                details=f"insufficient days for LOO analysis: n={len(daily)}, need >= 2",
            )

        total = sum(daily)
        target_sign = 1 if total > 0 else (-1 if total < 0 else 0)
        worst = total
        for sliced in leave_one_day_out(daily):
            s = sum(sliced)
            if abs(s) < abs(worst) or (s * total) < 0:
                worst = s
        worst_sign = 1 if worst > 0 else (-1 if worst < 0 else 0)
        passed = (target_sign == worst_sign) and target_sign != 0

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "total_pnl": float(total),
                "worst_loo_pnl": float(worst),
                "n_days": float(len(daily)),
            },
            details=(f"total={total:.2f}, worst LOO={worst:.2f} (sign-preserved={passed})"),
        )
```

- [ ] **Step 6.4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_loo_day_sensitivity.py -v --no-cov --tb=short
```
Expected: PASS — 4 tests green.

- [ ] **Step 6.5: Commit**

```bash
git add src/hft_platform/alpha/_sub_gates/loo_day_sensitivity.py tests/unit/alpha/test_sub_gate_loo_day_sensitivity.py
git commit -m "feat(alpha): add LOODaySensitivityGate sub-gate"
```

---

## Task 7: `OutlierTradeRemovalGate`

**Files:**
- Create: `src/hft_platform/alpha/_sub_gates/outlier_trade_removal.py`
- Test: `tests/unit/alpha/test_sub_gate_outlier_trade_removal.py`

- [ ] **Step 7.1: Write the failing tests**

```python
# tests/unit/alpha/test_sub_gate_outlier_trade_removal.py
"""Tests for OutlierTradeRemovalGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.outlier_trade_removal import OutlierTradeRemovalGate


@dataclass
class _FakeResult:
    trade_pnl: list[float] = field(default_factory=list)
    daily_pnl: list[Any] = field(default_factory=list)


class TestOutlierTradeRemovalGate:
    def test_passes_when_sign_robust_to_drop(self) -> None:
        gate = OutlierTradeRemovalGate()
        r = _FakeResult(trade_pnl=[10.0] * 200)
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 5.0})
        assert out.passed is True

    def test_fails_when_top_trades_carry_all_edge(self) -> None:
        gate = OutlierTradeRemovalGate()
        trades = [1000.0] * 5 + [-30.0] * 195
        r = _FakeResult(trade_pnl=trades)
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 5.0})
        assert out.passed is False
        assert out.metrics["pnl_after_drop"] < 0.0

    def test_falls_back_to_daily_when_no_trade_pnl(self) -> None:
        gate = OutlierTradeRemovalGate()
        r = _FakeResult(daily_pnl=[200.0, 1.0, 1.0, 1.0])
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 25.0})
        assert out.passed is True

    def test_no_data_fails(self) -> None:
        gate = OutlierTradeRemovalGate()
        r = _FakeResult()
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 5.0})
        assert out.passed is False
        assert "no trade or daily pnl" in out.details

    def test_zero_pct_passes(self) -> None:
        gate = OutlierTradeRemovalGate()
        r = _FakeResult(trade_pnl=[10.0, -1.0])
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 0.0})
        assert out.passed is True
```

- [ ] **Step 7.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_outlier_trade_removal.py -v --no-cov --tb=short
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 7.3: Implement `OutlierTradeRemovalGate`**

```python
# src/hft_platform/alpha/_sub_gates/outlier_trade_removal.py
"""Outlier-trade removal sub-gate."""
from __future__ import annotations

from typing import Any

from hft_platform.alpha._resampling import drop_top_trades
from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class OutlierTradeRemovalGate:
    """Sign of total PnL must survive removing the top X% of trades."""

    name = "outlier_trade_removal"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        pct_value = float(thresholds.get("outlier_trade_removal_pct", 0.0))
        pct = pct_value / 100.0 if pct_value > 1.0 else pct_value

        trade_pnl = list(getattr(result, "trade_pnl", None) or [])
        if not trade_pnl:
            daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
            if not daily:
                return SubGateResult(
                    name=self.name,
                    passed=False,
                    metrics={},
                    details="no trade or daily pnl to evaluate",
                )
            trade_pnl = daily

        total = sum(trade_pnl)
        kept = drop_top_trades(trade_pnl, pct=pct)
        residual = sum(kept)
        target_sign = 1 if total > 0 else (-1 if total < 0 else 0)
        residual_sign = 1 if residual > 0 else (-1 if residual < 0 else 0)
        passed = (
            (target_sign == residual_sign) and target_sign != 0
            if pct > 0
            else True
        )

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "drop_pct": float(pct * 100.0),
                "n_trades_in": float(len(trade_pnl)),
                "n_trades_kept": float(len(kept)),
                "pnl_total": float(total),
                "pnl_after_drop": float(residual),
            },
            details=(f"drop top {pct * 100:.1f}%: {total:.2f} -> {residual:.2f}"),
        )
```

- [ ] **Step 7.4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_outlier_trade_removal.py -v --no-cov --tb=short
```
Expected: PASS — 5 tests green.

- [ ] **Step 7.5: Commit**

```bash
git add src/hft_platform/alpha/_sub_gates/outlier_trade_removal.py tests/unit/alpha/test_sub_gate_outlier_trade_removal.py
git commit -m "feat(alpha): add OutlierTradeRemovalGate sub-gate"
```

---

## Task 8: `DayLevelBootstrapCIGate`

**Files:**
- Create: `src/hft_platform/alpha/_sub_gates/day_bootstrap_ci.py`
- Test: `tests/unit/alpha/test_sub_gate_day_bootstrap_ci.py`

- [ ] **Step 8.1: Write the failing tests**

```python
# tests/unit/alpha/test_sub_gate_day_bootstrap_ci.py
"""Tests for DayLevelBootstrapCIGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hft_platform.alpha._sub_gates.day_bootstrap_ci import DayLevelBootstrapCIGate


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestDayLevelBootstrapCIGate:
    def test_passes_for_clearly_positive_daily_pnl(self) -> None:
        gate = DayLevelBootstrapCIGate()
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=10.0, scale=1.0, size=100)).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "bootstrap_ci_lower_bound_min": 0.0,
                "bootstrap_n_resamples": 1000,
                "bootstrap_alpha": 0.05,
            },
        )
        assert out.passed is True
        assert out.metrics["ci_lower"] > 0.0

    def test_fails_for_zero_mean_noise(self) -> None:
        gate = DayLevelBootstrapCIGate()
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=0.0, scale=10.0, size=100)).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "bootstrap_ci_lower_bound_min": 0.0,
                "bootstrap_n_resamples": 1000,
                "bootstrap_alpha": 0.05,
            },
        )
        assert out.passed is False

    def test_fails_when_too_few_days(self) -> None:
        gate = DayLevelBootstrapCIGate()
        r = _FakeResult(daily_pnl=[1.0])
        out = gate.evaluate(r, config=None, thresholds={"bootstrap_ci_lower_bound_min": 0.0})
        assert out.passed is False
        assert "insufficient" in out.details
```

- [ ] **Step 8.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_day_bootstrap_ci.py -v --no-cov --tb=short
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 8.3: Implement `DayLevelBootstrapCIGate`**

```python
# src/hft_platform/alpha/_sub_gates/day_bootstrap_ci.py
"""Day-level bootstrap CI sub-gate."""
from __future__ import annotations

from typing import Any

import numpy as np

from hft_platform.alpha._resampling import day_bootstrap
from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class DayLevelBootstrapCIGate:
    """Bootstrap-mean lower CI bound on daily PnL must exceed threshold."""

    name = "day_bootstrap_ci"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
        ci_min = float(thresholds.get("bootstrap_ci_lower_bound_min", 0.0))
        n_resamples = int(thresholds.get("bootstrap_n_resamples", 2000))
        alpha = float(thresholds.get("bootstrap_alpha", 0.05))
        seed = int(thresholds.get("bootstrap_rng_seed", 42))

        if len(daily) < 2:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily))},
                details=f"insufficient sample for bootstrap: n={len(daily)}",
            )

        samples = day_bootstrap(daily, n_resamples=n_resamples, rng_seed=seed)
        means = samples.mean(axis=1)
        ci_lower = float(np.quantile(means, alpha))

        passed = ci_lower > ci_min
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "ci_lower": ci_lower,
                "ci_min": ci_min,
                "alpha": alpha,
                "n_resamples": float(n_resamples),
                "n_days": float(len(daily)),
            },
            details=(f"CI[{alpha:.2f}] lower={ci_lower:.4f} vs min {ci_min}"),
        )
```

- [ ] **Step 8.4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_day_bootstrap_ci.py -v --no-cov --tb=short
```
Expected: PASS — 3 tests green.

- [ ] **Step 8.5: Commit**

```bash
git add src/hft_platform/alpha/_sub_gates/day_bootstrap_ci.py tests/unit/alpha/test_sub_gate_day_bootstrap_ci.py
git commit -m "feat(alpha): add DayLevelBootstrapCIGate sub-gate"
```

---

## Task 9: `StationaryBlockBootstrapGate`

**Files:**
- Create: `src/hft_platform/alpha/_sub_gates/stationary_block_bootstrap.py`
- Test: `tests/unit/alpha/test_sub_gate_stationary_block_bootstrap.py`

- [ ] **Step 9.1: Write the failing tests**

```python
# tests/unit/alpha/test_sub_gate_stationary_block_bootstrap.py
"""Tests for StationaryBlockBootstrapGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hft_platform.alpha._sub_gates.stationary_block_bootstrap import (
    StationaryBlockBootstrapGate,
)


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestStationaryBlockBootstrapGate:
    def test_passes_for_strong_signal(self) -> None:
        gate = StationaryBlockBootstrapGate()
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=20.0, scale=1.0, size=100)).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "block_bootstrap_ci_lower_bound_min": 0.0,
                "block_bootstrap_block_size_days": 5,
                "block_bootstrap_n_resamples": 500,
                "block_bootstrap_alpha": 0.05,
            },
        )
        assert out.passed is True

    def test_fails_for_zero_mean_noise(self) -> None:
        gate = StationaryBlockBootstrapGate()
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=0.0, scale=10.0, size=100)).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "block_bootstrap_ci_lower_bound_min": 0.0,
                "block_bootstrap_block_size_days": 5,
                "block_bootstrap_n_resamples": 500,
                "block_bootstrap_alpha": 0.05,
            },
        )
        assert out.passed is False

    def test_fails_when_input_shorter_than_block_size(self) -> None:
        gate = StationaryBlockBootstrapGate()
        r = _FakeResult(daily_pnl=[1.0, 2.0])
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "block_bootstrap_ci_lower_bound_min": 0.0,
                "block_bootstrap_block_size_days": 5,
            },
        )
        assert out.passed is False
        assert "block_size" in out.details
```

- [ ] **Step 9.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_stationary_block_bootstrap.py -v --no-cov --tb=short
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 9.3: Implement `StationaryBlockBootstrapGate`**

```python
# src/hft_platform/alpha/_sub_gates/stationary_block_bootstrap.py
"""Stationary block-bootstrap CI sub-gate."""
from __future__ import annotations

from typing import Any

import numpy as np

from hft_platform.alpha._resampling import stationary_block_bootstrap
from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class StationaryBlockBootstrapGate:
    """Politis-Romano block-bootstrap mean lower CI bound > threshold."""

    name = "stationary_block_bootstrap"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
        ci_min = float(thresholds.get("block_bootstrap_ci_lower_bound_min", 0.0))
        block_size = int(thresholds.get("block_bootstrap_block_size_days", 5))
        n_resamples = int(thresholds.get("block_bootstrap_n_resamples", 1000))
        alpha = float(thresholds.get("block_bootstrap_alpha", 0.05))
        seed = int(thresholds.get("block_bootstrap_rng_seed", 42))

        if len(daily) < block_size:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily)), "block_size": float(block_size)},
                details=f"input length {len(daily)} < block_size {block_size}",
            )

        samples = stationary_block_bootstrap(
            daily, block_size=block_size, n_resamples=n_resamples, rng_seed=seed
        )
        means = samples.mean(axis=1)
        ci_lower = float(np.quantile(means, alpha))
        passed = ci_lower > ci_min

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "ci_lower": ci_lower,
                "ci_min": ci_min,
                "alpha": alpha,
                "block_size": float(block_size),
                "n_resamples": float(n_resamples),
                "n_days": float(len(daily)),
            },
            details=(
                f"block-bootstrap CI[{alpha:.2f}] lower={ci_lower:.4f} "
                f"(block={block_size}, n={n_resamples})"
            ),
        )
```

- [ ] **Step 9.4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_stationary_block_bootstrap.py -v --no-cov --tb=short
```
Expected: PASS — 3 tests green.

- [ ] **Step 9.5: Commit**

```bash
git add src/hft_platform/alpha/_sub_gates/stationary_block_bootstrap.py tests/unit/alpha/test_sub_gate_stationary_block_bootstrap.py
git commit -m "feat(alpha): add StationaryBlockBootstrapGate sub-gate"
```

---

## Task 10: `DeflatedSharpeForMakerGate`

**Files:**
- Create: `src/hft_platform/alpha/_sub_gates/deflated_sharpe_maker.py`
- Test: `tests/unit/alpha/test_sub_gate_deflated_sharpe_maker.py`

- [ ] **Step 10.1: Write the failing tests**

```python
# tests/unit/alpha/test_sub_gate_deflated_sharpe_maker.py
"""Tests for DeflatedSharpeForMakerGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hft_platform.alpha._sub_gates.deflated_sharpe_maker import (
    DeflatedSharpeForMakerGate,
)


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestDeflatedSharpeForMakerGate:
    def test_applies_only_to_maker(self) -> None:
        gate = DeflatedSharpeForMakerGate()
        assert gate.applies_to == {"maker"}

    def test_passes_for_strong_sharpe(self) -> None:
        gate = DeflatedSharpeForMakerGate()
        rng = np.random.default_rng(0)
        daily = rng.normal(loc=2.0, scale=1.0, size=200).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={"deflated_sharpe_min": 0.5, "deflated_n_trials": 1},
        )
        assert out.passed is True

    def test_fails_for_thin_sharpe_with_many_trials(self) -> None:
        gate = DeflatedSharpeForMakerGate()
        rng = np.random.default_rng(0)
        daily = rng.normal(loc=0.05, scale=1.0, size=30).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={"deflated_sharpe_min": 0.5, "deflated_n_trials": 100},
        )
        assert out.passed is False

    def test_insufficient_days_fails(self) -> None:
        gate = DeflatedSharpeForMakerGate()
        r = _FakeResult(daily_pnl=[1.0])
        out = gate.evaluate(r, config=None, thresholds={"deflated_sharpe_min": 0.5})
        assert out.passed is False
```

- [ ] **Step 10.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_deflated_sharpe_maker.py -v --no-cov --tb=short
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 10.3: Implement `DeflatedSharpeForMakerGate`**

```python
# src/hft_platform/alpha/_sub_gates/deflated_sharpe_maker.py
"""Deflated-Sharpe sub-gate for maker payloads.

Reuses the Bonferroni-style penalty from `_param_opt.py`:
    deflated_sharpe = sharpe_oos - sqrt(2 * log(n_trials) / oos_len)
"""
from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class DeflatedSharpeForMakerGate:
    """Maker-side deflated Sharpe must exceed `deflated_sharpe_min`."""

    name = "deflated_sharpe_maker"
    applies_to = {"maker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
        if len(daily) < 2:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"n_days": float(len(daily))},
                details=f"insufficient days for Sharpe: n={len(daily)}",
            )

        m = mean(daily)
        s = stdev(daily)
        sharpe = (m / s) * math.sqrt(252.0) if s > 0 else 0.0
        n_trials = max(1, int(thresholds.get("deflated_n_trials", 1)))
        oos_len = max(2, len(daily))
        penalty = math.sqrt(2.0 * math.log(n_trials) / oos_len) if n_trials > 1 else 0.0
        deflated = sharpe - penalty

        threshold = float(thresholds.get("deflated_sharpe_min", 0.5))
        passed = deflated >= threshold

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "sharpe": float(sharpe),
                "deflated_sharpe": float(deflated),
                "deflated_min": float(threshold),
                "n_trials": float(n_trials),
                "n_days": float(oos_len),
                "penalty": float(penalty),
            },
            details=(
                f"sharpe={sharpe:.2f}, deflated={deflated:.2f} "
                f"(penalty={penalty:.2f}, trials={n_trials}) vs min {threshold}"
            ),
        )
```

- [ ] **Step 10.4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_deflated_sharpe_maker.py -v --no-cov --tb=short
```
Expected: PASS — 4 tests green.

- [ ] **Step 10.5: Commit**

```bash
git add src/hft_platform/alpha/_sub_gates/deflated_sharpe_maker.py tests/unit/alpha/test_sub_gate_deflated_sharpe_maker.py
git commit -m "feat(alpha): add DeflatedSharpeForMakerGate sub-gate"
```

---

## Task 11: Register the 7 new sub-gates

**Files:**
- Modify: `src/hft_platform/alpha/_sub_gates/__init__.py:24-46`
- Test: `tests/unit/alpha/test_sub_gates_registration.py` (new)

- [ ] **Step 11.1: Write the failing test**

```python
# tests/unit/alpha/test_sub_gates_registration.py
"""Verify all 7 new sub-gates are auto-registered."""
from __future__ import annotations

from hft_platform.alpha._sub_gates import (
    clear_registry,
    ensure_builtin_sub_gates_registered,
    get_registered_sub_gates,
)


_NEW_GATE_NAMES = {
    "min_sample_size",
    "single_day_dominance",
    "loo_day_sensitivity",
    "outlier_trade_removal",
    "day_bootstrap_ci",
    "stationary_block_bootstrap",
    "deflated_sharpe_maker",
}


def test_all_new_gates_registered_after_clear_and_reset() -> None:
    clear_registry()
    ensure_builtin_sub_gates_registered()
    names = {g.name for g in get_registered_sub_gates()}
    missing = _NEW_GATE_NAMES - names
    assert not missing, f"missing gates: {missing}"


def test_registration_is_idempotent() -> None:
    ensure_builtin_sub_gates_registered()
    before = [g.name for g in get_registered_sub_gates()]
    ensure_builtin_sub_gates_registered()
    after = [g.name for g in get_registered_sub_gates()]
    assert before == after
```

- [ ] **Step 11.2: Run the test to verify it fails**

```bash
uv run pytest tests/unit/alpha/test_sub_gates_registration.py -v --no-cov --tb=short
```
Expected: FAIL — missing gates listed.

- [ ] **Step 11.3: Add registrations**

In `src/hft_platform/alpha/_sub_gates/__init__.py`, replace the body of `ensure_builtin_sub_gates_registered()` with:

```python
def ensure_builtin_sub_gates_registered() -> None:
    """Ensure all built-in sub-gates are registered (idempotent by name)."""
    from hft_platform.alpha._sub_gates.common import (
        MaxDrawdownGate,
        SharpeThresholdGate,
        WinningDayPctGate,
    )
    from hft_platform.alpha._sub_gates.day_bootstrap_ci import DayLevelBootstrapCIGate
    from hft_platform.alpha._sub_gates.deflated_sharpe_maker import (
        DeflatedSharpeForMakerGate,
    )
    from hft_platform.alpha._sub_gates.loo_day_sensitivity import LOODaySensitivityGate
    from hft_platform.alpha._sub_gates.maker import (
        FillQualityGate,
        FillRateValidationGate,
    )
    from hft_platform.alpha._sub_gates.min_sample_size import MinSampleSizeGate
    from hft_platform.alpha._sub_gates.outlier_trade_removal import (
        OutlierTradeRemovalGate,
    )
    from hft_platform.alpha._sub_gates.single_day_dominance import (
        SingleDayDominanceGate,
    )
    from hft_platform.alpha._sub_gates.stationary_block_bootstrap import (
        StationaryBlockBootstrapGate,
    )
    from hft_platform.alpha._sub_gates.taker import ICEvaluationGate

    existing_names = {g.name for g in get_registered_sub_gates()}
    candidates: list[SubGate] = [
        # Existing
        SharpeThresholdGate(),
        MaxDrawdownGate(),
        WinningDayPctGate(),
        FillQualityGate(),
        FillRateValidationGate(),
        ICEvaluationGate(),
        # New (Slice A)
        MinSampleSizeGate(),
        SingleDayDominanceGate(),
        LOODaySensitivityGate(),
        OutlierTradeRemovalGate(),
        DayLevelBootstrapCIGate(),
        StationaryBlockBootstrapGate(),
        DeflatedSharpeForMakerGate(),
    ]
    for gate in candidates:
        if gate.name not in existing_names:
            register_sub_gate(gate)
```

- [ ] **Step 11.4: Run all alpha unit tests to verify nothing broke**

```bash
uv run pytest tests/unit/alpha/ -v --no-cov --tb=short
```
Expected: PASS — registration tests + all 7 gate tests + resampling tests + profile tests green.

- [ ] **Step 11.5: Commit**

```bash
git add src/hft_platform/alpha/_sub_gates/__init__.py tests/unit/alpha/test_sub_gates_registration.py
git commit -m "feat(alpha): register 7 new small-sample sub-gates"
```

---

## Task 12: Gate C aggregator change — wire profile blocking

**Files:**
- Modify: `src/hft_platform/alpha/_gate_c.py:48-128` (function), `:225-232` (maker aggregator), `:515-528` (taker aggregator), `:260-285` (maker invocation site), `:530-565` (taker invocation site)
- Test: `tests/unit/alpha/test_gate_c_blocking.py` (new)

- [ ] **Step 12.1: Write the failing test**

```python
# tests/unit/alpha/test_gate_c_blocking.py
"""Unit tests for the Gate C blocking-subset aggregator."""
from __future__ import annotations

from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import ValidationProfile


_R47_FINGERPRINT = {
    "run_id": "test",
    "config_hash": "test",
    "instrument": "TMFD6",
    "strategy_name": "r47",
    "engine": "maker_engine",
    "queue_model": "QueueDepletionFill",
    "calibration_profile_id": "uncalibrated",
    "data_source": "ck",
    "latency_profile": "shioaji_measured_p95",
    "pnl_pts": 2398.0,
    "n_fills": 39,
    "n_trading_days": 31,
    "equity_curve": None,
    "pnl_per_fill": 61.5,
    "adverse_fill_pct": 0.30,
    "fill_rate_per_day": 1.26,
    "daily_pnl": [2325.0] + [2.4] * 30,
}


class TestInvokeSubGatesBlocking:
    def test_no_profile_returns_no_blocking_aggregate(self) -> None:
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_R47_FINGERPRINT,
            thresholds={"sharpe_is_min": 0.5, "winning_day_pct_min": 55},
            profile=None,
        )
        assert isinstance(advisory, list) and len(advisory) > 0
        assert blocking is None

    def test_strict_profile_aggregates_named_gates_to_false_for_r47(self) -> None:
        prof = ValidationProfile(
            name="test_strict",
            is_strict=True,
            thresholds={
                "maker": {
                    "min_fills": 300,
                    "min_days": 60,
                    "outlier_day_contribution_max_pct": 25.0,
                    "loo_day_sign_preserved": True,
                }
            },
            blocking_sub_gates=(
                "min_sample_size",
                "single_day_dominance",
                "loo_day_sensitivity",
            ),
        )
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_R47_FINGERPRINT,
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert blocking is not None
        assert blocking["passed"] is False
        failing = {f["name"] for f in blocking["failing"]}
        assert "min_sample_size" in failing
        assert "single_day_dominance" in failing
        assert "loo_day_sensitivity" in failing

    def test_strict_profile_passes_for_robust_payload(self) -> None:
        prof = ValidationProfile(
            name="test_strict",
            is_strict=True,
            thresholds={
                "maker": {
                    "min_fills": 100,
                    "min_days": 30,
                    "outlier_day_contribution_max_pct": 25.0,
                    "loo_day_sign_preserved": True,
                }
            },
            blocking_sub_gates=("min_sample_size", "single_day_dominance", "loo_day_sensitivity"),
        )
        robust = dict(_R47_FINGERPRINT)
        robust["n_fills"] = 300
        robust["n_trading_days"] = 60
        robust["daily_pnl"] = [10.0] * 60
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=robust,
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert blocking is not None and blocking["passed"] is True
```

- [ ] **Step 12.2: Run the test to verify it fails**

```bash
uv run pytest tests/unit/alpha/test_gate_c_blocking.py -v --no-cov --tb=short
```
Expected: FAIL — `ImportError: cannot import name '_invoke_sub_gates'`.

- [ ] **Step 12.3: Replace `_invoke_sub_gates_advisory` in `_gate_c.py`**

In `src/hft_platform/alpha/_gate_c.py`, replace the body of `_invoke_sub_gates_advisory` (lines 48-128) with the new function below. Keep the old function name as a thin wrapper so call-sites that don't yet pass a profile keep working unchanged.

```python
def _invoke_sub_gates(
    *,
    strategy_type: str,
    result_payload: dict,
    thresholds: dict,
    calibration_profile: Any | None = None,
    profile: Any | None = None,
) -> tuple[list[dict], dict | None]:
    """Invoke all applicable sub-gates and compute blocking aggregate.

    Returns:
        advisory: list of dicts, one per registered applicable gate.
        blocking: None if `profile is None`; otherwise
            ``{"passed": bool, "failing": [<gate_dict>], "names": [...], "profile": <name>}``
            with only gates listed in ``profile.blocking_sub_gates`` contributing
            to ``passed``. Errored gates are treated as ``passed=False`` for
            blocking purposes (fail-closed).
    """
    import numpy as np

    from hft_platform.alpha._sub_gates import (
        ensure_builtin_sub_gates_registered,
        get_registered_sub_gates,
    )
    from hft_platform.alpha._sub_gates.maker import FillRateValidationGate
    from hft_platform.backtest.result import BacktestResult

    ensure_builtin_sub_gates_registered()

    result = BacktestResult(
        run_id=result_payload.get("run_id", ""),
        config_hash=result_payload.get("config_hash", ""),
        instrument=result_payload.get("instrument", ""),
        strategy_name=result_payload.get("strategy_name", ""),
        strategy_type=strategy_type,  # type: ignore[arg-type]
        engine=result_payload.get("engine", "unknown"),
        queue_model=result_payload.get("queue_model", "unknown"),
        calibration_profile_id=result_payload.get("calibration_profile_id", "uncalibrated"),
        data_source=result_payload.get("data_source", "unknown"),
        latency_profile=str(result_payload.get("latency_profile", "")),
        pnl_pts=float(result_payload.get("pnl_pts", 0.0)),
        n_fills=int(result_payload.get("n_fills", 0)),
        n_trading_days=int(result_payload.get("n_trading_days", 0)),
        equity_curve=result_payload.get("equity_curve", np.zeros(1)),
        pnl_per_fill=result_payload.get("pnl_per_fill"),
        adverse_fill_pct=result_payload.get("adverse_fill_pct"),
        fill_rate_per_day=result_payload.get("fill_rate_per_day"),
        ic_is=result_payload.get("ic_is"),
        ic_oos=result_payload.get("ic_oos"),
        daily_pnl=list(result_payload.get("daily_pnl") or []),
    )
    if "trade_pnl" in result_payload:
        try:
            object.__setattr__(result, "trade_pnl", list(result_payload["trade_pnl"]))
        except Exception:  # noqa: BLE001
            pass

    blocking_names: set[str] = set(getattr(profile, "blocking_sub_gates", ()) or ())

    advisory: list[dict] = []
    blocking_failing: list[dict] = []
    blocking_seen: list[str] = []

    for gate in get_registered_sub_gates():
        if strategy_type not in gate.applies_to:
            continue
        try:
            if isinstance(gate, FillRateValidationGate):
                sub = gate.evaluate(
                    result,
                    config=None,
                    thresholds=thresholds,
                    profile=calibration_profile,
                )
            else:
                sub = gate.evaluate(result, config=None, thresholds=thresholds)
            entry = {
                "name": sub.name,
                "passed": sub.passed,
                "metrics": sub.metrics,
                "details": sub.details,
            }
            advisory.append(entry)
            if profile is not None and sub.name in blocking_names:
                blocking_seen.append(sub.name)
                if not sub.passed:
                    blocking_failing.append(entry)
        except Exception as exc:  # noqa: BLE001
            entry = {
                "name": getattr(gate, "name", "unknown"),
                "passed": None,
                "metrics": {},
                "details": f"sub-gate error: {exc!r}",
                "error": True,
            }
            advisory.append(entry)
            if profile is not None and entry["name"] in blocking_names:
                blocking_seen.append(entry["name"])
                blocking_failing.append(entry)

    if profile is None:
        return advisory, None
    return advisory, {
        "passed": len(blocking_failing) == 0,
        "failing": blocking_failing,
        "names": blocking_seen,
        "profile": profile.name,
    }


def _invoke_sub_gates_advisory(
    *,
    strategy_type: str,
    result_payload: dict,
    thresholds: dict,
    calibration_profile: Any | None = None,
) -> list[dict]:
    """Backward-compatible wrapper for callers that don't pass a profile."""
    advisory, _ = _invoke_sub_gates(
        strategy_type=strategy_type,
        result_payload=result_payload,
        thresholds=thresholds,
        calibration_profile=calibration_profile,
        profile=None,
    )
    return advisory
```

- [ ] **Step 12.4: Wire profile into the maker invocation site (around line 262)**

Replace the maker `_invoke_sub_gates_advisory(...)` call with:

```python
        maker_sub_gates, maker_blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload={
                "run_id": result.run_id,
                "config_hash": result.config_hash,
                "instrument": instrument,
                "strategy_name": alpha_id,
                "engine": "maker_engine",
                "queue_model": f"QueueDepletionFill(qf={qf})",
                "calibration_profile_id": "uncalibrated",
                "data_source": "clickhouse_direct",
                "latency_profile": getattr(result, "latency_profile", ""),
                "pnl_pts": _daily_pnl_points(daily_pnl),
                "n_fills": int(total_fills),
                "n_trading_days": int(n_days),
                "equity_curve": getattr(result, "equity_curve", None),
                "pnl_per_fill": float(pnl_per_fill) if pnl_per_fill is not None else None,
                "adverse_fill_pct": float(scorecard_data.get("adverse_fill_pct", 0)),
                "fill_rate_per_day": (float(total_fills) / max(float(n_days), 1.0)),
                "daily_pnl": _daily_pnl_sequence(daily_pnl),
            },
            thresholds=(
                config.profile.thresholds_for(strategy_type="maker") | maker_thresholds
                if config.profile is not None
                else maker_thresholds
            ),
            calibration_profile=None,
            profile=config.profile,
        )
```

Then update the `maker_passed` line:

```python
        maker_passed = all(maker_checks.values()) and (
            maker_blocking is None or maker_blocking["passed"]
        )
```

And add to `report.details`:

```python
                "sub_gates_advisory": maker_sub_gates,
                "sub_gates_blocking": maker_blocking,
```

- [ ] **Step 12.5: Wire profile into the taker invocation site (around line 535)**

Same pattern. Replace the taker `_invoke_sub_gates_advisory(...)` call with:

```python
    taker_sub_gates, taker_blocking = _invoke_sub_gates(
        strategy_type="taker",
        result_payload={
            "run_id": result.run_id,
            "config_hash": result.config_hash,
            "instrument": instrument,
            "strategy_name": alpha_id,
            "engine": "hftbacktest_v2",
            "queue_model": str(selected_cfg.queue_model),
            "calibration_profile_id": "uncalibrated",
            "data_source": "hftbt_npz",
            "latency_profile": getattr(result, "latency_profile", ""),
            "pnl_pts": (
                float(result.equity_curve[-1] - result.equity_curve[0]) if _eq is not None and len(_eq) > 1 else 0.0
            ),
            "n_fills": (int(len(result.signals)) if hasattr(result, "signals") and result.signals is not None else 0),
            "n_trading_days": int(len(_daily_pnl)),
            "equity_curve": _eq,
            "ic_is": float(result.ic_mean) if result.ic_mean is not None else None,
            "ic_oos": None,
            "daily_pnl": _daily_pnl,
        },
        thresholds=(
            (config.profile.thresholds_for(strategy_type="taker") | {
                "sharpe_is_min": float(config.min_sharpe_oos),
                "max_drawdown_pct": float(abs(config.max_abs_drawdown)) * 100,
                "winning_day_pct_min": 55.0,
                "ic_is_min": 0.03,
                "ic_oos_min": 0.02,
            })
            if config.profile is not None
            else {
                "sharpe_is_min": float(config.min_sharpe_oos),
                "max_drawdown_pct": float(abs(config.max_abs_drawdown)) * 100,
                "winning_day_pct_min": 55.0,
                "ic_is_min": 0.03,
                "ic_oos_min": 0.02,
            }
        ),
        calibration_profile=None,
        profile=config.profile,
    )
```

Then update the taker `passed` aggregation:

```python
    passed = (
        core_passed
        and bool(stat_gate_passed)
        and bool(wf_gate_passed)
        and bool(optimization_gate_passed)
        and bool(stress_eval.get("passed"))
        and bool(robustness_eval.get("passed"))
        and bool(trend_gate_passed)
        and (taker_blocking is None or taker_blocking["passed"])
    )
```

And add to `report.details`:

```python
            "sub_gates_advisory": taker_sub_gates,
            "sub_gates_blocking": taker_blocking,
```

- [ ] **Step 12.6: Run the new aggregator test plus the legacy advisory tests**

```bash
uv run pytest tests/unit/alpha/test_gate_c_blocking.py tests/integration/test_gate_c_sub_gates_e2e.py -v --no-cov --tb=short
```
Expected: PASS — new blocking tests green; existing e2e tests still green (loose path preserved).

- [ ] **Step 12.7: Commit**

```bash
git add src/hft_platform/alpha/_gate_c.py tests/unit/alpha/test_gate_c_blocking.py
git commit -m "feat(alpha): wire profile-driven blocking subset into Gate C aggregator"
```

---

## Task 13: Strict profile YAML

**Files:**
- Create: `config/research/profiles/vm_ul6_strict.yaml`
- Test: extend `tests/unit/alpha/test_validation_profile.py`

- [ ] **Step 13.1: Append the failing test**

Append to `tests/unit/alpha/test_validation_profile.py`:

```python
class TestShippedStrictProfile:
    def test_vm_ul6_strict_loads(self) -> None:
        from hft_platform.alpha._validation_profile import load_profile

        prof = load_profile("config/research/profiles/vm_ul6_strict.yaml")
        assert prof.name == "vm_ul6_strict"
        assert prof.is_strict is True
        for gate_name in (
            "sharpe_threshold",
            "max_drawdown",
            "winning_day_pct",
            "fill_quality",
            "fill_rate_validation",
            "ic_evaluation",
            "min_sample_size",
            "single_day_dominance",
            "loo_day_sensitivity",
            "outlier_trade_removal",
            "day_bootstrap_ci",
            "stationary_block_bootstrap",
            "deflated_sharpe_maker",
        ):
            assert gate_name in prof.blocking_sub_gates, gate_name
```

- [ ] **Step 13.2: Run to verify failure**

```bash
uv run pytest tests/unit/alpha/test_validation_profile.py::TestShippedStrictProfile -v --no-cov --tb=short
```
Expected: FAIL — `FileNotFoundError`.

- [ ] **Step 13.3: Confirm directory exists**

```bash
mkdir -p config/research/profiles
ls config/research/profiles
```

- [ ] **Step 13.4: Create the profile file**

```yaml
# config/research/profiles/vm_ul6_strict.yaml
# Strict promotion-eligibility profile for the HFT alpha pipeline.
# Required for any artifact entering Gate D (see promote_alpha()).
# Loose `make research` runs do not need this profile.
name: vm_ul6_strict
is_strict: true

thresholds:
  taker:
    sharpe_oos_min: 1.5
    max_drawdown_pct: 10
    winning_day_pct_min: 58
    min_oos_trades: 200
    min_oos_days: 30

  maker:
    sharpe_is_min: 1.0
    sharpe_oos_min: 1.0
    pnl_per_fill_min_pts: 2.0
    pnl_per_fill_min_pts_multiplier: 0.5
    edge_to_cost_ratio_min: 1.5
    adverse_fill_pct_max: 40
    winning_day_pct_min: 60
    max_drawdown_pct: 15
    fill_rate_deviation_max: 0.5

    # Slice A small-sample gates
    min_fills: 300
    min_days: 60
    outlier_day_contribution_max_pct: 25
    loo_day_sign_preserved: true
    outlier_trade_removal_pct: 5
    bootstrap_ci_lower_bound_min: 0
    bootstrap_n_resamples: 2000
    bootstrap_alpha: 0.05
    block_bootstrap_ci_lower_bound_min: 0
    block_bootstrap_block_size_days: 5
    block_bootstrap_n_resamples: 1000
    block_bootstrap_alpha: 0.05
    deflated_sharpe_min: 0.5
    deflated_n_trials: 1

blocking_sub_gates:
  # Existing — promoted from advisory to blocking under strict
  - sharpe_threshold
  - max_drawdown
  - winning_day_pct
  - fill_quality
  - fill_rate_validation
  - ic_evaluation
  # New (Slice A)
  - min_sample_size
  - single_day_dominance
  - loo_day_sensitivity
  - outlier_trade_removal
  - day_bootstrap_ci
  - stationary_block_bootstrap
  - deflated_sharpe_maker
```

- [ ] **Step 13.5: Run to verify pass**

```bash
uv run pytest tests/unit/alpha/test_validation_profile.py -v --no-cov --tb=short
```
Expected: PASS — all profile tests green.

- [ ] **Step 13.6: Commit**

```bash
git add config/research/profiles/vm_ul6_strict.yaml tests/unit/alpha/test_validation_profile.py
git commit -m "feat(alpha): ship vm_ul6_strict promotion profile"
```

---

## Task 14: Promotion-path enforcement

**Files:**
- Modify: `src/hft_platform/alpha/promotion.py:36` (PromotionConfig), `:126` (promote_alpha)
- Test: `tests/unit/test_alpha_promotion.py` (extend)

- [ ] **Step 14.1: Write the failing test**

Append to `tests/unit/test_alpha_promotion.py`:

```python
class TestStrictProfileRequirement:
    def test_promotion_requires_strict_profile(self, tmp_path: Path) -> None:
        from hft_platform.alpha.promotion import (
            PromotionConfig,
            PromotionError,
            promote_alpha,
        )

        config = PromotionConfig(
            alpha_id="test_alpha",
            project_root=str(tmp_path),
            scorecard_path=None,
            validation_profile=None,
        )
        with pytest.raises(PromotionError, match="strict profile required"):
            promote_alpha(config)

    def test_promotion_accepts_strict_profile(self, tmp_path: Path) -> None:
        from hft_platform.alpha._validation_profile import ValidationProfile
        from hft_platform.alpha.promotion import (
            PromotionConfig,
            PromotionError,
            promote_alpha,
        )

        prof = ValidationProfile(
            name="vm_ul6_strict",
            is_strict=True,
            thresholds={},
            blocking_sub_gates=("sharpe_threshold",),
        )
        config = PromotionConfig(
            alpha_id="test_alpha",
            project_root=str(tmp_path),
            scorecard_path=None,
            validation_profile=prof,
        )
        try:
            promote_alpha(config)
        except PromotionError as exc:
            assert "strict profile required" not in str(exc)
        except Exception:
            pass  # other downstream errors are out of scope here
```

- [ ] **Step 14.2: Run test to verify failure**

```bash
uv run pytest tests/unit/test_alpha_promotion.py::TestStrictProfileRequirement -v --no-cov --tb=short
```
Expected: FAIL — `PromotionError` not defined; `validation_profile` not a `PromotionConfig` field.

- [ ] **Step 14.3: Add `PromotionError` and `validation_profile`**

In `src/hft_platform/alpha/promotion.py`, add a new class near the top of the file (after the imports, before `class PromotionConfig`):

```python
class PromotionError(RuntimeError):
    """Raised when a promotion is rejected by a structural pre-check."""
```

Add an optional field to the `PromotionConfig` frozen dataclass (around line 36):

```python
    validation_profile: Any | None = None
```

(Confirm `from typing import Any` is imported; if not, add it.)

At the very top of `promote_alpha(config: PromotionConfig) -> PromotionResult` (around line 126), prepend:

```python
def promote_alpha(config: PromotionConfig) -> PromotionResult:
    profile = getattr(config, "validation_profile", None)
    if profile is None or not getattr(profile, "is_strict", False):
        raise PromotionError(
            f"strict profile required for Gate D entry; "
            f"got profile={getattr(profile, 'name', None)!r}"
        )
    # ... existing body unchanged
```

- [ ] **Step 14.4: Run the promotion test to verify pass**

```bash
uv run pytest tests/unit/test_alpha_promotion.py::TestStrictProfileRequirement -v --no-cov --tb=short
```
Expected: PASS.

- [ ] **Step 14.5: Run the full promotion test surface and adjust pre-existing fixtures**

```bash
uv run pytest tests/unit/test_alpha_promotion.py tests/unit/test_alpha_promotion_force.py -v --no-cov --tb=short
```
If any pre-existing test fails because it builds a `PromotionConfig` without `validation_profile`, **do not loosen the gate** — instead, update those test fixtures to inject a strict `ValidationProfile` (e.g., `ValidationProfile(name="test", is_strict=True, blocking_sub_gates=("sharpe_threshold",))`).

- [ ] **Step 14.6: Commit**

```bash
git add src/hft_platform/alpha/promotion.py tests/unit/test_alpha_promotion.py
git commit -m "feat(alpha): require strict ValidationProfile for Gate D entry"
```

---

## Task 15: Integration tests — R47-kill + robust-pass + loose-parity

**Files:**
- Create: `tests/integration/test_strict_profile_e2e.py`

- [ ] **Step 15.1: Write the integration tests**

```python
# tests/integration/test_strict_profile_e2e.py
"""End-to-end Slice A integration tests.

Three scenarios:
1. R47-OE1 fingerprint payload + strict profile -> Gate C KILL.
2. Robust payload + strict profile -> Gate C PASS.
3. Same payload as (1) without profile -> behavior identical to pre-change.
"""
from __future__ import annotations

from typing import Any

import pytest

from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import load_profile


@pytest.fixture(scope="module")
def strict_profile() -> Any:
    return load_profile("config/research/profiles/vm_ul6_strict.yaml")


def _r47_payload() -> dict:
    return {
        "run_id": "test_r47",
        "config_hash": "abc",
        "instrument": "TMFD6",
        "strategy_name": "r47_maker_tmf",
        "engine": "maker_engine",
        "queue_model": "QueueDepletionFill(qf=0.5)",
        "calibration_profile_id": "uncalibrated",
        "data_source": "ck",
        "latency_profile": "shioaji_measured_p95",
        "pnl_pts": 2398.0,
        "n_fills": 39,
        "n_trading_days": 31,
        "equity_curve": None,
        "pnl_per_fill": 61.5,
        "adverse_fill_pct": 0.30,
        "fill_rate_per_day": 1.26,
        "daily_pnl": [2325.0] + [2.4] * 30,
    }


def _robust_payload() -> dict:
    return {
        "run_id": "test_robust",
        "config_hash": "xyz",
        "instrument": "TXFD6",
        "strategy_name": "synthetic_robust",
        "engine": "maker_engine",
        "queue_model": "QueueDepletionFill(qf=0.5)",
        "calibration_profile_id": "uncalibrated",
        "data_source": "ck",
        "latency_profile": "shioaji_measured_p95",
        "pnl_pts": 60000.0,
        "n_fills": 360,
        "n_trading_days": 60,
        "equity_curve": None,
        "pnl_per_fill": 166.0,
        "adverse_fill_pct": 0.30,
        "fill_rate_per_day": 6.0,
        "daily_pnl": [1000.0] * 60,
    }


class TestStrictProfileEndToEnd:
    def test_r47_pattern_kills_under_strict_profile(self, strict_profile: Any) -> None:
        thresholds = strict_profile.thresholds_for(strategy_type="maker")
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_r47_payload(),
            thresholds=thresholds,
            profile=strict_profile,
        )
        assert blocking is not None
        assert blocking["passed"] is False, blocking
        failing = {f["name"] for f in blocking["failing"]}
        assert "min_sample_size" in failing
        assert "single_day_dominance" in failing
        assert "loo_day_sensitivity" in failing

    def test_robust_pattern_passes_under_strict_profile(self, strict_profile: Any) -> None:
        thresholds = strict_profile.thresholds_for(strategy_type="maker")
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_robust_payload(),
            thresholds=thresholds,
            profile=strict_profile,
        )
        assert blocking is not None
        assert blocking["passed"] is True, blocking["failing"]

    def test_loose_profile_preserves_advisory_only_behavior(self) -> None:
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_r47_payload(),
            thresholds={"sharpe_is_min": 0.5, "winning_day_pct_min": 55},
            profile=None,
        )
        assert blocking is None
        assert any(g["name"] == "fill_quality" for g in advisory)
```

- [ ] **Step 15.2: Run the integration tests**

```bash
uv run pytest tests/integration/test_strict_profile_e2e.py -v --no-cov --tb=short
```
Expected: PASS — 3 tests green.

- [ ] **Step 15.3: Commit**

```bash
git add tests/integration/test_strict_profile_e2e.py
git commit -m "test(alpha): integration tests for vm_ul6_strict profile"
```

---

## Task 16: Final verification + plan close-out

- [ ] **Step 16.1: Run the full alpha test surface**

```bash
uv run pytest tests/unit/alpha/ tests/integration/test_strict_profile_e2e.py tests/integration/test_gate_c_sub_gates_e2e.py tests/unit/test_alpha_promotion.py tests/unit/test_alpha_promotion_force.py -v --no-cov --tb=short
```
Expected: PASS.

- [ ] **Step 16.2: Run `make ci`**

```bash
make ci
```
Expected: format-check + lint + typecheck + coverage all green.

- [ ] **Step 16.3: Loose-profile parity smoke**

```bash
uv run python - <<'PY'
"""Smoke: pre-Slice-A advisory output shape unchanged when profile is None."""
from hft_platform.alpha._gate_c import _invoke_sub_gates_advisory
res = _invoke_sub_gates_advisory(
    strategy_type="maker",
    result_payload={"daily_pnl": [10.0, 20.0, 30.0], "n_fills": 100, "n_trading_days": 3,
                    "pnl_per_fill": 60.0, "adverse_fill_pct": 0.30, "fill_rate_per_day": 33.3,
                    "equity_curve": None, "instrument": "X", "run_id": "x", "config_hash": "x",
                    "engine": "x", "queue_model": "x", "calibration_profile_id": "x",
                    "data_source": "x", "latency_profile": "x", "pnl_pts": 60.0,
                    "strategy_name": "x"},
    thresholds={"sharpe_is_min": 0.0, "winning_day_pct_min": 0.0, "max_drawdown_pct": 100.0},
)
assert isinstance(res, list) and len(res) > 0
print(f"OK: {len(res)} advisory gates, names={[g['name'] for g in res]}")
PY
```
Expected: stdout `OK: <N> advisory gates, ...`.

- [ ] **Step 16.4: Profile validator manual smoke**

```bash
uv run python -c "from hft_platform.alpha._validation_profile import load_profile; p=load_profile('config/research/profiles/vm_ul6_strict.yaml'); print(p.name, p.is_strict, len(p.blocking_sub_gates))"
```
Expected: `vm_ul6_strict True 13`.

- [ ] **Step 16.5: Negative profile sanity check**

```bash
uv run python - <<'PY'
"""Verify validator rejects an unknown gate."""
from pathlib import Path
import yaml
from hft_platform.alpha._validation_profile import ProfileValidationError, load_profile
bad = Path("/tmp/_bad_profile.yaml")
bad.write_text(yaml.safe_dump({"name": "bad", "is_strict": True,
    "thresholds": {}, "blocking_sub_gates": ["sharpe_threshold", "BogusGate"]}))
try:
    load_profile(bad)
except ProfileValidationError as exc:
    print("OK:", exc)
else:
    print("FAIL: did not raise")
finally:
    bad.unlink()
PY
```
Expected: stdout starts with `OK:` and mentions `BogusGate`.

- [ ] **Step 16.6: Update `.agent/memory/lessons_learned.md`**

Append:

```markdown
## [ARCH] Slice A — Promotion Gate Hardening (2026-05)

**Context:** Gate C was advisory-only, allowing R47-OE1 (39 fills, 96.9% PnL from one day) to reach `R47_MAKER_TMF enabled: true`.

**Fix:**
- Profile-driven blocking via `config/research/profiles/vm_ul6_strict.yaml`.
- 7 new sub-gates: `min_sample_size`, `single_day_dominance`, `loo_day_sensitivity`, `outlier_trade_removal`, `day_bootstrap_ci`, `stationary_block_bootstrap`, `deflated_sharpe_maker`.
- `promote_alpha()` rejects any `PromotionConfig` without a strict `ValidationProfile`.
- Loose runs (no profile) preserve pre-Slice-A behavior bit-for-bit.

**Rule:** Any future promotion must run with `--validation-profile vm_ul6_strict` (or another `is_strict: true` profile that names at least the seven new small-sample gates as blocking).

**Commits:** <fill in commit range when slice closes>
```

- [ ] **Step 16.7: Final commit + push branch**

```bash
git add .agent/memory/lessons_learned.md
git commit -m "docs(memory): record Slice A promotion gate hardening lessons"
git push -u origin slice-a-promotion-gate-hardening
```

- [ ] **Step 16.8: Open PR**

```bash
gh pr create --title "Slice A: promotion gate hardening (vm_ul6_strict)" --body "$(cat <<'EOF'
## Summary
- Add `vm_ul6_strict` validation profile (config/research/profiles/) with profile-driven blocking sub-gates.
- Add 7 new small-sample sub-gates: `min_sample_size`, `single_day_dominance`, `loo_day_sensitivity`, `outlier_trade_removal`, `day_bootstrap_ci`, `stationary_block_bootstrap`, `deflated_sharpe_maker`.
- Wire `_invoke_sub_gates()` aggregator in Gate C so blocking-subset failures contribute to `passed`.
- Require strict `ValidationProfile` for `promote_alpha()` (Gate D entry).
- Loose runs (no profile) preserve pre-Slice-A advisory-only behavior bit-for-bit.

## Why
R47-OE1 audit showed `+2,398 NTD / 39 fills / 1 winning day` reached production with all sub-gates advisory. Strict profile catches this fingerprint at Gate C; promotion path refuses to enter Gate D without a strict profile.

## Test plan
- [x] Unit: 7 new sub-gates + resampling primitives + profile loader (~30 tests)
- [x] Integration: `tests/integration/test_strict_profile_e2e.py` covers R47-kill, robust-pass, loose-parity
- [x] `make ci` green (lint + typecheck + coverage)
- [x] Manual: `load_profile('config/research/profiles/vm_ul6_strict.yaml')` returns 13 blocking gates

## Out of scope
- Slice B (maker realism, MakerEngine MtM, QueueDepletionFill upgrade)
- Slice C (replay-diff parity gate)
- Slice D (alpha factory MVP, kill ledger, correlation clustering)
- Threshold calibration from historical run data (follow-up)
EOF
)"
```

---

## Self-review

**Spec coverage:** every section of the plan-mode spec maps to a task:
- Strict profile loader → Task 2
- Resampling primitives → Task 1
- 7 new sub-gates → Tasks 4-10
- Sub-gate registry update → Task 11
- Gate C aggregator change → Task 12
- Strict profile YAML → Task 13
- Promotion-path enforcement → Task 14
- Integration tests (R47-kill, robust-pass, loose-parity) → Task 15
- `make ci` + manual smokes → Task 16

**Type/name consistency:**
- Gate `name` attribute is snake_case across all 7 new gates (matches existing convention).
- `ValidationProfile.thresholds_for(strategy_type=...)` keyword-only and used identically in Tasks 12, 15.
- `_invoke_sub_gates()` returns `(advisory: list, blocking: dict | None)` everywhere.
- `PromotionError` defined once in `promotion.py` and imported in tests.
- New gate class names referenced consistently in registry, profile YAML, and tests.

**Placeholders:** none. Every step has either inline code, exact commands, or both.

**Out-of-scope drift:** explicitly listed at the top of the plan and in the PR body. Replay-diff (Slice C), MakerEngine realism (Slice B), and alpha factory (Slice D) are deliberately deferred.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-03-slice-a-promotion-gate-hardening.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch with checkpoints for review.

Which approach?
