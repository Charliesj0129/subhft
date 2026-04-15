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
from hft_platform.events import (
    FeatureUpdateEvent,
    GapEvent,
    LOBStatsEvent,
    TickEvent,
)
from hft_platform.strategies.simple_mm import SimpleMarketMaker
from hft_platform.strategy.base import QUALITY_FLAGS_CORRUPT

logger = get_logger("strategy.r47_maker")

# Feature indices (lob_shared_v3)
_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10
_IDX_TOXICITY_EMA50_X1000 = 21

_PRICE_SCALE = 10000
_LOG_INTERVAL = 500


# ── D1: Permutation Entropy ──────────────────────────────────────────────


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
        self._n_patterns = math.factorial(d)  # 24 for D=4
        self._h_max = math.log2(self._n_patterns)
        self._window_size = window
        # Circular buffer for last D QI values (ordinal ranking)
        self._qi_buf: deque[float] = deque(maxlen=d)
        self._qi_len = 0
        # Pattern histogram (pre-allocated, fixed size 24)
        self._pattern_counts = [0] * self._n_patterns
        # Sliding window of pattern indices
        pat_win = window - d + 1
        self._pat_deque: deque[int] = deque(maxlen=pat_win)
        self._h: float = 1.0  # start at max entropy (safe)
        self._warmup_done = False

    def update(self, qi_value: float) -> float:
        """Update with new QI_1 value, return normalized entropy H."""
        self._qi_buf.append(qi_value)
        self._qi_len += 1

        if self._qi_len < self._d:
            return self._h

        # Compute ordinal pattern from last D values
        buf = list(self._qi_buf)
        pattern_id = self._rank_to_id(buf)

        # Add to sliding window
        if len(self._pat_deque) == self._pat_deque.maxlen:
            # Remove oldest
            old = self._pat_deque[0]
            self._pattern_counts[old] -= 1
        self._pat_deque.append(pattern_id)
        self._pattern_counts[pattern_id] += 1

        n_samples = len(self._pat_deque)
        if n_samples < 20:
            return self._h

        self._warmup_done = True

        # Compute Shannon entropy from histogram
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
        # Compute ranks: rank[i] = number of values less than vals[i]
        # (with tie-breaking by position for stability)
        ranks = [0] * d
        for i in range(d):
            for j in range(d):
                if vals[j] < vals[i] or (vals[j] == vals[i] and j < i):
                    ranks[i] += 1
        # Lehmer code: for each position, count how many remaining
        # values in the suffix are smaller than the current rank
        idx = 0
        factorials = [1] * d  # factorials[i] = (d-1-i)!
        f = 1
        for i in range(d - 1, -1, -1):
            factorials[i] = f
            f *= d - i
        for i in range(d):
            # Count inversions: how many ranks[j] < ranks[i] for j > i
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


# ── D2: Queue Survival ───────────────────────────────────────────────────


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

        # Separate arrivals (positive delta) and departures (negative delta)
        if d_bid > 0:
            self._lambda_bid = alpha * d_bid + (1 - alpha) * self._lambda_bid
        elif d_bid < 0:
            self._mu_bid = alpha * (-d_bid) + (1 - alpha) * self._mu_bid

        if d_ask > 0:
            self._lambda_ask = alpha * d_ask + (1 - alpha) * self._lambda_ask
        elif d_ask < 0:
            self._mu_ask = alpha * (-d_ask) + (1 - alpha) * self._mu_ask

        # Gambler's ruin depletion probability
        q_bid = max(bid_qty, 1)
        q_ask = max(ask_qty, 1)

        rho_bid = self._mu_bid / max(self._lambda_bid, 1e-6)
        rho_ask = self._mu_ask / max(self._lambda_ask, 1e-6)

        # P(depletion) = min(1, (mu/lambda)^q) — clamped
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


# ── D3: MFG Inventory Proxy ──────────────────────────────────────────────


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
        self._flow_std_ema: float = 1.0  # avoid div-by-zero
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
        # Track std via EMA of squared deviation
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


# ── R47 Strategy ─────────────────────────────────────────────────────────


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
        # D1: PE
        pe_danger_threshold: float = 0.55,
        pe_window: int = 100,
        # D2: Queue
        queue_cancel_threshold: float = 0.7,
        queue_ema_alpha: float = 0.05,
        # D3: MFG
        mfg_skew_z_threshold: float = 2.0,
        mfg_ema_alpha: float = 0.01,
        # Gates
        spread_threshold_pts: int = 5,  # TMFD6: breakeven 4 pts (RT 40 NTD / 10 NTD per pt)
        toxicity_max: int = 700,
        # D4: QI adverse-selection skew
        qi_skew_threshold: float = 1.0,  # 1.0 = disabled; 0.10 = optimized for TMFD6
        qi_widen_ticks: int = 1,
        # Cross-instrument execution
        trade_symbol: str = "",  # if set, place orders on this symbol instead of signal symbol
        **kwargs: object,
    ) -> None:
        super().__init__(strategy_id=strategy_id, **kwargs)
        self._trade_symbol = trade_symbol

        # D1
        self._pe_danger = pe_danger_threshold
        self._pe_states: dict[str, _PEState] = {}
        self._pe_window = pe_window

        # D2
        self._queue_cancel_thresh = queue_cancel_threshold
        self._queue_states: dict[str, _QueueState] = {}
        self._queue_ema = queue_ema_alpha

        # D3
        self._mfg_skew_z_thresh = mfg_skew_z_threshold
        self._mfg_states: dict[str, _MFGState] = {}
        self._mfg_ema = mfg_ema_alpha

        # Gates
        self._spread_thresh_scaled = spread_threshold_pts * _PRICE_SCALE
        self._toxicity_max = toxicity_max

        # D4: QI skew
        self._qi_skew_thresh = qi_skew_threshold
        self._qi_widen_ticks = qi_widen_ticks

        # Feature cache
        self._feature_cache: dict[str, tuple[int | float, ...]] = {}

        # Counters
        self._stats_count = 0
        self._pe_blocked = 0
        self._queue_suppressed = 0
        self._mfg_skewed = 0
        self._qi_widened = 0
        self._spread_blocked = 0
        self._toxicity_blocked = 0
        self._quotes_sent = 0

        # D2 quote suppression flags (set in on_features, read in on_stats)
        self._suppress_bid: bool = False
        self._suppress_ask: bool = False

        # D4 QI skew widening (set in on_features, read in _generate_quotes)
        self._qi_widen_bid: int = 0
        self._qi_widen_ask: int = 0

        # Local position tracker — bypasses stale StrategyContext cache.
        # StrategyRunner's _positions_dirty flag can lag behind fills,
        # causing self.position() to return stale values (e.g. 0) while
        # actual position is non-zero. This dict is updated directly in
        # on_fill() and is authoritative for max_pos enforcement.
        self._local_pos: dict[str, int] = {}

        # Pending order counters — incremented when buy()/sell() is called,
        # decremented on fill or cancel/reject (on_order). Prevents sending
        # more orders than max_pos allows when multiple ROD orders are resting.
        self._pending_buy: dict[str, int] = {}
        self._pending_sell: dict[str, int] = {}

        # Last quoted prices per symbol — suppress requotes when price hasn't moved.
        # ROD orders rest at exchange; sending the same price again just stacks orders.
        self._last_bid: dict[str, int] = {}
        self._last_ask: dict[str, int] = {}

        # DEC2-008: Fill dedup — prevent double-counting from duplicate broker callbacks.
        self._seen_fill_ids: set[str] = set()
        self._FILL_DEDUP_MAX = 500

        # F2: Active order tracking — maps symbol to broker order_id.
        # Used to cancel stale ROD orders before requoting at a new price.
        # Without this, ROD orders stack at the exchange and fill on any
        # price touch (76-order burst incident 2026-04-15 RC-3).
        self._active_buy_oid: dict[str, str] = {}
        self._active_sell_oid: dict[str, str] = {}

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

    # ── Event Handlers ────────────────────────────────────────────────

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
        # Skip corrupted features (GAP, STATE_RESET, OUT_OF_ORDER)
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

        # D1: Update PE with QI_1 (L1 imbalance)
        if len(features) > _IDX_L1_IMBALANCE_PPM:
            qi_val = float(features[_IDX_L1_IMBALANCE_PPM]) / 1_000_000
            pe = self._get_pe(symbol)
            pe.update(qi_val)

        # D2: Update queue state with L1 quantities
        if len(features) > _IDX_L1_ASK_QTY:
            bid_qty = int(features[_IDX_L1_BID_QTY])
            ask_qty = int(features[_IDX_L1_ASK_QTY])
            queue = self._get_queue(symbol)
            queue.update(bid_qty, ask_qty)

            # D2: Set quote suppression flags for on_stats()
            # Since BaseStrategy.buy/sell don't return order IDs,
            # we suppress the NEXT quote placement instead of cancelling.
            # Disable semantics: threshold >= 1.0 means "never suppress"
            # (p_depl is always in [0, 1], so > 1.0 is never true).
            # WARNING: threshold=0.0 means "always suppress" (any p_depl > 0).
            self._suppress_bid = False
            self._suppress_ask = False
            if queue.warmed_up and self._queue_cancel_thresh < 1.0:
                if queue.p_depl_bid > self._queue_cancel_thresh:
                    self._suppress_bid = True
                if queue.p_depl_ask > self._queue_cancel_thresh:
                    self._suppress_ask = True
                if self._suppress_bid or self._suppress_ask:
                    self._queue_suppressed += 1

            # D4: QI adverse-selection skew — widen the side under pressure
            # QI > 0 = buying pressure → asks likely to be adversely filled → widen ask
            # QI < 0 = selling pressure → bids likely to be adversely filled → widen bid
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
        # DEC2-008: Fill dedup to prevent position double-counting.
        fid = getattr(event, "fill_id", None) or ""
        if fid and fid in self._seen_fill_ids:
            logger.warning("r47_duplicate_fill_skipped", fill_id=fid, symbol=event.symbol)
            return
        if fid:
            self._seen_fill_ids.add(fid)
            if len(self._seen_fill_ids) > self._FILL_DEDUP_MAX:
                # Evict ~half (set has no ordering, but this bounds memory)
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
        # Each fill consumes one pending slot
        if event.side == Side.BUY:
            self._pending_buy[sym] = max(0, self._pending_buy.get(sym, 0) - event.qty)
            # F2: fill consumed the resting order — clear active tracking
            self._active_buy_oid.pop(sym, None)
        else:
            self._pending_sell[sym] = max(0, self._pending_sell.get(sym, 0) - event.qty)
            self._active_sell_oid.pop(sym, None)
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
        # F2: Capture broker order_id on SUBMITTED for cancel-before-requote.
        if event.status == OrderStatus.SUBMITTED:
            if event.side == Side.BUY:
                self._active_buy_oid[sym] = event.order_id
            else:
                self._active_sell_oid[sym] = event.order_id
            return
        if event.status not in (OrderStatus.CANCELLED, OrderStatus.FAILED):
            return
        # Clear active order tracking on terminal state
        if event.side == Side.BUY:
            if self._active_buy_oid.get(sym) == event.order_id:
                del self._active_buy_oid[sym]
        else:
            if self._active_sell_oid.get(sym) == event.order_id:
                del self._active_sell_oid[sym]
        # DEC2-002: remaining_qty=0 means fully filled before cancel arrived.
        # Decrement by 0 (no-op) — the fill already decremented pending.
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
        Uses feedback.side when available for precise decrement; falls back to
        decrementing both sides (safe: R47 sends qty=1 per side).
        """
        # DEC2-001: Approved feedback (e.g., DLQ expiry for dispatched orders)
        # must NOT decrement pending — the order was sent to the broker and
        # will be accounted for via on_fill/on_order.
        if getattr(feedback, "was_approved", False):
            return
        sym = feedback.symbol
        side = getattr(feedback, "side", None)
        # F3: Do NOT clear _last_bid/_last_ask on rejection.
        # Clearing price gate + decrementing pending re-arms both gates
        # simultaneously, creating a reject→resend amplification loop
        # (76-order burst incident 2026-04-15 RC-2).
        if side == Side.BUY:
            self._pending_buy[sym] = max(0, self._pending_buy.get(sym, 0) - 1)
        elif side == Side.SELL:
            self._pending_sell[sym] = max(0, self._pending_sell.get(sym, 0) - 1)
        else:
            # Fallback: no side info — decrement both (conservative)
            self._pending_buy[sym] = max(0, self._pending_buy.get(sym, 0) - 1)
            self._pending_sell[sym] = max(0, self._pending_sell.get(sym, 0) - 1)
        logger.debug(
            "r47_risk_rejection_pending_released",
            symbol=sym,
            reason=feedback.reason_code,
            side=side.name if side else "both",
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
        # F2: Clear active order IDs — after gap, SUBMITTED callbacks may have
        # been lost, so stale IDs would cause cancel requests for unknown orders.
        self._active_buy_oid.clear()
        self._active_sell_oid.clear()
        # NOTE: Do NOT clear _pending_buy/_pending_sell here.
        # Clearing pending resets max_pos protection, allowing the strategy
        # to send unbounded orders (76-order burst incident 2026-04-15 RC-1).
        # If gap swallowed fill/cancel callbacks, stale pending counters
        # block further quoting — this is the SAFE failure mode (liveness
        # issue, not safety issue). Strategy restart recovers from this.
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

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Main quoting logic with 3-layer gating."""
        symbol = event.symbol
        self._stats_count += 1

        # ── Validity guard ────────────────────────────────────────────
        if (
            event.mid_price_x2 is None
            or event.spread_scaled is None
            or event.mid_price_x2 <= 0
            or event.spread_scaled <= 0
        ):
            return

        # ── Spread gate (hard floor) ──────────────────────────────────
        if event.spread_scaled < self._spread_thresh_scaled:
            self._spread_blocked += 1
            return  # suppress all quotes this tick

        # ── Toxicity gate ─────────────────────────────────────────────
        features = self._feature_cache.get(symbol)
        if features and len(features) > _IDX_TOXICITY_EMA50_X1000:
            toxicity = int(features[_IDX_TOXICITY_EMA50_X1000])
            if toxicity > self._toxicity_max:
                self._toxicity_blocked += 1
                return  # suppress all quotes this tick

        # ── D1: PE Regime Gate ────────────────────────────────────────
        pe = self._get_pe(symbol)
        if pe.warmed_up:
            h = pe.h
            if h < self._pe_danger:
                # Market too structured (trending) — do NOT quote
                self._pe_blocked += 1
                if self._stats_count % _LOG_INTERVAL == 1:
                    logger.debug("r47_pe_blocked", symbol=symbol, h=round(h, 4))
                return  # suppress all quotes this tick

        # ── Generate and place quotes ─────────────────────────────────
        self._generate_quotes(symbol, event, pe)

    def _generate_quotes(self, symbol: str, event: LOBStatsEvent, pe: _PEState) -> None:
        """Compute quote prices and place orders with D3 widening + D2 suppression."""
        mfg = self._get_mfg(symbol)
        mfg_widen_bid, mfg_widen_ask = self._compute_mfg_widening(mfg, event.spread_scaled)

        mid_price_x2 = event.mid_price_x2
        spread_scaled = event.spread_scaled
        exec_sym = self._exec_symbol(symbol)
        pos = self._local_position(exec_sym)

        # Micro price with imbalance
        imbalance_adj = int(event.imbalance * spread_scaled * 20 * 2 // 100)
        micro_price_x2 = mid_price_x2 + imbalance_adj

        # Inventory skew
        tick_size_scaled = max(1, spread_scaled * 50 // 100)
        skew_x2 = -(pos * tick_size_scaled * 2) // 5
        fair_value_x2 = micro_price_x2 + skew_x2

        # Quote width — widen if PE indicates intermediate structure
        half_spread_scaled = max(1, spread_scaled // 2)
        pe_width_mult = 2 if (pe.warmed_up and pe.h < 0.70) else 1
        base_width = max(tick_size_scaled, half_spread_scaled) * pe_width_mult

        # D3: Asymmetric spread widening (MFG inventory)
        # D4: QI adverse-selection skew (widen side under pressure)
        qi_widen_bid_scaled = self._qi_widen_bid * _PRICE_SCALE
        qi_widen_ask_scaled = self._qi_widen_ask * _PRICE_SCALE
        bid_price_scaled = (fair_value_x2 - (base_width + mfg_widen_bid + qi_widen_bid_scaled) * 2) // 2
        ask_price_scaled = (fair_value_x2 + (base_width + mfg_widen_ask + qi_widen_ask_scaled) * 2) // 2

        # Snap to tick grid: TMFD6/TXFD6 tick = 1 pt = 10000 scaled.
        # Bid rounds DOWN, ask rounds UP to avoid crossing the spread.
        bid_price_scaled = (bid_price_scaled // _PRICE_SCALE) * _PRICE_SCALE
        ask_price_scaled = -(-ask_price_scaled // _PRICE_SCALE) * _PRICE_SCALE

        # Execution with D2 quote suppression — use exec_sym for orders.
        # Price-gate: only send a new ROD if the price has moved by >= 1 tick
        # from the last quoted price. ROD orders rest at the exchange; resending
        # the same price just stacks redundant orders.
        # Also gate on pending orders so resting RODs can't exceed max_pos.
        max_pos = self._max_pos
        pending_buy = self._pending_buy.get(exec_sym, 0)
        pending_sell = self._pending_sell.get(exec_sym, 0)
        bid_moved = bid_price_scaled != self._last_bid.get(exec_sym, -1)
        ask_moved = ask_price_scaled != self._last_ask.get(exec_sym, -1)
        # DEC2-004: qty is ALWAYS 1. pending_buy/sell increment by 1 to match.
        # If qty changes, pending tracking MUST be updated to use actual qty.
        _qty = 1

        # F1: Hard position cap — _local_pos survives on_gap and cannot be
        # reset by bus overflow or risk rejection. This is the last defense
        # against order stacking (76-order burst incident 2026-04-15).
        # pos already comes from _local_position(exec_sym) which uses _local_pos.
        can_buy = pos + pending_buy < max_pos and pos < max_pos
        can_sell = pos - pending_sell > -max_pos and pos > -max_pos

        # F2: Cancel stale ROD before requoting at new price to prevent stacking.
        if bid_moved:
            old_buy_oid = self._active_buy_oid.get(exec_sym)
            if old_buy_oid:
                self.cancel(exec_sym, old_buy_oid)
        if ask_moved:
            old_sell_oid = self._active_sell_oid.get(exec_sym)
            if old_sell_oid:
                self.cancel(exec_sym, old_sell_oid)

        if can_buy and not self._suppress_bid and bid_moved:
            self.buy(exec_sym, bid_price_scaled, _qty)
            self._pending_buy[exec_sym] = pending_buy + _qty
            self._last_bid[exec_sym] = bid_price_scaled
            self._quotes_sent += 1
        if can_sell and not self._suppress_ask and ask_moved:
            self.sell(exec_sym, ask_price_scaled, _qty)
            self._pending_sell[exec_sym] = pending_sell + _qty
            self._last_ask[exec_sym] = ask_price_scaled
            self._quotes_sent += 1

        if self._stats_count % _LOG_INTERVAL == 1:
            self._log_stats(symbol, pe, mfg, event.spread_scaled, pos)

    def _compute_mfg_widening(self, mfg: _MFGState, spread_scaled: int) -> tuple[int, int]:
        """D3: Asymmetric spread widening on capitulation side."""
        if not mfg.warmed_up or mfg.capitulation_z <= self._mfg_skew_z_thresh:
            return 0, 0
        tick_size = max(1, spread_scaled * 50 // 100)
        skew_mult = min(3, int(mfg.capitulation_z - self._mfg_skew_z_thresh + 1))
        widen = tick_size * skew_mult
        self._mfg_skewed += 1
        if mfg.flow_direction > 0:
            return 0, widen  # widen ask
        if mfg.flow_direction < 0:
            return widen, 0  # widen bid
        return 0, 0

    def _log_stats(self, symbol: str, pe: _PEState, mfg: _MFGState, spread_scaled: int, pos: int) -> None:
        logger.info(
            "r47_stats",
            symbol=symbol,
            h=round(pe.h, 4) if pe.warmed_up else None,
            p_depl_bid=round(self._get_queue(symbol).p_depl_bid, 3),
            p_depl_ask=round(self._get_queue(symbol).p_depl_ask, 3),
            mfg_z=round(mfg.capitulation_z, 2) if mfg.warmed_up else None,
            spread_pts=spread_scaled // _PRICE_SCALE,
            pos=pos,
            quotes=self._quotes_sent,
            pe_blk=self._pe_blocked,
            q_suppress=self._queue_suppressed,
            mfg_skew=self._mfg_skewed,
            spr_blk=self._spread_blocked,
            tox_blk=self._toxicity_blocked,
            qi_wdn=self._qi_widened,
        )

    # Note: cancel-by-order-id is not possible because BaseStrategy.buy()/sell()
    # don't return order IDs. Quote suppression (not placing new quotes) is the
    # effective mechanism — same pattern as OpportunisticMM's spread gate.
