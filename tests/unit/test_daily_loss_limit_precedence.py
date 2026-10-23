"""Which configured number is the operative daily loss cap.

Three values in the production config claimed to be "the daily loss
limit": ``config/env/prod/risk.yaml`` (loaded by nothing; removed 2026-10-23),
``global_defaults.max_daily_loss`` in ``strategy_limits.yaml``, and
``intraday_pnl.hard_limit_ntd``. Only the last one stops trading, because
``DailyLossLimitValidator`` selects ``_hard_limit_threshold_scaled if
_intraday_pnl_enabled else _default_max_daily_loss`` and production defines an
``intraday_pnl`` block.

These tests pin that precedence so the comments describing it in both YAMLs
cannot silently go stale — the failure mode that left ``drawdown_recovery_pct``
unread for months.
"""

from pathlib import Path

import yaml

from hft_platform.risk.validators import DailyLossLimitValidator

_PROD_LIMITS = Path(__file__).resolve().parents[2] / "config" / "env" / "prod" / "strategy_limits.yaml"
_PROD_RISK = Path(__file__).resolve().parents[2] / "config" / "env" / "prod" / "risk.yaml"


def _ntd(amount_ntd: int) -> int:
    """NTD -> accumulator units (1 NTD == price_scale == 10,000 units)."""
    return amount_ntd * 10_000


def _make_validator(*, max_daily_loss: int, hard_limit_ntd: int | None) -> DailyLossLimitValidator:
    cfg: dict = {"global_defaults": {"max_daily_loss": max_daily_loss}}
    if hard_limit_ntd is not None:
        cfg["intraday_pnl"] = {
            "soft_limit_ntd": hard_limit_ntd // 2,
            "hard_limit_ntd": hard_limit_ntd,
            "price_scale": 10000,
        }
    return DailyLossLimitValidator(cfg, None)


def test_intraday_hard_limit_overrides_global_max_daily_loss():
    """With both configured, the intraday hard limit is the one that halts.

    ``max_daily_loss`` is set far out of reach, so a halt at the 1,000 NTD
    intraday limit can only have come from ``hard_limit_ntd``.
    """
    v = _make_validator(max_daily_loss=_ntd(100_000), hard_limit_ntd=1_000)

    v.update_unrealized(_ntd(-900))
    assert v.halt_triggered is False, "must not halt below the intraday limit"

    v.update_unrealized(_ntd(-1_100))
    assert v.halt_triggered is True
    assert v._halt_reason == "DAILY_LOSS_LIMIT"


def test_global_max_daily_loss_used_when_no_intraday_block():
    """Without an intraday_pnl block the global default is the live cap."""
    v = _make_validator(max_daily_loss=_ntd(2_000), hard_limit_ntd=None)
    assert v._intraday_pnl_enabled is False

    v.update_unrealized(_ntd(-1_900))
    assert v.halt_triggered is False

    v.update_unrealized(_ntd(-2_100))
    assert v.halt_triggered is True


def test_prod_config_documents_the_cap_that_actually_binds():
    """The prod YAML must keep the operative cap the smallest of the three.

    If someone raises ``hard_limit_ntd`` above ``max_daily_loss`` without
    noticing the precedence, the effective stop moves and the annotations in
    both files become wrong. Fail here rather than in production.
    """
    limits = yaml.safe_load(_PROD_LIMITS.read_text(encoding="utf-8"))
    intraday = limits["intraday_pnl"]
    globals_ = limits["global_defaults"]

    assert intraday, "prod defines intraday_pnl, so it is the operative cap"
    operative_scaled = intraday["hard_limit_ntd"] * 10_000
    fallback_scaled = globals_["max_daily_loss"]

    assert operative_scaled < fallback_scaled, (
        "intraday_pnl.hard_limit_ntd is the live stop; keeping it below "
        "global_defaults.max_daily_loss keeps the fallback a true upper bound"
    )
    assert intraday["soft_limit_ntd"] < intraday["hard_limit_ntd"]
    for removed in ("peak_drawdown_pct", "drawdown_recovery_pct", "peak_drawdown_min_peak_ntd"):
        assert removed not in intraday, f"{removed} is read by nothing since the peak rule was removed"


def test_prod_has_no_unloaded_risk_annotation_file():
    """config/env/prod/risk.yaml was loaded by nothing and disagreed with the
    loaded limits (10,000 NTD daily, 6,000/10,000 StormGuard, 2 orders/s). It
    was removed 2026-10-23; strategy_limits.yaml is the only risk file."""
    assert not _PROD_RISK.exists()
