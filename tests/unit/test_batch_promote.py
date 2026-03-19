from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


def _setup_experiment(
    tmp_path: Path,
    alpha_id: str,
    sharpe_oos: float = 1.5,
    max_drawdown: float = 0.1,
    correlation_pool_max: float = 0.3,
    latency_profile: dict | None = None,
) -> Path:
    """Create a minimal experiment run."""
    exp_dir = tmp_path / "experiments"
    runs_dir = exp_dir / "runs"
    run_id = f"run_{alpha_id}"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    sc: dict = {
        "sharpe_oos": sharpe_oos,
        "max_drawdown": max_drawdown,
        "correlation_pool_max": correlation_pool_max,
        "turnover": 0.5,
    }
    if latency_profile:
        sc["latency_profile"] = latency_profile
    sc_path = run_dir / "scorecard.json"
    sc_path.write_text(json.dumps(sc))
    (run_dir / "backtest_report.json").write_text("{}")

    meta = {
        "run_id": run_id,
        "alpha_id": alpha_id,
        "config_hash": "abc",
        "timestamp": "2026-03-01T00:00:00",
        "data_paths": [],
        "metrics": {"sharpe_oos": sharpe_oos},
        "gate_status": {},
        "scorecard_path": str(sc_path),
        "backtest_report_path": str(run_dir / "backtest_report.json"),
        "signals_path": None,
        "equity_path": None,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta))
    return exp_dir


class TestBatchPromoter:
    def test_empty_experiments(self, tmp_path: Path) -> None:
        from hft_platform.alpha.batch_promote import BatchPromoter

        exp_dir = tmp_path / "experiments"
        exp_dir.mkdir()
        (exp_dir / "runs").mkdir()

        promoter = BatchPromoter(experiments_dir=str(exp_dir))
        results = promoter.run_fleet(dry_run=True)
        assert results == []

    def test_discovers_and_evaluates(self, tmp_path: Path) -> None:
        from hft_platform.alpha.batch_promote import BatchPromoter

        exp_dir = _setup_experiment(tmp_path, "alpha_a", sharpe_oos=2.0)

        mock_result = MagicMock()
        mock_result.approved = False
        mock_result.to_dict.return_value = {"approved": False, "reason": "mock"}

        with patch("hft_platform.alpha.batch_promote.promote_alpha", return_value=mock_result):
            promoter = BatchPromoter(experiments_dir=str(exp_dir))
            results = promoter.run_fleet(dry_run=True)

        assert len(results) == 1
        assert results[0]["alpha_id"] == "alpha_a"

    def test_handles_promotion_error(self, tmp_path: Path) -> None:
        from hft_platform.alpha.batch_promote import BatchPromoter

        exp_dir = _setup_experiment(tmp_path, "alpha_err", sharpe_oos=2.0)

        with patch(
            "hft_platform.alpha.batch_promote.promote_alpha",
            side_effect=ValueError("test error"),
        ):
            promoter = BatchPromoter(experiments_dir=str(exp_dir))
            results = promoter.run_fleet(dry_run=True)

        assert len(results) == 1
        assert results[0]["approved"] is False
        assert "test error" in results[0].get("error", "")

    def test_top_n_limit(self, tmp_path: Path) -> None:
        from hft_platform.alpha.batch_promote import BatchPromoter

        exp_dir = None
        for i in range(5):
            exp_dir = _setup_experiment(tmp_path, f"alpha_{i}", sharpe_oos=2.0 + i * 0.1)

        mock_result = MagicMock()
        mock_result.approved = False
        mock_result.to_dict.return_value = {}

        with patch("hft_platform.alpha.batch_promote.promote_alpha", return_value=mock_result):
            assert exp_dir is not None
            promoter = BatchPromoter(experiments_dir=str(exp_dir))
            results = promoter.run_fleet(dry_run=True, top_n=3)

        assert len(results) <= 3

    def test_filter_by_alpha_ids(self, tmp_path: Path) -> None:
        from hft_platform.alpha.batch_promote import BatchPromoter

        exp_dir = _setup_experiment(tmp_path, "alpha_a", sharpe_oos=2.0)
        _setup_experiment(tmp_path, "alpha_b", sharpe_oos=2.5)

        mock_result = MagicMock()
        mock_result.approved = True
        mock_result.to_dict.return_value = {"approved": True}

        with patch("hft_platform.alpha.batch_promote.promote_alpha", return_value=mock_result):
            promoter = BatchPromoter(experiments_dir=str(exp_dir))
            results = promoter.run_fleet(
                dry_run=True,
                alpha_ids=["alpha_a"],
            )

        assert len(results) == 1
        assert results[0]["alpha_id"] == "alpha_a"

    def test_sorted_by_sharpe(self, tmp_path: Path) -> None:
        from hft_platform.alpha.batch_promote import BatchPromoter

        exp_dir = _setup_experiment(tmp_path, "low_sharpe", sharpe_oos=1.1)
        _setup_experiment(tmp_path, "high_sharpe", sharpe_oos=3.0)

        call_order: list[str] = []
        mock_result = MagicMock()
        mock_result.approved = False
        mock_result.to_dict.return_value = {}

        def _capture_promote(config):
            call_order.append(config.alpha_id)
            return mock_result

        with patch("hft_platform.alpha.batch_promote.promote_alpha", side_effect=_capture_promote):
            promoter = BatchPromoter(experiments_dir=str(exp_dir))
            promoter.run_fleet(dry_run=True)

        # High Sharpe should be evaluated first
        assert call_order[0] == "high_sharpe"
