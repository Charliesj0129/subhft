"""CandidateGateCAlpha: the Gate C replay sees the signal the candidate loop scored."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest

from research.candidate_loop.compiler import evaluate_regime, evaluate_signal
from research.candidate_loop.gate_c_alpha import CandidateGateCAlpha, day_signal
from research.candidate_loop.panels import Panel
from research.candidate_loop.validator import ValidCandidate, validate_line


def _candidate(**overrides: object) -> ValidCandidate:
    base: dict = {
        "name": "obi_gate_c_probe",
        "family": "order_book_imbalance",
        "hypothesis": "L1 imbalance predicts the next-event mid move on synthetic data.",
        "features": [{"name": "imb_l1", "formula": "book_imbalance(1)"}],
        "signal_formula": "imb_l1",
        "label": "future_mid_return(horizon='1_events')",
        "horizon": "1_events",
        "expected_sign": "positive",
    }
    base.update(overrides)
    result = validate_line(json.dumps(base), seen_hashes=set())
    assert isinstance(result, ValidCandidate), getattr(result, "detail", "")
    return result


def _panel(n: int = 400, *, seed: int = 7, day: str = "2026-04-13", start_ns: int = 0, dir_clean: bool = True) -> Panel:
    rng = np.random.default_rng(seed)
    x = rng.uniform(-0.9, 0.9, size=n)
    local_ts = start_ns + np.arange(n, dtype=np.int64) * 1_000_000
    mid = 100.0 + np.cumsum(rng.normal(scale=0.1, size=n))
    cols: dict[str, np.ndarray] = {
        "exch_ts": local_ts.copy(),
        "local_ts": local_ts,
        "mid": mid,
        "microprice": mid.copy(),
        "spread_ticks": rng.integers(1, 4, size=n).astype(np.float64),
        "trade_buy_qty": np.cumsum(rng.integers(0, 3, size=n)).astype(np.float64),
        "trade_sell_qty": np.cumsum(rng.integers(0, 3, size=n)).astype(np.float64),
    }
    for lvl in range(1, 6):
        cols[f"bid_qty_{lvl}"] = (2.0 + x) if lvl == 1 else np.full(n, 1.0)
        cols[f"ask_qty_{lvl}"] = (2.0 - x) if lvl == 1 else np.full(n, 1.0)
        cols[f"bid_px_{lvl}"] = mid - 0.5 * lvl
        cols[f"ask_px_{lvl}"] = mid + 0.5 * lvl
    return Panel(columns=cols, meta={"day": day, "symbol": "TXFD6", "tick_size": 1.0, "dir_clean": dir_clean})


def test_alpha_returns_the_evaluator_signal_at_every_panel_row() -> None:
    valid = _candidate()
    panel = _panel()
    alpha = CandidateGateCAlpha(valid, [panel], instrument="TXFD6")

    replayed = np.array([alpha.update(local_ts=int(ts)) for ts in panel.columns["local_ts"]])

    expected = evaluate_signal(valid.signal_ast, panel.columns)
    np.testing.assert_array_equal(replayed, np.where(np.isfinite(expected), expected, 0.0))
    assert np.count_nonzero(replayed) > 0


def test_alpha_between_rows_holds_the_last_delivered_batch() -> None:
    panel = _panel(n=5)
    alpha = CandidateGateCAlpha(_candidate(), [panel], instrument="TXFD6")
    _, signal = day_signal(_candidate(), panel)
    ts = panel.columns["local_ts"]

    assert alpha.update(local_ts=int(ts[0]) - 1) == 0.0
    assert alpha.update(local_ts=int(ts[2]) + 500_000) == signal[2]
    assert alpha.update(local_ts=int(ts[3]) - 1) == signal[2]
    assert alpha.get_signal() == signal[2]


def test_alpha_regime_off_rows_carry_no_signal() -> None:
    valid = _candidate(regime_filter="spread_ticks() <= 1")
    panel = _panel()
    alpha = CandidateGateCAlpha(valid, [panel], instrument="TXFD6")

    replayed = np.array([alpha.update(local_ts=int(ts)) for ts in panel.columns["local_ts"]])

    regime = evaluate_regime(valid.regime_ast, panel.columns)
    assert np.all(replayed[~regime] == 0.0)
    assert np.count_nonzero(replayed[regime]) > 0


def test_alpha_negative_expected_sign_flips_the_signal() -> None:
    panel = _panel()
    positive = CandidateGateCAlpha(_candidate(), [panel], instrument="TXFD6")
    negative = CandidateGateCAlpha(_candidate(expected_sign="negative"), [panel], instrument="TXFD6")

    for ts in panel.columns["local_ts"][::37]:
        assert negative.update(local_ts=int(ts)) == -positive.update(local_ts=int(ts))


def test_alpha_trade_imbalance_on_a_dirty_direction_day_has_no_signal() -> None:
    valid = _candidate(
        name="ti_gate_c_probe",
        family="trade_flow",
        hypothesis="Signed trade flow over 50 events predicts the next-event mid move.",
        features=[{"name": "trade_imb", "formula": "trade_imbalance('50_events')"}],
        signal_formula="trade_imb",
    )
    clean = _panel(day="2026-04-13", dir_clean=True)
    dirty = _panel(day="2026-04-14", start_ns=10**12, dir_clean=False, seed=8)
    alpha = CandidateGateCAlpha(valid, [clean, dirty], instrument="TXFD6")

    assert alpha.days_without_signal == ["2026-04-14"]
    assert all(alpha.update(local_ts=int(ts)) == 0.0 for ts in dirty.columns["local_ts"])
    assert any(alpha.update(local_ts=int(ts)) != 0.0 for ts in clean.columns["local_ts"])


def test_alpha_reads_each_day_from_its_own_panel() -> None:
    first = _panel(day="2026-04-13", seed=1)
    second = _panel(day="2026-04-14", start_ns=10**12, seed=2)
    alpha = CandidateGateCAlpha(_candidate(), [second, first], instrument="TXFD6")

    _, first_signal = day_signal(_candidate(), first)
    _, second_signal = day_signal(_candidate(), second)
    assert alpha.update(local_ts=int(first.columns["local_ts"][10])) == first_signal[10]
    assert alpha.update(local_ts=int(second.columns["local_ts"][10])) == second_signal[10]


def test_alpha_manifest_is_a_draft_taker_on_the_instrument() -> None:
    alpha = CandidateGateCAlpha(_candidate(), [_panel()], instrument="TXFD6")

    assert alpha.manifest.strategy_type == "taker"
    assert alpha.manifest.instrument == "TXFD6"
    assert alpha.manifest.data_fields == ("imb_l1",)


def test_alpha_needs_local_ts_and_a_panel() -> None:
    alpha = CandidateGateCAlpha(_candidate(), [_panel()], instrument="TXFD6")
    with pytest.raises(TypeError):
        alpha.update(bid_px=1.0)
    with pytest.raises(ValueError):
        CandidateGateCAlpha(_candidate(), [], instrument="TXFD6")


GOLDEN_NPZ = (
    Path(__file__).resolve().parents[4] / "research" / "data" / "raw" / "txfd6" / "TXFD6_2026-04-13_l2.hftbt.npz"
)


class _RecordingAlpha(CandidateGateCAlpha):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]  # forwarded as given
        self.calls: list[int] = []

    def update(self, *args: object, **kwargs: object) -> float:
        self.calls.append(int(kwargs["local_ts"]))  # type: ignore[call-overload]  # bridge passes an int
        return super().update(*args, **kwargs)


@pytest.mark.skipif(not GOLDEN_NPZ.exists(), reason="golden-day NPZ not present")
def test_alpha_on_the_golden_day_sees_the_replay_on_the_panel_clock(tmp_path) -> None:
    pytest.importorskip("hftbacktest")
    from research.backtest.cost_models import TAIFEXCost
    from research.backtest.taker_sim import TakerSim
    from research.candidate_loop.panels import load_l2_events, replay_to_panel

    data, _ = load_l2_events(GOLDEN_NPZ)
    data = data[:300_000]  # bounded runtime
    day_file = tmp_path / "golden_head.npz"
    np.savez_compressed(day_file, data=data)
    panel = Panel(columns=replay_to_panel(data, tick_size=1.0), meta={"day": "2026-04-13", "dir_clean": True})
    alpha = _RecordingAlpha(_candidate(), [panel], instrument="TXFD6")
    free = TAIFEXCost(instrument="TXFD6", commission_pts_per_side=0.0, tax_pts_per_side=0.0, point_value_nwd=200)

    TakerSim(alpha=alpha, instrument="TXFD6", cost_model=free, latency_us=1000).run_day(
        dt.date(2026, 4, 13), str(day_file)
    )

    calls = np.asarray(alpha.calls, dtype=np.int64)
    panel_ts = np.sort(panel.columns["local_ts"].astype(np.int64))
    assert calls.size > 1000
    # The replay clock is the panel clock: every tick falls inside the panel's span and
    # over half land exactly on the end of a panel batch (a different clock would match
    # none). The bridge also ticks inside a batch; those ticks read the previous batch.
    assert panel_ts[0] <= calls.min() and calls.max() <= panel_ts[-1]
    assert np.isin(calls, panel_ts).mean() > 0.5
    replayed = np.array([alpha.update(local_ts=int(ts)) for ts in calls])
    assert np.count_nonzero(replayed) > 0.5 * calls.size
