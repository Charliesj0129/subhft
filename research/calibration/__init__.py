"""research.calibration — Queue model calibration framework for HFT Platform.

Top-level pipeline:
  audit   — inventory live fills + L2 data intersection
  sweep   — grid search over queue model candidates
  validate — held-out verification
  save/load — persistent YAML profiles

Use `python -m research.calibration.cli` for CLI invocation.
"""
from research.calibration.audit import (
    InstrumentAuditResult,
    audit_all,
    audit_ck_export_parquet,
    audit_clickhouse_fills,
    find_l2_data_days,
)
from research.calibration.config import (
    CalibrationNotFoundError,
    CalibrationProfile,
    load_calibration_profile,
    save_calibration_profile,
)
from research.calibration.probe_strategy import PassiveQuoteProbe, ProbeAction
from research.calibration.scoring import (
    DEFAULT_WEIGHTS,
    CalibrationScore,
    DailyFillSummary,
    compute_score,
)
from research.calibration.sweep import (
    QueueModelCandidate,
    SweepResult,
    generate_candidates,
    sweep_exponent,
)
from research.calibration.validate import (
    determine_confidence,
    split_days,
    validate_on_heldout,
)

__all__ = [
    "DEFAULT_WEIGHTS",
    "CalibrationNotFoundError",
    "CalibrationProfile",
    "CalibrationScore",
    "DailyFillSummary",
    "InstrumentAuditResult",
    "PassiveQuoteProbe",
    "ProbeAction",
    "QueueModelCandidate",
    "SweepResult",
    "audit_all",
    "audit_ck_export_parquet",
    "audit_clickhouse_fills",
    "compute_score",
    "determine_confidence",
    "find_l2_data_days",
    "generate_candidates",
    "load_calibration_profile",
    "save_calibration_profile",
    "split_days",
    "sweep_exponent",
    "validate_on_heldout",
]
