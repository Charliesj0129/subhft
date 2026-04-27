"""T5 backtest harness — R52 C1 AMHP Dynamic-Spread Maker.

Drives the L1 modulator + AMHP estimator on the TMFD6 31-day window via
`research.backtest.maker_engine.MakerEngine` (CK-direct ground truth, per
`hft-backtest-calibration` SKILL — never use power-prob queue model alone).

Profile: `v2026-04-24_measured` asymmetric — submit/modify P95 = 395 ms,
cancel P95 = 59 ms.  Cancel 6.7× faster than submit; structurally favors
dynamic re-quote.

Outputs:
  - stdout JSON summary (Sharpe, A1-tightened criteria, kill-flag PASS/FAIL)
  - `outputs/r52_amhp_dynamic_spread/t5_run_{ts}.json` (full daily PnL + telemetry)
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from research.alphas.r52_amhp_dynamic_spread.impl import (
    _BASE_SPREAD_PTS,
    _IIR_CRITICAL,
    _MULT_CAP,
    _RHO_CRITICAL,
    _RHO_LOW,
    _AMHPState,
)
from research.backtest.cost_models import load_cost_profile
from research.backtest.fill_models import QueueDepletionFill, QueuePosition
from research.backtest.maker_engine import (
    CancelQuote,
    ClickHouseSource,
    Hold,
    LatencyProfile,
    PostQuote,
    TickData,
)

logger = structlog.get_logger("r52_amhp.backtest")

# CK source price scale is x1_000_000 (per ClickHouseSource.load_day).
_CK_SCALE = 1_000_000
# AMHP modulator parameters (mirrored from impl.py for direct sim usage).
_ALPHA_RHO = 1.6
_ALPHA_IIR = 0.9


@dataclass
class _DayContext:
    """Per-day backtest accumulator — used for K1 daily PnL distribution and
    K3 modulator-gain delta vs R47 baseline."""

    date: str
    fills: int = 0
    fills_with_modulator: int = 0
    modulator_gain_pts_total: float = 0.0
    pnl_pts_gross: float = 0.0
    pnl_pts_net: float = 0.0
    rho_minute_samples: int = 0
    rho_critical_minutes: int = 0


class AmhpMakerBacktestStrategy:
    """Adapts the R52 AMHP modulator + estimator to the `MakerStrategy`
    protocol consumed by `MakerEngine`.

    Layer-1 dynamic spread floor; Layer-2 AMHP signals; Layer-3 R47 baseline
    quoting (post buy at bid, sell at ask whenever observed_spread >=
    spread_target).

    Day-level covariates (γ_io, γ_us) are settable — when both are zero the
    strategy collapses to per-quote granularity and inherits R52 meta-kill.
    The default zero values are intentional for the K2 falsifier.
    """

    __slots__ = (
        "_state",
        "_position",
        "_max_pos",
        "_base_spread_pts",
        "_alpha_rho",
        "_alpha_iir",
        "_rho_low",
        "_rho_critical",
        "_iir_critical",
        "_mult_cap",
        # day-level covariate coefficients persisted across daily resets
        "_gamma_io_persistent",
        "_gamma_us_persistent",
        # telemetry hooks updated by the harness
        "_on_post_callback",
        "_on_modulator_active_callback",
        "_last_minute",
        "_critical_minute_count",
        "_minute_sample_count",
        # buy/sell post tracking (for entry price → fill PnL accounting)
        "_pending_buy_price",
        "_pending_sell_price",
        # last-seen best bid/ask for trade-direction inference
        "_last_bid",
        "_last_ask",
    )

    def __init__(
        self,
        max_pos: int = 3,
        base_spread_pts: int = _BASE_SPREAD_PTS,
        alpha_rho: float = _ALPHA_RHO,
        alpha_iir: float = _ALPHA_IIR,
        rho_low: float = _RHO_LOW,
        rho_critical: float = _RHO_CRITICAL,
        iir_critical: float = _IIR_CRITICAL,
        mult_cap: float = _MULT_CAP,
        gamma_io: float = 0.0,
        gamma_us: float = 0.0,
    ) -> None:
        self._state = _AMHPState()
        self._state.set_mu_coefficients(gamma_io=gamma_io, gamma_us=gamma_us)
        self._position = 0
        self._max_pos = max_pos
        self._base_spread_pts = base_spread_pts
        self._alpha_rho = alpha_rho
        self._alpha_iir = alpha_iir
        self._rho_low = rho_low
        self._rho_critical = rho_critical
        self._iir_critical = iir_critical
        self._mult_cap = mult_cap
        self._gamma_io_persistent = gamma_io
        self._gamma_us_persistent = gamma_us
        self._on_post_callback: Any = None
        self._on_modulator_active_callback: Any = None
        self._last_minute: int = -1
        self._critical_minute_count: int = 0
        self._minute_sample_count: int = 0
        self._pending_buy_price: int = 0
        self._pending_sell_price: int = 0
        self._last_bid: int = 0
        self._last_ask: int = 0

    # -- helper for day boundary reset ----------------------------------

    def reset_for_day(
        self, *, io_z: float = 0.0, us_overnight: float = 0.0,
        us_window_active: bool = False,
    ) -> None:
        """Reset internal state at the start of each backtest day.  K2 / K1
        diffusion mechanism: refresh day-level covariates here."""
        self._state = _AMHPState()
        # Preserve covariate coefficients across days
        self._state.set_mu_coefficients(
            gamma_io=self._gamma_io_persistent,
            gamma_us=self._gamma_us_persistent,
        )
        self._state.set_day_covariates(
            io_z=io_z,
            us_overnight=us_overnight,
            us_window_active=us_window_active,
        )
        self._position = 0
        self._last_minute = -1
        self._critical_minute_count = 0
        self._minute_sample_count = 0
        self._pending_buy_price = 0
        self._pending_sell_price = 0
        self._last_bid = 0
        self._last_ask = 0

    def set_persistent_gammas(self, gamma_io: float, gamma_us: float) -> None:
        # Stash so reset_for_day preserves them.
        self._gamma_io_persistent = gamma_io
        self._gamma_us_persistent = gamma_us
        self._state.set_mu_coefficients(gamma_io=gamma_io, gamma_us=gamma_us)

    # -- modulator ------------------------------------------------------

    def _multiplier(self, rho_hat: float, iir_abs: float) -> float:
        if rho_hat >= self._rho_critical or iir_abs >= self._iir_critical:
            return self._mult_cap
        excess_rho = max(0.0, rho_hat - self._rho_low)
        m = 1.0 + self._alpha_rho * excess_rho + self._alpha_iir * iir_abs
        return min(self._mult_cap, max(1.0, m))

    # -- MakerStrategy protocol ----------------------------------------

    def feed_trade(self, tick: TickData) -> None:
        """Direct trade feed — called by the custom day-runner so the AMHP
        estimator sees every trade event (the stock `MakerEngine._run_day`
        does NOT pass trades to `strategy.on_tick`)."""
        if not tick.is_trade or tick.trade_volume <= 0:
            return
        # Direction inference via last-known bid/ask comparison (tick rule).
        # If trade_price >= last best ask → buyer-initiated (+1).
        # If trade_price <= last best bid → seller-initiated (-1).
        # Else within the spread: default to alternating sign (rare).
        direction = 0
        if self._last_minute_bid_ask_seen():
            if tick.trade_price >= self._last_ask:
                direction = +1
            elif tick.trade_price <= self._last_bid:
                direction = -1
        if direction == 0:
            direction = +1 if (tick.exch_ts // 1_000_000) % 2 == 0 else -1
        self._state.update_trade(tick.exch_ts, direction)

    def _last_minute_bid_ask_seen(self) -> bool:
        return self._last_bid > 0 and self._last_ask > 0

    def on_tick(self, tick: TickData) -> list:
        # No-op for trade events (engine never calls us with them — feed_trade
        # path is the canonical entry).
        if tick.is_trade and tick.trade_volume > 0:
            return [Hold()]

        # bidask event — track best bid/ask for trade-direction inference
        if tick.bid_price > 0 and tick.ask_price > 0:
            self._last_bid = tick.bid_price
            self._last_ask = tick.ask_price

        # 2) Track minute-bucket ρ̂ critical frequency (K5 telemetry).
        if self._state.warmed_up:
            minute = tick.exch_ts // (60 * 1_000_000_000)
            if minute != self._last_minute:
                self._last_minute = minute
                self._minute_sample_count += 1
                if self._state.rho_hat() > self._rho_critical:
                    self._critical_minute_count += 1

        # 3) Compute modulator and L1 spread target.
        rho_hat = self._state.rho_hat() if self._state.warmed_up else 0.0
        iir_abs = abs(self._state.iir()) if self._state.warmed_up else 0.0
        multiplier = self._multiplier(rho_hat, iir_abs)
        spread_target_pts = max(
            self._base_spread_pts,
            int(self._base_spread_pts * multiplier),
        )

        # 4) Quote only when observed spread >= dynamic target.
        if tick.spread_pts < spread_target_pts:
            return [Hold()]

        actions: list = []
        # Buy at bid, sell at ask (R47 maker baseline pattern).
        if self._position < self._max_pos:
            actions.append(PostQuote(side="buy", price=tick.bid_price, qty=1))
            self._pending_buy_price = tick.bid_price
        if self._position > -self._max_pos:
            actions.append(PostQuote(side="sell", price=tick.ask_price, qty=1))
            self._pending_sell_price = tick.ask_price

        # Telemetry hook — caller can record per-tick modulator activity.
        if self._on_modulator_active_callback is not None and multiplier > 1.0:
            try:
                self._on_modulator_active_callback(
                    spread_target_pts=spread_target_pts,
                    observed_spread_pts=tick.spread_pts,
                    multiplier=multiplier,
                    rho_hat=rho_hat,
                    iir_abs=iir_abs,
                    bid_price=tick.bid_price,
                    ask_price=tick.ask_price,
                )
            except Exception:
                pass

        return actions or [Hold()]

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        if side == "buy":
            self._position += 1
        else:
            self._position -= 1

    # -- telemetry surface for caller ----------------------------------

    @property
    def critical_minute_count(self) -> int:
        return self._critical_minute_count

    @property
    def minute_sample_count(self) -> int:
        return self._minute_sample_count

    @property
    def state(self) -> _AMHPState:
        return self._state


# ============================================================================
# Backtest driver
# ============================================================================


def trading_dates(start: str, end: str) -> list[str]:
    """Trading dates between [start, end] inclusive that have TMFD6 data.

    Queries ClickHouse for distinct dates; falls back to calendar enumeration
    if CK is unreachable (raised by MakerEngine.run anyway).
    """
    import requests

    pwd = os.environ.get("CLICKHOUSE_PASSWORD", "changeme")
    url = "http://localhost:8123/"
    sql = (
        "SELECT DISTINCT toString(toDate(fromUnixTimestamp64Nano(exch_ts))) AS d "
        "FROM hft.market_data "
        f"WHERE symbol = 'TMFD6' AND toDate(fromUnixTimestamp64Nano(exch_ts)) "
        f"BETWEEN '{start}' AND '{end}' ORDER BY d"
    )
    resp = requests.post(url, params={"password": pwd}, data=sql, timeout=60)
    resp.raise_for_status()
    rows = resp.text.strip().split("\n")
    return [r for r in rows if r]


def _run_day_with_trade_feed(  # noqa: C901
    strategy: "AmhpMakerBacktestStrategy",
    events: list[TickData],
    fill_model: Any,
    latency: LatencyProfile,
) -> tuple[list[dict], int]:
    """Custom day-runner that feeds trade events to the strategy AMHP estimator
    (engine's `_run_day` does not pass trade events to strategy.on_tick).

    Mirrors `MakerEngine._run_day` closely but adds a `strategy.feed_trade()`
    side-call on every trade event so the Hawkes intensity stays current.
    """
    buy_order: QueuePosition | None = None
    sell_order: QueuePosition | None = None
    position = 0
    fills: list[dict] = []
    cur_bid = cur_ask = cur_bid_v = cur_ask_v = 0

    pending: list[tuple[int, str, QueuePosition | None]] = []
    place_ns = latency.place_ns
    cancel_ns = latency.cancel_ns

    def _apply_pending(now_ts: int) -> None:
        nonlocal buy_order, sell_order, pending
        remaining: list[tuple[int, str, QueuePosition | None]] = []
        for ts, op, payload in pending:
            if ts <= now_ts:
                if op == "place_buy":
                    buy_order = payload
                elif op == "place_sell":
                    sell_order = payload
                elif op == "cancel_buy":
                    buy_order = None
                elif op == "cancel_sell":
                    sell_order = None
            else:
                remaining.append((ts, op, payload))
        pending = remaining

    for event in events:
        _apply_pending(event.exch_ts)

        if event.is_trade:
            # Feed trade to strategy's AMHP estimator (this is what
            # MakerEngine._run_day does NOT do — the gap we close here).
            strategy.feed_trade(event)

            # Then check fills against the trade.
            mid = (
                (cur_bid + cur_ask) / (2 * event.scale) if cur_bid > 0 else 0
            )
            if buy_order is not None:
                result = fill_model.check_fills(
                    [buy_order],
                    event.trade_price,
                    event.trade_volume,
                )
                if result:
                    fills.append(
                        {
                            "side": "buy",
                            "price": buy_order.price,
                            "mid": mid,
                            "spread_pts": (
                                (cur_ask - cur_bid) // event.scale
                                if cur_bid > 0
                                else 0
                            ),
                        }
                    )
                    strategy.on_fill("buy", buy_order.price, mid)
                    position += 1
                    buy_order = None
            if sell_order is not None:
                result = fill_model.check_fills(
                    [sell_order],
                    event.trade_price,
                    event.trade_volume,
                )
                if result:
                    fills.append(
                        {
                            "side": "sell",
                            "price": sell_order.price,
                            "mid": mid,
                            "spread_pts": (
                                (cur_ask - cur_bid) // event.scale
                                if cur_bid > 0
                                else 0
                            ),
                        }
                    )
                    strategy.on_fill("sell", sell_order.price, mid)
                    position -= 1
                    sell_order = None
            continue

        # bidask event
        cur_bid = event.bid_price
        cur_ask = event.ask_price
        cur_bid_v = event.bid_qty
        cur_ask_v = event.ask_qty

        if cur_ask <= cur_bid:
            continue

        if buy_order is not None and buy_order.price != cur_bid:
            buy_order = None
        if sell_order is not None and sell_order.price != cur_ask:
            sell_order = None

        actions = strategy.on_tick(event)
        for action in actions:
            if isinstance(action, PostQuote):
                qp = fill_model.post_quote(
                    action.side,
                    action.price,
                    cur_bid_v if action.side == "buy" else cur_ask_v,
                )
                op = "place_buy" if action.side == "buy" else "place_sell"
                if place_ns == 0:
                    if action.side == "buy":
                        buy_order = qp
                    else:
                        sell_order = qp
                else:
                    pending.append((event.exch_ts + place_ns, op, qp))
            elif isinstance(action, CancelQuote):
                op = "cancel_buy" if action.side == "buy" else "cancel_sell"
                if cancel_ns == 0:
                    if action.side == "buy":
                        buy_order = None
                    else:
                        sell_order = None
                else:
                    pending.append((event.exch_ts + cancel_ns, op, None))

    return fills, position


def run_backtest(
    *,
    start_date: str = "2026-01-27",
    end_date: str = "2026-03-26",
    instrument: str = "TMFD6",
    max_pos: int = 3,
    gamma_io: float = 0.0,
    gamma_us: float = 0.0,
) -> dict[str, Any]:
    """Execute the T5 backtest end-to-end and return a structured summary.

    Uses a custom day-runner that feeds trade events to the AMHP estimator
    (the standard `MakerEngine._run_day` does not pass trades to the strategy).
    """
    cost = load_cost_profile(instrument)
    fill_model = QueueDepletionFill(queue_fraction=0.5)  # CK-calibrated
    # v2026-04-24_measured asymmetric profile.
    latency = LatencyProfile(
        place_ns=395_000_000,         # submit / modify P95 = 395 ms
        cancel_ns=59_000_000,         # cancel P95 = 59 ms (asymmetric)
    )

    strategy = AmhpMakerBacktestStrategy(
        max_pos=max_pos, gamma_io=gamma_io, gamma_us=gamma_us,
    )
    strategy.set_persistent_gammas(gamma_io=gamma_io, gamma_us=gamma_us)

    dates = trading_dates(start_date, end_date)
    if not dates:
        raise RuntimeError(f"No CK rows for {instrument} in {start_date}..{end_date}")

    logger.info(
        "backtest_start",
        instrument=instrument,
        dates=len(dates),
        start=start_date,
        end=end_date,
        gamma_io=gamma_io,
        gamma_us=gamma_us,
    )

    ck_source = ClickHouseSource()
    ck_source.health_check()

    daily_pnl: list[dict] = []
    equity: list[float] = [0.0]
    total_gross = 0.0
    total_fills = 0
    spread_breakdown: dict[int, dict] = {}

    for date in dates:
        events = ck_source.load_day(instrument, date)
        if not events:
            continue
        # P2 #10 fix: reset strategy state (position, AMHP estimator,
        # dynamic-spread modulator, intraday counters) at each day boundary.
        # Without this, the strategy carries stale `_position` and Hawkes
        # state across days while `_run_day_with_trade_feed` zeroes its local
        # `position` & FIFO accounting per day -> daily PnL diverges from
        # actual gating decisions.
        strategy.reset_for_day()
        day_fills, day_pos = _run_day_with_trade_feed(
            strategy, events, fill_model, latency,
        )
        gross, trips, wins = _compute_fifo_pnl(day_fills)
        net = cost.apply(gross, len(day_fills))
        total_gross += gross
        total_fills += len(day_fills)
        equity.append(equity[-1] + net)
        daily_pnl.append({
            "date": date,
            "pnl_pts": round(net, 2),
            "gross_pts": round(gross, 2),
            "fills": len(day_fills),
            "trips": trips,
            "wins": wins,
            "final_pos": day_pos,
        })
        for f in day_fills:
            spr = f.get("spread_pts", 0)
            if spr not in spread_breakdown:
                spread_breakdown[spr] = {"fills": 0, "gross_pnl": 0.0}
            spread_breakdown[spr]["fills"] += 1

    eq = np.array(equity)
    total_net = sum(d["pnl_pts"] for d in daily_pnl)
    daily_returns = np.diff(eq)

    sharpe = 0.0
    if len(daily_returns) > 1 and np.std(daily_returns) > 0:
        sharpe = float(np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252))

    max_dd = 0.0
    peak = eq[0]
    for v in eq:
        peak = max(peak, v)
        dd = (peak - v) / max(abs(peak), 1e-9)
        max_dd = max(max_dd, dd)

    # K5 minute-bucket frequency
    k5_freq_pct = 0.0
    if strategy.minute_sample_count > 0:
        k5_freq_pct = (
            100.0 * strategy.critical_minute_count / strategy.minute_sample_count
        )

    # K4 multi-scale ACF — populate per-scale ρ_k snapshot
    rho_per_scale = strategy._state.rho_hat_per_scale()

    summary = _build_summary(
        dates=dates,
        instrument=instrument,
        daily_pnl=daily_pnl,
        total_pnl=total_net,
        total_gross=total_gross,
        total_fills=total_fills,
        sharpe=sharpe,
        max_dd_pct=max_dd * 100,
        spread_breakdown=spread_breakdown,
        cost=cost,
        latency=latency,
        strategy=strategy,
        k5_freq_pct=k5_freq_pct,
        rho_per_scale=rho_per_scale,
        gamma_io=gamma_io,
        gamma_us=gamma_us,
    )
    return summary


def _compute_fifo_pnl(fills: list[dict]) -> tuple[float, int, int]:
    """FIFO PnL matching (mirrors `MakerEngine._compute_fifo_pnl`)."""
    buy_q: list[float] = []
    sell_q: list[float] = []
    realized = 0.0
    trips = 0
    wins = 0
    scale = 1_000_000

    for f in fills:
        price_pts = f["price"] / scale
        if f["side"] == "buy":
            if sell_q:
                sp = sell_q.pop(0)
                pnl = sp - price_pts
                realized += pnl
                trips += 1
                if pnl > 0:
                    wins += 1
            else:
                buy_q.append(price_pts)
        else:
            if buy_q:
                bp = buy_q.pop(0)
                pnl = price_pts - bp
                realized += pnl
                trips += 1
                if pnl > 0:
                    wins += 1
            else:
                sell_q.append(price_pts)
    return realized, trips, wins


def _build_summary(
    *,
    dates: list[str],
    instrument: str,
    daily_pnl: list[dict],
    total_pnl: float,
    total_gross: float,
    total_fills: int,
    sharpe: float,
    max_dd_pct: float,
    spread_breakdown: dict[int, dict],
    cost: Any,
    latency: LatencyProfile,
    strategy: "AmhpMakerBacktestStrategy",
    k5_freq_pct: float,
    rho_per_scale: tuple[float, float, float],
    gamma_io: float,
    gamma_us: float,
) -> dict[str, Any]:
    daily = daily_pnl
    n_days = len(daily)
    winning_days = sum(1 for d in daily if d["pnl_pts"] > 0)
    distinct_fill_days = sum(1 for d in daily if d["fills"] > 0)

    # K1 max_day_pct  — guard against division by zero AND signed-cancellation.
    if total_pnl > 0:
        # use abs for the day with biggest *contribution* (positive or negative).
        max_pos_day = max((d["pnl_pts"] for d in daily), default=0.0)
        k1_max_day_pct = 100.0 * max_pos_day / total_pnl if total_pnl > 0 else 0.0
    else:
        # Net PnL non-positive — K1 max_day_pct undefined; report concentration
        # as max_abs_day / sum_abs_day for the scorecard.
        sum_abs = sum(abs(d["pnl_pts"]) for d in daily)
        max_abs = max((abs(d["pnl_pts"]) for d in daily), default=0.0)
        k1_max_day_pct = 100.0 * max_abs / sum_abs if sum_abs > 0 else 0.0

    # Bootstrap 95% CI on total PnL across days.
    rng = np.random.default_rng(seed=20260425)
    samples = np.array([d["pnl_pts"] for d in daily], dtype=float)
    if len(samples) > 0:
        boots = rng.choice(samples, size=(2000, len(samples)), replace=True).sum(axis=1)
        ci_lower = float(np.percentile(boots, 2.5))
        ci_upper = float(np.percentile(boots, 97.5))
    else:
        ci_lower = ci_upper = 0.0
    bootstrap_excludes_zero = (ci_lower > 0.0) or (ci_upper < 0.0)

    # Jackknife sign-flip — recompute sum dropping each day, count sign flips.
    sign_flip = False
    if len(samples) > 1:
        full_sign = math.copysign(1, total_pnl) if total_pnl != 0 else 0.0
        for i in range(len(samples)):
            jk = float(samples.sum() - samples[i])
            if total_pnl != 0 and jk != 0 and math.copysign(1, jk) != full_sign:
                sign_flip = True
                break

    # Average spread at entry — derive from per-spread breakdown counts.
    per_spread = {str(k): v for k, v in spread_breakdown.items()}
    if total_fills > 0 and per_spread:
        weighted = sum(int(s) * v["fills"] for s, v in per_spread.items())
        avg_spread_at_entry = weighted / total_fills
    else:
        avg_spread_at_entry = 0.0

    # K3 modulator gain — derived as: when modulator active, average captured
    # half-spread minus baseline.  Without per-fill tracking from MakerEngine,
    # we approximate via the spread-bucket distribution: fills with spread >
    # base × 1.0 are "modulator-active".  This is a coarse approximation; T6
    # may want to switch to a per-fill audit hook.
    base_pt = _BASE_SPREAD_PTS
    fills_modulator_active = sum(
        v["fills"] for s, v in per_spread.items() if int(s) > base_pt
    )
    fills_baseline = sum(
        v["fills"] for s, v in per_spread.items() if int(s) == base_pt
    )
    if fills_modulator_active > 0 and fills_baseline > 0:
        avg_spread_modulator = sum(
            int(s) * v["fills"] for s, v in per_spread.items() if int(s) > base_pt
        ) / fills_modulator_active
        avg_spread_baseline = sum(
            int(s) * v["fills"] for s, v in per_spread.items() if int(s) == base_pt
        ) / fills_baseline
        # Half-spread captured under each = avg_spread / 2 minus per-side cost.
        modulator_per_fill_gain_pts = (avg_spread_modulator - avg_spread_baseline) / 2.0
    else:
        modulator_per_fill_gain_pts = 0.0

    # Win-rate (Sharpe and max_dd already passed in via args).
    pnls = [d["pnl_pts"] for d in daily]
    win_rate = (sum(1 for v in pnls if v > 0) / max(1, len(pnls))) * 100

    # Edge-vs-cost ratio.
    rt_cost = cost.rt_cost_pts
    if total_fills > 0:
        net_per_trade = total_pnl / total_fills        # net pt PnL per fill
        gross_per_trade = total_gross / total_fills
    else:
        net_per_trade = 0.0
        gross_per_trade = 0.0
    edge_vs_cost = (gross_per_trade / rt_cost) if rt_cost > 0 else 0.0

    return {
        "instrument": instrument,
        "gamma_io": gamma_io,
        "gamma_us": gamma_us,
        "K4_rho_ms": round(rho_per_scale[0], 4),
        "K4_rho_min": round(rho_per_scale[1], 4),
        "K4_rho_hr": round(rho_per_scale[2], 4),
        "dates": dates,
        "n_days": n_days,
        "total_pnl_pts": round(total_pnl, 2),
        "total_gross_pts": round(total_gross, 2),
        "total_fills": total_fills,
        "winning_days": winning_days,
        "distinct_fill_days": distinct_fill_days,
        "win_rate_pct": round(win_rate, 1),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "rt_cost_pts": rt_cost,
        "avg_spread_at_entry_pts": round(avg_spread_at_entry, 2),
        "edge_vs_cost_ratio": round(edge_vs_cost, 3),
        "net_per_trade_pts": round(net_per_trade, 4),
        "gross_per_trade_pts": round(gross_per_trade, 4),
        # K1
        "K1_max_day_pct": round(k1_max_day_pct, 2),
        "K1_winning_days": winning_days,
        "K1_distinct_fill_days": distinct_fill_days,
        # K3
        "K3_modulator_per_fill_gain_pts": round(modulator_per_fill_gain_pts, 3),
        "K3_modulator_fill_count": fills_modulator_active,
        # K5
        "K5_rho_critical_freq_pct": round(k5_freq_pct, 3),
        "K5_minute_samples": strategy.minute_sample_count,
        # A1-tightened
        "bootstrap_ci_lower": round(ci_lower, 2),
        "bootstrap_ci_upper": round(ci_upper, 2),
        "bootstrap_excludes_zero": bootstrap_excludes_zero,
        "jackknife_sign_flip": sign_flip,
        # Latency profile
        "latency_profile_id": "v2026-04-24_measured",
        "submit_p95_ms": latency.place_ns / 1_000_000,
        "cancel_p95_ms": latency.cancel_ns / 1_000_000,
        # Daily breakdown
        "daily_pnl": daily,
        "per_spread_breakdown": per_spread,
    }


def main() -> int:
    summary = run_backtest()
    out_dir = Path("outputs/r52_amhp_dynamic_spread")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"t5_run_{ts}.json"
    path.write_text(json.dumps(summary, indent=2, default=str))
    logger.info("backtest_done", output=str(path), pnl=summary["total_pnl_pts"])
    sys.stdout.write(json.dumps(summary, indent=2, default=str))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
