"""``scripts/latency/shioaji_rtt_bulk_probe.py`` stamps in timezone-aware UTC and times the run monotonically.

The report's ``ts_utc`` came from ``datetime.utcnow()`` (deprecated since Python 3.12, naive) and ``elapsed_s``
from ``time.time()``, which jumps when the wall clock is stepped. The probe needs a Shioaji login, so these tests
drive ``main()`` with a fake SDK and a fake ``time`` module (the script's own alias, not the global one).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")
pytest.importorskip("shioaji")

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "latency" / "shioaji_rtt_bulk_probe.py"


def _load(monkeypatch: pytest.MonkeyPatch):
    name = "shioaji_rtt_bulk_probe_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)  # @dataclass resolves its module through sys.modules
    spec.loader.exec_module(module)
    return module


class _FakeFutures:
    def __getitem__(self, code: str) -> SimpleNamespace:
        return SimpleNamespace(code=code)


class _FakeApi:
    def __init__(self) -> None:
        self.Contracts = SimpleNamespace(Futures=_FakeFutures())

    def login(self, **_kwargs) -> None:
        return None


class _FakeTime:
    """Only ``monotonic`` and ``sleep``: a call to ``time.time()`` raises AttributeError."""

    def __init__(self, ticks: list[float]) -> None:
        self._ticks = iter(ticks)

    def monotonic(self) -> float:
        return next(self._ticks)

    def sleep(self, _seconds: float) -> None:
        return None


def _run_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ticks: list[float]) -> dict:
    module = _load(monkeypatch)
    monkeypatch.setattr(module, "time", _FakeTime(ticks))
    monkeypatch.setattr(module, "sj", SimpleNamespace(Shioaji=lambda simulation: _FakeApi()))
    monkeypatch.setattr(module, "_probe_one_cycle", lambda *a, **k: (100, 200, 300, object()))
    monkeypatch.setattr(module, "_trade_filled", lambda trade: False)
    monkeypatch.setenv("SHIOAJI_API_KEY", "k")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
    out = tmp_path / "report.json"
    monkeypatch.setattr(
        sys, "argv", ["probe", "--mode", "sim", "--iters", "2", "--warmup", "0", "--sleep", "0", "--out", str(out)]
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert module.main() == 0
    return json.loads(out.read_text())


def test_elapsed_comes_from_the_monotonic_clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = _run_probe(monkeypatch, tmp_path, ticks=[100.0, 103.5])

    assert report["meta"]["elapsed_s"] == 3.5


def test_report_timestamp_is_utc_with_a_z_suffix_and_no_deprecation_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _run_probe(monkeypatch, tmp_path, ticks=[0.0, 1.0])

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z", report["meta"]["ts_utc"])
