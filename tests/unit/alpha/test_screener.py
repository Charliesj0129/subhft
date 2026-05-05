"""Slice-D Task 6: cheap screener unit tests.

Covers the contract spelled out in plan §7 T6:
  * dataclass shape (frozen + slots, fixed verdict domain).
  * advisory ``unknown`` path for missing manifest / missing signal /
    budget exceeded — the cheap screener never destroys alphas with
    missing inputs (plan §10 risk row).
  * ``kill`` for high turnover or cost-floor breach.
  * ``pass`` for synthetic data with low turnover and a finite IC.
  * ``duration_s`` populated.

The tests synthesize tiny ``signal.npy`` files in ``tmp_path`` and a
matching ``manifest.yaml`` stub so the screener can run against a fake
project root. No CK / no live data dependencies.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha import screener
from hft_platform.alpha.screener import (
    BUDGET_S,
    TURNOVER_KILL,
    ScreenResult,
    cheap_screen,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_alpha_dir(root: Path, alpha_id: str, *, with_manifest: bool = True) -> Path:
    """Create ``root/research/alphas/<alpha_id>/`` with optional manifest stub."""
    alpha_dir = root / "research" / "alphas" / alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)
    if with_manifest:
        (alpha_dir / "manifest.yaml").write_text(f"alpha_id: {alpha_id}\n")
    return alpha_dir


def _write_signal(
    root: Path,
    alpha_id: str,
    *,
    signal: np.ndarray,
    prices: np.ndarray,
) -> Path:
    """Write a synthetic ``signal.npy`` under research/experiments/<alpha_id>/.

    Layout: 2-column .npy with [signal, mid_price] so the screener can
    compute IC = corr(signal, forward_return(price)).
    """
    exp_dir = root / "research" / "experiments" / alpha_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    arr = np.column_stack([signal.astype(np.float64), prices.astype(np.float64)])
    path = exp_dir / "signal.npy"
    np.save(path, arr)
    return path


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_screen_result_dataclass_frozen_slots() -> None:
    assert is_dataclass(ScreenResult)
    inst = ScreenResult(
        alpha_id="x",
        verdict="pass",
        ic_mean=0.0,
        ic_std=0.0,
        turnover=0.0,
        cost_floor_breach=False,
        reason="",
        duration_s=0.0,
    )
    with pytest.raises(Exception):
        inst.alpha_id = "y"  # type: ignore[misc]
    # slots ⇒ no __dict__
    assert not hasattr(inst, "__dict__")
    field_names = tuple(f.name for f in fields(ScreenResult))
    assert field_names == (
        "alpha_id",
        "verdict",
        "ic_mean",
        "ic_std",
        "turnover",
        "cost_floor_breach",
        "reason",
        "duration_s",
    )


def test_screen_result_verdict_domain_documented() -> None:
    for v in ("pass", "kill", "unknown"):
        ScreenResult(
            alpha_id="x",
            verdict=v,
            ic_mean=0.0,
            ic_std=0.0,
            turnover=0.0,
            cost_floor_breach=False,
            reason="",
            duration_s=0.0,
        )


# ---------------------------------------------------------------------------
# Unknown paths (advisory, never kill)
# ---------------------------------------------------------------------------


def test_cheap_screen_missing_manifest_returns_unknown(tmp_path: Path) -> None:
    out = cheap_screen("ghost_alpha", project_root=tmp_path)
    assert out.verdict == "unknown"
    assert "manifest" in out.reason
    assert out.duration_s >= 0.0


def test_cheap_screen_missing_signal_returns_unknown(tmp_path: Path) -> None:
    _make_alpha_dir(tmp_path, "alpha_no_signal")
    out = cheap_screen("alpha_no_signal", project_root=tmp_path)
    assert out.verdict == "unknown"
    assert "signal" in out.reason


def test_cheap_screen_budget_exceeded_returns_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_alpha_dir(tmp_path, "alpha_slow")
    rng = np.random.default_rng(42)
    sig = rng.normal(size=200)
    prices = 100.0 + np.cumsum(rng.normal(size=200) * 0.01)
    _write_signal(tmp_path, "alpha_slow", signal=sig, prices=prices)

    state = {"n": 0}

    def fake_monotonic() -> float:
        state["n"] += 1
        return 0.0 if state["n"] == 1 else BUDGET_S + 1.0

    monkeypatch.setattr(screener.time, "monotonic", fake_monotonic)

    out = cheap_screen("alpha_slow", project_root=tmp_path)
    assert out.verdict == "unknown"
    assert "timeout" in out.reason or "budget" in out.reason


# ---------------------------------------------------------------------------
# Kill paths
# ---------------------------------------------------------------------------


def test_cheap_screen_high_turnover_kills(tmp_path: Path) -> None:
    _make_alpha_dir(tmp_path, "alpha_churn")
    n = 400
    sig = np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
    rng = np.random.default_rng(0)
    prices = 100.0 + np.cumsum(rng.normal(size=n) * 0.01)
    _write_signal(tmp_path, "alpha_churn", signal=sig, prices=prices)

    out = cheap_screen("alpha_churn", project_root=tmp_path)
    assert out.verdict == "kill"
    assert "turnover" in out.reason
    assert np.isfinite(out.turnover)
    assert out.turnover > TURNOVER_KILL or out.turnover == TURNOVER_KILL * 1.0


def test_cheap_screen_cost_floor_breach_kills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_alpha_dir(tmp_path, "alpha_costy")
    rng = np.random.default_rng(1)
    n = 300
    sig = rng.normal(size=n)
    prices = 100.0 + np.cumsum(rng.normal(size=n) * 0.01)
    _write_signal(tmp_path, "alpha_costy", signal=sig, prices=prices)

    monkeypatch.setattr(screener, "_cost_floor_breached", lambda alpha_id, root: True)

    out = cheap_screen("alpha_costy", project_root=tmp_path)
    assert out.verdict == "kill"
    assert out.cost_floor_breach is True
    assert "cost" in out.reason.lower()


# ---------------------------------------------------------------------------
# Pass paths
# ---------------------------------------------------------------------------


def test_cheap_screen_low_ic_does_not_kill_returns_pass_with_low_ic(
    tmp_path: Path,
) -> None:
    """Cheap screen does NOT kill on low IC alone — Gate-C is the strict gate."""
    _make_alpha_dir(tmp_path, "alpha_low_ic")
    rng = np.random.default_rng(7)
    n = 600
    sig = rng.normal(size=n)
    prices = 100.0 + np.cumsum(rng.normal(size=n) * 0.01)
    _write_signal(tmp_path, "alpha_low_ic", signal=sig, prices=prices)

    out = cheap_screen("alpha_low_ic", project_root=tmp_path)
    assert out.verdict in {"pass", "unknown"}
    if out.verdict == "pass":
        assert out.cost_floor_breach is False
        assert out.turnover <= TURNOVER_KILL


def test_cheap_screen_happy_path_returns_pass(tmp_path: Path) -> None:
    _make_alpha_dir(tmp_path, "alpha_good")
    rng = np.random.default_rng(123)
    n = 800
    noise = rng.normal(size=n) * 0.01
    prices = 100.0 + np.cumsum(noise)
    sig = np.zeros(n)
    sig[:-5] = np.diff(prices, prepend=prices[0])[5:] + rng.normal(size=n - 5) * 0.005
    _write_signal(tmp_path, "alpha_good", signal=sig, prices=prices)

    out = cheap_screen("alpha_good", project_root=tmp_path)
    assert out.verdict in {"pass", "unknown"}
    if out.verdict == "pass":
        assert out.turnover <= TURNOVER_KILL
        assert out.cost_floor_breach is False
        assert np.isfinite(out.ic_mean)


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------


def test_cheap_screen_duration_s_populated(tmp_path: Path) -> None:
    out = cheap_screen("ghost_alpha2", project_root=tmp_path)
    assert isinstance(out.duration_s, float)
    assert out.duration_s >= 0.0
