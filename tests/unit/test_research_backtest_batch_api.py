from __future__ import annotations

import hashlib
import json

import numpy as np

from research.backtest.hbt_runner import BacktestConfig, ResearchBacktestRunner
from research.registry.schemas import AlphaManifest
from research.registry.scorecard import compute_scorecard


class _RowOnlyAlpha:
    def __init__(self) -> None:
        self._last = 0.0
        self.calls = 0
        self.manifest = AlphaManifest(
            alpha_id="row_only",
            hypothesis="test",
            formula="dummy",
            paper_refs=(),
            data_fields=("ofi", "qty"),
            complexity="O(n)",
        )

    def reset(self) -> None:
        self._last = 0.0

    def update(self, **kwargs) -> float:
        self.calls += 1
        ofi = float(kwargs.get("ofi", 0.0))
        qty = float(kwargs.get("qty", 1.0))
        self._last = (0.5 * self._last) + (ofi / max(1.0, qty))
        return self._last

    def get_signal(self) -> float:
        return self._last


def test_research_backtest_auto_batch_adapter(tmp_path):
    n = 32
    data = np.zeros(n, dtype=[("mid", "f8"), ("ofi", "f8"), ("qty", "i8")])
    data["mid"] = np.linspace(100, 101, n)
    data["ofi"] = np.linspace(-1, 1, n)
    data["qty"] = np.arange(1, n + 1)
    path = tmp_path / "research.npz"
    np.savez(path, data=data)

    alpha = _RowOnlyAlpha()
    runner = ResearchBacktestRunner(alpha, BacktestConfig(data_paths=[str(path)]))

    assert callable(getattr(runner.alpha, "update_batch", None))

    result = runner.run()

    assert result.signals.shape == (n,)
    assert np.isfinite(result.signals).all()
    assert alpha.calls == n


def test_research_backtest_uses_all_data_paths(tmp_path):
    n1 = 16
    n2 = 24

    data1 = np.zeros(n1, dtype=[("mid", "f8"), ("ofi", "f8"), ("qty", "i8"), ("local_ts", "i8")])
    data1["mid"] = np.linspace(100, 101, n1)
    data1["ofi"] = np.linspace(-1, 1, n1)
    data1["qty"] = np.arange(1, n1 + 1)
    data1["local_ts"] = np.arange(n1, dtype=np.int64) * 1_000_000

    data2 = np.zeros(n2, dtype=[("mid", "f8"), ("ofi", "f8"), ("qty", "i8"), ("local_ts", "i8")])
    data2["mid"] = np.linspace(101, 99, n2)
    data2["ofi"] = np.linspace(1, -1, n2)
    data2["qty"] = np.arange(1, n2 + 1)
    data2["local_ts"] = np.arange(n2, dtype=np.int64) * 1_000_000

    p1 = tmp_path / "d1.npz"
    p2 = tmp_path / "d2.npz"
    np.savez(p1, data=data1)
    np.savez(p2, data=data2)

    alpha = _RowOnlyAlpha()
    runner = ResearchBacktestRunner(alpha, BacktestConfig(data_paths=[str(p1), str(p2)]))
    result = runner.run()

    assert result.signals.shape == (n1 + n2,)
    assert result.positions.shape == (n1 + n2,)
    assert np.isfinite(result.equity_curve).all()
    assert result.latency_profile["model_applied"] is True


def test_extract_price_accepts_current_mid_alias():
    alpha = _RowOnlyAlpha()
    runner = ResearchBacktestRunner(alpha, BacktestConfig(data_paths=[]))
    data = np.zeros(4, dtype=[("current_mid", "f8"), ("trade_vol", "f8")])
    data["current_mid"] = np.array([100.0, 100.2, 100.1, 100.3], dtype=np.float64)
    data["trade_vol"] = np.array([3.0, 4.0, 2.0, 5.0], dtype=np.float64)

    px = runner._extract_price(data)
    vol = runner._extract_volume(data, len(px))

    assert np.allclose(px, data["current_mid"])
    assert np.allclose(vol, data["trade_vol"])


def test_extract_price_accepts_bid_px_ask_px_alias():
    alpha = _RowOnlyAlpha()
    runner = ResearchBacktestRunner(alpha, BacktestConfig(data_paths=[]))
    data = np.zeros(4, dtype=[("bid_px", "f8"), ("ask_px", "f8"), ("trade_vol", "f8")])
    data["bid_px"] = np.array([99.9, 100.1, 100.0, 100.2], dtype=np.float64)
    data["ask_px"] = np.array([100.1, 100.3, 100.2, 100.4], dtype=np.float64)
    data["trade_vol"] = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float64)

    px = runner._extract_price(data)
    expected_mid = (data["bid_px"] + data["ask_px"]) / 2.0

    assert np.allclose(px, expected_mid)


def test_compute_scorecard_reads_data_meta_provenance(tmp_path):
    data_path = tmp_path / "synthetic.npy"
    np.save(data_path, np.asarray([1.0, 2.0, 3.0], dtype=np.float64))
    fingerprint = hashlib.sha256(data_path.read_bytes()[:1024]).hexdigest()

    meta_path = tmp_path / "synthetic.npy.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "rng_seed": 42,
                "generator_script": "research/tools/synth_lob_gen.py",
                "data_ul": 5,
            }
        ),
        encoding="utf-8",
    )

    scorecard = compute_scorecard(
        {"sharpe_oos": 1.2, "regime_ic": {"volatile": 0.08}},
        data_meta_path=str(meta_path),
    )
    assert scorecard.rng_seed == 42
    assert scorecard.generator_script == "research/tools/synth_lob_gen.py"
    assert scorecard.data_ul == 5
    assert scorecard.data_fingerprint == fingerprint
    assert scorecard.regime_ic["volatile"] == 0.08
