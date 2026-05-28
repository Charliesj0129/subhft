"""C1 re-validation — TXFD6 adaptive maker (Chávez-Casillas demand function).

Re-validation under the 2026-04-27 user-mandated rule: "kill requires backtest".
The original C1 (R1) was T2-killed via Kill Checklist alone with NO empirical
evidence. This implementation produces the missing backtest scorecard.

Mechanism (per T1_R1_C1_researcher_proposal_README.md):
  - Spread eligibility gate: only quote when observed spread >= 4 pt (TXFD6
    working zone where half-spread can amortize 3.0 pt RT).
  - Adaptive delta*: per Chávez-Casillas, Figueroa-López, Yu, Zhang (2024)
    eq. 3.2, the optimal posting distance from microprice is a function of the
    current intercept alpha and slope beta of the empirical demand function
    (per-side, separate alpha_b/beta_b for buys, alpha_a/beta_a for sells).
  - Online demand-function estimator: rolling 30-min window with exponential
    decay tau = 5 min, re-estimated every 5 s. Refit produces the random-
    coefficient (alpha, beta) per side. (T1 risk #2 commitment.)
  - Microprice (Stoikov 2014) as reservation reference:
      microprice = (bid * ask_qty + ask * bid_qty) / (bid_qty + ask_qty)
    used in lieu of mid for symmetric quote-width sizing.
  - Bid/ask execution (no mid pricing — required by hft-backtest-calibration
    skill when expected_edge < 2x spread).

Prices: scaled int x1_000_000 (CK convention; same as c33 base).
Cost model: TXFD6 retail RT = 3.0 pt (commission 0.6 + tax 2.4),
cited from `memory/feedback_taifex_fee_structure.md`.
Latency profile: r47_maker_shioaji_p95_v2026-04-24_measured (place P95 395 ms,
cancel P95 59 ms). F2_maker per-fill wedge = 7.3 pt at 395 ms.

Research-module float exception (Rule 11 of 25-architecture-governance):
this file is offline / CLI-invoked research code. Strategy decisions use
scaled-integer arithmetic; only the demand-function regression uses floats
(rolling fit on float intensities is inherent to the Chávez-Casillas method).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from structlog import get_logger

from research.backtest.maker_engine import (
    Hold,
    PostQuote,
    TickData,
)
from research.registry.schemas import (
    AlphaManifest,
    AlphaStatus,
    AlphaTier,
)

logger = get_logger("alpha.c1_revalidation_txfd6_chavez_casillas")

_TXF_POINT_VALUE_NTD = 200
_TXF_RT_COST_PTS = 3.0
_SCALE = 1_000_000
_TICK_SIZE_SCALED = 1 * _SCALE  # TXFD6 tick = 1 pt


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C1Params:
    """C1 adaptive maker parameters.

    spread_threshold_pts
        Minimum TXFD6 spread (pts) to evaluate quoting. T1 working zone = 4.
    max_pos
        Inventory cap. T1 risk #4 fills_distinct_days_min favours small caps.
        Default 1 (matches deployed R47 TMFD6); swept {1, 3} in runner.
    inventory_skew_tenths
        Fixed skew per contract (R47 best practice; tenths of a tick).
    fit_window_sec
        Sliding window for demand-function regression. T1 risk #2 = 30 min.
    fit_decay_tau_sec
        Exponential decay constant for fit weights. T1 risk #2 = 5 min.
    fit_refresh_sec
        Re-estimation cadence. T1 risk #2 = 5 s.
    fit_min_samples
        Minimum samples in window before adaptive layer activates (else
        falls back to R47-minimal touch quoting).
    delta_min_ticks / delta_max_ticks
        Bounds on adaptive posting distance (in ticks from microprice).
        delta_min=0 means post at touch; delta_max=3 caps deeper posting.
    """

    spread_threshold_pts: int = 4
    max_pos: int = 1
    inventory_skew_tenths: int = 2
    fit_window_sec: int = 1800
    fit_decay_tau_sec: int = 300
    fit_refresh_sec: int = 5
    fit_min_samples: int = 50
    delta_min_ticks: int = 0
    delta_max_ticks: int = 3
    # Cold-start exploration: post randomly in {0,1,2} ticks for the first
    # cold_start_n_quotes per side per day to seed delta-variance for the
    # demand-function regression. Required by Chávez-Casillas math (β cannot
    # be fit without δ-variance); not a hypothesis change. Documented in
    # scorecard "Implementation note" section per team-lead 2026-04-27.
    cold_start_n_quotes: int = 50
    cold_start_seed: int = 42


# ---------------------------------------------------------------------------
# Adaptive demand-function estimator (Chávez-Casillas eq. 2.3 / 3.2)
# ---------------------------------------------------------------------------


@dataclass
class _DemandFitState:
    """Per-side rolling fit of the demand function alpha + beta * delta.

    Stores (delta_pts, fill_indicator, ts_ns, weight) tuples in a deque.
    Refit on demand when refresh_sec elapsed.
    """

    samples: Deque[tuple[float, float, int]] = field(default_factory=deque)
    last_fit_ns: int = 0
    alpha: float = 0.0
    beta: float = 0.0
    n_fits: int = 0
    valid: bool = False


def _exponential_weighted_fit(
    samples: list[tuple[float, float, int]],
    now_ns: int,
    decay_tau_ns: int,
) -> tuple[float, float, bool]:
    """Fit y = alpha + beta * x with exponential time-decay weights.

    samples: (x=delta_pts, y=fill_indicator, ts_ns).
    Returns (alpha, beta, valid). valid=False when degenerate.
    """
    if len(samples) < 2:
        return 0.0, 0.0, False
    weights: list[float] = []
    xs: list[float] = []
    ys: list[float] = []
    for x, y, ts_ns in samples:
        age_ns = max(0, now_ns - ts_ns)
        w = 2.718281828 ** (-age_ns / decay_tau_ns) if decay_tau_ns > 0 else 1.0
        weights.append(w)
        xs.append(x)
        ys.append(y)
    sw = sum(weights)
    if sw <= 0:
        return 0.0, 0.0, False
    mx = sum(w * x for w, x in zip(weights, xs)) / sw
    my = sum(w * y for w, y in zip(weights, ys)) / sw
    sxx = sum(w * (x - mx) * (x - mx) for w, x in zip(weights, xs))
    sxy = sum(
        w * (x - mx) * (y - my) for w, x, y in zip(weights, xs, ys)
    )
    if sxx <= 1e-9:
        return my, 0.0, True  # constant-x degenerate; intercept-only
    beta = sxy / sxx
    alpha = my - beta * mx
    return alpha, beta, True


# ---------------------------------------------------------------------------
# C1 Adaptive Maker — MakerEngine-compatible
# ---------------------------------------------------------------------------


class C1ChavezCasillasAdaptiveMaker:
    """TXFD6 adaptive maker with Chávez-Casillas demand-function quoting.

    Conforms to research.backtest.maker_engine.MakerStrategy protocol.
    """

    __slots__ = (
        "_params",
        "_position",
        "_last_bid",
        "_last_ask",
        "_tick_count",
        "_spread_blocked",
        "_max_pos_blocked",
        "_quotes_posted",
        "_active_symbol",
        "_buy_fit",
        "_sell_fit",
        "_window_ns",
        "_decay_ns",
        "_refresh_ns",
        "_quote_history_buy",
        "_quote_history_sell",
        "_fit_used_count",
        "_fit_fallback_count",
        "_cold_start_buy_remaining",
        "_cold_start_sell_remaining",
        "_cold_start_used_count",
        "_rng",
    )

    def __init__(
        self,
        params: C1Params | None = None,
        active_symbol: str = "TXFD6",
    ) -> None:
        self._params = params or C1Params()
        self._position = 0
        self._last_bid: int | None = None
        self._last_ask: int | None = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._max_pos_blocked = 0
        self._quotes_posted = 0
        self._active_symbol = active_symbol
        self._buy_fit = _DemandFitState()
        self._sell_fit = _DemandFitState()
        self._window_ns = self._params.fit_window_sec * 1_000_000_000
        self._decay_ns = self._params.fit_decay_tau_sec * 1_000_000_000
        self._refresh_ns = self._params.fit_refresh_sec * 1_000_000_000
        # Quote history: (delta_pts, ts_ns, filled_yet) for fit-sample feedback.
        self._quote_history_buy: list[tuple[float, int, bool]] = []
        self._quote_history_sell: list[tuple[float, int, bool]] = []
        self._fit_used_count = 0
        self._fit_fallback_count = 0
        # Cold-start exploration counters (reset per-day via reset()/on_gap()).
        self._cold_start_buy_remaining = self._params.cold_start_n_quotes
        self._cold_start_sell_remaining = self._params.cold_start_n_quotes
        self._cold_start_used_count = 0
        # Deterministic RNG for reproducibility.
        import random
        self._rng = random.Random(self._params.cold_start_seed)

    # ---- Observability ---------------------------------------------------

    @property
    def position(self) -> int:
        return self._position

    @property
    def params(self) -> C1Params:
        return self._params

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def spread_blocked(self) -> int:
        return self._spread_blocked

    @property
    def max_pos_blocked(self) -> int:
        return self._max_pos_blocked

    @property
    def quotes_posted(self) -> int:
        return self._quotes_posted

    @property
    def active_symbol(self) -> str:
        return self._active_symbol

    @property
    def fit_stats(self) -> dict:
        return {
            "buy_fits": self._buy_fit.n_fits,
            "sell_fits": self._sell_fit.n_fits,
            "buy_alpha": self._buy_fit.alpha,
            "buy_beta": self._buy_fit.beta,
            "sell_alpha": self._sell_fit.alpha,
            "sell_beta": self._sell_fit.beta,
            "fit_used": self._fit_used_count,
            "fit_fallback": self._fit_fallback_count,
            "cold_start_used": self._cold_start_used_count,
            "cold_start_buy_remaining": self._cold_start_buy_remaining,
            "cold_start_sell_remaining": self._cold_start_sell_remaining,
        }

    # ---- MakerStrategy protocol -----------------------------------------

    def on_tick(
        self, tick: TickData
    ) -> list[PostQuote | object]:
        self._tick_count += 1
        if tick.is_trade:
            return [Hold()]
        return self._on_bidask(tick)

    def _on_bidask(
        self, tick: TickData
    ) -> list[PostQuote | object]:
        params = self._params
        if tick.bid_price <= 0 or tick.ask_price <= 0:
            return [Hold()]
        if tick.ask_price <= tick.bid_price:
            return [Hold()]

        scale = tick.scale
        spread_raw = tick.ask_price - tick.bid_price
        spread_pts = spread_raw // scale
        if spread_pts < params.spread_threshold_pts:
            self._spread_blocked += 1
            return [Hold()]

        # Microprice (Stoikov 2014): weighted-mid by far-side qty.
        bid_q = max(1, tick.bid_qty)
        ask_q = max(1, tick.ask_qty)
        microprice = (
            tick.bid_price * ask_q + tick.ask_price * bid_q
        ) / (bid_q + ask_q)

        # Refresh demand-function fit on cadence.
        self._maybe_refit(tick.exch_ts)

        # Per-side delta selection:
        #   - Cold-start phase (first cold_start_n_quotes per side): random
        #     exploration in {delta_min..delta_max} to seed the demand-function
        #     regression with delta-variance (β cannot be fit otherwise).
        #   - Adaptive phase: Chávez-Casillas closed-form δ* from the fit.
        #   - Fallback: touch quoting (delta_min) when fit not yet valid.
        if self._cold_start_buy_remaining > 0:
            delta_buy_ticks = self._rng.randint(
                params.delta_min_ticks, params.delta_max_ticks
            )
            self._cold_start_used_count += 1
        else:
            delta_buy_ticks = self._adaptive_delta(self._buy_fit)
        if self._cold_start_sell_remaining > 0:
            delta_sell_ticks = self._rng.randint(
                params.delta_min_ticks, params.delta_max_ticks
            )
            self._cold_start_used_count += 1
        else:
            delta_sell_ticks = self._adaptive_delta(self._sell_fit)

        # Inventory skew (R47 best practice) — fixed 0.X ticks per contract.
        pos = self._position
        skew_scaled = (pos * params.inventory_skew_tenths * scale) // 10

        # Compute quote prices.
        # buy_quote = bid - delta_buy_ticks * tick (snap below touch when
        #   delta>0 means more passive); sell_quote = ask + delta_sell_ticks.
        # Tick-grid snap: scaled int division.
        bid_quote = (
            tick.bid_price
            - delta_buy_ticks * _TICK_SIZE_SCALED
            - skew_scaled
        )
        ask_quote = (
            tick.ask_price
            + delta_sell_ticks * _TICK_SIZE_SCALED
            - skew_scaled
        )

        # Sanity: don't cross.
        if bid_quote >= ask_quote:
            return [Hold()]
        # Sanity: bid <= touch bid; ask >= touch ask.
        if bid_quote > tick.bid_price:
            bid_quote = tick.bid_price
        if ask_quote < tick.ask_price:
            ask_quote = tick.ask_price

        actions: list[PostQuote | object] = []
        max_pos = params.max_pos

        # Buy side
        if pos < max_pos:
            if bid_quote != self._last_bid:
                actions.append(PostQuote(side="buy", price=bid_quote, qty=1))
                self._last_bid = bid_quote
                self._quotes_posted += 1
                # Record posting for demand-function fit feedback.
                delta_pts_buy = (tick.bid_price - bid_quote) / scale
                self._quote_history_buy.append(
                    (delta_pts_buy, tick.exch_ts, False)
                )
        else:
            self._max_pos_blocked += 1

        # Sell side
        if pos > -max_pos:
            if ask_quote != self._last_ask:
                actions.append(PostQuote(side="sell", price=ask_quote, qty=1))
                self._last_ask = ask_quote
                self._quotes_posted += 1
                delta_pts_sell = (ask_quote - tick.ask_price) / scale
                self._quote_history_sell.append(
                    (delta_pts_sell, tick.exch_ts, False)
                )

        # Trim quote history to fit window.
        cutoff_ns = tick.exch_ts - self._window_ns
        self._quote_history_buy = [
            (d, t, f) for d, t, f in self._quote_history_buy if t >= cutoff_ns
        ]
        self._quote_history_sell = [
            (d, t, f) for d, t, f in self._quote_history_sell if t >= cutoff_ns
        ]

        return actions or [Hold()]

    def _maybe_refit(self, now_ns: int) -> None:
        if (
            now_ns - self._buy_fit.last_fit_ns >= self._refresh_ns
            and len(self._quote_history_buy) >= self._params.fit_min_samples
        ):
            samples = [(d, 1.0 if f else 0.0, t) for d, t, f in self._quote_history_buy]
            alpha, beta, valid = _exponential_weighted_fit(
                samples, now_ns, self._decay_ns
            )
            self._buy_fit.alpha = alpha
            self._buy_fit.beta = beta
            self._buy_fit.valid = valid
            self._buy_fit.last_fit_ns = now_ns
            self._buy_fit.n_fits += 1

        if (
            now_ns - self._sell_fit.last_fit_ns >= self._refresh_ns
            and len(self._quote_history_sell) >= self._params.fit_min_samples
        ):
            samples = [(d, 1.0 if f else 0.0, t) for d, t, f in self._quote_history_sell]
            alpha, beta, valid = _exponential_weighted_fit(
                samples, now_ns, self._decay_ns
            )
            self._sell_fit.alpha = alpha
            self._sell_fit.beta = beta
            self._sell_fit.valid = valid
            self._sell_fit.last_fit_ns = now_ns
            self._sell_fit.n_fits += 1

    def _adaptive_delta(self, fit: _DemandFitState) -> int:
        """Chávez-Casillas eq. 3.2 closed-form delta* (in ticks).

        Simplified large-tick variant: optimal_delta = -alpha / (2*beta)
        when beta < 0 (demand decreases in delta). Bounded by
        [delta_min_ticks, delta_max_ticks].

        Falls back to delta_min_ticks (touch) when fit not valid.
        """
        if not fit.valid:
            self._fit_fallback_count += 1
            return self._params.delta_min_ticks
        # When beta >= 0, demand is non-decreasing in delta — degenerate.
        # Chávez-Casillas requires beta<0 (downward-sloping demand).
        if fit.beta >= -1e-6:
            self._fit_fallback_count += 1
            return self._params.delta_min_ticks
        delta_optimal = -fit.alpha / (2.0 * fit.beta)
        self._fit_used_count += 1
        if delta_optimal < self._params.delta_min_ticks:
            return self._params.delta_min_ticks
        if delta_optimal > self._params.delta_max_ticks:
            return self._params.delta_max_ticks
        return int(delta_optimal)

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        if side == "buy":
            self._position += 1
            self._last_bid = None
            # Mark most recent buy quote as filled for fit feedback.
            if self._quote_history_buy:
                d, t, _ = self._quote_history_buy[-1]
                self._quote_history_buy[-1] = (d, t, True)
        elif side == "sell":
            self._position -= 1
            self._last_ask = None
            if self._quote_history_sell:
                d, t, _ = self._quote_history_sell[-1]
                self._quote_history_sell[-1] = (d, t, True)
        else:
            logger.warning("c1_unknown_fill_side", side=side)

    def on_gap(self) -> None:
        self._last_bid = None
        self._last_ask = None
        self._quote_history_buy.clear()
        self._quote_history_sell.clear()
        self._buy_fit = _DemandFitState()
        self._sell_fit = _DemandFitState()

    def reset(self) -> None:
        self._position = 0
        self._last_bid = None
        self._last_ask = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._max_pos_blocked = 0
        self._quotes_posted = 0
        self._buy_fit = _DemandFitState()
        self._sell_fit = _DemandFitState()
        self._quote_history_buy = []
        self._quote_history_sell = []
        self._fit_used_count = 0
        self._fit_fallback_count = 0

    def reset_for_day(self) -> None:
        """Full per-day reset called between backtest days.

        Resets ALL intra-day state (position, fits, quote history, counters,
        cold-start exploration budget).  Without this, multi-day backtest
        loops accumulate stale `_position` / fit state across days while the
        per-day FIFO PnL accounting assumes flat-start each day, biasing
        daily PnL and Gate C scorecard.

        Cold-start exploration budget is reset so each day re-bootstraps the
        demand fit (intentional: overnight fit assumptions decay).
        """
        self.reset()
        self._cold_start_buy_remaining = self._params.cold_start_n_quotes
        self._cold_start_sell_remaining = self._params.cold_start_n_quotes
        self._cold_start_used_count = 0


# ---------------------------------------------------------------------------
# AlphaProtocol shim
# ---------------------------------------------------------------------------


class C1Alpha:
    """AlphaProtocol wrapper for registry smoke-path."""

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C1Params | None = None,
        active_symbol: str = "TXFD6",
    ) -> None:
        self._maker = C1ChavezCasillasAdaptiveMaker(
            params=params, active_symbol=active_symbol
        )
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c1_revalidation_txfd6_chavez_casillas_adaptive",
            hypothesis=(
                "Re-validation of R1/C1 under 2026-04-27 'kill requires "
                "backtest' rule. Chávez-Casillas (2024) adaptive demand-"
                "function quoting on TXFD6 working zone (spread >= 4 pt). "
                "Microprice reservation reference. Cost RT 3.0 pt; F9 cost-"
                "tier carve-out vs TMFD6 (RT 4.0 pt). T1 best-case expected "
                "edge ~7.5 pt vs 6 pt 2x floor (25% margin)."
            ),
            formula=(
                "spread_gate(>=4pt) -> microprice = "
                "(bid*ask_qty + ask*bid_qty)/(bid_qty+ask_qty); "
                "rolling 30-min demand fit y=alpha+beta*delta with tau=5min "
                "exp decay, refit every 5s; delta* = -alpha/(2*beta) bounded "
                "[0,3] ticks; quote bid - delta*_b*tick - skew, "
                "ask + delta*_a*tick - skew."
            ),
            paper_refs=(
                "2024_Chavez-Casillas_Figueroa-Lopez_Yu_Zhang_adaptive_MM_"
                "inventory_liquidation_arxiv2405.11444",
                "2014_Stoikov_microprice",
                "2018_Krause_Fiegen_Guhr_emergence_stylized_facts_arxiv1812.07369",
                "feedback_taifex_fee_structure",
                "r47_maker_strategy",
                "r47_structural_properties",
            ),
            data_fields=(
                "bid_price",
                "ask_price",
                "bid_qty",
                "ask_qty",
                "trade_price",
                "trade_volume",
                "exch_ts",
            ),
            complexity="O(W) per refit, W = window samples (~5000-10000)",
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="r47_maker_shioaji_p95_v2026-04-24_measured",
            roles_used=("architect", "code-reviewer"),
            skills_used=("hft-backtester",),
            feature_set_version=None,
            strategy_type="maker",
            instrument="TXFD6",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def maker(self) -> C1ChavezCasillasAdaptiveMaker:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._maker.reset()
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C1Alpha",
    "C1Params",
    "C1ChavezCasillasAdaptiveMaker",
    "_TXF_POINT_VALUE_NTD",
    "_TXF_RT_COST_PTS",
]
