"""A shortlisted candidate as a Gate C alpha: its panel signal replayed tick by tick.

The candidate loop scores a candidate on event-clock panels
(``panels.build_panel``) built from the same ``*_l2.hftbt.npz`` day files that
Gate C replays. :class:`CandidateGateCAlpha` evaluates the candidate's
validator-canonical signal on those panels once, with the evaluator's rules
(``evaluator.evaluate_day``), and answers each ``update(local_ts=...)`` from
the bridge with the signal of the latest panel row whose ``local_ts`` is at or
before the tick: the last batch the replay has fully delivered. Nothing is
re-implemented for Gate C, so the signal Gate C trades is the one the loop
scored.

Rules carried over from the evaluator:

* a regime filter that is false, or a NaN signal, is no signal (0.0);
* ``expected_sign: negative`` flips the sign, so a positive value always means buy;
* a ``trade_imbalance`` candidate on a day whose panel is not ``dir_clean``
  (ClickHouse ``trade_direction`` coverage below ``DIR_CLEAN_THRESHOLD``) has no
  signal that day, where the evaluator skips the day.

The alpha is a pure lookup: ``reset`` keeps the precomputed series.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from hft_platform.contracts.alpha import AlphaManifest, AlphaStatus
from research.candidate_loop.compiler import evaluate_regime, evaluate_signal
from research.candidate_loop.panels import Panel
from research.candidate_loop.validator import ValidCandidate


class CandidateGateCAlpha:
    """Gate C alpha for one validated candidate over a fixed set of day panels."""

    def __init__(self, valid: ValidCandidate, panels: Sequence[Panel], *, instrument: str) -> None:
        if not panels:
            raise ValueError("a candidate alpha needs at least one day panel")
        self._valid = valid
        self._manifest = _manifest(valid, instrument=instrument)
        ts_parts: list[np.ndarray] = []
        signal_parts: list[np.ndarray] = []
        self.days_without_signal: list[str] = []
        for panel in panels:
            local_ts, signal = day_signal(valid, panel)
            if not np.any(signal != 0.0):
                self.days_without_signal.append(str(panel.meta.get("day", "")))
            ts_parts.append(local_ts)
            signal_parts.append(signal)
        local_ts = np.concatenate(ts_parts)
        order = np.argsort(local_ts, kind="stable")
        self._local_ts = local_ts[order]
        self._signal = np.concatenate(signal_parts)[order]
        self._last = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    def update(self, *args: Any, **kwargs: Any) -> float:
        ts = kwargs.get("local_ts")
        if ts is None:
            raise TypeError("CandidateGateCAlpha.update needs local_ts")
        idx = int(np.searchsorted(self._local_ts, int(ts), side="right")) - 1
        self._last = float(self._signal[idx]) if idx >= 0 else 0.0
        return self._last

    def reset(self) -> None:
        self._last = 0.0

    def get_signal(self) -> float:
        return self._last


def day_signal(valid: ValidCandidate, panel: Panel) -> tuple[np.ndarray, np.ndarray]:
    """One day's ``(local_ts, signal)``, signed so positive means buy and 0.0 means no signal."""
    cols = panel.columns
    if not cols or cols["local_ts"].size == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64)
    local_ts = cols["local_ts"].astype(np.int64)
    if valid.uses_trade_imbalance and not bool(panel.meta.get("dir_clean", False)):
        return local_ts, np.zeros(local_ts.size, dtype=np.float64)
    signal = np.asarray(evaluate_signal(valid.signal_ast, cols), dtype=np.float64)
    if valid.regime_ast is not None:
        signal = np.where(evaluate_regime(valid.regime_ast, cols), signal, np.nan)
    if valid.candidate.expected_sign == "negative":
        signal = -signal
    return local_ts, np.where(np.isfinite(signal), signal, 0.0)


def _manifest(valid: ValidCandidate, *, instrument: str) -> AlphaManifest:
    cand = valid.candidate
    formula = cand.signal_formula if not cand.regime_filter else f"{cand.signal_formula} if {cand.regime_filter}"
    return AlphaManifest(
        alpha_id=valid.alpha_id,
        hypothesis=cand.hypothesis,
        formula=formula,
        paper_refs=(),
        data_fields=tuple(sorted(feature.name for feature in cand.features)),
        complexity="O(1)",
        status=AlphaStatus.DRAFT,
        latency_profile=None,
        strategy_type="taker",
        instrument=instrument,
    )
