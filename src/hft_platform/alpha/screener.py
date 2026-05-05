"""Slice-D Task 6: cheap screener (IC + turnover + cost-floor pre-check).

Cheap, single-alpha pre-filter that runs *before* Gate-C bootstrap. It is
deliberately lenient on IC (Gate-C is the strict gate) but kills hard on
two pathologies that make downstream gates pointless:

  * **Turnover > 2.0/day** — sign-flip churn that no reasonable cost
    structure survives.
  * **Cost-floor breach** — alphas whose expected per-fill PnL is below
    the configured floor (placeholder pending Slice B's
    ``cost_floor_per_fill_pts`` constant; see TODO below).

Verdict semantics (plan §7 T6, §10 risk row):

  * ``'pass'``  — IC ≥ ``IC_MIN_ABS`` *or* simply not-killable
                  (low IC alone is allowed; Gate-C will catch it).
  * ``'kill'``  — turnover > ``TURNOVER_KILL`` *or* cost-floor breach.
                  Reason populated.
  * ``'unknown'`` — manifest missing / signal missing / IC uncomputable
                  (insufficient observations) / 60-second budget
                  exceeded. Advisory; never converted to ``'kill'``,
                  protecting alphas with missing input data from
                  destructive auto-kill.

Module is offline-only (``research/`` and ``alpha/`` are permitted to use
``float`` per ``.agent/rules/25-architecture-governance.md`` §11). Hot-path
laws do not apply here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tunables (defaults documented in plan §7 T6)
# ---------------------------------------------------------------------------

#: IC magnitude below which the screener emits an *advisory* low-IC note in
#: the reason field but still returns ``verdict='pass'`` — Gate-C is the
#: strict IC gate, not the cheap screener.
IC_MIN_ABS: float = 0.005

#: Sign-flip turnover at or above which the screener returns
#: ``verdict='kill'``. Same per-day convention as
#: ``research/tools/feature_screener.py``. Note: ``mean(|Δsign|)``
#: saturates at 2.0 (perfect alternation), so the kill condition uses
#: ``>=`` not ``>`` — a perfectly-alternating signal is the worst case
#: and must be killable.
TURNOVER_KILL: float = 2.0

#: Cost-floor breach threshold (points). Placeholder until Slice B exposes
#: ``cost_floor_per_fill_pts``; see ``_cost_floor_breached`` for the TODO.
COST_FLOOR_PTS: float = 0.0

#: Hard wall-clock budget per ``cheap_screen`` call. Plan DoD-D1: each
#: alpha must produce a ``ScreenResult`` within 60 s.
BUDGET_S: float = 60.0

#: Closed verdict domain — a perfect 3-element set. Anything outside this
#: is a bug; ``Literal`` makes that machine-checkable.
Verdict = Literal["pass", "kill", "unknown"]

#: Forward-return horizon (ticks) — mirrors
#: ``research/tools/feature_screener.py:_FORWARD_HORIZON``.
_FORWARD_HORIZON: int = 5

#: Minimum usable observations after warmup/NaN drop — mirrors
#: ``research/tools/feature_screener.py:_MIN_OBS``.
_MIN_OBS: int = 50


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """One alpha's cheap-screen verdict (plan §7 T6 API)."""

    alpha_id: str
    verdict: Verdict  # 'pass' | 'kill' | 'unknown'
    ic_mean: float
    ic_std: float
    turnover: float
    cost_floor_breach: bool
    reason: str  # populated when verdict in {'kill','unknown'}
    duration_s: float


# ---------------------------------------------------------------------------
# IC math (ported from research/tools/feature_screener.py:103-138)
# ---------------------------------------------------------------------------
# We port rather than import because feature_screener.py mangles sys.path at
# import time and its public API screens *all* feature columns, not a single
# alpha signal. The math is identical to keep behaviour consistent across
# screeners.


def _forward_returns(prices: np.ndarray, horizon: int = _FORWARD_HORIZON) -> np.ndarray:
    """Log forward returns at fixed tick horizon. NaN for warmup tail."""
    fwd = np.full(len(prices), np.nan)
    base = prices[:-horizon].astype(np.float64)
    safe = prices[horizon:].astype(np.float64)
    mask = (base > 0.0) & (safe > 0.0)
    fwd[:-horizon][mask] = np.log(safe[mask] / base[mask])
    return fwd


def _ic(signal: np.ndarray, returns: np.ndarray) -> float:
    """Pearson correlation of signal vs forward returns. NaN if too few obs."""
    mask = np.isfinite(signal) & np.isfinite(returns)
    if mask.sum() < _MIN_OBS:
        return float("nan")
    s, r = signal[mask], returns[mask]
    if s.std() < 1e-12 or r.std() < 1e-12:
        return 0.0
    return float(np.corrcoef(s, r)[0, 1])


def _turnover(signal: np.ndarray) -> float:
    """Sign-flip turnover: mean(|Δsign(signal)|). NaN if too few obs."""
    s = signal[np.isfinite(signal)]
    if len(s) < _MIN_OBS:
        return float("nan")
    return float(np.abs(np.diff(np.sign(s))).mean())


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _signal_path(alpha_id: str, root: Path) -> Path:
    return root / "research" / "experiments" / alpha_id / "signal.npy"


def _manifest_path(alpha_id: str, root: Path) -> Path:
    return root / "research" / "alphas" / alpha_id / "manifest.yaml"


def _load_signal(path: Path) -> np.ndarray:
    """Load a 2-column ``signal.npy`` ([signal, mid_price]) as float64."""
    raw = np.load(path, allow_pickle=False)
    if raw.dtype.names is not None:
        arr = np.column_stack([raw[f].astype(np.float64) for f in raw.dtype.names])
    else:
        arr = raw.astype(np.float64)
    return arr.reshape(-1, 1) if arr.ndim == 1 else arr


# ---------------------------------------------------------------------------
# Cost-floor pre-check (Slice B integration point)
# ---------------------------------------------------------------------------


def _cost_floor_breached(alpha_id: str, root: Path) -> bool:
    """Return True iff the alpha's expected per-fill PnL is below the floor.

    TODO(slice-b): wire to ``cost_floor_per_fill_pts`` once Slice B
    publishes a stable accessor. For now we read an optional sidecar at
    ``research/experiments/<alpha_id>/cost_floor.txt`` containing a single
    float (points). If the file exists and the value is < ``COST_FLOOR_PTS``,
    we report a breach. If absent, we report no breach (no information ⇒
    do not kill on this axis).
    """
    sidecar = root / "research" / "experiments" / alpha_id / "cost_floor.txt"
    if not sidecar.exists():
        return False
    try:
        value = float(sidecar.read_text().strip())
    except (OSError, ValueError):
        return False
    return value < COST_FLOOR_PTS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _result(
    alpha_id: str,
    *,
    verdict: Verdict,
    ic_mean: float = float("nan"),
    ic_std: float = float("nan"),
    turnover: float = float("nan"),
    cost_floor_breach: bool = False,
    reason: str = "",
    started: float,
) -> ScreenResult:
    duration = max(0.0, time.monotonic() - started)
    return ScreenResult(
        alpha_id=alpha_id,
        verdict=verdict,
        ic_mean=ic_mean,
        ic_std=ic_std,
        turnover=turnover,
        cost_floor_breach=cost_floor_breach,
        reason=reason,
        duration_s=duration,
    )


def _budget_exceeded(started: float) -> bool:
    return (time.monotonic() - started) > BUDGET_S


def cheap_screen(
    alpha_id: str,
    *,
    project_root: Path = Path("."),
    ic_min_abs: float | None = None,
    turnover_kill: float | None = None,
) -> ScreenResult:
    """Run the cheap screener for one alpha.

    Args:
        alpha_id: research alpha identifier (matches dir name under
            ``research/alphas/``).
        project_root: repo root; manifest is looked up at
            ``project_root/research/alphas/<alpha_id>/manifest.yaml`` and
            signal data at
            ``project_root/research/experiments/<alpha_id>/signal.npy``.
        ic_min_abs: optional override for the advisory low-IC floor
            (default: module-level :data:`IC_MIN_ABS`).
        turnover_kill: optional override for the turnover kill threshold
            (default: module-level :data:`TURNOVER_KILL`).

    Returns:
        ``ScreenResult`` with verdict in ``{'pass', 'kill', 'unknown'}``.
        ``'unknown'`` is advisory — callers MUST NOT downgrade it to a kill.
    """
    eff_ic_min_abs = float(IC_MIN_ABS if ic_min_abs is None else ic_min_abs)
    eff_turnover_kill = float(TURNOVER_KILL if turnover_kill is None else turnover_kill)
    started = time.monotonic()
    logger.debug("cheap_screen_start", alpha_id=alpha_id)

    # Manifest gate.
    manifest = _manifest_path(alpha_id, project_root)
    if not manifest.exists():
        return _result(
            alpha_id,
            verdict="unknown",
            reason=f"manifest_not_found:{manifest.as_posix()}",
            started=started,
        )

    if _budget_exceeded(started):
        return _result(
            alpha_id,
            verdict="unknown",
            reason="budget_exceeded:timeout_after_manifest",
            started=started,
        )

    # Signal gate.
    sig_path = _signal_path(alpha_id, project_root)
    if not sig_path.exists():
        return _result(
            alpha_id,
            verdict="unknown",
            reason=f"signal_not_found:{sig_path.as_posix()}",
            started=started,
        )

    if _budget_exceeded(started):
        return _result(
            alpha_id,
            verdict="unknown",
            reason="budget_exceeded:timeout_before_signal_load",
            started=started,
        )

    # Load + decompose into [signal, prices].
    try:
        arr = _load_signal(sig_path)
    except (OSError, ValueError) as exc:
        return _result(
            alpha_id,
            verdict="unknown",
            reason=f"signal_load_failed:{exc.__class__.__name__}",
            started=started,
        )

    if arr.shape[1] < 2:
        return _result(
            alpha_id,
            verdict="unknown",
            reason="signal_shape_invalid:expected_at_least_2_cols",
            started=started,
        )

    signal = arr[:, 0]
    prices = arr[:, 1]

    if _budget_exceeded(started):
        return _result(
            alpha_id,
            verdict="unknown",
            reason="budget_exceeded:timeout_before_compute",
            started=started,
        )

    # IC + turnover.
    fwd = _forward_returns(prices)
    ic_mean = _ic(signal, fwd)
    turnover_v = _turnover(signal)

    if _budget_exceeded(started):
        return _result(
            alpha_id,
            verdict="unknown",
            ic_mean=ic_mean if np.isfinite(ic_mean) else float("nan"),
            turnover=turnover_v if np.isfinite(turnover_v) else float("nan"),
            reason="budget_exceeded:timeout_after_compute",
            started=started,
        )

    # If IC could not be computed at all, treat as unknown — we have no
    # evidence to kill or pass.
    if not np.isfinite(ic_mean) or not np.isfinite(turnover_v):
        return _result(
            alpha_id,
            verdict="unknown",
            ic_mean=ic_mean,
            turnover=turnover_v,
            reason="insufficient_observations",
            started=started,
        )

    # ic_std is not estimated by the cheap screener (single-pass IC); we
    # surface NaN here and let Gate-C's bootstrap fill it in.
    ic_std = float("nan")

    # Cost-floor pre-check.
    breach = _cost_floor_breached(alpha_id, project_root)

    # Kill conditions.
    if turnover_v >= eff_turnover_kill:
        return _result(
            alpha_id,
            verdict="kill",
            ic_mean=ic_mean,
            ic_std=ic_std,
            turnover=turnover_v,
            cost_floor_breach=breach,
            reason=f"turnover_above_kill_threshold:{turnover_v:.4f}>={eff_turnover_kill}",
            started=started,
        )
    if breach:
        return _result(
            alpha_id,
            verdict="kill",
            ic_mean=ic_mean,
            ic_std=ic_std,
            turnover=turnover_v,
            cost_floor_breach=True,
            reason="cost_floor_breach",
            started=started,
        )

    # Pass — even on low IC. Annotate reason if IC is below the advisory
    # floor (Gate-C will be the gate that actually kills on IC).
    reason = ""
    if abs(ic_mean) < eff_ic_min_abs:
        reason = f"low_ic_advisory:|ic|={abs(ic_mean):.4f}<{eff_ic_min_abs}"

    return _result(
        alpha_id,
        verdict="pass",
        ic_mean=ic_mean,
        ic_std=ic_std,
        turnover=turnover_v,
        cost_floor_breach=False,
        reason=reason,
        started=started,
    )


__all__ = [
    "BUDGET_S",
    "COST_FLOOR_PTS",
    "IC_MIN_ABS",
    "TURNOVER_KILL",
    "ScreenResult",
    "cheap_screen",
]
