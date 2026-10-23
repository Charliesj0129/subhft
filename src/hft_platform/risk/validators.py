import os
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec, PriceScaleProvider, SymbolMetadataPriceScaleProvider
from hft_platform.risk.expiry import expiry_window_ns, parse_cutoff_minute

if TYPE_CHECKING:
    from hft_platform.feed_adapter.lob_engine import LOBEngine

logger = get_logger("risk_validators")


class RiskValidator:
    def __init__(
        self,
        config: Dict[str, Any],
        price_scale_provider: PriceScaleProvider | None = None,
        lob: Optional["LOBEngine"] = None,
        position_provider: Any | None = None,
    ):
        self.config = config
        self.defaults = config.get("global_defaults", {})
        self.strat_configs = config.get("strategies", {})
        provider = price_scale_provider or SymbolMetadataPriceScaleProvider()
        self.price_codec = PriceCodec(provider)
        self.lob = lob
        self._position_provider = position_provider
        self._shared_scale_cache: Dict[str, int] = {}

    def _derive_from_defaults(self) -> None:
        """Re-derive every config-dependent scalar from ``self.defaults``/``self.config``.

        Subclasses that snapshot config values in ``__init__`` MUST override
        this and do the snapshotting here instead, calling it from
        ``__init__``. ``reload`` then re-runs exactly the same derivation, so
        the two can never drift.

        Runtime state (accumulated PnL, latched HALT, cooldown deadlines) is
        deliberately NOT touched: a config reload must not clear a live halt.
        """

    def reload(self, new_config: Dict[str, Any]) -> None:
        """Adopt *new_config* — rebind, clear caches, and re-derive scalars.

        ``RiskEngine.on_config_reload`` used to rebind ``config``/``defaults``/
        ``strat_configs`` and clear any attribute with "cache" in its name, and
        stop there. But every validator reads its DEFAULTS off scalars
        snapshotted in ``__init__`` (``_max_price_cap_raw``,
        ``_hard_limit_threshold_scaled``, ...), which nothing re-derived, so
        clearing the caches merely re-populated them from the stale scalars.
        Raising ``max_price_cap`` or tightening ``intraday_pnl.hard_limit_ntd``
        over SIGHUP was a silent no-op while the reload logged success — and
        SIGHUP exists precisely for adjusting money-facing thresholds mid-session
        without a restart. Per-strategy and per-symbol overrides did take
        effect, which made the failure a partial one and harder to notice.
        """
        self.config = new_config
        self.defaults = new_config.get("global_defaults", {})
        self.strat_configs = new_config.get("strategies", {})
        for attr in list(vars(self)):
            if "cache" in attr.lower():
                obj = getattr(self, attr, None)
                if isinstance(obj, dict):
                    obj.clear()
        self._shared_scale_cache.clear()
        self._derive_from_defaults()

    def _scale_factor(self, symbol: str) -> int:
        cache = self._shared_scale_cache
        value = cache.get(symbol)
        if value is None:
            value = int(self.price_codec.scale_factor(symbol))
            cache[symbol] = value
        return value or 1

    def _current_position(self, symbol: str, strategy_id: str) -> int:
        """Resolve net position via ``position_provider`` if present.

        Accepts either a callable ``(symbol, strategy_id) -> int`` or a provider
        object exposing ``net_qty_for_symbol`` / ``positions``. Missing provider
        returns 0 (no position — treated as "cannot verify reduction").
        """
        provider = self._position_provider
        if provider is None:
            return 0
        if callable(provider):
            try:
                return int(provider(symbol, strategy_id) or 0)
            except Exception:  # noqa: BLE001 — provider errors are non-fatal
                return 0

        nq_fn = getattr(provider, "net_qty_for_symbol", None)
        if nq_fn is not None:
            try:
                return int(nq_fn(symbol, strategy_id))
            except Exception:  # noqa: BLE001
                return 0

        positions = getattr(provider, "positions", {})
        net_qty = 0
        for pos in positions.values():
            if getattr(pos, "symbol", None) != symbol:
                continue
            if getattr(pos, "strategy_id", strategy_id) != strategy_id:
                continue
            net_qty += int(getattr(pos, "net_qty", 0) or 0)
        return net_qty

    def reduces_position(self, intent: OrderIntent) -> bool:
        """Return True iff ``intent`` strictly reduces |net_position|.

        Bug 21 (2026-04-17): Safety gates like SOFT_LIMIT,
        DAILY_LOSS_LIMIT, MAX_NOTIONAL were blocking cover orders, preventing
        strategies from exiting adverse positions. The predicate here allows
        validators to distinguish cover orders from openers and bypass safety
        gates that would otherwise deadlock the strategy.

        Semantics:
          * CANCEL / FORCE_FLAT → always True (escape hatches)
          * NEW where ``abs(current + signed_qty) < abs(current)`` → True
          * Over-cover that flips sign and matches/grows magnitude → False
            (treated as new opener — strictly safer)
          * Flat position or no position_provider wired → False
        """
        if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            return True
        if self._position_provider is None:
            return False
        try:
            current = self._current_position(intent.symbol, intent.strategy_id)
        except Exception:  # noqa: BLE001 — never raise from a predicate
            return False
        if current == 0:
            return False
        signed = int(intent.qty if intent.side == Side.BUY else -intent.qty)
        return abs(current + signed) < abs(current)

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        """Return (Approved, Reason)."""
        return True, "OK"


class PriceBandValidator(RiskValidator):
    _PRODUCT_CAP_KEYS: dict[str, str] = {
        "future": "max_price_cap_futures",
        "option": "max_price_cap_options",
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_price_scaled_cache: Dict[str, int] = {}
        self._tick_size_scaled_cache: Dict[str, int] = {}
        self._band_ticks_cache: Dict[str, int] = {}
        self._derive_from_defaults()

    def _derive_from_defaults(self) -> None:
        self._max_price_cap_raw = float(self.defaults.get("max_price_cap", 5000.0))  # precision-config
        self._tick_size_raw = float(self.defaults.get("tick_size", 0.01))  # precision-config
        self._product_caps_raw: Dict[str, float] = {}
        for ptype, key in self._PRODUCT_CAP_KEYS.items():
            val = self.defaults.get(key)
            if val is not None:
                self._product_caps_raw[ptype] = float(val)
        # Per-root caps (``max_price_cap_root_TMF``). A per-*symbol* key names a
        # delivery month and therefore expires: on 2026-08-10 the live TMFH6 had
        # no entry and silently fell through to the futures default of 50,000
        # while TAIEX traded near 45,033 — ~11% from rejecting every order. A
        # root outlives every roll.
        self._root_caps_raw: Dict[str, float] = {}
        _root_prefix = "max_price_cap_root_"
        for key, val in self.defaults.items():
            if isinstance(key, str) and key.startswith(_root_prefix) and val is not None:
                self._root_caps_raw[key[len(_root_prefix) :]] = float(val)
        self._mid_price_max_age_ns = int(float(self.defaults.get("mid_price_max_age_s", 10)) * 1_000_000_000)

    def _resolve_root_cap_raw(self, symbol: str) -> float | None:
        """Return the configured cap for *symbol*'s product root, or ``None``.

        A symbol belongs to a root when it is that root plus one delivery-month
        letter and one year digit (``TMF`` -> ``TMFH6``). The length test rather
        than a bare prefix match is what keeps longer option codes from
        inheriting a futures cap.
        """
        for root, cap in self._root_caps_raw.items():
            if len(symbol) == len(root) + 2 and symbol.startswith(root):
                return cap
        return None

    def _resolve_cap_raw(self, symbol: str) -> float:
        """Resolve price cap: per-symbol > per-root > per-product-type > global."""
        sym_cap = self.defaults.get(f"max_price_cap_{symbol}")
        if sym_cap is not None:
            return float(sym_cap)
        root_cap = self._resolve_root_cap_raw(symbol)
        if root_cap is not None:
            return root_cap
        metadata = getattr(getattr(self.price_codec, "provider", None), "metadata", None)
        if metadata is not None:
            ptype = metadata.product_type(symbol)
            if ptype in self._product_caps_raw:
                return self._product_caps_raw[ptype]
        return self._max_price_cap_raw

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        if intent.price <= 0:
            return False, "PRICE_ZERO_OR_NEG"

        qty = getattr(intent, "qty", 0)
        if qty <= 0:
            return False, "QTY_ZERO_OR_NEG"

        # Fat Finger Protection: Absolute price cap
        scale = self._scale_factor(intent.symbol)
        max_price_scaled = self._max_price_scaled_cache.get(intent.symbol)
        if max_price_scaled is None:
            cap_raw = self._resolve_cap_raw(intent.symbol)
            max_price_scaled = int(cap_raw * scale)
            self._max_price_scaled_cache[intent.symbol] = max_price_scaled

        if intent.price > max_price_scaled:
            return False, f"PRICE_EXCEEDS_CAP({intent.symbol}): {intent.price} > {max_price_scaled}"

        # LOB-relative price band validation
        if self.lob is not None:
            mid_price = self._get_mid_price(intent.symbol)
            if mid_price is not None and mid_price > 0:
                strat_cfg = self.strat_configs.get(intent.strategy_id, {})
                band_ticks = self._band_ticks_cache.get(intent.strategy_id)
                if band_ticks is None:
                    band_ticks = int(strat_cfg.get("price_band_ticks", self.defaults.get("price_band_ticks", 20)))
                    self._band_ticks_cache[intent.strategy_id] = band_ticks
                tick_size_scaled = self._tick_size_scaled_cache.get(intent.symbol)
                if tick_size_scaled is None:
                    tick_size_scaled = int(self._tick_size_raw * scale)
                    self._tick_size_scaled_cache[intent.symbol] = tick_size_scaled

                # Calculate allowed band: mid_price +/- band_ticks * tick_size
                band_width = band_ticks * tick_size_scaled
                lower_bound = mid_price - band_width
                upper_bound = mid_price + band_width

                if intent.price < lower_bound or intent.price > upper_bound:
                    return False, (
                        f"PRICE_OUTSIDE_BAND: price={intent.price} mid={mid_price} band=[{lower_bound}, {upper_bound}]"
                    )

        return True, "OK"

    def _get_mid_price(self, symbol: str) -> Optional[int]:
        """Get mid price from LOB as scaled integer.

        Note: LOB stores prices in scaled format already. The mid_price from
        get_book_snapshot is mid_price_x2/2.0, which is already scaled.
        Returns None if the book is stale (older than mid_price_max_age_s seconds).
        """
        if self.lob is None:
            return None
        try:
            # Check freshness before returning any mid_price value
            book_state = self.lob.get_book(symbol) if hasattr(self.lob, "get_book") else None
            if book_state is not None and hasattr(book_state, "exch_ts") and book_state.exch_ts > 0:
                age_ns = timebase.now_ns() - book_state.exch_ts
                if age_ns > self._mid_price_max_age_ns:
                    return None  # Stale — treat as unknown during feed gaps

            get_l1_scaled = getattr(self.lob, "get_l1_scaled", None)
            if callable(get_l1_scaled):
                l1 = get_l1_scaled(symbol)
                if l1 is not None and len(l1) >= 4:
                    mid_price_x2 = int(l1[3] or 0)
                    if mid_price_x2 > 0:
                        return mid_price_x2 // 2
            book = self.lob.get_book_snapshot(symbol)
            if book and book.get("mid_price"):
                # mid_price from LOB is already in scaled units (as float)
                # Just convert to int
                return int(book["mid_price"])
        except Exception as e:
            logger.warning("Failed to get mid price from LOB", symbol=symbol, error=str(e))
        return None


class MaxNotionalValidator(RiskValidator):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_notional_scaled_cache: Dict[tuple[str, str], int] = {}
        self._derive_from_defaults()

    def _derive_from_defaults(self) -> None:
        self._default_max_notional_raw = self.defaults.get("max_notional", 10_000_000)

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        # Bug 21: allow orders that strictly reduce |net_position| to bypass
        # notional cap so a stuck position can always be closed.
        if self.reduces_position(intent):
            return True, "REDUCE_ONLY_BYPASS"

        cache_key = (intent.strategy_id, intent.symbol)
        max_notional_scaled = self._max_notional_scaled_cache.get(cache_key)
        if max_notional_scaled is None:
            strat_cfg = self.strat_configs.get(intent.strategy_id, {})
            max_notional_raw = strat_cfg.get("max_notional", self._default_max_notional_raw)
            scale = self._scale_factor(intent.symbol)
            max_notional_scaled = int(max_notional_raw * scale)
            self._max_notional_scaled_cache[cache_key] = max_notional_scaled

        notional_scaled = intent.price * intent.qty
        if notional_scaled > max_notional_scaled:
            return False, f"MAX_NOTIONAL_EXCEEDED: {notional_scaled} > {max_notional_scaled}"

        return True, "OK"


class PositionLimitValidator(RiskValidator):
    """Stateless validator: rejects orders where abs(qty) exceeds max_position_lots."""

    __slots__ = ("_default_max_position_lots", "_max_position_cache")

    def __init__(self, *args: Any, position_provider: Any | None = None, **kwargs: Any) -> None:
        # position_provider is now on the base class; forward it.
        kwargs.setdefault("position_provider", position_provider)
        super().__init__(*args, **kwargs)
        self._max_position_cache: Dict[str, int] = {}
        self._derive_from_defaults()

    def _derive_from_defaults(self) -> None:
        self._default_max_position_lots: int = int(self.defaults.get("max_position_lots", 1_000))

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            return True, "OK"

        cache_key = intent.strategy_id
        max_lots = self._max_position_cache.get(cache_key)
        if max_lots is None:
            strat_cfg = self.strat_configs.get(intent.strategy_id, {})
            max_lots = int(
                strat_cfg.get(
                    "max_position_lots",
                    strat_cfg.get("max_position", self._default_max_position_lots),
                )
            )
            self._max_position_cache[cache_key] = max_lots

        current_qty = self._current_position(intent.symbol, intent.strategy_id)
        signed_qty = int(intent.qty if intent.side == Side.BUY else -intent.qty)
        resulting_qty = current_qty + signed_qty
        if abs(resulting_qty) > max_lots:
            # Bug 21 applies here too, and this was the one safety gate missing
            # it: MaxNotional, DailyLossLimit and PerSymbolNotional all bypass
            # for cover orders, so a position already over its cap was rejected
            # in *both* directions and could never be unwound. Live evidence
            # 2026-09-04: TMFI6 sat at -5 lots against max_position_lots=1 for
            # 59 min while 21 covering intents were rejected as
            # "POSITION_LIMIT_EXCEEDED: abs(-4) > 1".
            #
            # reduces_position() is strictly monotone toward flat
            # (abs(current + signed) < abs(current)) and returns False for an
            # over-cover that flips sign and matches or grows magnitude, so this
            # can never open or grow exposure -- it only lets the strategy walk
            # back to flat. Evaluated on the reject path only, so the approving
            # path costs no extra position lookup.
            if self.reduces_position(intent):
                return True, "REDUCE_ONLY_BYPASS"
            return False, f"POSITION_LIMIT_EXCEEDED: abs({resulting_qty}) > {max_lots}"

        return True, "OK"


class DailyLossLimitValidator(RiskValidator):
    """Stateful validator: rejects orders when accumulated daily loss exceeds limit.

    Tracks cumulative PnL updates per strategy (realized) plus a single platform-wide
    unrealized PnL value supplied by the caller via update_unrealized().  Both are
    combined when evaluating the limit.

    Reset boundary: 05:00 Taiwan Standard Time (UTC+8) = 21:00 UTC of the
    *previous* calendar day.  This aligns with Taiwan futures settlement.

    Prices are scaled int x10000 per platform conventions.
    Uses timebase.now_ns() for time — never datetime.now().

    Accumulator unit: ``NTD * price_scale``. Both producers (PositionStore for
    realized, MtMCalculator for unrealized) have already multiplied the price
    delta by the instrument's contract multiplier / point value, so the value
    that arrives here is money, scaled -- not points.

    Intraday watermark extensions (opt-in via config ``intraday_pnl`` section):
    - Soft limit: rejects new orders (not CANCEL/FORCE_FLAT) when loss exceeds
      ``soft_limit_ntd``, with cooldown-guarded recovery.
    - Hard limit: triggers HALT when loss exceeds ``hard_limit_ntd`` (overrides
      ``max_daily_loss`` when intraday_pnl is configured).

    A loss is the trading day's net change -- realized since the 05:00 roll
    plus today's share of unrealized -- and nothing else. The give-back-from-peak
    rule (``peak_drawdown_pct``, 40%) was removed 2026-10-23: it judged a
    retracement inside a winning day as a stop, and on 2026-10-22 16:03Z it
    halted R47 with the day 1,270 NTD up.
    """

    __slots__ = (
        "_default_max_daily_loss",
        "_accumulated_loss",
        "_current_reset_boundary_ns",
        "_unrealized_pnl",
        "halt_triggered",
        "_halt_release_pending",
        # Intraday watermark state
        "_intraday_pnl_enabled",
        "soft_limit_active",
        "_soft_limit_cooldown_until_ns",
        "_soft_limit_threshold_scaled",
        "_soft_recovery_threshold_scaled",
        "_soft_limit_cooldown_ns",
        "_hard_limit_threshold_scaled",
        "_unrealized_stale",
        "_last_consistent_total_pnl",
        "_last_consistent_ts_ns",
        "_halt_reason",
        "_unrealized_day_open",
        "_day_open_known",
        "_unrealized_complete",
        "_position_loss_limit_scaled",
    )

    # How long the last time-consistent PnL total may stand in for the live
    # sum. The supervisor feeds ``update_unrealized`` at 1 Hz, so the real gap
    # after a fill is under a second; five seconds is five missed ticks, which
    # is a dead mark-to-market rather than normal jitter.
    _STALE_GRACE_NS: int = 5 * 1_000_000_000

    # Nanoseconds per calendar day
    _NS_PER_DAY: int = 86_400 * 1_000_000_000
    # 05:00 Taiwan (UTC+8) = 21:00 UTC = 21 * 3600 seconds into the UTC day
    _RESET_OFFSET_NS: int = 21 * 3600 * 1_000_000_000

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Stored as a positive threshold; loss is compared as abs value
        # Accumulated realized PnL per strategy (negative = loss, positive = gain); scaled int
        self._accumulated_loss: Dict[str, int] = {}
        # Epoch-ns of the last 21:00 UTC reset boundary (cached)
        self._current_reset_boundary_ns: int = self._current_boundary_ns()
        # Platform-wide unrealized PnL (scaled int, updated externally)
        self._unrealized_pnl: int = 0
        # Set to True when limit is breached. Cleared by the 21:00 UTC daily
        # boundary in _maybe_reset(); _force_reset() is a test/manual override
        # with no production caller, so it is not a recovery path.
        self.halt_triggered: bool = False
        # True when the calendar boundary passed while the stop was latched but
        # no PnL snapshot was available to justify releasing it. The stop stands
        # until a fresh snapshot arrives; see roll_daily_boundary().
        self._halt_release_pending: bool = False

        self._derive_from_defaults()

        # Runtime watermark state
        self.soft_limit_active: bool = False
        self._soft_limit_cooldown_until_ns: int = 0

        # Two-clock consistency. ``_accumulated_loss`` is fed at the fill;
        # ``_unrealized_pnl`` is fed by the 1 Hz mark-to-market tick. Between a
        # closing fill and the next tick the same money sits in both halves, so
        # a naive sum counts it twice. THESHOW 2026-09-11T00:45:41Z: a book that
        # only ever rose, +795 -> +1045 -> +2320, recorded a peak of +4270 and
        # halted for 20 h on a 45.7% "drawdown" that was exactly the stale mark.
        # (That gate, the peak watermark, is gone; the soft and hard limits
        # still read this total.)
        self._unrealized_stale: bool = False
        self._last_consistent_total_pnl: int = 0
        self._last_consistent_ts_ns: int = 0

        # Which branch latched the stop: ``DAILY_LOSS_LIMIT`` or "". Kept as a
        # field so the persisted state and the manual latch can name it.
        self._halt_reason: str = ""

        # The daily limit judges what the book did TODAY. ``_unrealized_pnl``
        # is each position's PnL since entry, so a lot carried overnight
        # brought every previous day's loss into every new day: on THESHOW one
        # TMFJ6 long re-latched the 5,000 NTD stop at every 05:00 reset from
        # 10/16 to its settlement on 10/21, while the stop itself kept the lot
        # from being closed. ``_unrealized_day_open`` is the since-entry figure
        # at the start of the trading day (the first complete snapshot after
        # the 05:00 boundary); today's unrealized is the difference. Exact for
        # closes too: a carried lot closed today realizes (exit - entry), and
        # subtracting (open - entry) leaves (exit - open).
        #
        # Until a day-open figure is known (boot, or a boundary rolled with no
        # complete snapshot) the since-entry figure is used unchanged, which is
        # the old and more conservative reading.
        self._unrealized_day_open: int = 0
        self._day_open_known: bool = False
        self._unrealized_complete: bool = False

    def _derive_from_defaults(self) -> None:
        """Thresholds only. Runtime state (accumulated loss, latched HALT,
        cooldown deadline) is untouched, so a SIGHUP reload
        cannot clear a live halt or reset the drawdown watermark."""
        # Stored as a positive threshold; loss is compared as abs value
        self._default_max_daily_loss: int = int(self.defaults.get("max_daily_loss", 500_000_000))

        # --- Intraday watermark config ---
        ipnl_cfg = self.config.get("intraday_pnl", {})
        self._intraday_pnl_enabled: bool = bool(ipnl_cfg)

        if self._intraday_pnl_enabled:
            price_scale: int = int(ipnl_cfg.get("price_scale", 10000))
            # 1 NTD == price_scale accumulator units.
            #
            # The accumulator is fed by ``record_pnl`` (realized, from
            # PositionStore) and ``update_unrealized`` (from MtMCalculator).
            # Both producers compute ``delta_price_scaled * qty *
            # contract_multiplier`` where ``contract_multiplier`` IS the
            # instrument's point value (positions.py PositionState.update,
            # mtm.py MtMCalculator._unrealized). So the point value is already
            # inside the number that arrives here, and its unit is
            # ``NTD * price_scale`` for futures and stocks alike.
            #
            # This used to divide by ``point_value`` a second time
            # (``price_scale // point_value``), which enforced every threshold
            # at 1/point_value of its configured value: on 2026-08-21 in
            # production ``hard_limit_ntd: 5000`` halted at 670 NTD of realized
            # loss and ``soft_limit_ntd: 500`` fired at 50 NTD. The error
            # factor was the point value itself -- 10x for TMF, 50x for MXF,
            # 200x for TXF. It was unreachable until fills started recording
            # again, which is why it survived this long.
            ntd_to_scaled: int = price_scale

            self._soft_limit_threshold_scaled: int = int(ipnl_cfg.get("soft_limit_ntd", 500) * ntd_to_scaled)
            self._hard_limit_threshold_scaled: int = int(ipnl_cfg.get("hard_limit_ntd", 1000) * ntd_to_scaled)
            self._soft_recovery_threshold_scaled: int = int(ipnl_cfg.get("soft_recovery_ntd", 300) * ntd_to_scaled)
            self._soft_limit_cooldown_ns: int = int(ipnl_cfg.get("soft_limit_cooldown_s", 60) * 1_000_000_000)
            # Loss on one position since its entry, across days, at which the
            # platform closes it (``risk/loss_exit.py``). The daily limit no
            # longer sees a carried lot's earlier losses, so this is what bounds
            # them. Defaults to the hard daily limit, the figure that bounded
            # them before.
            self._position_loss_limit_scaled: int = int(
                ipnl_cfg.get("position_loss_limit_ntd", ipnl_cfg.get("hard_limit_ntd", 1000)) * ntd_to_scaled
            )
        else:
            self._soft_limit_threshold_scaled = 0
            self._hard_limit_threshold_scaled = 0
            self._soft_recovery_threshold_scaled = 0
            self._soft_limit_cooldown_ns = 0
            self._position_loss_limit_scaled = 0

    @staticmethod
    def _current_boundary_ns() -> int:
        """Return epoch-ns of the most recent 21:00 UTC reset boundary."""
        now_ns = timebase.now_ns()
        ns_per_day = 86_400 * 1_000_000_000
        offset = 21 * 3600 * 1_000_000_000
        # Shift time back by offset so that floor-to-day gives us the last 21:00 UTC
        return ((now_ns - offset) // ns_per_day) * ns_per_day + offset

    def _maybe_reset(self, *, release_halt: bool = False) -> None:
        """Reset accumulated losses if the 05:00 Taiwan (21:00 UTC) boundary has passed.

        Forward-only. ``timebase.now_ns()`` is ``time.time_ns()`` -- the wall
        clock -- so an NTP correction or a manual clock set can move it
        backward across the boundary. The old ``!=`` comparison read that as a
        new trading day and cleared a latched financial stop without one ever
        passing; ``>`` makes a backward clock a no-op, which is the fail-closed
        direction. A forward jump over several days still resets exactly once.

        ``release_halt`` defaults to False: the calendar work happens, the stop
        stands. Only ``update_unrealized()`` passes True, and only when the
        snapshot it carries is complete. ``check()`` and ``record_pnl()`` must
        never release -- a fill can land just after the boundary and before the
        supervisor's first roll, and letting ``record_pnl()`` clear the latch
        there would both lift the stop on no valuation at all and advance the
        boundary, so the supervisor's later roll could no longer re-arm it.
        """
        boundary_ns = self._current_boundary_ns()
        if boundary_ns > self._current_reset_boundary_ns:
            logger.info(
                "DailyLossLimitValidator: daily reset (05:00 TST)",
                prev_boundary_ns=self._current_reset_boundary_ns,
                new_boundary_ns=boundary_ns,
                strategies_reset=list(self._accumulated_loss.keys()),
                halt_released=release_halt,
            )
            self._accumulated_loss.clear()
            self._current_reset_boundary_ns = boundary_ns
            # A new trading day: its opening figure is the next complete
            # snapshot, which ``update_unrealized`` records.
            self._day_open_known = False
            # Reset soft-limit state on daily boundary
            self.soft_limit_active = False
            self._soft_limit_cooldown_until_ns = 0
            if release_halt:
                self._unrealized_pnl = 0
                self.halt_triggered = False
                self._halt_release_pending = False
                self._halt_reason = ""
            else:
                # Calendar rolled, but this caller brought no complete PnL to
                # justify reopening.
                if self.halt_triggered:
                    self._halt_release_pending = True
                # Yesterday's unrealized figure must not be carried into the new
                # day as evidence. Zeroing it outright would let a dead
                # mark-to-market look like a flat book, so clamp instead: a
                # stale GAIN is dropped, a stale LOSS is kept. Both directions
                # then fail closed -- a leftover +10,000 cannot offset today's
                # -5,500 into looking safe, and a leftover loss cannot be
                # erased by the calendar. The first complete snapshot overwrites
                # this value anyway.
                if self._unrealized_pnl > 0:
                    self._unrealized_pnl = 0
            # The accumulator was just cleared, so whatever unrealized survived
            # the roll IS the total, and both halves now carry the same stamp.
            self._unrealized_stale = False
            self._last_consistent_total_pnl = self.daily_unrealized()
            self._last_consistent_ts_ns = timebase.now_ns()

    def roll_daily_boundary(self) -> None:
        """Advance the calendar boundary WITHOUT granting authorization to trade.

        The supervisor calls this every tick, including ticks where
        mark-to-market failed or never ran. That is deliberate: the boundary is
        a calendar fact, and leaving it behind an unrelated failure is what made
        the 2026-08-26 stop permanent.

        Clearing ``halt_triggered`` is not a calendar fact. It is an
        authorization to send orders again, and an authorization needs evidence
        that the loss being released is actually known. This caller has none --
        it has no PnL value to offer, which is why it exists as a separate
        method at all. So the accumulator rolls, the stop stands, and the next
        successful ``update_unrealized()`` releases it. A dead mark-to-market
        therefore holds the stop instead of clearing it while exposure is
        unknown.
        """
        self._maybe_reset(release_halt=False)

    def _force_reset(self) -> None:
        """Unconditionally clear all accumulated state (e.g. for testing or manual override)."""
        self._accumulated_loss.clear()
        self._unrealized_pnl = 0
        self.halt_triggered = False
        self._halt_release_pending = False
        self._halt_reason = ""
        self._current_reset_boundary_ns = self._current_boundary_ns()
        self.soft_limit_active = False
        self._soft_limit_cooldown_until_ns = 0
        self._unrealized_stale = False
        self._last_consistent_total_pnl = 0
        self._last_consistent_ts_ns = 0
        self._unrealized_day_open = 0
        self._day_open_known = False
        self._unrealized_complete = False

    def daily_unrealized(self) -> int:
        """Today's share of the unrealized PnL: since-entry now minus since-entry at the day's open.

        Before the day's opening figure is known this is the since-entry figure.
        On an incomplete valuation an unpriced lot drops out of ``_unrealized_pnl``
        but not out of the opening figure, which could turn its loss into an
        apparent gain; the smaller of the two readings is used then.
        """
        if not self._day_open_known:
            return self._unrealized_pnl
        today = self._unrealized_pnl - self._unrealized_day_open
        if not self._unrealized_complete:
            return min(today, self._unrealized_pnl)
        return today

    @property
    def position_loss_limit_scaled(self) -> int:
        """Since-entry loss on one position at which the platform closes it; 0 when not configured."""
        return self._position_loss_limit_scaled

    @property
    def day_open_known(self) -> bool:
        return self._day_open_known

    @property
    def halt_reason(self) -> str:
        """Which branch latched the stop: ``DAILY_LOSS_LIMIT`` or ``""``."""
        return self._halt_reason

    def export_state(self) -> Dict[str, Any]:
        """The day's runtime state, for ``risk/daily_loss_state.py`` to persist.

        Everything a restart used to throw away: the realized accumulators, the
        latch and its reason, the soft-limit state and the
        day's opening mark. Unrealized PnL is left out on purpose -- the first
        mark-to-market tick after boot recomputes it from the restored book.
        """
        return {
            "boundary_ns": self._current_reset_boundary_ns,
            "accumulated": dict(self._accumulated_loss),
            "halt_triggered": self.halt_triggered,
            "halt_reason": self._halt_reason,
            "halt_release_pending": self._halt_release_pending,
            "soft_limit_active": self.soft_limit_active,
            "soft_limit_cooldown_until_ns": self._soft_limit_cooldown_until_ns,
            "unrealized_day_open": self._unrealized_day_open,
            "day_open_known": self._day_open_known,
            # Read by nothing here. The pre-B4a restore_state requires both as
            # ints and drops the whole document without them, so a same-day
            # rollback would start the day's realized loss from zero. Remove
            # once no deployable build predates B4a.
            "peak_drawdown_halt_ts_ns": 0,
            "peak_pnl_scaled": 0,
        }

    def restore_state(self, state: Dict[str, Any]) -> bool:
        """Load a state written by :meth:`export_state` in this trading day.

        Returns False, changing nothing, when the state belongs to another
        trading day: the day roll would have cleared it anyway, and a daily
        stop that must outlive the roll is a manual latch on disk, not this.
        Raises ``ValueError`` on a malformed document, also changing nothing.

        A document written before the peak-drawdown rule was removed may carry
        ``halt_reason: PEAK_DRAWDOWN`` and the two peak fields. The fields are
        ignored, and that latch is restored as released: the rule that set it
        no longer exists, so nothing could ever lift it again.
        """
        ints = ("boundary_ns", "soft_limit_cooldown_until_ns")
        bools = ("halt_triggered", "halt_release_pending", "soft_limit_active", "day_open_known")
        for key in (*ints, "unrealized_day_open"):
            if type(state.get(key)) is not int:
                raise ValueError(f"daily loss state field {key!r} is not an int")
        for key in bools:
            if type(state.get(key)) is not bool:
                raise ValueError(f"daily loss state field {key!r} is not a bool")
        accumulated = state.get("accumulated")
        if not isinstance(accumulated, dict) or not all(
            isinstance(k, str) and type(v) is int for k, v in accumulated.items()
        ):
            raise ValueError("daily loss state field 'accumulated' is not a map of str to int")
        reason = state.get("halt_reason")
        if reason not in ("", "DAILY_LOSS_LIMIT", "PEAK_DRAWDOWN"):
            raise ValueError(f"daily loss state field 'halt_reason' is {reason!r}")
        if state["boundary_ns"] != self._current_boundary_ns():
            return False
        halt_triggered = state["halt_triggered"]
        if reason == "PEAK_DRAWDOWN":
            logger.warning("daily_loss_state_peak_drawdown_latch_dropped", halt_triggered=halt_triggered)
            halt_triggered = False
            reason = ""
        self._current_reset_boundary_ns = state["boundary_ns"]
        self._accumulated_loss = dict(accumulated)
        self.halt_triggered = halt_triggered
        self._halt_reason = reason
        self._halt_release_pending = state["halt_release_pending"] and halt_triggered
        self.soft_limit_active = state["soft_limit_active"]
        self._soft_limit_cooldown_until_ns = state["soft_limit_cooldown_until_ns"]
        self._unrealized_day_open = state["unrealized_day_open"]
        self._day_open_known = state["day_open_known"]
        self._last_consistent_total_pnl = sum(self._accumulated_loss.values())
        self._last_consistent_ts_ns = timebase.now_ns()
        return True

    def update_unrealized(self, unrealized_scaled: int, *, complete: bool = False) -> None:
        """Update the platform-wide unrealized PnL used in loss calculations.

        Also evaluates whether total (realized + unrealized) exceeds the hard
        limit, setting ``halt_triggered`` immediately so that the supervisor
        loop can escalate StormGuard without waiting for a new intent.

        Args:
            unrealized_scaled: Total unrealized PnL in scaled int (x10000).
                               Negative = unrealized loss; positive = unrealized gain.
            complete: True only when every non-flat position was priced --
                      ``MtMSnapshot.complete``. It defaults to False because an
                      unqualified integer is not evidence: ``calculate()`` skips
                      unpriced positions and sums the rest, so an entirely
                      unpriced book reports 0, which is indistinguishable from a
                      flat one. A partial total may still LATCH the stop (that
                      direction is safe); only a complete one may release it.
        """
        # The daily reset runs BEFORE the latch is consulted, because the latch
        # gates every other way of reaching it. ``check()`` needs an intent and
        # StormGuard HALT rejects intents upstream of the validator;
        # ``record_pnl()`` needs a fill and HALT blocks the orders that make
        # one. This supervisor tick is the only caller left, so reaching the
        # reset through ``if not self.halt_triggered`` made the stop permanent:
        # on THESHOW the hard limit latched 2026-08-26 14:24:35Z and HALT was
        # still held at 09:32Z the next day, 12.5 h past the 21:00Z boundary,
        # with the gateway down the whole time.
        self._maybe_reset(release_halt=complete)
        self._unrealized_pnl = unrealized_scaled
        self._unrealized_complete = complete
        if complete and not self._day_open_known:
            self._unrealized_day_open = unrealized_scaled
            self._day_open_known = True
        # This snapshot is read from the position store the fill has already
        # updated, so it restores time-consistency between the two feeds
        # regardless of whether every position could be priced. Completeness is
        # a separate axis, carried by ``complete``.
        self._unrealized_stale = False
        self._last_consistent_total_pnl = sum(self._accumulated_loss.values()) + self.daily_unrealized()
        self._last_consistent_ts_ns = timebase.now_ns()
        if self._halt_release_pending and complete:
            # A supervisor tick already rolled the calendar while the stop was
            # latched, but had no PnL to justify lifting it. This call carries
            # the fresh snapshot that was missing, so the stop lifts now and the
            # new day is judged on the new number below.
            self._halt_release_pending = False
            self.halt_triggered = False
            self._halt_reason = ""
            logger.info(
                "DailyLossLimitValidator: daily stop released on complete PnL snapshot",
                unrealized_pnl=unrealized_scaled,
            )
        if not self.halt_triggered:
            self._evaluate_halt_from_unrealized()

    def _evaluate_halt_from_unrealized(self) -> None:
        """Check total (realized + unrealized) against hard limit without an intent.

        Sets ``halt_triggered`` if the combined loss exceeds the hard limit,
        allowing the supervisor to escalate StormGuard between order evaluations.
        """
        self._maybe_reset()
        total_realized = sum(self._accumulated_loss.values())
        total_pnl = total_realized + self.daily_unrealized()
        if total_pnl >= 0:
            return
        loss_magnitude = -total_pnl
        max_daily_loss = (
            self._hard_limit_threshold_scaled if self._intraday_pnl_enabled else self._default_max_daily_loss
        )
        if loss_magnitude >= max_daily_loss:
            self.halt_triggered = True
            self._halt_reason = "DAILY_LOSS_LIMIT"
            logger.warning(
                "DailyLossLimitValidator: hard limit exceeded via unrealized update",
                total_realized=total_realized,
                unrealized_pnl=self._unrealized_pnl,
                daily_unrealized=self.daily_unrealized(),
                total_pnl=total_pnl,
                max_daily_loss=max_daily_loss,
            )

    def record_pnl(self, strategy_id: str, pnl_delta: int) -> None:
        """Record a realized PnL delta (negative = loss) for a strategy.

        Args:
            strategy_id: Strategy identifier string.
            pnl_delta: Realized PnL change in scaled int (x10000). Negative = loss.
        """
        self._maybe_reset()
        current = self._accumulated_loss.get(strategy_id, 0)
        self._accumulated_loss[strategy_id] = current + pnl_delta
        if self._unrealized_pnl != 0:
            # A realizing fill landed while a mark is outstanding. Until the
            # next mark-to-market tick the two feeds disagree about the same
            # money, so the gates fall back to the last total whose halves were
            # sampled together. When unrealized is zero there is nothing to
            # double-count and the sum is already consistent, which is why the
            # flag is conditional rather than unconditional.
            self._unrealized_stale = True
        else:
            self._last_consistent_total_pnl = sum(self._accumulated_loss.values())
            self._last_consistent_ts_ns = timebase.now_ns()

    def _evaluate_soft_limit(
        self,
        intent: OrderIntent,
        total_pnl: int,
    ) -> Tuple[bool, str] | None:
        """Soft-limit recovery / rejection branch (P3-c2 extraction).

        Returns:
            ``None`` when soft limit is not active, or has just recovered
            (caller should fall through to subsequent checks).
            ``(True, reason)`` if a flat-cooldown bypass applies.
            ``(False, reason)`` to reject the intent.
        """
        if not self.soft_limit_active:
            return None

        now_ns = timebase.now_ns()
        # Recovery: loss must be below recovery threshold AND cooldown must
        # have elapsed.
        recovery_loss = -total_pnl  # positive = loss, negative = gain
        if recovery_loss < self._soft_recovery_threshold_scaled and now_ns >= self._soft_limit_cooldown_until_ns:
            logger.info(
                "DailyLossLimitValidator: soft limit recovered",
                total_pnl=total_pnl,
                recovery_threshold_scaled=self._soft_recovery_threshold_scaled,
            )
            self.soft_limit_active = False
            self._soft_limit_cooldown_until_ns = 0
            return None

        current_pos: int | None = None
        if self._position_provider is not None:
            try:
                current_pos = self._current_position(intent.symbol, intent.strategy_id)
            except Exception:  # noqa: BLE001 — fail closed if position lookup breaks
                current_pos = None
        # Bug #39: maker-style strategies can deadlock in SOFT_LIMIT when
        # flat because no intent qualifies as reduce-only, so PnL never
        # moves and recovery_loss never improves. After the cooldown
        # expires, allow new orders again only while flat. Once exposed,
        # the existing SOFT_LIMIT reduce-only behavior still applies.
        if current_pos == 0 and now_ns >= self._soft_limit_cooldown_until_ns:
            logger.info(
                "DailyLossLimitValidator: soft limit flat cooldown bypass",
                strategy_id=intent.strategy_id,
                total_pnl=total_pnl,
            )
            return True, "SOFT_LIMIT_FLAT_COOLDOWN_BYPASS"
        return False, (f"SOFT_LIMIT: loss={-total_pnl} >= threshold={self._soft_limit_threshold_scaled}")

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        # a. Bypass CANCEL and FORCE_FLAT for all checks
        if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            return True, "OK"

        # Bug 21 (2026-04-17): cover orders must bypass SOFT_LIMIT
        # / DAILY_LOSS_LIMIT so a strategy stuck in adverse PnL can always reduce
        # risk. Without this R47 deadlocked short -1 TMFE6 while drawdown grew
        # from -120 NTD to -2680 NTD (realized on manual cover).
        if self.reduces_position(intent):
            return True, "REDUCE_ONLY_BYPASS"

        # b. Daily reset check
        self._maybe_reset()

        # c. Compute total PnL (realized + unrealized)
        # NOTE: accumulated is per-strategy realized, _unrealized_pnl is platform-wide.
        # When _intraday_pnl_enabled=True, global limits are used — consistent.
        # When False (legacy), per-strategy limits apply to this mixed total, meaning
        # strategy A can be blocked by strategy B's unrealized losses. This is an
        # intentionally conservative design — see docstring. Per-strategy unrealized
        # tracking would require a per-strategy feed from ExecutionRouter.
        if self._intraday_pnl_enabled:
            # Global scope: sum ALL strategies' realized PnL
            accumulated = sum(self._accumulated_loss.values())
        else:
            # Legacy per-strategy scope
            accumulated = self._accumulated_loss.get(intent.strategy_id, 0)
        total_pnl = accumulated + self.daily_unrealized()

        # c2. Two-clock guard. ``record_pnl`` books a realized delta at the fill;
        # ``update_unrealized`` refreshes the mark on the 1 Hz supervisor tick.
        # In the window between them the closing fill's PnL sits in both halves,
        # so the sum counts it twice -- upward after a winning close, downward
        # after a losing one. Gate on the last total whose halves were sampled
        # together: at most one tick old, on a series only sampled once a tick.
        #
        # Bounded by ``_STALE_GRACE_NS``. The substitution is only ever right
        # while the next tick really is coming. If mark-to-market dies -- it is
        # wrapped in a try in the supervisor loop -- no tick arrives, and
        # holding the last consistent total indefinitely would hide every
        # subsequent realized loss from these gates. Past the grace the raw sum
        # comes back, which is exactly the pre-fix behaviour: still the
        # fail-closed direction, on a book whose valuation is already unknown.
        if (
            self._intraday_pnl_enabled
            and self._unrealized_stale
            and (timebase.now_ns() - self._last_consistent_ts_ns) <= self._STALE_GRACE_NS
        ):
            total_pnl = self._last_consistent_total_pnl

        # e. Hard halt is sticky — no recovery (only when intraday watermark is enabled;
        #    when disabled the original per-strategy evaluation runs each check)
        if self.halt_triggered and self._intraday_pnl_enabled:
            return False, "DAILY_LOSS_HALT_TRIGGERED"

        # f. Soft limit recovery / rejection (extracted helper).
        soft_decision = self._evaluate_soft_limit(intent, total_pnl)
        if soft_decision is not None:
            return soft_decision

        # h. Net gain or breakeven — no further loss checks needed
        if total_pnl >= 0:
            return True, "OK"

        loss_magnitude = -total_pnl  # positive value representing combined loss

        # i. Hard limit check — evaluated before soft limit so hard limit always wins
        strat_cfg = self.strat_configs.get(intent.strategy_id, {})
        if self._intraday_pnl_enabled:
            max_daily_loss = self._hard_limit_threshold_scaled
        else:
            max_daily_loss = int(strat_cfg.get("max_daily_loss", self._default_max_daily_loss))

        if loss_magnitude >= max_daily_loss:
            self.halt_triggered = True
            self._halt_reason = "DAILY_LOSS_LIMIT"
            logger.warning(
                "DailyLossLimitValidator: daily loss limit exceeded",
                strategy_id=intent.strategy_id,
                accumulated_loss=accumulated,
                unrealized_pnl=self._unrealized_pnl,
                daily_unrealized=self.daily_unrealized(),
                total_pnl=total_pnl,
                max_daily_loss=max_daily_loss,
            )
            return False, f"DAILY_LOSS_LIMIT_EXCEEDED: loss={loss_magnitude} >= limit={max_daily_loss}"

        # j. Soft limit trigger
        if self._intraday_pnl_enabled and loss_magnitude >= self._soft_limit_threshold_scaled:
            now_ns = timebase.now_ns()
            self.soft_limit_active = True
            self._soft_limit_cooldown_until_ns = now_ns + self._soft_limit_cooldown_ns
            logger.warning(
                "DailyLossLimitValidator: soft limit triggered",
                strategy_id=intent.strategy_id,
                total_pnl=total_pnl,
                soft_limit_threshold=self._soft_limit_threshold_scaled,
            )
            return False, f"SOFT_LIMIT: loss={loss_magnitude} >= threshold={self._soft_limit_threshold_scaled}"

        return True, "OK"


class ExpiringContractValidator(RiskValidator):
    """Refuse to open or add to a monthly future past its expiry cutoff.

    From ``expiry_guard.cutoff`` (default 13:00 Taipei) to the end of a
    contract's delivery date, an intent that does not strictly reduce the
    position is rejected;
    ``risk/loss_exit.py`` closes what is left. Without this a maker keeps
    quoting the expiring month until the session ends and reopens every lot the
    exit closes. CANCEL, FORCE_FLAT and reducing orders always pass.

    Config: ``expiry_guard: {enabled: true, cutoff: "13:00"}``. Enabled unless
    explicitly disabled.
    """

    __slots__ = ("_enabled", "_cutoff_minute", "_window_ns_cache")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._derive_from_defaults()

    def _derive_from_defaults(self) -> None:
        cfg = self.config.get("expiry_guard", {}) or {}
        self._enabled: bool = bool(cfg.get("enabled", True))
        self._cutoff_minute: int = parse_cutoff_minute(cfg.get("cutoff"))
        # symbol -> expiry window [start, end) in epoch-ns (``NEVER_NS`` for
        # non-monthly codes). Rebuilt here rather than cleared by ``reload``:
        # that walks ``vars(self)``, which does not see ``__slots__``.
        self._window_ns_cache: Dict[str, Tuple[int, int]] = {}

    @property
    def cutoff_minute(self) -> int:
        return self._cutoff_minute

    @property
    def enabled(self) -> bool:
        return self._enabled

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if not self._enabled or intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            return True, "OK"
        now_ns = timebase.now_ns()
        window = self._window_ns_cache.get(intent.symbol)
        if window is None:
            window = expiry_window_ns(intent.symbol, now_ns, self._cutoff_minute)
            self._window_ns_cache[intent.symbol] = window
        if not (window[0] <= now_ns < window[1]):
            return True, "OK"
        if self.reduces_position(intent):
            return True, "REDUCE_ONLY_BYPASS"
        return False, f"EXPIRY_CUTOFF: {intent.symbol} past its last-trading-day cutoff"


class PerSymbolNotionalValidator(RiskValidator):
    """Reject orders where per-symbol notional (price * qty / scale) exceeds the configured limit.

    Config resolution order:
      1. strategies.<id>.symbol_limits.<symbol>.max_notional
      2. global_defaults.per_symbol_max_notional
      3. Hard-coded fallback (50_000_000)

    Cache is keyed by (strategy_id, symbol) with a bounded cardinality of
    ``_MAX_CACHE_ENTRIES`` (default 10_000) per CE2-12 governance rule.
    """

    __slots__ = (
        "_default_per_symbol_max_notional_raw",
        "_per_symbol_notional_cache",
        "_MAX_CACHE_ENTRIES",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._derive_from_defaults()

    def _derive_from_defaults(self) -> None:
        self._default_per_symbol_max_notional_raw: int = int(self.defaults.get("per_symbol_max_notional", 50_000_000))
        self._per_symbol_notional_cache: Dict[tuple[str, str], int] = {}
        self._MAX_CACHE_ENTRIES: int = int(os.getenv("HFT_RISK_PER_SYMBOL_CACHE_MAX", "10000"))

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        # Bug 21: cover orders bypass per-symbol notional cap.
        if self.reduces_position(intent):
            return True, "REDUCE_ONLY_BYPASS"

        cache_key = (intent.strategy_id, intent.symbol)
        max_notional_scaled = self._per_symbol_notional_cache.get(cache_key)
        if max_notional_scaled is None:
            # Resolve config: strategy-level symbol_limits > global default
            strat_cfg = self.strat_configs.get(intent.strategy_id, {})
            symbol_limits = strat_cfg.get("symbol_limits", {})
            sym_cfg = symbol_limits.get(intent.symbol, {})
            max_notional_raw = sym_cfg.get(
                "max_notional",
                self._default_per_symbol_max_notional_raw,
            )

            scale = self._scale_factor(intent.symbol)
            max_notional_scaled = int(int(max_notional_raw) * scale)

            # Bounded cache — evict all on overflow (simple, safe)
            if len(self._per_symbol_notional_cache) >= self._MAX_CACHE_ENTRIES:
                logger.warning(
                    "PerSymbolNotionalValidator cache overflow, clearing",
                    size=len(self._per_symbol_notional_cache),
                    max=self._MAX_CACHE_ENTRIES,
                )
                self._per_symbol_notional_cache.clear()

            self._per_symbol_notional_cache[cache_key] = max_notional_scaled

        # notional = price * qty (both in scaled-int space).
        # To compare against a raw-currency limit that was also pre-scaled,
        # the comparison is direct: price_scaled * qty vs max_notional_raw * scale.
        notional_scaled = intent.price * intent.qty
        if notional_scaled > max_notional_scaled:
            return False, (f"PER_SYMBOL_NOTIONAL_EXCEEDED: {notional_scaled} > {max_notional_scaled}")

        return True, "OK"

    def clear_cache(self) -> None:
        """Clear the per-symbol notional cache (used by config hot-reload)."""
        self._per_symbol_notional_cache.clear()
