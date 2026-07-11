"""Chronological real-data evaluation and BBO execution for NWO v0."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.bars import Bars, build_bars

from .indicator import IndicatorConfig, compute_bwm_weights, compute_indicator

STAGE_WINDOWS = {
    "development": (None, "2026-04-15"),
    "primary_oos": ("2026-04-16", "2026-05-20"),
    "confirmation_oos": ("2026-05-21", "2026-06-04"),
}

FRONT_MONTH_WINDOWS = {
    "b6": (None, "2026-02-18", "development"),
    "c6": ("2026-02-19", "2026-03-18", "development"),
    "d6": ("2026-03-19", "2026-04-15", "development"),
    "e6": ("2026-04-16", "2026-05-20", "primary_oos"),
    "f6": ("2026-05-21", "2026-06-04", "confirmation_oos"),
}


@dataclass(frozen=True)
class EvaluationBars:
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    date: np.ndarray
    is_session_close: np.ndarray
    bid_open: np.ndarray
    ask_open: np.ndarray
    contract: np.ndarray
    stage: np.ndarray


@dataclass(frozen=True)
class Trade:
    side: int
    entry_px: float
    exit_px: float
    entry_date: str
    exit_date: str
    stage: str
    regime: str
    exit_reason: str
    entry_bar: int = -1
    exit_bar: int = -1

    @property
    def gross_points(self) -> float:
        return (self.exit_px - self.entry_px) * self.side


@dataclass(frozen=True)
class ExecutionResult:
    trades: list[Trade]
    bbo_attempts: int
    bbo_skipped: int


def _trade_metrics(trades: list[Trade], extra_cost: float) -> dict[str, float | int | None]:
    net = np.asarray([trade.gross_points - extra_cost for trade in trades], dtype=float)
    if net.size == 0:
        return {
            "n_trades": 0,
            "net_total": 0.0,
            "net_mean": None,
            "net_median": None,
            "win_rate": None,
            "profit_factor": None,
            "max_drawdown_points": 0.0,
        }
    positive = float(net[net > 0].sum())
    negative = float(-net[net < 0].sum())
    cumulative = np.cumsum(net)
    peaks = np.maximum.accumulate(np.r_[0.0, cumulative])
    drawdowns = peaks[1:] - cumulative
    return {
        "n_trades": int(net.size),
        "net_total": round(float(net.sum()), 4),
        "net_mean": round(float(net.mean()), 4),
        "net_median": round(float(np.median(net)), 4),
        "win_rate": round(float(np.mean(net > 0)), 4),
        "profit_factor": round(positive / negative, 4) if negative > 0 else None,
        "max_drawdown_points": round(float(drawdowns.max(initial=0.0)), 4),
    }


def summarize_trades(
    trades: list[Trade], *, cost_levels: tuple[float, ...] = (0.0, 2.0, 4.0, 8.0)
) -> dict[str, dict]:
    """Summarize BBO-realized trades before and after explicit extra RT costs."""

    def cost_grid(selected: list[Trade]) -> dict[str, dict]:
        return {f"{cost:g}pt": _trade_metrics(selected, cost) for cost in cost_levels}

    def concentration_grid(selected: list[Trade]) -> dict[str, dict]:
        out = {}
        for cost in cost_levels:
            daily: dict[str, float] = {}
            for trade in selected:
                daily[trade.exit_date] = daily.get(trade.exit_date, 0.0) + trade.gross_points - cost
            values = np.asarray(list(daily.values()), dtype=float)
            if not values.size:
                cell = {
                    "n_days": 0,
                    "best_day": None,
                    "best_day_loo_total": 0.0,
                    "best_day_share_of_positive_net": None,
                    "largest_abs_day_share": None,
                }
            else:
                total = float(values.sum())
                best = float(values.max())
                denominator = float(np.abs(values).sum())
                cell = {
                    "n_days": int(values.size),
                    "best_day": round(best, 4),
                    "best_day_loo_total": round(total - best, 4),
                    "best_day_share_of_positive_net": round(best / total, 4) if total > 0 else None,
                    "largest_abs_day_share": round(float(np.abs(values).max()) / denominator, 4)
                    if denominator > 0
                    else None,
                }
            out[f"{cost:g}pt"] = cell
        return out

    stages = sorted({trade.stage for trade in trades})
    regimes = sorted({trade.regime for trade in trades})
    return {
        "all": cost_grid(trades),
        "by_stage": {
            stage: cost_grid([trade for trade in trades if trade.stage == stage]) for stage in stages
        },
        "by_regime": {
            regime: cost_grid([trade for trade in trades if trade.regime == regime]) for regime in regimes
        },
        "by_stage_regime": {
            stage: {
                regime: cost_grid(
                    [trade for trade in trades if trade.stage == stage and trade.regime == regime]
                )
                for regime in regimes
                if any(trade.stage == stage and trade.regime == regime for trade in trades)
            }
            for stage in stages
        },
        "concentration_by_stage": {
            stage: concentration_grid([trade for trade in trades if trade.stage == stage]) for stage in stages
        },
    }


def _position_by_bar(n_bars: int, trades: list[Trade]) -> np.ndarray:
    position = np.zeros(n_bars, dtype=int)
    for trade in trades:
        if trade.entry_bar < 0 or trade.exit_bar < trade.entry_bar:
            continue
        stop = trade.exit_bar if trade.exit_reason == "flip" else trade.exit_bar + 1
        position[trade.entry_bar:stop] = trade.side
    return position


def beta_neutral_by_stage(
    bars: EvaluationBars,
    trades: list[Trade],
    *,
    side: str,
    n_permutations: int = 1000,
) -> dict[str, dict[str, float | int | bool | None]]:
    """Measure timing return above a same-exposure market beta by stage."""
    position = _position_by_bar(len(bars.close), trades)
    if side == "long":
        position = np.where(position > 0, 1, 0)
    rng = np.random.default_rng(20260612)
    result = {}
    for stage in sorted(set(str(value) for value in bars.stage)):
        returns = []
        active = []
        for i in range(1, len(bars.close)):
            if bars.stage[i] != stage or bars.date[i] != bars.date[i - 1]:
                continue
            # Positions enter at the current bar's opening BBO, so a position's
            # first active bar only earns close - open.  Using close - prev_close
            # on an entry bar would credit the prior-close -> open (opening /
            # session) gap to a position that did not yet exist, biasing the
            # beta-neutral excess.  Continuation bars (held from the prior close)
            # earn the full close - close move.
            is_entry = position[i] != 0 and position[i] != position[i - 1]
            prev_ref = float(bars.open[i]) if is_entry else float(bars.close[i - 1])
            returns.append(float(bars.close[i]) - prev_ref)
            active.append(int(position[i]))
        r = np.asarray(returns, dtype=float)
        a = np.asarray(active, dtype=int)
        stage_trades = [trade for trade in trades if trade.stage == stage]
        if not r.size:
            result[stage] = {"n_bars": 0, "n_trades": len(stage_trades)}
            continue
        exposure = float(np.mean(a != 0))
        market_mean = float(r.mean())
        strategy = a * r
        strategy_mean = float(strategy.mean())
        excess_per_bar = strategy_mean - exposure * market_mean
        observed = strategy_mean
        ge = 0
        for _ in range(n_permutations):
            if float((rng.permutation(a) * r).mean()) >= observed:
                ge += 1
        p_value = (ge + 1) / (n_permutations + 1)
        excess_total = excess_per_bar * len(r)
        result[stage] = {
            "n_bars": int(r.size),
            "n_trades": len(stage_trades),
            "bars_in_position": int(np.sum(a != 0)),
            "exposure_frac": round(exposure, 4),
            "market_mean_bar_return": round(market_mean, 4),
            "strategy_mean_bar_return": round(strategy_mean, 4),
            "excess_per_bar_vs_beta": round(excess_per_bar, 4),
            "excess_total_points": round(excess_total, 4),
            "excess_per_trade_points": round(excess_total / len(stage_trades), 4)
            if stage_trades
            else None,
            "permutation_p_value_one_sided": round(p_value, 4),
            "significant_at_0_05": p_value < 0.05,
        }
    return result


def _take(values: np.ndarray | None, mask: np.ndarray) -> np.ndarray:
    if values is None:
        return np.full(int(mask.sum()), np.nan)
    return np.asarray(values)[mask]


def _window_bars(
    bars: Bars, *, start: str | None, end: str | None, stage: str
) -> EvaluationBars:
    dates = np.asarray(bars.date).astype(str)
    mask = np.ones(len(dates), dtype=bool)
    if start is not None:
        mask &= dates >= start
    if end is not None:
        mask &= dates <= end
    count = int(mask.sum())
    return EvaluationBars(
        open=np.asarray(bars.open)[mask],
        high=np.asarray(bars.high)[mask],
        low=np.asarray(bars.low)[mask],
        close=np.asarray(bars.close)[mask],
        volume=np.asarray(bars.volume)[mask],
        date=dates[mask],
        is_session_close=np.asarray(bars.is_session_close)[mask],
        bid_open=_take(bars.bid_open, mask),
        ask_open=_take(bars.ask_open, mask),
        contract=np.full(count, bars.contract, dtype=object),
        stage=np.full(count, stage, dtype=object),
    )


def _stage_bars(bars: Bars, stage: str) -> EvaluationBars:
    start, end = STAGE_WINDOWS[stage]
    return _window_bars(bars, start=start, end=end, stage=stage)


def _combine_evaluation_parts(parts: list[EvaluationBars]) -> EvaluationBars:
    if not parts:
        raise ValueError("at least one evaluation part is required")

    def join(field: str) -> np.ndarray:
        return np.concatenate([getattr(part, field) for part in parts])

    combined = EvaluationBars(
        open=join("open"),
        high=join("high"),
        low=join("low"),
        close=join("close"),
        volume=join("volume"),
        date=join("date"),
        is_session_close=join("is_session_close"),
        bid_open=join("bid_open"),
        ask_open=join("ask_open"),
        contract=join("contract"),
        stage=join("stage"),
    )
    if len(combined.date) > 1 and np.any(combined.date[1:] < combined.date[:-1]):
        raise ValueError("evaluation bars are not chronological after frozen window selection")
    validate_one_contract_per_date(combined)
    return combined


def validate_one_contract_per_date(bars: EvaluationBars) -> None:
    """Reject panel-like chains that count multiple contracts on one date."""
    contracts_by_date: dict[str, set[str]] = {}
    for date, contract in zip(bars.date, bars.contract, strict=True):
        contracts_by_date.setdefault(str(date), set()).add(str(contract))
    conflicts = {
        date: sorted(contracts)
        for date, contracts in contracts_by_date.items()
        if len(contracts) > 1
    }
    if conflicts:
        raise ValueError(f"multiple contracts selected on the same date: {conflicts}")


def build_front_month_chain(contract_inputs: dict[str, Bars]) -> EvaluationBars:
    """Build the frozen B6-F6 front-month chain for one market."""
    missing = set(FRONT_MONTH_WINDOWS) - set(contract_inputs)
    if missing:
        raise ValueError(f"missing front-month inputs: {sorted(missing)}")
    parts = []
    for suffix, (start, end, stage) in FRONT_MONTH_WINDOWS.items():
        bars = contract_inputs[suffix]
        if not bars.contract.lower().endswith(suffix):
            raise ValueError(
                f"contract input {suffix} does not match bars contract {bars.contract}"
            )
        parts.append(_window_bars(bars, start=start, end=end, stage=stage))
    return _combine_evaluation_parts(parts)


def build_evaluation_bars(stage_inputs: dict[str, Bars]) -> EvaluationBars:
    """Apply frozen windows and concatenate D6 -> E6 -> F6 chronologically."""
    missing = set(STAGE_WINDOWS) - set(stage_inputs)
    if missing:
        raise ValueError(f"missing stage inputs: {sorted(missing)}")
    parts = [_stage_bars(stage_inputs[stage], stage) for stage in STAGE_WINDOWS]
    return _combine_evaluation_parts(parts)


def simulate_trades(
    bars: EvaluationBars,
    *,
    trigger_long: np.ndarray,
    trigger_short: np.ndarray,
    regime: np.ndarray,
    close_half_spread: float = 0.5,
    side: str = "both",
) -> ExecutionResult:
    """Execute close-confirmed signals at next-open BBO and force-flat daily."""
    n = len(bars.close)
    trigger_long = np.asarray(trigger_long, dtype=bool)
    trigger_short = np.asarray(trigger_short, dtype=bool)
    regime = np.asarray(regime, dtype=object)
    if not (len(trigger_long) == len(trigger_short) == len(regime) == n):
        raise ValueError("signals, regimes, and bars must have identical lengths")
    if side not in {"both", "long"}:
        raise ValueError("side must be 'both' or 'long'")

    trades: list[Trade] = []
    position = 0
    entry_px = 0.0
    entry_date = ""
    entry_stage = ""
    entry_regime = "unavailable"
    entry_bar = -1
    attempts = 0
    skipped = 0

    def close_position(exit_px: float, exit_date: str, reason: str, exit_bar: int) -> None:
        nonlocal position, entry_px, entry_date, entry_stage, entry_regime, entry_bar
        trades.append(
            Trade(
                side=position,
                entry_px=entry_px,
                exit_px=float(exit_px),
                entry_date=entry_date,
                exit_date=exit_date,
                stage=entry_stage,
                regime=entry_regime,
                exit_reason=reason,
                entry_bar=entry_bar,
                exit_bar=exit_bar,
            )
        )
        position = 0
        entry_px = 0.0
        entry_date = ""
        entry_stage = ""
        entry_regime = "unavailable"
        entry_bar = -1

    for i in range(n):
        if bars.is_session_close[i] and position != 0:
            exit_px = bars.close[i] - close_half_spread if position == 1 else bars.close[i] + close_half_spread
            close_position(float(exit_px), str(bars.date[i]), "session_close", i)

        if bars.is_session_close[i] or i + 1 >= n or bars.date[i + 1] != bars.date[i]:
            continue
        if trigger_long[i]:
            desired = 1
        elif trigger_short[i]:
            desired = -1 if side == "both" else 0
        else:
            continue
        if desired == position or (desired == 0 and position == 0):
            continue

        attempts += 1
        fill = bars.ask_open[i + 1] if desired > position else bars.bid_open[i + 1]
        if not np.isfinite(fill) or fill <= 0:
            skipped += 1
            continue
        if position != 0:
            close_position(float(fill), str(bars.date[i + 1]), "flip", i + 1)
        if desired != 0:
            position = desired
            entry_px = float(fill)
            entry_date = str(bars.date[i + 1])
            entry_stage = str(bars.stage[i + 1])
            entry_regime = str(regime[i])
            entry_bar = i + 1

    if position != 0:
        exit_px = bars.close[-1] - close_half_spread if position == 1 else bars.close[-1] + close_half_spread
        close_position(float(exit_px), str(bars.date[-1]), "end_of_data", n - 1)
    return ExecutionResult(trades=trades, bbo_attempts=attempts, bbo_skipped=skipped)


def _date_ranges(bars: EvaluationBars) -> dict[str, dict[str, str | int | None]]:
    out: dict[str, dict[str, str | int | None]] = {}
    for stage in sorted(set(bars.stage)):
        dates = bars.date[bars.stage == stage]
        date_values = [str(value) for value in dates]
        out[str(stage)] = {
            "start": min(date_values) if date_values else None,
            "end": max(date_values) if date_values else None,
            "n_bars": int(dates.size),
            "n_days": int(np.unique(dates).size),
        }
    return out


def _contract_date_ranges(bars: EvaluationBars) -> dict[str, dict[str, str | int]]:
    out = {}
    for contract in sorted(set(str(value) for value in bars.contract)):
        dates = [str(value) for value in bars.date[bars.contract == contract]]
        out[contract] = {
            "start": min(dates),
            "end": max(dates),
            "n_bars": len(dates),
            "n_days": len(set(dates)),
        }
    return out


def _prefix_audit(bars: EvaluationBars, config: IndicatorConfig, full) -> dict[str, object]:
    prefix_n = max(1, int(len(bars.close) * 0.7))
    prefix = compute_indicator(
        bars.open[:prefix_n],
        bars.high[:prefix_n],
        bars.low[:prefix_n],
        bars.close[:prefix_n],
        config=config,
    )
    checks = {
        "oscillator": bool(
            np.allclose(prefix.oscillator, full.oscillator[:prefix_n], equal_nan=True)
        ),
        "signal": bool(np.allclose(prefix.signal, full.signal[:prefix_n], equal_nan=True)),
        "learned_weights": bool(
            np.allclose(prefix.learned_weights, full.learned_weights[:prefix_n], equal_nan=True)
        ),
        "trigger_long": bool(np.array_equal(prefix.trigger_long, full.trigger_long[:prefix_n])),
        "trigger_short": bool(np.array_equal(prefix.trigger_short, full.trigger_short[:prefix_n])),
    }
    return {"prefix_bars": prefix_n, "checks": checks, "pass": all(checks.values())}


def evaluate_bars(
    bars: EvaluationBars,
    *,
    config: IndicatorConfig | None = None,
    cost_levels: tuple[float, ...] = (0.0, 2.0, 4.0, 8.0),
) -> dict[str, object]:
    """Evaluate one chronological D6/E6/F6 stream and return JSON-safe evidence."""
    config = config or IndicatorConfig()
    indicator = compute_indicator(bars.open, bars.high, bars.low, bars.close, config=config)
    variants = {}
    executions = {}
    for variant, side in (("long_short", "both"), ("long_only", "long")):
        execution = simulate_trades(
            bars,
            trigger_long=indicator.trigger_long,
            trigger_short=indicator.trigger_short,
            regime=indicator.regime,
            side=side,
        )
        attempts = execution.bbo_attempts
        executions[variant] = execution
        variants[variant] = {
            "execution": {
                "bbo_attempts": attempts,
                "bbo_skipped": execution.bbo_skipped,
                "bbo_coverage": round((attempts - execution.bbo_skipped) / attempts, 4)
                if attempts
                else None,
            },
            "summary": summarize_trades(execution.trades, cost_levels=cost_levels),
            "beta_neutral": beta_neutral_by_stage(bars, execution.trades, side=side),
            "trades": [
                {
                    **asdict(trade),
                    "gross_points": round(trade.gross_points, 4),
                }
                for trade in execution.trades
            ],
        }

    stages = sorted(set(str(stage) for stage in bars.stage))
    signals_by_stage = {
        stage: {
            "long": int(indicator.trigger_long[bars.stage == stage].sum()),
            "short": int(indicator.trigger_short[bars.stage == stage].sum()),
        }
        for stage in stages
    }
    primary_execution = executions["long_short"]
    attempts = primary_execution.bbo_attempts
    return {
        "schema": "research.neural_weight_oscillator_zeiierman.v0",
        "fidelity": "disclosed_formula_causal_reconstruction",
        "source_url": "https://tw.tradingview.com/script/bfu1hmkS-Neural-Weight-Oscillator-Zeiierman/",
        "parameters": asdict(config),
        "fixed_bwm_weights": compute_bwm_weights().round(8).tolist(),
        "bars_by_stage": {stage: int(np.sum(bars.stage == stage)) for stage in stages},
        "date_ranges": _date_ranges(bars),
        "contracts_by_stage": {
            stage: sorted(set(str(value) for value in bars.contract[bars.stage == stage]))
            for stage in stages
        },
        "contract_date_ranges": _contract_date_ranges(bars),
        "signals_by_stage": signals_by_stage,
        "online_learning_updates": int(indicator.learning_updates[-1]) if len(bars.close) else 0,
        "final_learned_weights": indicator.learned_weights[-1].round(8).tolist()
        if len(bars.close)
        else [0.0, 0.0, 0.0],
        "causal_audit": _prefix_audit(bars, config, indicator),
        "execution": {
            "bbo_attempts": attempts,
            "bbo_skipped": primary_execution.bbo_skipped,
            "bbo_coverage": round((attempts - primary_execution.bbo_skipped) / attempts, 4)
            if attempts
            else None,
            "next_open": "buy_at_ask_sell_at_bid",
            "session_close": "last_trade_close_plus_or_minus_0.5pt",
        },
        "variants": variants,
    }


def _metric(result: dict, variant: str, stage: str, cost: str) -> dict:
    by_stage = result["variants"][variant]["summary"]["by_stage"]
    return by_stage.get(stage, {}).get(cost, {"n_trades": 0, "net_total": 0.0})


def _robustness_cell(result: dict, stage: str) -> tuple[dict, dict, dict]:
    variant = result["variants"]["long_short"]
    metric = variant["summary"]["by_stage"].get(stage, {}).get(
        "2pt", {"n_trades": 0, "net_total": 0.0}
    )
    concentration = variant["summary"]["concentration_by_stage"].get(stage, {}).get(
        "2pt", {"best_day_loo_total": 0.0}
    )
    beta = variant["beta_neutral"].get(stage, {"excess_total_points": 0.0})
    return metric, concentration, beta


def classify_primary_verdict(result: dict) -> str:
    """Classify the frozen TXF expanded-retrospective result."""
    primary, primary_concentration, primary_beta = _robustness_cell(result, "primary_oos")
    confirmation, confirmation_concentration, confirmation_beta = _robustness_cell(
        result, "confirmation_oos"
    )
    if (confirmation.get("net_total") or 0.0) <= 0 or (primary.get("net_total") or 0.0) <= 0:
        return "NOT_CONFIRMED"
    if primary.get("n_trades", 0) < 10 or confirmation.get("n_trades", 0) < 10:
        return "INSUFFICIENT_SAMPLE"
    robust = (
        (primary_concentration.get("best_day_loo_total") or 0.0) > 0
        and (confirmation_concentration.get("best_day_loo_total") or 0.0) > 0
        and (primary_beta.get("excess_total_points") or 0.0) > 0
        and (confirmation_beta.get("excess_total_points") or 0.0) > 0
    )
    return "SUPPORTED_RETROSPECTIVELY" if robust else "NOT_CONFIRMED"


def classify_transfer_verdict(result: dict) -> str:
    """Classify TMF transfer evidence without allowing it to upgrade TXF."""
    primary, primary_concentration, primary_beta = _robustness_cell(result, "primary_oos")
    confirmation, confirmation_concentration, confirmation_beta = _robustness_cell(
        result, "confirmation_oos"
    )
    if (primary.get("net_total") or 0.0) < 0 or (confirmation.get("net_total") or 0.0) < 0:
        return "transfer_conflict"
    support = (
        primary.get("n_trades", 0) >= 10
        and confirmation.get("n_trades", 0) >= 10
        and (primary.get("net_total") or 0.0) > 0
        and (confirmation.get("net_total") or 0.0) > 0
        and (primary_concentration.get("best_day_loo_total") or 0.0) > 0
        and (confirmation_concentration.get("best_day_loo_total") or 0.0) > 0
        and (primary_beta.get("excess_total_points") or 0.0) > 0
        and (confirmation_beta.get("excess_total_points") or 0.0) > 0
    )
    return "transfer_support" if support else "transfer_inconclusive"


def evaluate_markets(
    market_bars: dict[str, EvaluationBars],
    *,
    config: IndicatorConfig | None = None,
    cost_levels: tuple[float, ...] = (0.0, 2.0, 4.0, 8.0),
) -> dict[str, object]:
    """Evaluate TXF and TMF independently under one frozen research contract."""
    missing = {"txf", "tmf"} - set(market_bars)
    if missing:
        raise ValueError(f"missing market streams: {sorted(missing)}")
    markets = {
        market: evaluate_bars(bars, config=config, cost_levels=cost_levels)
        for market, bars in market_bars.items()
    }
    return {
        "schema": "research.neural_weight_oscillator_zeiierman.expanded.v1",
        "governance": "expanded_retrospective_oos",
        "primary_market": "txf",
        "transfer_market": "tmf",
        "primary_verdict": classify_primary_verdict(markets["txf"]),
        "transfer_verdict": classify_transfer_verdict(markets["tmf"]),
        "markets": markets,
    }


def render_expanded_markdown(result: dict) -> str:
    """Render the combined TXF primary and TMF transfer evidence."""
    lines = [
        "# Neural Weight Oscillator Expanded TXF/TMF Chain",
        "",
        f"Governance: `{result['governance']}`",
        "",
        f"TXF primary verdict: **{result['primary_verdict']}**",
        f"TMF transfer verdict: **{result['transfer_verdict']}**",
        "",
        "The expanded dates are retrospective OOS completion, not newly unseen OOS. "
        "No parameter selection uses these results.",
        "",
        "## OOS Robustness At 2pt",
        "",
        "| Market | Stage | Contracts | Days | Trades | Net total | Best-day LOO | Beta excess/trade | Beta p-value |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for market in ("txf", "tmf"):
        market_result = result["markets"][market]
        variant = market_result["variants"]["long_short"]
        for stage in ("primary_oos", "confirmation_oos"):
            metric = variant["summary"]["by_stage"].get(stage, {}).get("2pt", {})
            concentration = variant["summary"]["concentration_by_stage"].get(stage, {}).get(
                "2pt", {}
            )
            beta = variant["beta_neutral"].get(stage, {})
            date_cell = market_result["date_ranges"].get(stage, {})
            contracts = ", ".join(market_result["contracts_by_stage"].get(stage, []))
            lines.append(
                f"| {market.upper()} | {stage} | {contracts} | {date_cell.get('n_days', 0)} | "
                f"{metric.get('n_trades', 0)} | {metric.get('net_total', 0.0)} | "
                f"{concentration.get('best_day_loo_total')} | "
                f"{beta.get('excess_per_trade_points')} | "
                f"{beta.get('permutation_p_value_one_sided')} |"
            )
    lines.extend(
        [
            "",
            "## Contract Coverage",
            "",
            "| Market | Contract | Start | End | Bars | Days |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    for market in ("txf", "tmf"):
        for contract, cell in result["markets"][market]["contract_date_ranges"].items():
            lines.append(
                f"| {market.upper()} | {contract} | {cell['start']} | {cell['end']} | "
                f"{cell['n_bars']} | {cell['n_days']} |"
            )
    lines.extend(["", "## Causality And Execution", ""])
    for market in ("txf", "tmf"):
        market_result = result["markets"][market]
        lines.append(
            f"- {market.upper()}: prefix={market_result['causal_audit']['pass']}, "
            f"BBO coverage={market_result['execution']['bbo_coverage']}, "
            f"learning updates={market_result['online_learning_updates']}"
        )
    lines.extend(
        [
            "",
            "## Data Caveat",
            "",
            "F6 confirmation bars use a current-state read-only ClickHouse reconstruction. "
            "The 2026-06-01 through 2026-06-04 overlap does not match the earlier canonical "
            "raw snapshot exactly, so this run carries `source_snapshot_break` and cannot "
            "upgrade the candidate for promotion.",
            "",
            "Point-cost columns are sensitivity tests. TXF and TMF point PnL are not treated as "
            "equal cash values; transfer interpretation uses direction and robustness.",
            "",
        ]
    )
    return "\n".join(lines)


def _research_verdict(result: dict) -> str:
    primary = _metric(result, "long_short", "primary_oos", "2pt")
    confirm = _metric(result, "long_short", "confirmation_oos", "2pt")
    if confirm.get("n_trades", 0) > 0 and (confirm.get("net_mean") or 0.0) <= 0:
        return "NOT_CONFIRMED"
    if primary.get("n_trades", 0) < 10 or confirm.get("n_trades", 0) < 10:
        return "INSUFFICIENT_SAMPLE"
    if primary.get("net_mean", 0.0) > 0 and confirm.get("net_mean", 0.0) > 0:
        return "PROMISING_RESEARCH_ONLY"
    return "REJECT"


def render_markdown(result: dict) -> str:
    primary = _metric(result, "long_short", "primary_oos", "2pt")
    confirmation = _metric(result, "long_short", "confirmation_oos", "2pt")
    primary_long = _metric(result, "long_only", "primary_oos", "2pt")
    primary_concentration = result["variants"]["long_short"]["summary"][
        "concentration_by_stage"
    ].get("primary_oos", {}).get("2pt", {})
    primary_beta = result["variants"]["long_short"]["beta_neutral"].get("primary_oos", {})
    confirmation_beta = result["variants"]["long_short"]["beta_neutral"].get(
        "confirmation_oos", {}
    )
    lines = [
        "# Neural Weight Oscillator Zeiierman v0",
        "",
        f"Research verdict: **{_research_verdict(result)}**",
        "",
        "This is a disclosed-formula causal reconstruction, not a 1:1 Pine port.",
        "Signals are confirmed at close and executed at next-open BBO.",
        "Source: https://tw.tradingview.com/script/bfu1hmkS-Neural-Weight-Oscillator-Zeiierman/",
        "",
        "## Data Windows",
        "",
        "| Stage | Start | End | Bars | Days |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for stage, cell in result["date_ranges"].items():
        lines.append(
            f"| {stage} | {cell['start']} | {cell['end']} | {cell['n_bars']} | {cell['n_days']} |"
        )
    lines.extend(
        [
            "",
            "## Long/Short Stage Results",
            "",
            "| Stage | Extra RT cost | Trades | Net mean | Net total | Win rate | Max DD |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for stage in ("development", "primary_oos", "confirmation_oos"):
        for cost in ("0pt", "2pt", "4pt", "8pt"):
            cell = _metric(result, "long_short", stage, cost)
            lines.append(
                f"| {stage} | {cost} | {cell.get('n_trades', 0)} | {cell.get('net_mean')} | "
                f"{cell.get('net_total', 0.0)} | {cell.get('win_rate')} | "
                f"{cell.get('max_drawdown_points')} |"
            )
    lines.extend(
        [
            "",
            "## OOS Robustness",
            "",
            "| Stage | Variant | Trades @2pt | Net total @2pt | Best-day LOO @2pt | Beta excess/trade | Beta p-value |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for stage in ("primary_oos", "confirmation_oos"):
        for variant in ("long_short", "long_only"):
            metric = _metric(result, variant, stage, "2pt")
            concentration = (
                result["variants"][variant]["summary"]["concentration_by_stage"]
                .get(stage, {})
                .get("2pt", {})
            )
            beta = result["variants"][variant]["beta_neutral"].get(stage, {})
            lines.append(
                f"| {stage} | {variant} | {metric.get('n_trades', 0)} | "
                f"{metric.get('net_total', 0.0)} | {concentration.get('best_day_loo_total')} | "
                f"{beta.get('excess_per_trade_points')} | {beta.get('permutation_p_value_one_sided')} |"
            )
    lines.extend(
        [
            "",
            "## Stage And Regime At 2pt",
            "",
            "| Stage | Regime | Trades | Net total | Net mean |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    stage_regime = result["variants"]["long_short"]["summary"]["by_stage_regime"]
    for stage in ("development", "primary_oos", "confirmation_oos"):
        for regime, grid in stage_regime.get(stage, {}).items():
            cell = grid["2pt"]
            lines.append(
                f"| {stage} | {regime} | {cell['n_trades']} | {cell['net_total']} | {cell['net_mean']} |"
            )
    execution = result["execution"]
    audit = result["causal_audit"]
    lines.extend(
        [
            "",
            "## Execution And Causality",
            "",
            f"- BBO attempts: {execution['bbo_attempts']}",
            f"- BBO skipped: {execution['bbo_skipped']}",
            f"- BBO coverage: {execution['bbo_coverage']}",
            f"- Prefix invariance: {audit['pass']} ({audit['checks']})",
            f"- Online learning updates: {result['online_learning_updates']}",
            "",
            "## Interpretation",
            "",
            f"- E6 long/short at 2pt: {primary.get('net_total')} points across "
            f"{primary.get('n_trades')} trades; removing the best day leaves "
            f"{primary_concentration.get('best_day_loo_total')} points.",
            f"- E6 long-only at 2pt: {primary_long.get('net_total')} points, so the apparent E6 result "
            "depends on short exposure rather than a stable directional oscillator edge.",
            f"- E6 beta-neutral permutation p-value: "
            f"{primary_beta.get('permutation_p_value_one_sided')}; timing alpha is not significant.",
            f"- F6 confirmation at 2pt: {confirmation.get('net_total')} points across "
            f"{confirmation.get('n_trades')} trades; beta excess/trade is "
            f"{confirmation_beta.get('excess_per_trade_points')} points.",
            "- No parameter search was performed after observing E6/F6.",
            "",
            "The 0pt column already includes observed next-open bid/ask spread. The 2pt, 4pt, and 8pt "
            "columns apply additional round-trip cost stress. Session-close exits use a conservative "
            "0.5-point half-spread because the shared bar builder stores BBO at bar open only.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bar-min", type=int, default=5)
    parser.add_argument("--raw-dir", type=Path, default=Path("research/data/raw"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "research/experiments/validations/neural_weight_oscillator_zeiierman_v0/result_5m_day.json"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/codex/neural_weight_oscillator_zeiierman_v0_report.md"),
    )
    args = parser.parse_args()

    stage_inputs = {
        "development": build_bars(args.raw_dir, "txfd6", args.bar_min, session="day"),
        "primary_oos": build_bars(args.raw_dir, "txfe6", args.bar_min, session="day"),
        "confirmation_oos": build_bars(args.raw_dir, "txff6", args.bar_min, session="day"),
    }
    bars = build_evaluation_bars(stage_inputs)
    result = evaluate_bars(bars)
    result["data_source"] = "real_l2_trade_and_bbo_reconstruction"
    result["contracts"] = {
        "development": "TXFD6",
        "primary_oos": "TXFE6",
        "confirmation_oos": "TXFF6",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    args.report.write_text(render_markdown(result))
    print(json.dumps({"output": str(args.output), "report": str(args.report), "verdict": _research_verdict(result)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
