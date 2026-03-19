"""Promotion data types — extracted from promotion.py to avoid circular imports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PromotionConfig:
    alpha_id: str
    owner: str
    project_root: str = "."
    experiments_dir: str = "research/experiments"
    scorecard_path: str | None = None
    shadow_sessions: int = 0
    min_shadow_sessions: int = 5
    drift_alerts: int = 0
    execution_reject_rate: float = 0.0
    max_execution_reject_rate: float = 0.01
    require_paper_trade_governance: bool = False
    paper_trade_summary_path: str | None = None
    min_paper_trade_calendar_days: int = 7
    min_paper_trade_trading_days: int = 5
    min_paper_trade_session_minutes: int = 60
    min_sharpe_oos: float = 0.7
    max_abs_drawdown: float = 0.2
    max_turnover: float = 2.0
    max_correlation: float = 0.7
    enable_rust_readiness_gate: bool = False
    rust_module_name: str | None = None
    rust_parity_test_path: str = "tests/unit/test_rust_hotpath_parity.py"
    rust_parity_timeout_s: int = 180
    enforce_rust_benchmark_gate: bool = False
    rust_benchmark_cmd: str = (
        "uv run python tests/benchmark/perf_regression_gate.py "
        "--baseline tests/benchmark/.benchmark_baseline.json "
        "--current benchmark.json "
        "--threshold 0.10"
    )
    canary_weight: float | None = None
    expiry_days: int = 30
    max_live_slippage_bps: float = 3.0
    max_live_drawdown_contribution: float = 0.02
    max_execution_error_rate: float = 0.01
    force: bool = False
    write_promotion_config: bool = True
    config_version: str = "v1"
    parent_config_version: str | None = None
    # Feature set version from the alpha manifest.  When set, Gate D warns
    # (warn-only, non-blocking) if it doesn't match the live engine version.
    manifest_feature_set_version: str | None = None


@dataclass(frozen=True)
class PromotionChecklistItem:
    label: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PromotionChecklist:
    items: list[PromotionChecklistItem]

    def all_passed(self) -> bool:
        return all(i.passed for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_passed": self.all_passed(),
            "items": [{"label": i.label, "passed": i.passed, "detail": i.detail} for i in self.items],
        }


@dataclass(frozen=True)
class PromotionResult:
    alpha_id: str
    approved: bool
    forced: bool
    gate_d_passed: bool
    gate_e_passed: bool
    gate_f_passed: bool
    canary_weight: float
    integration_report_path: str
    promotion_decision_path: str
    promotion_config_path: str | None
    reasons: list[str]
    paper_governance_report_path: str | None = None
    checklist: PromotionChecklist | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.checklist is not None:
            d["checklist"] = self.checklist.to_dict()
        return d
