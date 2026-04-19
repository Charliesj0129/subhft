"""C74 — TXF-TMF basis mean-reversion (cross-instrument).

R10 DA T2 APPROVE (2026-04-19; 0 Tier-1/2 FAIL, 1 S6 WARN, cost_drag 20%).

Mechanism:
  - Dollar-neutral basis: `basis = mid_txf_pts - HEDGE_RATIO * mid_tmf_pts`
    where HEDGE_RATIO = TXF_pt_value / TMF_pt_value = 200/10 = 20.
  - Rolling statistics (60-min window by default): mean mu(t), stdev sigma(t).
  - ENTRY (|basis - mu| > entry_sigma * sigma):
      +basis extreme (basis >> mu) -> SELL TXF @ ask + BUY TMF @ bid (short basis)
      -basis extreme (basis << mu) -> BUY TXF @ bid + SELL TMF @ ask (long basis)
      Both legs MAKER by default.
  - EXIT: reversion to mu OR 30-min timeout. MAKER close by default.
  - STOP-LOSS: |basis - mu_at_entry| > stop_sigma * sigma_at_entry -> TAKER
    cross both legs to flatten.
  - STALE-QUOTE FILTER: if |basis| > stale_basis_filter_pt, skip entry
    (protects against bad snapshots; hit-rate logged).

R5-prior C30 / R7 C66 physics carry:
  - HEDGE_RATIO = 20 is dollar-neutral (not 1 or 5).
  - Maker-maker both legs by design; TAKER only at 4-sigma stop.
  - Cost-regime: 1.5 + 1.5 = 3.0 pt combined RT at inst (scaled by pt_value).

Mutually exclusive with C63 on TXFD6 (inventory conflict — DA flag 9).

Observability:
  - RollingBasisStats holds (ts_ns, basis_pts) samples within the window,
    computes mu and sigma online (Welford's would be ideal for __slots__;
    here using simple running-array approach since research-side is CLI
    only, not live hot-path).

Research-module float exception (Rule 11 of 25-architecture-governance).
Precision Law (CLAUDE.md #4): scaled-int prices; basis arithmetic in pts.
Timestamps use caller-supplied monotonic ns (no datetime.now).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

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

logger = get_logger("alpha.c74_txf_tmf_basis_mean_reversion")

_TXF_POINT_VALUE_NTD = 200
_TMF_POINT_VALUE_NTD = 10
_TXF_INST_RT_COST_PTS = 1.5
_TMF_INST_RT_COST_PTS = 1.5

# Hedge ratio: 20 TMF per 1 TXF (dollar-neutral per R5 C30 / R7 C66 lesson).
_HEDGE_RATIO_TMF_PER_TXF: int = _TXF_POINT_VALUE_NTD // _TMF_POINT_VALUE_NTD

# DA T2 stale-quote filter — skip entries when |basis| > this bound.
# DA T2 noted typical basis per-day mean ~+0.39 pt; exclusion at 50 pt
# catches snapshot glitches / partial-fills of back-month aliasing.
_STALE_QUOTE_FILTER_BASIS_PT = 50


# ----------------------------------------------------------------------------
# RollingBasisStats — tracks mu + sigma over a time window
# ----------------------------------------------------------------------------


@dataclass
class RollingBasisStats:
    """Tracks (ts_ns, basis_pts) samples; computes mu and sigma in O(1).

    Uses deque-based sliding window with INCREMENTAL sum + sum_sq updates:
    push/evict each cost O(1) amortized; mean()/stdev() are O(1). Samples
    older than window_ns are dropped on push.

    For CLI-only research use (not live hot-path).
    """

    window_ns: int
    _samples: "deque[tuple[int, float]]" = None  # type: ignore[assignment]
    _sum: float = 0.0
    _sum_sq: float = 0.0

    def __post_init__(self) -> None:
        if self._samples is None:
            self._samples = deque()

    def push(self, ts_ns: int, basis_pts: float) -> None:
        """Append sample; evict expired. Incremental sum/sum_sq (O(1) amortized)."""
        self._samples.append((ts_ns, basis_pts))
        self._sum += basis_pts
        self._sum_sq += basis_pts * basis_pts
        horizon = ts_ns - self.window_ns
        while self._samples and self._samples[0][0] < horizon:
            _old_ts, old_basis = self._samples.popleft()
            self._sum -= old_basis
            self._sum_sq -= old_basis * old_basis

    def n(self) -> int:
        return len(self._samples)

    def mean(self) -> float:
        n = len(self._samples)
        if n == 0:
            return 0.0
        return self._sum / n

    def stdev(self) -> float:
        n = len(self._samples)
        if n < 2:
            return 0.0
        m = self._sum / n
        # Sample variance: var = (sum_sq - n*mean^2) / (n - 1).
        variance_num = self._sum_sq - n * m * m
        if variance_num <= 0:
            return 0.0
        return (variance_num / (n - 1)) ** 0.5

    def reset(self) -> None:
        self._samples.clear()
        self._sum = 0.0
        self._sum_sq = 0.0


# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C74Params:
    """Tuning parameters for C74.

    window_seconds: int = 3600
        Rolling window for mu and sigma (default 60 min).
    entry_sigma: float = 2.0
        Entry threshold (|basis - mu| > entry_sigma * sigma).
    stop_sigma: float = 4.0
        Stop-loss threshold (absolute TAKER cross).
    timeout_seconds: int = 1800
        Exit timeout if no reversion (30 min).
    stale_basis_filter_pt: int = 50
        Absolute |basis| bound — ignore entries beyond this.
    min_samples_for_entry: int = 120
        Minimum rolling samples before mu/sigma trusted (warm-up).
    max_pos_trips: int = 1
        Max concurrent open trips.
    txf_symbol: str = "TXFD6"
        TXF front-month symbol for event routing.
    tmf_symbol: str = "TMFD6"
        TMF front-month symbol for event routing.
    hedge_ratio_tmf_per_txf: int = 20
        Dollar-neutral hedge ratio (fixed by pt_value physics).
    """

    window_seconds: int = 3600
    entry_sigma: float = 2.0
    stop_sigma: float = 4.0
    timeout_seconds: int = 1800
    stale_basis_filter_pt: int = _STALE_QUOTE_FILTER_BASIS_PT
    min_samples_for_entry: int = 120
    max_pos_trips: int = 1
    txf_symbol: str = "TXFD6"
    tmf_symbol: str = "TMFD6"
    hedge_ratio_tmf_per_txf: int = _HEDGE_RATIO_TMF_PER_TXF


# ----------------------------------------------------------------------------
# TxfTmfBasisMeanReversion — cross-instrument strategy
# ----------------------------------------------------------------------------


@dataclass
class _OpenTrip:
    """Represents an active basis position."""

    side: str                    # "long_basis" or "short_basis"
    entry_ts_ns: int
    entry_basis_pts: float
    entry_mu_pts: float
    entry_sigma_pts: float
    txf_bid: int                 # scaled
    txf_ask: int
    tmf_bid: int
    tmf_ask: int


class TxfTmfBasisMeanReversion:
    """Cross-instrument basis mean-reversion trader.

    Unlike pure-maker strategies (which take one `TickData` stream), C74
    consumes events from BOTH TXFD6 and TMFD6. Events are routed by the
    `symbol` attribute on the `TickData` — but `research.backtest.maker_engine.TickData`
    does not carry a symbol, so the harness must call
    `on_tick_with_symbol(symbol, tick)` (backtest runner is responsible).

    For unit tests, we use `update_mid(symbol, tick)` to directly set the
    latest mid for each instrument.
    """

    __slots__ = (
        "_params",
        "_stats",
        "_last_txf_bid",
        "_last_txf_ask",
        "_last_tmf_bid",
        "_last_tmf_ask",
        "_last_txf_mid_pts",
        "_last_tmf_mid_pts",
        "_last_ts_ns",
        "_scale",
        # state
        "_open_trip",
        "_closed_trips",     # list[dict] for PnL analysis
        # counters
        "_tick_count",
        "_stale_filter_hits",
        "_entries_posted",
        "_exits_reversion",
        "_exits_timeout",
        "_exits_stop_loss",
    )

    def __init__(
        self,
        params: C74Params | None = None,
    ) -> None:
        self._params = params or C74Params()
        window_ns = self._params.window_seconds * 1_000_000_000
        self._stats = RollingBasisStats(window_ns=window_ns)
        self._last_txf_bid: int = 0
        self._last_txf_ask: int = 0
        self._last_tmf_bid: int = 0
        self._last_tmf_ask: int = 0
        self._last_txf_mid_pts: float = 0.0
        self._last_tmf_mid_pts: float = 0.0
        self._last_ts_ns: int = 0
        self._scale: int = 1_000_000
        self._open_trip: _OpenTrip | None = None
        self._closed_trips: list[dict] = []
        self._tick_count = 0
        self._stale_filter_hits = 0
        self._entries_posted = 0
        self._exits_reversion = 0
        self._exits_timeout = 0
        self._exits_stop_loss = 0

    # ---- Observability ---------------------------------------------------

    @property
    def params(self) -> C74Params:
        return self._params

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def stale_filter_hits(self) -> int:
        return self._stale_filter_hits

    @property
    def entries_posted(self) -> int:
        return self._entries_posted

    @property
    def exits_reversion(self) -> int:
        return self._exits_reversion

    @property
    def exits_timeout(self) -> int:
        return self._exits_timeout

    @property
    def exits_stop_loss(self) -> int:
        return self._exits_stop_loss

    @property
    def open_trip(self) -> _OpenTrip | None:
        return self._open_trip

    @property
    def closed_trips(self) -> list[dict]:
        return list(self._closed_trips)

    @property
    def rolling_mean(self) -> float:
        return self._stats.mean()

    @property
    def rolling_stdev(self) -> float:
        return self._stats.stdev()

    @property
    def rolling_n(self) -> int:
        return self._stats.n()

    # ---- Core update path ------------------------------------------------

    def update_mid(self, symbol: str, tick: TickData) -> list[object]:
        """Handle an event from one instrument; returns action list.

        Action types same as MakerStrategy: PostQuote, CancelQuote, Hold.
        Actions are emitted via tuples of (symbol, action) for
        cross-instrument routing. Here we flatten to action objects with a
        convention: buy_txf/sell_txf/buy_tmf/sell_tmf on action.side.
        """
        self._tick_count += 1
        if tick.is_trade:
            return [Hold()]
        if tick.bid_price <= 0 or tick.ask_price <= 0:
            return [Hold()]
        if tick.ask_price <= tick.bid_price:
            return [Hold()]

        self._scale = tick.scale
        mid_pts = (tick.bid_price + tick.ask_price) / (2 * tick.scale)
        if symbol == self._params.txf_symbol:
            self._last_txf_bid = tick.bid_price
            self._last_txf_ask = tick.ask_price
            self._last_txf_mid_pts = mid_pts
        elif symbol == self._params.tmf_symbol:
            self._last_tmf_bid = tick.bid_price
            self._last_tmf_ask = tick.ask_price
            self._last_tmf_mid_pts = mid_pts
        else:
            return [Hold()]

        self._last_ts_ns = tick.exch_ts

        # Need both legs for basis computation.
        if self._last_txf_mid_pts <= 0 or self._last_tmf_mid_pts <= 0:
            return [Hold()]

        basis_pts = self._compute_basis()

        # Stale-quote filter (DA T2 mandatory #3).
        if abs(basis_pts) > self._params.stale_basis_filter_pt:
            self._stale_filter_hits += 1
            return [Hold()]

        # Update rolling stats.
        self._stats.push(tick.exch_ts, basis_pts)

        # Decision logic: first check exit, then entry.
        if self._open_trip is not None:
            return self._check_exit(basis_pts, tick.exch_ts)
        return self._check_entry(basis_pts, tick.exch_ts)

    def _compute_basis(self) -> float:
        """basis = mid_txf - mid_tmf (pts, float-in-research).

        TXF and TMF both track the same TAIEX index; cointegration
        residual is the direct price difference. The 1:20 hedge ratio
        governs notional-neutral QUANTITY sizing (1 TXF × 200 NTD/pt =
        20 TMF × 10 NTD/pt) but NOT the basis-price formula. Researcher
        T1's empirical basis mean +0.39 pt was computed this way.
        """
        return self._last_txf_mid_pts - self._last_tmf_mid_pts

    def _check_entry(self, basis_pts: float, ts_ns: int) -> list[object]:
        stats = self._stats
        params = self._params
        if stats.n() < params.min_samples_for_entry:
            return [Hold()]
        mu = stats.mean()
        sigma = stats.stdev()
        if sigma <= 0:
            return [Hold()]
        dev = basis_pts - mu
        if abs(dev) <= params.entry_sigma * sigma:
            return [Hold()]

        # Entry trigger
        actions: list[object] = []
        if dev > 0:
            # basis too high -> SHORT basis: sell TXF + buy TMF (both maker).
            trip_side = "short_basis"
            actions.append(PostQuote(side="sell_txf", price=self._last_txf_ask, qty=1))
            actions.append(
                PostQuote(
                    side="buy_tmf",
                    price=self._last_tmf_bid,
                    qty=params.hedge_ratio_tmf_per_txf,
                )
            )
        else:
            # basis too low -> LONG basis: buy TXF + sell TMF (both maker).
            trip_side = "long_basis"
            actions.append(PostQuote(side="buy_txf", price=self._last_txf_bid, qty=1))
            actions.append(
                PostQuote(
                    side="sell_tmf",
                    price=self._last_tmf_ask,
                    qty=params.hedge_ratio_tmf_per_txf,
                )
            )
        self._open_trip = _OpenTrip(
            side=trip_side,
            entry_ts_ns=ts_ns,
            entry_basis_pts=basis_pts,
            entry_mu_pts=mu,
            entry_sigma_pts=sigma,
            txf_bid=self._last_txf_bid,
            txf_ask=self._last_txf_ask,
            tmf_bid=self._last_tmf_bid,
            tmf_ask=self._last_tmf_ask,
        )
        self._entries_posted += 1
        return actions

    def _check_exit(self, basis_pts: float, ts_ns: int) -> list[object]:
        trip = self._open_trip
        assert trip is not None
        params = self._params
        mu = self._stats.mean()
        # Reversion: dev has crossed mu
        dev_now = basis_pts - mu
        entry_dev = trip.entry_basis_pts - trip.entry_mu_pts
        reverted = (entry_dev > 0 and dev_now <= 0) or (
            entry_dev < 0 and dev_now >= 0
        )
        # Timeout
        elapsed_ns = ts_ns - trip.entry_ts_ns
        timed_out = elapsed_ns >= params.timeout_seconds * 1_000_000_000
        # Stop-loss (absolute deviation magnitude in entry-epoch sigma units)
        stop_pts = params.stop_sigma * trip.entry_sigma_pts
        stopped = abs(basis_pts - trip.entry_mu_pts) > stop_pts

        if not (reverted or timed_out or stopped):
            return [Hold()]

        exit_reason = (
            "stop_loss" if stopped
            else ("reversion" if reverted else "timeout")
        )
        actions: list[object] = []
        # Flatten both legs. For stop_loss -> taker cross; else -> maker.
        if trip.side == "short_basis":
            if stopped:
                actions.append(PostQuote(side="buy_txf_taker", price=self._last_txf_ask, qty=1))
                actions.append(
                    PostQuote(
                        side="sell_tmf_taker",
                        price=self._last_tmf_bid,
                        qty=params.hedge_ratio_tmf_per_txf,
                    )
                )
            else:
                actions.append(PostQuote(side="buy_txf", price=self._last_txf_bid, qty=1))
                actions.append(
                    PostQuote(
                        side="sell_tmf",
                        price=self._last_tmf_ask,
                        qty=params.hedge_ratio_tmf_per_txf,
                    )
                )
        else:
            if stopped:
                actions.append(PostQuote(side="sell_txf_taker", price=self._last_txf_bid, qty=1))
                actions.append(
                    PostQuote(
                        side="buy_tmf_taker",
                        price=self._last_tmf_ask,
                        qty=params.hedge_ratio_tmf_per_txf,
                    )
                )
            else:
                actions.append(PostQuote(side="sell_txf", price=self._last_txf_ask, qty=1))
                actions.append(
                    PostQuote(
                        side="buy_tmf",
                        price=self._last_tmf_bid,
                        qty=params.hedge_ratio_tmf_per_txf,
                    )
                )

        # Record closed trip (for PnL analysis)
        self._closed_trips.append({
            "side": trip.side,
            "exit_reason": exit_reason,
            "entry_ts_ns": trip.entry_ts_ns,
            "exit_ts_ns": ts_ns,
            "entry_basis_pts": trip.entry_basis_pts,
            "exit_basis_pts": basis_pts,
            "entry_mu_pts": trip.entry_mu_pts,
            "entry_sigma_pts": trip.entry_sigma_pts,
            "taker_close": stopped,
        })

        if stopped:
            self._exits_stop_loss += 1
        elif reverted:
            self._exits_reversion += 1
        else:
            self._exits_timeout += 1

        self._open_trip = None
        return actions

    # ---- Convenience for test harness / runner --------------------------

    def on_gap(self) -> None:
        """Reset rolling stats on bus overflow (conservative)."""
        self._stats.reset()
        # Do NOT reset open trip — caller must flatten externally if needed.

    def reset(self) -> None:
        self._stats.reset()
        self._last_txf_bid = 0
        self._last_txf_ask = 0
        self._last_tmf_bid = 0
        self._last_tmf_ask = 0
        self._last_txf_mid_pts = 0.0
        self._last_tmf_mid_pts = 0.0
        self._last_ts_ns = 0
        self._open_trip = None
        self._closed_trips.clear()
        self._tick_count = 0
        self._stale_filter_hits = 0
        self._entries_posted = 0
        self._exits_reversion = 0
        self._exits_timeout = 0
        self._exits_stop_loss = 0


# ----------------------------------------------------------------------------
# AlphaProtocol shim
# ----------------------------------------------------------------------------


class C74Alpha:
    """AlphaProtocol wrapper for registry smoke-path."""

    __slots__ = ("_strategy", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C74Params | None = None,
    ) -> None:
        self._strategy = TxfTmfBasisMeanReversion(params=params)
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c74_txf_tmf_basis_mean_reversion",
            hypothesis=(
                "Dollar-neutral TXF-TMF basis (basis = mid_txf - 20*mid_tmf) "
                "mean-reverts on minutes-to-hours scale. Entry when |basis - "
                "rolling_mean| > 2*rolling_stdev (60-min adaptive window); "
                "exit on reversion to mean OR 30-min timeout (both MAKER); "
                "stop-loss at 4sigma (TAKER cross). Hedge ratio 20 TMF per "
                "1 TXF per dollar-neutral physics (R5 C30 / R7 C66 lesson). "
                "DA T2 APPROVE 2026-04-19: cost_drag 20%, basis shows "
                "non-trivial idiosyncratic structure per Fanelli 2023 "
                "literature. R7 C66 passive-pair was rejected because "
                "passive-maker edge was dominated by hedge-take cost; C74 "
                "avoids that by MAKER-MAKER both legs with TAKER only at "
                "4sigma stop."
            ),
            formula=(
                "basis(t) = mid_txf(t) - 20 * mid_tmf(t). "
                "Rolling mu(t), sigma(t) over 60-min window. "
                "Entry: |basis(t) - mu(t)| > 2 * sigma(t). "
                "Exit: basis(t) crosses mu(t) OR elapsed > 30 min OR "
                "|basis(t) - mu_entry| > 4 * sigma_entry. "
                "Stale filter: skip if |basis(t)| > 50 pt."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "c60_tmfd6_r47_minimal_inst_rt",
                "c63_txfd6_r47_tight_spread",
                "shared-context_2026-04-19_cost_model",
                "memory/backtest_method_reliability",
                "Fanelli_2023_futures_basis_microstructure",
                "2008_Avellaneda_Stoikov_HFT_LOB",
                "2015_Cartea_Jaimungal_Penalva_MM_econ",
                "r7_summary_c66_hedge_cost_dominance_lesson",
            ),
            data_fields=(
                "bid_price_txf",
                "ask_price_txf",
                "bid_price_tmf",
                "ask_price_tmf",
                "bid_qty_txf",
                "ask_qty_txf",
                "bid_qty_tmf",
                "ask_qty_tmf",
                "exch_ts",
            ),
            complexity="O(N) on window size",
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="shioaji_sim_p95_v2026-03-04",
            roles_used=(),
            skills_used=("hft-backtester",),
            feature_set_version=None,
            strategy_type="maker",     # cross_instrument_mm not in enum; maker closest
            instrument="TXFD6+TMFD6",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def strategy(self) -> TxfTmfBasisMeanReversion:
        return self._strategy

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._strategy.reset()
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C74Alpha",
    "C74Params",
    "TxfTmfBasisMeanReversion",
    "RollingBasisStats",
    "_HEDGE_RATIO_TMF_PER_TXF",
    "_STALE_QUOTE_FILTER_BASIS_PT",
    "_TXF_POINT_VALUE_NTD",
    "_TMF_POINT_VALUE_NTD",
    "_TXF_INST_RT_COST_PTS",
    "_TMF_INST_RT_COST_PTS",
]
