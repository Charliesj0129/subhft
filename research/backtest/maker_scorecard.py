"""Passive maker scorecard helpers for research backtests.

The generic alpha scorecard focuses on signal statistics. Passive maker
promotion needs extra execution evidence: queue preservation, cancel pressure,
profitable fills, adverse fills, and inventory cleanup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "maker_scorecard.v1"


@dataclass(frozen=True)
class MakerScorecard:
    total_pnl: float = 0.0
    total_fills: int = 0
    total_quotes: int = 0
    total_cancels: int = 0
    price_change_cancels: int = 0
    n_days: int = 0
    winning_days: int = 0
    pnl_per_fill: float | None = None
    fill_to_quote_pct: float | None = None
    cancel_to_quote_pct: float | None = None
    price_change_cancel_pct: float | None = None
    profitable_fill_pct: float | None = None
    adverse_fill_pct: float | None = None
    avg_queue_wait_ms: float | None = None
    winning_day_pct: float | None = None
    max_drawdown: float = 0.0
    max_abs_final_inventory: int = 0
    fill_sample_count: int = 0
    latency_profile: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    daily: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "total_pnl": self.total_pnl,
            "total_fills": self.total_fills,
            "total_quotes": self.total_quotes,
            "total_cancels": self.total_cancels,
            "price_change_cancels": self.price_change_cancels,
            "n_days": self.n_days,
            "winning_days": self.winning_days,
            "pnl_per_fill": self.pnl_per_fill,
            "fill_to_quote_pct": self.fill_to_quote_pct,
            "cancel_to_quote_pct": self.cancel_to_quote_pct,
            "price_change_cancel_pct": self.price_change_cancel_pct,
            "profitable_fill_pct": self.profitable_fill_pct,
            "adverse_fill_pct": self.adverse_fill_pct,
            "avg_queue_wait_ms": self.avg_queue_wait_ms,
            "winning_day_pct": self.winning_day_pct,
            "max_drawdown": self.max_drawdown,
            "max_abs_final_inventory": self.max_abs_final_inventory,
            "fill_sample_count": self.fill_sample_count,
            "latency_profile": dict(self.latency_profile),
            "config": dict(self.config),
            "daily": [dict(day) for day in self.daily],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MakerScorecard":
        schema = str(payload.get("schema", ""))
        if schema and schema != SCHEMA:
            raise ValueError(f"unsupported maker scorecard schema: {schema}")
        return cls(
            total_pnl=_round6(_float(payload.get("total_pnl"))),
            total_fills=_int(payload.get("total_fills")),
            total_quotes=_int(payload.get("total_quotes")),
            total_cancels=_int(payload.get("total_cancels")),
            price_change_cancels=_int(payload.get("price_change_cancels")),
            n_days=_int(payload.get("n_days")),
            winning_days=_int(payload.get("winning_days")),
            pnl_per_fill=_optional_float(payload.get("pnl_per_fill")),
            fill_to_quote_pct=_optional_float(payload.get("fill_to_quote_pct")),
            cancel_to_quote_pct=_optional_float(payload.get("cancel_to_quote_pct")),
            price_change_cancel_pct=_optional_float(payload.get("price_change_cancel_pct")),
            profitable_fill_pct=_optional_float(payload.get("profitable_fill_pct")),
            adverse_fill_pct=_optional_float(payload.get("adverse_fill_pct")),
            avg_queue_wait_ms=_optional_float(payload.get("avg_queue_wait_ms")),
            winning_day_pct=_optional_float(payload.get("winning_day_pct")),
            max_drawdown=_round6(_float(payload.get("max_drawdown"))),
            max_abs_final_inventory=_int(payload.get("max_abs_final_inventory")),
            fill_sample_count=_int(payload.get("fill_sample_count")),
            latency_profile=_mapping(payload.get("latency_profile")),
            config=_mapping(payload.get("config")),
            daily=tuple(_mapping(day) for day in _sequence(payload.get("daily"))),
        )


@dataclass(frozen=True)
class MakerPromotionThresholds:
    min_total_pnl: float = 0.0
    min_total_fills: int = 50
    min_profitable_fill_pct: float = 57.0
    min_winning_day_pct: float = 55.0
    max_cancel_to_quote_pct: float = 50.0
    max_abs_final_inventory: int = 0
    require_latency_profile: bool = True


@dataclass(frozen=True)
class MakerGateCheck:
    name: str
    passed: bool
    value: float | int | str | None
    threshold: float | int | str | None


@dataclass(frozen=True)
class MakerGateDecision:
    passed: bool
    checks: tuple[MakerGateCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [
                {
                    "name": check.name,
                    "passed": check.passed,
                    "value": check.value,
                    "threshold": check.threshold,
                }
                for check in self.checks
            ],
        }


def compute_maker_scorecard(
    daily_results: Sequence[Mapping[str, Any]],
    *,
    fills: Sequence[Mapping[str, Any]] | None = None,
    latency_profile: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> MakerScorecard:
    """Aggregate passive maker backtest outputs into promotion evidence."""
    days = tuple(_normalize_day(day) for day in daily_results)
    total_pnl = _round6(sum(_float(day.get("pnl")) for day in days))
    total_fills = sum(_int(day.get("fills")) for day in days)
    total_quotes = sum(_int(day.get("quotes")) for day in days)
    total_cancels = sum(_int(day.get("cancels")) for day in days)
    price_change_cancels = sum(_int(day.get("px_chg")) for day in days)
    n_days = len(days)
    winning_days = sum(1 for day in days if _float(day.get("pnl")) > 0)
    max_drawdown = _round6(max((_float(day.get("max_dd")) for day in days), default=0.0))
    max_abs_final_inventory = max((abs(_int(day.get("final_pos"))) for day in days), default=0)

    fill_payloads = tuple(_mapping(fill) for fill in (fills or ()))
    pnl_samples = [_float(fill.get("pnl_pts", fill.get("pnl"))) for fill in fill_payloads]
    profitable_fill_pct = _pct(sum(1 for pnl in pnl_samples if pnl > 0), len(pnl_samples))

    adverse_flags = [_bool_or_none(fill.get("is_adverse", fill.get("adverse"))) for fill in fill_payloads]
    adverse_flags = [flag for flag in adverse_flags if flag is not None]
    adverse_fill_pct = _pct(sum(1 for flag in adverse_flags if flag), len(adverse_flags))

    queue_waits = [_queue_wait_ms(fill) for fill in fill_payloads]
    queue_waits = [wait for wait in queue_waits if wait is not None]
    avg_queue_wait_ms = _round6(sum(queue_waits) / len(queue_waits)) if queue_waits else None

    return MakerScorecard(
        total_pnl=total_pnl,
        total_fills=total_fills,
        total_quotes=total_quotes,
        total_cancels=total_cancels,
        price_change_cancels=price_change_cancels,
        n_days=n_days,
        winning_days=winning_days,
        pnl_per_fill=_ratio(total_pnl, total_fills),
        fill_to_quote_pct=_pct(total_fills, total_quotes),
        cancel_to_quote_pct=_pct(total_cancels, total_quotes),
        price_change_cancel_pct=_pct(price_change_cancels, total_quotes),
        profitable_fill_pct=profitable_fill_pct,
        adverse_fill_pct=adverse_fill_pct,
        avg_queue_wait_ms=avg_queue_wait_ms,
        winning_day_pct=_pct(winning_days, n_days),
        max_drawdown=max_drawdown,
        max_abs_final_inventory=max_abs_final_inventory,
        fill_sample_count=len(fill_payloads),
        latency_profile=_mapping(latency_profile),
        config=_mapping(config),
        daily=days,
    )


def evaluate_maker_scorecard(
    scorecard: MakerScorecard,
    thresholds: MakerPromotionThresholds | None = None,
) -> MakerGateDecision:
    """Evaluate scorecard against conservative passive-maker promotion gates."""
    th = thresholds or MakerPromotionThresholds()
    checks = (
        _min_check("total_pnl", scorecard.total_pnl, th.min_total_pnl),
        _min_check("total_fills", scorecard.total_fills, th.min_total_fills),
        _min_check("profitable_fill_pct", scorecard.profitable_fill_pct, th.min_profitable_fill_pct),
        _min_check("winning_day_pct", scorecard.winning_day_pct, th.min_winning_day_pct),
        _max_check("cancel_to_quote_pct", scorecard.cancel_to_quote_pct, th.max_cancel_to_quote_pct),
        _max_check("max_abs_final_inventory", scorecard.max_abs_final_inventory, th.max_abs_final_inventory),
        MakerGateCheck(
            name="latency_profile_present",
            passed=(bool(scorecard.latency_profile) if th.require_latency_profile else True),
            value=scorecard.latency_profile.get("latency_profile_id") if scorecard.latency_profile else None,
            threshold="required" if th.require_latency_profile else "optional",
        ),
    )
    return MakerGateDecision(passed=all(check.passed for check in checks), checks=checks)


def save_maker_scorecard(path: str | Path, scorecard: MakerScorecard) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scorecard.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def load_maker_scorecard(path: str | Path) -> MakerScorecard:
    return MakerScorecard.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _normalize_day(day: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "date": str(day.get("date", "")),
        "pnl": _round6(_float(day.get("pnl", day.get("total_pnl")))),
        "fills": _int(day.get("fills", day.get("n_fills"))),
        "quotes": _int(day.get("quotes", day.get("quotes_placed"))),
        "cancels": _int(day.get("cancels")),
        "px_chg": _int(day.get("px_chg", day.get("price_change_cancels"))),
        "max_dd": _round6(_float(day.get("max_dd", day.get("max_drawdown")))),
        "final_pos": _int(day.get("final_pos", day.get("final_position"))),
    }


def _min_check(name: str, value: float | int | None, threshold: float | int) -> MakerGateCheck:
    return MakerGateCheck(
        name=name,
        passed=value is not None and value >= threshold,
        value=value,
        threshold=threshold,
    )


def _max_check(name: str, value: float | int | None, threshold: float | int) -> MakerGateCheck:
    return MakerGateCheck(
        name=name,
        passed=value is not None and value <= threshold,
        value=value,
        threshold=threshold,
    )


def _ratio(numerator: float, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return _round6(numerator / denominator)


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return _round6(numerator / denominator * 100.0)


def _queue_wait_ms(fill: Mapping[str, Any]) -> float | None:
    if fill.get("queue_wait_ms") is not None:
        return _round6(_float(fill.get("queue_wait_ms")))
    if fill.get("queue_wait_ns") is not None:
        return _round6(_float(fill.get("queue_wait_ns")) / 1_000_000.0)
    return None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items()}
    return {}


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return _round6(_float(value))


def _int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y"}:
            return True
        if lowered in {"0", "false", "no", "n"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _round6(value: float) -> float:
    return round(float(value), 6)
