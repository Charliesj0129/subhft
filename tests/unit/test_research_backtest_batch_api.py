from __future__ import annotations

import numpy as np

from research.backtest.hbt_runner import BacktestConfig, ResearchBacktestRunner
from research.registry.schemas import AlphaManifest


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
