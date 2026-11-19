"""R47 Maker — Three-Layer Market-Making Strategy.

Extends SimpleMarketMaker with three signal layers:
  D1: Permutation Entropy H on QI_1 — regime gate (quote/no-quote)
  D2: Queue Survival — M/M/1 depletion probability → quote suppression
  D3: MFG Inventory Proxy — cumulative signed flow → asymmetric spread widening

Supports cross-instrument mode: signal from TXFD6, trade on TMFD6.
Set ``trade_symbol`` param to override execution symbol.

Research: research/alphas/r47_maker_pivot/ (Gate 0 validated, Stage 4 backtested)

Economics (TMFD6 — 微台期):
    1 point = 10 NTD, RT cost = 40 NTD = 4.0 points
    Breakeven spread: > 4.0 points
"""

from __future__ import annotations

import math
from collections import deque

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus
from hft_platform.contracts.strategy import RiskFeedback, Side
from hft_platform.core.timebase import now_ns as _now_ns
from hft_platform.events import (
    FeatureUpdateEvent,
    GapEvent,
    LOBStatsEvent,
    TickEvent,
)
from hft_platform.strategies.simple_mm import SimpleMarketMaker
from hft_platform.strategy.base import QUALITY_FLAGS_CORRUPT

logger = get_logger("strategy.r47_maker")

_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10
_IDX_TOXICITY_EMA50_X1000 = 21

_PRICE_SCALE = 10000
_LOG_INTERVAL = 500


class _PEState:
    """Sliding-window Permutation Entropy on QI_1 (D=4, pre-allocated)."""

    __slots__ = (
        "_d",
        "_n_patterns",
        "_h_max",
        "_window_size",
        "_qi_buf",
        "_qi_len",
        "_pattern_counts",
        "_pat_deque",
        "_h",
        "_warmup_done",
    )

    def __init__(self, d: int = 4, window: int = 100) -> None:
        self._d = d
        self._n_patterns = math.factorial(d)
        self._h_max = math.log2(self._n_patterns)
        self._window_size = window
        self._qi_buf: deque[float] = deque(maxlen=d)
        self._qi_len = 0
        self._pattern_counts = [0] * self._n_patterns
        pat_win = window - d + 1
        self._pat_deque: deque[int] = deque(maxlen=pat_win)
        self._h: float = 1.0
        self._warmup_done = False

    def update(self, qi_value: float) -> float:
        """Update with new QI_1 value, return normalized entropy H."""
        self._qi_buf.append(qi_value)
        self._qi_len += 1

        if self._qi_len < self._d:
            return self._h

        buf = list(self._qi_buf)
        pattern_id = self._rank_to_id(buf)

        if len(self._pat_deque) == self._pat_deque.maxlen:
            old = self._pat_deque[0]
            self._pattern_counts[old] -= 1
        self._pat_deque.append(pattern_id)
        self._pattern_counts[pattern_id] += 1

        n_samples = len(self._pat_deque)
        if n_samples < 20:
            return self._h

        self._warmup_done = True

        h = 0.0
        for c in self._pattern_counts:
            if c > 0:
                p = c / n_samples
                h -= p * math.log2(p)
        self._h = h / self._h_max if self._h_max > 0 else 0.0
        return self._h

    @staticmethod
    def _rank_to_id(vals: list[float]) -> int:
        """Map D values to pattern index via Lehmer code (factorial number system).

        Produces a bijective mapping from permutations to 0..D!-1.
        For D=4: 24 unique indices guaranteed (no collisions).
        """
        d = len(vals)
        ranks = [0] * d
        for i in range(d):
            for j in range(d):
                if vals[j] < vals[i] or (vals[j] == vals[i] and j < i):
                    ranks[i] += 1
        idx = 0
        factorials = [1] * d
        f = 1
        for i in range(d - 1, -1, -1):
            factorials[i] = f
            f *= d - i
        for i in range(d):
            count = 0
            for j in range(i + 1, d):
                if ranks[j] < ranks[i]:
                    count += 1
            idx += count * factorials[i]
        return idx

    @property
    def h(self) -> float:
        return self._h

    @property
    def warmed_up(self) -> bool:
        return self._warmup_done


class _QueueState:
    """M/M/1 queue survival estimation from L1 snapshot diffs."""

    __slots__ = (
        "_ema_alpha",
        "_lambda_bid",
        "_mu_bid",
        "_lambda_ask",
        "_mu_ask",
        "_prev_bv",
        "_prev_av",
        "_p_depl_bid",
        "_p_depl_ask",
        "_warmed_up",
        "_update_count",
    )

    def __init__(self, ema_alpha: float = 0.05) -> None:
        self._ema_alpha = ema_alpha
        self._lambda_bid = 1.0
        self._mu_bid = 1.0
        self._lambda_ask = 1.0
        self._mu_ask = 1.0
        self._prev_bv: int = 0
        self._prev_av: int = 0
        self._p_depl_bid: float = 0.5
        self._p_depl_ask: float = 0.5
        self._warmed_up = False
        self._update_count = 0

    def update(self, bid_qty: int, ask_qty: int) -> tuple[float, float]:
        """Update with L1 quantities, return (P_depl_bid, P_depl_ask)."""
        self._update_count += 1

        if self._update_count < 2:
            self._prev_bv = bid_qty
            self._prev_av = ask_qty
            return self._p_depl_bid, self._p_depl_ask

        if self._update_count > 50:
            self._warmed_up = True

        alpha = self._ema_alpha
        d_bid = bid_qty - self._prev_bv
        d_ask = ask_qty - self._prev_av

        if d_bid > 0:
            self._lambda_bid = alpha * d_bid + (1 - alpha) * self._lambda_bid
        elif d_bid < 0:
            self._mu_bid = alpha * (-d_bid) + (1 - alpha) * self._mu_bid

        if d_ask > 0:
            self._lambda_ask = alpha * d_ask + (1 - alpha) * self._lambda_ask
        elif d_ask < 0:
            self._mu_ask = alpha * (-d_ask) + (1 - alpha) * self._mu_ask

        q_bid = max(bid_qty, 1)
        q_ask = max(ask_qty, 1)

        rho_bid = self._mu_bid / max(self._lambda_bid, 1e-6)
        rho_ask = self._mu_ask / max(self._lambda_ask, 1e-6)

        self._p_depl_bid = min(1.0, rho_bid**q_bid) if rho_bid > 0 else 0.0
        self._p_depl_ask = min(1.0, rho_ask**q_ask) if rho_ask > 0 else 0.0

        self._prev_bv = bid_qty
        self._prev_av = ask_qty
        return self._p_depl_bid, self._p_depl_ask

    @property
    def p_depl_bid(self) -> float:
        return self._p_depl_bid

    @property
    def p_depl_ask(self) -> float:
        return self._p_depl_ask

    @property
    def warmed_up(self) -> bool:
        return self._warmed_up


class _MFGState:
    """Cumulative signed flow proxy for MM inventory estimation."""

    __slots__ = (
        "_ema_alpha",
        "_signed_flow_ema",
        "_flow_std_ema",
        "_capitulation_z",
        "_update_count",
        "_warmed_up",
    )

    def __init__(self, ema_alpha: float = 0.01) -> None:
        self._ema_alpha = ema_alpha
        self._signed_flow_ema: float = 0.0
        self._flow_std_ema: float = 1.0
        self._capitulation_z: float = 0.0
        self._update_count = 0
        self._warmed_up = False

    def update_tick(self, direction: int, volume: int) -> None:
        """Update with signed trade event."""
        self._update_count += 1
        if self._update_count > 200:
            self._warmed_up = True

        signed = direction * volume
        alpha = self._ema_alpha
        self._signed_flow_ema = alpha * signed + (1 - alpha) * self._signed_flow_ema
        dev_sq = (signed - self._signed_flow_ema) ** 2
        self._flow_std_ema = alpha * dev_sq + (1 - alpha) * self._flow_std_ema
        std = max(math.sqrt(self._flow_std_ema), 1e-6)
        self._capitulation_z = abs(self._signed_flow_ema) / std

    @property
    def capitulation_z(self) -> float:
        return self._capitulation_z

    @property
    def flow_direction(self) -> int:
        """Direction of cumulative flow: +1 = net buying, -1 = net selling."""
        if self._signed_flow_ema > 0:
            return 1
        elif self._signed_flow_ema < 0:
            return -1
        return 0

    @property
    def warmed_up(self) -> bool:
        return self._warmed_up


class R47MakerStrategy(SimpleMarketMaker):
    """Three-layer maker strategy with PE regime gate, queue survival
    cancel trigger, MFG inventory skew, and D4 QI adverse-selection skew.

    Parameters
    ----------
    pe_danger_threshold : float
        PE entropy H below which market is too structured (trending).
        Quote very conservatively or pull.
    queue_cancel_threshold : float
        P(depletion) above which to cancel near-side quote.
    mfg_skew_z_threshold : float
        MFG |z-score| above which to apply inventory skew.
    spread_threshold_pts : int
        Minimum spread in points to quote (inherited from OpMM concept).
    toxicity_max : int
        Maximum toxicity x1000 to allow quoting.
    qi_skew_threshold : float
        L1 queue imbalance |QI| above which to widen the adverse side.
        QI = (bid_qty - ask_qty) / (bid_qty + ask_qty).
        When QI > threshold (buying pressure), widen ask by qi_widen_ticks.
        When QI < -threshold (selling pressure), widen bid.
        Set to 1.0 to disable (|QI| never exceeds 1.0).
    qi_widen_ticks : int
        Number of ticks to widen the adverse side when QI threshold is hit.
    """

    def __init__(
        self,
        strategy_id: str = "r47_maker",
        pe_danger_threshold: float = 0.55,
        pe_window: int = 100,
        queue_cancel_threshold: float = 0.7,
        queue_ema_alpha: float = 0.05,
        mfg_skew_z_threshold: float = 2.0,
        mfg_ema_alpha: float = 0.01,
        spread_threshold_pts: int = 5,
        toxicity_max: int = 700,
        qi_skew_threshold: float = 1.0,
        qi_widen_ticks: int = 1,
        stale_quote_max_ticks: int = 3,
        quote_cooldown_ms: int = 1000,
        trade_symbol: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(strategy_id=strategy_id, **kwargs)
        self._trade_symbol = trade_symbol

        self._pe_danger = pe_danger_threshold
        self._pe_states: dict[str, _PEState] = {}
        self._pe_window = pe_window

        self._queue_cancel_thresh = queue_cancel_threshold
        self._queue_states: dict[str, _QueueState] = {}
        self._queue_ema = queue_ema_alpha

        self._mfg_skew_z_thresh = mfg_skew_z_threshold
        self._mfg_states: dict[str, _MFGState] = {}
        self._mfg_ema = mfg_ema_alpha

        self._spread_thresh_scaled = spread_threshold_pts * _PRICE_SCALE
        self._toxicity_max = toxicity_max

        self._qi_skew_thresh = qi_skew_threshold
        self._qi_widen_ticks = qi_widen_ticks

        self._stale_quote_max_scaled = stale_quote_max_ticks * _PRICE_SCALE

        self._feature_cache: dict[str, tuple[int | float, ...]] = {}

        self._stats_count = 0
        self._pe_blocked = 0
        self._queue_suppressed = 0
        self._mfg_skewed = 0
        self._qi_widened = 0
        self._spread_blocked = 0
        self._toxicity_blocked = 0
        self._quotes_sent = 0
        self._stale_cancels = 0
        self._nonpositive_quote_blocked = 0

        self._suppress_bid: bool = False
        self._suppress_ask: bool = False

        self._qi_widen_bid: int = 0
        self._qi_widen_ask: int = 0

        self._local_pos: dict[str, int] = {}

        self._pending_buy: dict[str, int] = {}
        self._pending_sell: dict[str, int] = {}

        self._last_bid: dict[str, int] = {}
        self._last_ask: dict[str, int] = {}

        self._seen_fill_ids: set[str] = set()
        self._FILL_DEDUP_MAX = 500

        self._ttl_released_keys: set[str] = set()
        self._TTL_RELEASED_MAX = 2048

        self._active_buy_oid: dict[str, str] = {}
        self._active_sell_oid: dict[str, str] = {}

        self._inflight_buy_oids: dict[str, set[str]] = {}
        self._inflight_sell_oids: dict[str, set[str]] = {}

        self._metric_stale_cancels = None
        self._metric_inflight_oids = None
        self._metric_pending_qty = None
        self._metric_gate_blocked = None
        self._gate_blocked_published: dict[str, int] = {}
        try:
            from hft_platform.observability.metrics import MetricsRegistry as _MR

            m = _MR.get()
            self._metric_stale_cancels = getattr(m, "strategy_stale_cancels_total", None)
            self._metric_inflight_oids = getattr(m, "strategy_inflight_oids", None)
            self._metric_pending_qty = getattr(m, "strategy_pending_qty", None)
            self._metric_gate_blocked = getattr(m, "strategy_gate_blocked_total", None)
        except Exception as exc:  # pragma: no cover
            logger.debug("r47_metrics_lookup_fallback", error=str(exc))

        self._last_quote_ns: dict[str, int] = {}
        self._QUOTE_COOLDOWN_NS: int = quote_cooldown_ms * 1_000_000

        logger.info(
            "R47MakerStrategy initialized",
            pe_danger=pe_danger_threshold,
            queue_cancel=queue_cancel_threshold,
            mfg_z=mfg_skew_z_threshold,
            spread_pts=spread_threshold_pts,
            qi_skew=qi_skew_threshold,
            qi_widen=qi_widen_ticks,
        )

    def _get_pe(self, symbol: str) -> _PEState:
        state = self._pe_states.get(symbol)
        if state is None:
            state = _PEState(d=4, window=self._pe_window)
            self._pe_states[symbol] = state
        return state

    def _get_queue(self, symbol: str) -> _QueueState:
        state = self._queue_states.get(symbol)
        if state is None:
            state = _QueueState(ema_alpha=self._queue_ema)
            self._queue_states[symbol] = state
        return state

    def _get_mfg(self, symbol: str) -> _MFGState:
        state = self._mfg_states.get(symbol)
        if state is None:
            state = _MFGState(ema_alpha=self._mfg_ema)
            self._mfg_states[symbol] = state
        return state

    def _exec_symbol(self, signal_symbol: str) -> str:
        """Return the symbol to use for order placement and position tracking."""
        return self._trade_symbol if self._trade_symbol else signal_symbol

    def on_tick(self, event: TickEvent) -> None:
        """D3: Update MFG signed flow on each trade."""
        symbol = event.symbol
        mfg = self._get_mfg(symbol)
        direction = getattr(event, "trade_direction", 0)
        volume = event.volume if event.volume else 1
        if direction != 0:
            mfg.update_tick(direction, volume)

    def on_features(self, event: FeatureUpdateEvent) -> None:
        """Cache features + update D1 PE and D2 Queue states."""
        if event.values is None:
            return
        if event.quality_flags & QUALITY_FLAGS_CORRUPT:
            logger.debug(
                "r47_features_skipped_corrupt",
                symbol=event.symbol,
                quality_flags=event.quality_flags,
            )
            return
        symbol = event.symbol
        self._feature_cache[symbol] = event.values

        features = event.values

        if len(features) > _IDX_L1_IMBALANCE_PPM:
            qi_val = float(features[_IDX_L1_IMBALANCE_PPM]) / 1_000_000
            pe = self._get_pe(symbol)
            pe.update(qi_val)

        if len(features) > _IDX_L1_ASK_QTY:
            bid_qty = int(features[_IDX_L1_BID_QTY])
            ask_qty = int(features[_IDX_L1_ASK_QTY])
            queue = self._get_queue(symbol)
            queue.update(bid_qty, ask_qty)

            self._suppress_bid = False
            self._suppress_ask = False
            if queue.warmed_up and self._queue_cancel_thresh < 1.0:
                if queue.p_depl_bid > self._queue_cancel_thresh:
                    self._suppress_bid = True
                if queue.p_depl_ask > self._queue_cancel_thresh:
                    self._suppress_ask = True
                if self._suppress_bid or self._suppress_ask:
                    self._queue_suppressed += 1

            self._qi_widen_bid = 0
            self._qi_widen_ask = 0
            if self._qi_skew_thresh < 1.0:
                total_qty = bid_qty + ask_qty
                if total_qty > 0:
                    qi = (bid_qty - ask_qty) / total_qty
                    if qi > self._qi_skew_thresh:
                        self._qi_widen_ask = self._qi_widen_ticks
                        self._qi_widened += 1
                    elif qi < -self._qi_skew_thresh:
                        self._qi_widen_bid = self._qi_widen_ticks
                        self._qi_widened += 1

    def on_fill(self, event: FillEvent) -> None:
        """Track position locally to avoid stale StrategyContext cache."""
        fid = getattr(event, "fill_id", None) or ""
        if fid and fid in self._seen_fill_ids:
            logger.warning("r47_duplicate_fill_skipped", fill_id=fid, symbol=event.symbol)
            return
        if fid:
            self._seen_fill_ids.add(fid)
            if len(self._seen_fill_ids) > self._FILL_DEDUP_MAX:
                evict = len(self._seen_fill_ids) - self._FILL_DEDUP_MAX // 2
                victims = set()
                for fid in self._seen_fill_ids:
                    if len(victims) >= evict:
                        break
                    victims.add(fid)
                self._seen_fill_ids -= victims
        sym = event.symbol
        delta = event.qty if event.side == Side.BUY else -event.qty
        self._local_pos[sym] = self._local_pos.get(sym, 0) + delta
        if event.side == Side.BUY:
            self._pending_buy[sym] = max(0, self._pending_buy.get(sym, 0) - event.qty)
            self._active_buy_oid.pop(sym, None)
            self._discard_inflight_oid(sym, Side.BUY, event.order_id)
        else:
            self._pending_sell[sym] = max(0, self._pending_sell.get(sym, 0) - event.qty)
            self._active_sell_oid.pop(sym, None)
            self._discard_inflight_oid(sym, Side.SELL, event.order_id)
        if self._pending_buy.get(sym, 0) == 0 and self._pending_sell.get(sym, 0) == 0:
            self._last_quote_ns.pop(sym, None)
        logger.info(
            "r47_fill",
            symbol=sym,
            side=event.side.name,
            qty=event.qty,
            price=event.price,
            local_pos=self._local_pos[sym],
            ctx_pos=self.position(sym),
        )

    def on_order(self, event: OrderEvent) -> None:
        """Track order IDs for cancel-before-requote and release pending on terminal."""
        sym = event.symbol
        if event.status == OrderStatus.SUBMITTED:
            if event.side == Side.BUY:
                self._active_buy_oid[sym] = event.order_id
                self._inflight_buy_oids.setdefault(sym, set()).add(event.order_id)
                self._set_inflight_metric(sym, Side.BUY)
            else:
                self._active_sell_oid[sym] = event.order_id
                self._inflight_sell_oids.setdefault(sym, set()).add(event.order_id)
                self._set_inflight_metric(sym, Side.SELL)
            return
        if event.status not in (OrderStatus.CANCELLED, OrderStatus.FAILED):
            return
        if event.side == Side.BUY:
            if self._active_buy_oid.get(sym) == event.order_id:
                del self._active_buy_oid[sym]
            self._discard_inflight_oid(sym, Side.BUY, event.order_id)
        else:
            if self._active_sell_oid.get(sym) == event.order_id:
                del self._active_sell_oid[sym]
            self._discard_inflight_oid(sym, Side.SELL, event.order_id)
        coid = getattr(event, "client_order_id", "")
        if coid and coid in self._ttl_released_keys:
            self._ttl_released_keys.discard(coid)
            logger.info(
                "r47_terminal_after_ttl_release_skipped",
                symbol=sym,
                client_order_id=coid,
                status=event.status.name,
            )
            return
        remaining = max(0, event.remaining_qty)
        if event.side == Side.BUY:
            self._pending_buy[sym] = max(0, self._pending_buy.get(sym, 0) - remaining)
        else:
            self._pending_sell[sym] = max(0, self._pending_sell.get(sym, 0) - remaining)
        logger.debug(
            "r47_order_terminal",
            symbol=sym,
            status=event.status.name,
            side=event.side.name,
            remaining=remaining,
        )

    def on_risk_feedback(self, feedback: RiskFeedback) -> None:
        """Release pending slot on pre-broker risk rejection.

        Without this, a risk-rejected order leaves _pending_buy or _pending_sell
        elevated, potentially freezing one side of quoting permanently.
        Requires feedback.side for precise decrement; ignores feedback without
        side info to prevent the "double-decrement" bug (Bug 9, 2026-04-16):
        typed intent tuples produce side=None via getattr(tuple, "side", None),
        causing the old else branch to decrement BOTH counters simultaneously,
        resetting max_pos protection and allowing unlimited order stacking.
        """
        if getattr(feedback, "was_approved", False):
            return
        sym = feedback.symbol
        side = getattr(feedback, "side", None)
        if side == Side.BUY:
            self._pending_buy[sym] = max(0, self._pending_buy.get(sym, 0) - 1)
        elif side == Side.SELL:
            self._pending_sell[sym] = max(0, self._pending_sell.get(sym, 0) - 1)
        else:
            logger.warning(
                "r47_risk_feedback_no_side",
                symbol=sym,
                reason=feedback.reason_code,
                feedback_side=repr(side),
            )
            return
        if feedback.reason_code == "live_order_ttl_expired":
            self._ttl_released_keys.add(f"{feedback.strategy_id}:{feedback.intent_id}")
            if len(self._ttl_released_keys) > self._TTL_RELEASED_MAX:
                evict = len(self._ttl_released_keys) - self._TTL_RELEASED_MAX // 2
                victims: set[str] = set()
                for key in self._ttl_released_keys:
                    if len(victims) >= evict:
                        break
                    victims.add(key)
                self._ttl_released_keys -= victims
        logger.debug(
            "r47_risk_rejection_pending_released",
            symbol=sym,
            reason=feedback.reason_code,
            side=side.name if hasattr(side, "name") else str(side),
        )

    def on_gap(self, event: GapEvent) -> None:
        """Reset stale streaming state after bus overflow."""
        self._feature_cache.clear()
        self._pe_states.clear()
        self._queue_states.clear()
        self._mfg_states.clear()
        self._suppress_bid = False
        self._suppress_ask = False
        self._qi_widen_bid = 0
        self._qi_widen_ask = 0
        self._last_bid.clear()
        self._last_ask.clear()
        self._active_buy_oid.clear()
        self._active_sell_oid.clear()
        self._inflight_buy_oids.clear()
        self._inflight_sell_oids.clear()
        logger.warning(
            "r47_gap_event_state_reset",
            missed=event.missed_count,
            strategy=self.strategy_id,
        )

    def seed_local_pos(self, positions: dict[str, int]) -> None:
        """Explicitly seed local position state (call after startup reconciliation).

        Only seeds symbols not already tracked — safe to call multiple times.
        """
        for sym, qty in positions.items():
            if sym not in self._local_pos:
                self._local_pos[sym] = qty
                logger.info("r47_local_pos_seeded", symbol=sym, pos=qty, source="explicit")

    def _local_position(self, symbol: str) -> int:
        """Return local fill-tracked position (authoritative for max_pos).

        Lazily seeds from StrategyContext on first access so a restart with
        open broker positions doesn't treat the strategy as flat.
        """
        if symbol not in self._local_pos:
            ctx_pos = self.position(symbol)
            if ctx_pos != 0:
                self._local_pos[symbol] = ctx_pos
                logger.info("r47_local_pos_seeded", symbol=symbol, pos=ctx_pos, source="ctx")
        return self._local_pos.get(symbol, 0)

    def _inflight_oids_for_side(self, symbol: str, side: Side) -> set[str]:
        return (
            self._inflight_buy_oids.setdefault(symbol, set())
            if side == Side.BUY
            else self._inflight_sell_oids.setdefault(symbol, set())
        )

    def _set_inflight_metric(self, symbol: str, side: Side) -> None:
        if self._metric_inflight_oids is None:
            return
        inflight = self._inflight_oids_for_side(symbol, side)
        self._metric_inflight_oids.labels(strategy=self.strategy_id, side=side.name).set(len(inflight))

    def _discard_inflight_oid(self, symbol: str, side: Side, order_id: str) -> None:
        """Remove exact or Shioaji prefix-expanded oid from D3 inflight state."""
        if not order_id:
            return
        inflight = self._inflight_oids_for_side(symbol, side)
        if order_id in inflight:
            inflight.discard(order_id)
        else:
            for oid in tuple(inflight):
                if order_id.startswith(oid) or oid.startswith(order_id):
                    inflight.discard(oid)
        self._set_inflight_metric(symbol, side)

    def _cancel_tracked_side(
        self,
        symbol: str,
        side: Side,
        *,
        count_stale_metric: bool = False,
        clear_last_quote: bool = False,
    ) -> int:
        """Cancel all known oids for one side and clear local oid state.

        This keeps the D1 stale reconciler and the legacy price-moved
        cancel-before-requote path behaviorally identical: after dispatching a
        cancel, the same oid is no longer eligible for duplicate cancel intents
        while the broker terminal callback is still in flight.
        """
        inflight = self._inflight_oids_for_side(symbol, side)
        targets = set(inflight)
        if side == Side.BUY:
            primary = self._active_buy_oid.pop(symbol, None)
        else:
            primary = self._active_sell_oid.pop(symbol, None)
        if primary:
            targets.add(primary)
        if not targets:
            return 0

        for oid in sorted(targets):
            self.cancel(symbol, oid)
            if count_stale_metric:
                self._stale_cancels += 1
                if self._metric_stale_cancels is not None:
                    self._metric_stale_cancels.labels(strategy=self.strategy_id, side=side.name).inc()

        inflight.clear()
        self._set_inflight_metric(symbol, side)
        if clear_last_quote:
            if side == Side.BUY:
                self._last_bid.pop(symbol, None)
            else:
                self._last_ask.pop(symbol, None)
        return len(targets)

    def _reconcile_stale_quotes(self, event: LOBStatsEvent) -> None:
        """D1 (2026-04-21 incident fix): cancel active orders whose last quoted
        price is too far from current mid in EITHER direction.

        Runs on EVERY ``on_stats`` before any gate short-circuit. Today's
        incident: spread gate blocked 96.3% of ticks while median spread was
        3 pts (threshold 5 pts), leaving orders like v004N @ 37754 sitting
        41 seconds while mid ran 30+ pts away.

        Uses absolute distance, so both directions are covered:
          - BUY posted low, mid ran up → stale, market won't reach it (or will
            reach adversely far later).
          - BUY posted high, mid dropped → adverse-selection-risk, about to
            fill at a bad price. Cancel proactively.
          - symmetrically for SELL.

        Optimistically clears ``_active_*_oid`` after cancel dispatch to
        prevent re-canceling the same oid on subsequent ticks (source of 210
        ``cancel_already_terminal`` events today). The broker's CANCELLED
        callback remains the authoritative teardown for ``_pending_*``.
        """
        if self._stale_quote_max_scaled <= 0:
            return
        if event.mid_price_x2 is None or event.spread_scaled is None:
            return
        exec_sym = self._exec_symbol(event.symbol)
        mid_scaled = event.mid_price_x2 // 2
        max_dist = self._stale_quote_max_scaled

        last_bid = self._last_bid.get(exec_sym)
        if last_bid is not None and last_bid > 0 and abs(mid_scaled - last_bid) > max_dist:
            self._cancel_tracked_side(exec_sym, Side.BUY, count_stale_metric=True, clear_last_quote=True)

        last_ask = self._last_ask.get(exec_sym)
        if last_ask is not None and last_ask > 0 and abs(last_ask - mid_scaled) > max_dist:
            self._cancel_tracked_side(exec_sym, Side.SELL, count_stale_metric=True, clear_last_quote=True)

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Main quoting logic with 3-layer gating."""
        symbol = event.symbol
        self._stats_count += 1

        if (
            event.mid_price_x2 is None
            or event.spread_scaled is None
            or event.mid_price_x2 <= 0
            or event.spread_scaled <= 0
        ):
            return

        self._reconcile_stale_quotes(event)

        blocked_by: str | None = None

        if event.spread_scaled < self._spread_thresh_scaled:
            self._spread_blocked += 1
            blocked_by = "spread"

        if blocked_by is None:
            features = self._feature_cache.get(symbol)
            if features and len(features) > _IDX_TOXICITY_EMA50_X1000:
                toxicity = int(features[_IDX_TOXICITY_EMA50_X1000])
                if toxicity > self._toxicity_max:
                    self._toxicity_blocked += 1
                    blocked_by = "toxicity"

        pe = self._get_pe(symbol)
        if blocked_by is None and pe.warmed_up and pe.h < self._pe_danger:
            self._pe_blocked += 1
            blocked_by = "pe"

        if blocked_by is None:
            self._generate_quotes(symbol, event, pe)

        if self._stats_count % _LOG_INTERVAL == 1:
            self._log_stats(symbol, pe, event.spread_scaled, blocked_by)

    def _generate_quotes(self, symbol: str, event: LOBStatsEvent, pe: _PEState) -> None:
        """Compute quote prices and place orders with D3 widening + D2 suppression."""
        exec_sym = self._exec_symbol(symbol)

        now_ns = _now_ns()
        last_q = self._last_quote_ns.get(exec_sym, 0)
        if last_q > 0 and (now_ns - last_q) < self._QUOTE_COOLDOWN_NS:
            return

        if event.mid_price_x2 is None or event.spread_scaled is None:
            return

        mfg = self._get_mfg(symbol)
        mfg_widen_bid, mfg_widen_ask = self._compute_mfg_widening(mfg, event.spread_scaled)

        mid_price_x2 = event.mid_price_x2
        spread_scaled = event.spread_scaled
        pos = self._local_position(exec_sym)

        imbalance_adj = int(event.imbalance * spread_scaled * 20 * 2 // 100)
        micro_price_x2 = mid_price_x2 + imbalance_adj

        tick_size_scaled = max(1, spread_scaled * 50 // 100)
        skew_x2 = -(pos * tick_size_scaled * 2) // 5
        fair_value_x2 = micro_price_x2 + skew_x2

        half_spread_scaled = max(1, spread_scaled // 2)
        pe_width_mult = 2 if (pe.warmed_up and pe.h < 0.70) else 1
        base_width = max(tick_size_scaled, half_spread_scaled) * pe_width_mult

        qi_widen_bid_scaled = self._qi_widen_bid * _PRICE_SCALE
        qi_widen_ask_scaled = self._qi_widen_ask * _PRICE_SCALE
        bid_price_scaled = (fair_value_x2 - (base_width + mfg_widen_bid + qi_widen_bid_scaled) * 2) // 2
        ask_price_scaled = (fair_value_x2 + (base_width + mfg_widen_ask + qi_widen_ask_scaled) * 2) // 2

        bid_price_scaled = (bid_price_scaled // _PRICE_SCALE) * _PRICE_SCALE
        ask_price_scaled = -(-ask_price_scaled // _PRICE_SCALE) * _PRICE_SCALE

        if bid_price_scaled <= 0 or ask_price_scaled <= 0:
            self._nonpositive_quote_blocked += 1
            logger.warning(
                "r47_nonpositive_quote_blocked",
                symbol=symbol,
                exec_symbol=exec_sym,
                bid=bid_price_scaled,
                ask=ask_price_scaled,
                mid_x2=mid_price_x2,
                spread_scaled=spread_scaled,
            )
            return

        max_pos = self._max_pos
        pending_buy = self._pending_buy.get(exec_sym, 0)
        pending_sell = self._pending_sell.get(exec_sym, 0)
        bid_moved = bid_price_scaled != self._last_bid.get(exec_sym, -1)
        ask_moved = ask_price_scaled != self._last_ask.get(exec_sym, -1)
        _qty = 1

        can_buy = pos + pending_buy < max_pos and pos < max_pos
        can_sell = pos - pending_sell > -max_pos and pos > -max_pos

        if bid_moved:
            self._cancel_tracked_side(exec_sym, Side.BUY)
        if ask_moved:
            self._cancel_tracked_side(exec_sym, Side.SELL)

        quoted = False
        if can_buy and not self._suppress_bid and bid_moved:
            self.buy(exec_sym, bid_price_scaled, _qty)
            self._pending_buy[exec_sym] = pending_buy + _qty
            self._last_bid[exec_sym] = bid_price_scaled
            self._quotes_sent += 1
            quoted = True
        if can_sell and not self._suppress_ask and ask_moved:
            self.sell(exec_sym, ask_price_scaled, _qty)
            self._pending_sell[exec_sym] = pending_sell + _qty
            self._last_ask[exec_sym] = ask_price_scaled
            self._quotes_sent += 1
            quoted = True
        if quoted:
            self._last_quote_ns[exec_sym] = now_ns

    def _compute_mfg_widening(self, mfg: _MFGState, spread_scaled: int) -> tuple[int, int]:
        """D3: Asymmetric spread widening on capitulation side."""
        if not mfg.warmed_up or mfg.capitulation_z <= self._mfg_skew_z_thresh:
            return 0, 0
        tick_size = max(1, spread_scaled * 50 // 100)
        skew_mult = min(3, int(mfg.capitulation_z - self._mfg_skew_z_thresh + 1))
        widen = tick_size * skew_mult
        self._mfg_skewed += 1
        if mfg.flow_direction > 0:
            return 0, widen
        if mfg.flow_direction < 0:
            return widen, 0
        return 0, 0

    def _publish_gate_and_pending_metrics(self, exec_sym: str) -> None:
        """Export the two states that were unobservable during the 2026-08-10
        freeze: how much the strategy thinks is working at the broker, and how
        often each gate is suppressing quotes.

        Gate counters publish the delta accumulated since the last call rather
        than incrementing per tick. The spread gate alone fired on 94.19% of
        1.3M ticks, and a per-tick ``.labels()`` lookup on that path is exactly
        the kind of avoidable hot-path work Core Law #1 rules out. Deltas keep
        the counter exact at 1/500th of the calls.
        """
        if self._metric_pending_qty is not None:
            self._metric_pending_qty.labels(strategy=self.strategy_id, symbol=exec_sym, side=Side.BUY.name).set(
                self._pending_buy.get(exec_sym, 0)
            )
            self._metric_pending_qty.labels(strategy=self.strategy_id, symbol=exec_sym, side=Side.SELL.name).set(
                self._pending_sell.get(exec_sym, 0)
            )
        if self._metric_gate_blocked is not None:
            for gate, total in (
                ("spread", self._spread_blocked),
                ("toxicity", self._toxicity_blocked),
                ("pe", self._pe_blocked),
            ):
                delta = total - self._gate_blocked_published.get(gate, 0)
                if delta > 0:
                    self._metric_gate_blocked.labels(strategy=self.strategy_id, gate=gate).inc(delta)
                    self._gate_blocked_published[gate] = total

    def _log_stats(self, symbol: str, pe: _PEState, spread_scaled: int, blocked_by: str | None) -> None:
        """Periodic diagnostic. Called from on_stats on *every* path, blocked or
        not — see the comment at the gate chain for why that matters.

        ``mfg`` and ``pos`` are resolved here rather than passed in, because the
        blocked paths never compute them.
        """
        exec_sym = self._exec_symbol(symbol)
        mfg = self._get_mfg(symbol)
        self._publish_gate_and_pending_metrics(exec_sym)
        logger.info(
            "r47_stats",
            symbol=symbol,
            blocked_by=blocked_by,
            pending_buy=self._pending_buy.get(exec_sym, 0),
            pending_sell=self._pending_sell.get(exec_sym, 0),
            h=round(pe.h, 4) if pe.warmed_up else None,
            p_depl_bid=round(self._get_queue(symbol).p_depl_bid, 3),
            p_depl_ask=round(self._get_queue(symbol).p_depl_ask, 3),
            mfg_z=round(mfg.capitulation_z, 2) if mfg.warmed_up else None,
            spread_pts=spread_scaled // _PRICE_SCALE,
            pos=self._local_position(exec_sym),
            quotes=self._quotes_sent,
            pe_blk=self._pe_blocked,
            q_suppress=self._queue_suppressed,
            mfg_skew=self._mfg_skewed,
            spr_blk=self._spread_blocked,
            tox_blk=self._toxicity_blocked,
            qi_wdn=self._qi_widened,
            stale_cnl=self._stale_cancels,
            nonpos_blk=self._nonpositive_quote_blocked,
        )
