from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import yaml

from hft_platform.cli._alpha import cmd_alpha_screen
from research.combinatorial.promote import promote_smma_candidate
from research.combinatorial.smma_dataset import BarDataset, save_governed_dataset
from research.combinatorial.smma_runner import Candidate, _candidate_id
from research.combinatorial.smma_screen import is_smma_screen_package, run_smma_screen


def _candidate() -> Candidate:
    identity = {
        "family": "smma",
        "root": "TXF",
        "timeframe_min": 60,
        "expression": "close_l3_atr14_distance",
        "horizon": "1h",
        "direction": 1,
        "threshold": 0.0,
    }
    return Candidate(
        candidate_id=_candidate_id(identity),
        root="TXF",
        timeframe_min=60,
        expression="close_l3_atr14_distance",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=20260726,
        complexity=1,
    )


def _dataset() -> BarDataset:
    start = date(2026, 3, 19)
    roots: list[str] = []
    timeframes: list[int] = []
    contracts: list[str] = []
    days: list[str] = []
    sessions: list[str] = []
    timestamps: list[int] = []
    prices: list[float] = []
    resets: list[bool] = []
    closes: list[bool] = []
    cursor = 0
    for day_index in range(40):
        day = (start + timedelta(days=day_index)).isoformat()
        for bar in range(4):
            roots.append("TXF")
            timeframes.append(60)
            contracts.append("TXFG6")
            days.append(day)
            sessions.append("day")
            timestamps.append(cursor * 3_600_000_000_000)
            prices.append(20_000.0 + np.sin(cursor / 4.0) * 8.0 + cursor * 0.05)
            resets.append(cursor == 0)
            closes.append(bar == 3)
            cursor += 1
        roots.append("TXF")
        timeframes.append(1440)
        contracts.append("TXFG6")
        days.append(day)
        sessions.append("full")
        timestamps.append(day_index * 86_400_000_000_000)
        prices.append(20_000.0 + np.sin(day_index / 3.0) * 12.0 + day_index * 0.2)
        resets.append(day_index == 0)
        closes.append(True)
    close = np.asarray(prices, dtype=np.float64)
    return BarDataset(
        root=np.asarray(roots, dtype="<U3"),
        timeframe_min=np.asarray(timeframes, dtype=np.int16),
        contract=np.asarray(contracts, dtype="<U8"),
        trading_day=np.asarray(days, dtype="<U10"),
        session=np.asarray(sessions, dtype="<U5"),
        ts_ns=np.asarray(timestamps, dtype=np.int64),
        open=close - 0.2,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=np.arange(close.size, dtype=np.float64) + 1.0,
        bid_open=close - 0.5,
        ask_open=close + 0.5,
        bid_qty_open=np.full(close.size, 10.0),
        ask_qty_open=np.full(close.size, 12.0),
        bid_close=close - 0.2,
        ask_close=close + 0.2,
        bid_qty_close=np.full(close.size, 8.0),
        ask_qty_close=np.full(close.size, 9.0),
        reset=np.asarray(resets, dtype=np.bool_),
        session_close=np.asarray(closes, dtype=np.bool_),
    )


def test_smma_screen_writes_gate_a_c_screen_only_scorecard(tmp_path) -> None:
    candidate = _candidate()
    alphas_dir = tmp_path / "research" / "alphas"
    promote_smma_candidate(
        candidate.expression,
        alpha_id="smma_test_candidate",
        owner="research",
        instrument="TXFD6",
        candidate_spec=asdict(candidate),
        out_dir=alphas_dir,
    )
    data_path = tmp_path / "dataset.npz"
    save_governed_dataset(
        data_path,
        _dataset(),
        query_evidence=[{"query_sha256": "test", "guard_overall": "pass"}],
        code_fingerprint="test",
    )

    assert is_smma_screen_package("smma_test_candidate", project_root=tmp_path)
    result = run_smma_screen(
        alpha_id="smma_test_candidate",
        data_path=data_path,
        experiments_dir=tmp_path / "experiments",
        project_root=tmp_path,
        skip_gate_b_tests=True,
    )

    assert result["screen_only"] is True
    assert result["promotion_eligible"] is False
    assert result["gate_a"]["passed"] is True
    assert result["gate_b"]["passed"] is True
    assert result["gate_c"]["details"]["final_holdout_excluded"] is True
    assert set(result["gate_c"]["details"]["horizon_metrics"]) == {"1h", "4h", "session"}
    payload = json.loads(Path(result["scorecard_path"]).read_text(encoding="utf-8"))
    assert payload["screen_only"] is True
    assert payload["promotion_eligible"] is False


def test_smma_screen_accepts_two_minute_fibonacci_candidate(tmp_path) -> None:
    identity = {
        "family": "smma",
        "root": "TXF",
        "timeframe_min": 2,
        "expression": "close_l1_2_atr14_spread",
        "horizon": "1h",
        "direction": 1,
        "threshold": 0.0,
    }
    candidate = Candidate(
        candidate_id=_candidate_id(identity),
        root="TXF",
        timeframe_min=2,
        expression="close_l1_2_atr14_spread",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=20260726,
        complexity=1,
    )
    alphas_dir = tmp_path / "research" / "alphas"
    promote_smma_candidate(
        candidate.expression,
        alpha_id="smma_test_two_minute",
        owner="research",
        instrument="TXFD6",
        candidate_spec=asdict(candidate),
        out_dir=alphas_dir,
    )
    dataset = _dataset()
    dataset.timeframe_min[dataset.timeframe_min == 60] = 2
    data_path = tmp_path / "dataset.npz"
    save_governed_dataset(
        data_path,
        dataset,
        query_evidence=[{"query_sha256": "test", "guard_overall": "pass"}],
        code_fingerprint="test",
    )

    result = run_smma_screen(
        alpha_id="smma_test_two_minute",
        data_path=data_path,
        experiments_dir=tmp_path / "experiments",
        project_root=tmp_path,
        skip_gate_b_tests=True,
    )

    assert result["gate_a"]["passed"] is True
    assert result["gate_b"]["passed"] is True
    payload = json.loads(Path(result["scorecard_path"]).read_text(encoding="utf-8"))
    assert payload["candidate"]["timeframe_min"] == 2
    assert payload["screen_only"] is True
    assert payload["promotion_eligible"] is False


def test_cli_screen_routes_smma_package_to_family_adapter(monkeypatch, capsys, tmp_path) -> None:
    import research.combinatorial.smma_screen as screen

    manifest_path = tmp_path / "research" / "alphas" / "smma_test_candidate" / "manifest.yaml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "experiment_metadata": {
                    "family": "smma",
                    "screen_only_required": True,
                }
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def fake_screen(**kwargs):
        captured.update(kwargs)
        return {"passed": True, "screen_only": True}

    monkeypatch.setattr(screen, "run_smma_screen", fake_screen)
    monkeypatch.chdir(tmp_path)
    cmd_alpha_screen(
        Namespace(
            alpha_id="smma_test_candidate",
            data=["dataset.npz"],
            experiments_dir="experiments",
            skip_gate_b_tests=True,
            out=None,
        )
    )

    assert captured["data_path"] == "dataset.npz"
    assert json.loads(capsys.readouterr().out)["screen_only"] is True
