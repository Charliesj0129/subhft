"""alpha_strategy_bridge.py — Wraps AlphaProtocol as BaseStrategy for HftBacktestAdapter.

Part B of the dirty-data-repair + golden-data pipeline plan.

The bridge extracts L1 LOB data from LOBStatsEvent, calls alpha.update(**payload),
records (ts_ns, signal, mid_price) in signal_log, and returns empty OrderIntents
(position management is handled by HftNativeRunner from the signal log).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from hft_platform.contracts.strategy import OrderIntent
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy, StrategyContext

_PRICE_SCALE = 10_000  # platform default

# FeatureEngine tuple keys (lob_shared_v1, 16 values).
# Legacy ``fe_*`` prefix preserved for backward compatibility with alphas that
# read these specific kwarg names. Do not rename -- doing so silently breaks
# every existing alpha that wires to the v1 schema.
_FE_KEYS: tuple[str, ...] = (
    "fe_best_bid",
    "fe_best_ask",
    "fe_mid_x2",
    "fe_spread",
    "fe_bid_depth",
    "fe_ask_depth",
    "fe_imbalance_ppm",
    "fe_microprice_x2",
    "fe_l1_bid_qty",
    "fe_l1_ask_qty",
    "fe_l1_imbalance_ppm",
    "fe_ofi_l1_raw",
    "fe_ofi_l1_cum",
    "fe_ofi_l1_ema8",
    "fe_spread_ema8",
    "fe_imbalance_ema8_ppm",
)

# FE-v3 (lob_shared_v3) canonical names, indices 0..26.
# Source of truth: ``src/hft_platform/feature/registry.py`` builders
# ``build_default_lob_feature_set_v1/v2/v3``. Keep aligned with the registry.
_FE_KEYS_V3: tuple[str, ...] = (
    "best_bid",                  # 0
    "best_ask",                  # 1
    "mid_price_x2",              # 2
    "spread_scaled",             # 3
    "bid_depth",                 # 4
    "ask_depth",                 # 5
    "depth_imbalance_ppm",       # 6
    "microprice_x2",             # 7
    "l1_bid_qty",                # 8
    "l1_ask_qty",                # 9
    "l1_imbalance_ppm",          # 10
    "ofi_l1_raw",                # 11
    "ofi_l1_cum",                # 12
    "ofi_l1_ema8",               # 13
    "spread_ema8_scaled",        # 14
    "depth_imbalance_ema8_ppm",  # 15
    "ofi_depth_norm_ppm",        # 16
    "ret_autocov_5s_x1e6",       # 17
    "tob_survival_ms",           # 18
    "impact_surprise_x1000",     # 19
    "deep_depth_momentum_x1000", # 20
    "toxicity_ema50_x1000",      # 21
    "ofi_l1_ema5s",              # 22
    "ofi_l1_ema30s",             # 23
    "imbalance_ema5s_ppm",       # 24
    "spread_ema30s",             # 25
    "spread_ema300s",            # 26
)


class AlphaStrategyBridge(BaseStrategy):
    """Wraps AlphaProtocol as a BaseStrategy for HftBacktestAdapter.

    This bridge does NOT manage orders itself.  Instead it records every
    (ts_ns, signal, mid_price) tuple in signal_log so that HftNativeRunner
    can extract them after the backtest loop and compute BacktestResult metrics
    using the same infrastructure as ResearchBacktestRunner.

    Args:
        alpha: Any object implementing AlphaProtocol (manifest, reset, update).
        max_position: Upper bound on absolute position size (used by callers).
        signal_threshold: Minimum |signal| to act on (used by callers).
        symbol: Asset symbol string (used to filter events).
        price_scale: Divisor to convert scaled-integer prices to float.
    """

    def __init__(
        self,
        alpha: Any,
        *,
        max_position: int = 5,
        signal_threshold: float = 0.3,
        symbol: str = "",
        price_scale: int = _PRICE_SCALE,
        strategy_id: str = "alpha_bridge",
    ):
        super().__init__(strategy_id=strategy_id, subscribe_symbols=[symbol] if symbol else [])
        self._alpha = alpha
        self.max_position = int(max_position)
        self.signal_threshold = float(signal_threshold)
        self._price_scale = int(price_scale)
        self._symbol = symbol
        self._signal_log: list[tuple[int, float, float]] = []  # (ts_ns, signal, mid_price)

        # OFI state
        self._prev_bid_qty: float = 0.0
        self._prev_ask_qty: float = 0.0
        self._ofi_cum: float = 0.0
        self._first_tick_seen: bool = False

    def reset(self) -> None:
        """Reset alpha state and clear signal log."""
        self._signal_log.clear()
        self._prev_bid_qty = 0.0
        self._prev_ask_qty = 0.0
        self._ofi_cum = 0.0
        self._first_tick_seen = False
        try:
            self._alpha.reset()
        except Exception:
            pass

    @property
    def signal_log(self) -> list[tuple[int, float, float]]:
        """Read-only access to accumulated (ts_ns, signal, mid_price) tuples."""
        return self._signal_log

    # ------------------------------------------------------------------
    # BaseStrategy event dispatch
    # ------------------------------------------------------------------

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Respond to LOBStatsEvent: call alpha, record signal."""
        ts_ns = int(event.ts)

        # Extract float prices from scaled integers
        best_bid = float(event.best_bid) / self._price_scale
        best_ask = float(event.best_ask) / self._price_scale
        mid_price = (best_bid + best_ask) / 2.0

        bid_depth = float(getattr(event, "bid_depth", 0) or 0)
        ask_depth = float(getattr(event, "ask_depth", 0) or 0)
        imbalance = float(getattr(event, "imbalance", 0.0) or 0.0)

        # OFI computation — skip delta on first tick to avoid spurious spike
        if not self._first_tick_seen:
            self._first_tick_seen = True
            self._prev_bid_qty = bid_depth
            self._prev_ask_qty = ask_depth
            ofi_l1_raw = 0.0
        else:
            delta_bid = bid_depth - self._prev_bid_qty
            delta_ask = ask_depth - self._prev_ask_qty
            ofi_l1_raw = delta_bid - delta_ask
            self._prev_bid_qty = bid_depth
            self._prev_ask_qty = ask_depth
        self._ofi_cum += ofi_l1_raw

        # Build payload matching typical alpha.update() field names
        payload: dict[str, Any] = {
            "bid_px": best_bid,
            "ask_px": best_ask,
            "bid_qty": bid_depth,
            "ask_qty": ask_depth,
            "mid_price": mid_price,
            "current_mid": mid_price,
            "spread_bps": (best_ask - best_bid) / mid_price * 10_000.0 if mid_price > 0.0 else 0.0,
            "volume": 0.0,  # LOBStatsEvent does not carry trade volume
            "trade_vol": 0.0,
            "imbalance": imbalance,
            "ofi_l1_raw": ofi_l1_raw,
            "ofi_l1_cum": self._ofi_cum,
            "local_ts": ts_ns,
        }

        # Enrich with FeatureEngine values if available.
        #
        # Two enrichment shapes are supported additively:
        #
        # * v1 (16-tuple): legacy ``fe_*``-prefixed keys (``_FE_KEYS``) for
        #   alphas that wire to the v1 schema. Untouched semantics.
        # * v3 (>=27-tuple): registry-canonical names (``_FE_KEYS_V3``) AND
        #   a single ``features=tuple(ft)`` kwarg so AlphaProtocol-style
        #   alphas that consume the full feature vector by index can
        #   resolve every FE-v3 entry. Without this, every feature at
        #   indices 16-26 (e.g. ``ofi_l1_ema5s``, ``ofi_l1_ema30s``,
        #   ``deep_depth_momentum_x1000``) is silently dropped, which
        #   masquerades as an all-zero signal in Gate C output.
        try:
            if hasattr(self, "ctx") and self.ctx is not None:
                ft = self.ctx.get_feature_tuple(self._symbol)
                if ft is not None:
                    if len(ft) >= len(_FE_KEYS_V3):
                        ft_tuple = tuple(ft)
                        payload["features"] = ft_tuple
                        for k, v in zip(_FE_KEYS_V3, ft_tuple):
                            payload[k] = v
                    elif len(ft) >= len(_FE_KEYS):
                        for k, v in zip(_FE_KEYS, ft):
                            payload[k] = v
        except Exception:
            pass  # Graceful degradation — FeatureEngine may not be wired

        try:
            signal = float(self._alpha.update(**payload))
        except TypeError:
            # Some alphas only accept positional-style; try positional via keyword subset
            try:
                signal = float(
                    self._alpha.update(
                        bid_px=best_bid,
                        ask_px=best_ask,
                        bid_qty=bid_depth,
                        ask_qty=ask_depth,
                    )
                )
            except Exception:
                signal = 0.0
        except Exception:
            signal = 0.0

        # Codex adversarial-review 2026-05-06 finding 4 (HIGH): if the alpha
        # exposes a discrete fire gate (e.g. c75's 300-tick warmup +
        # spread-regime guard), the backtest must respect it; otherwise Gate C
        # scores a different strategy than the one declared for live use.
        # Gated-signal approach: when ``should_fire() == 0`` the composite is
        # zeroed before the position-converter sees it, preserving the existing
        # ``signal_threshold``-based interface for alphas that don't expose a
        # fire gate. The ``hasattr`` guard is load-bearing -- without it,
        # alphas that rely on raw signal-to-position semantics break.
        should_fire = getattr(self._alpha, "should_fire", None)
        if callable(should_fire):
            try:
                if int(should_fire()) == 0:
                    signal = 0.0
            except Exception:
                # Fail-open on a misbehaving fire gate is safer than
                # fail-closed -- the threshold gate downstream still filters.
                pass

        self._signal_log.append((ts_ns, signal, mid_price))

    # Return empty intents — HftNativeRunner drives position management externally
    # (the adapter's order execution is not used by this runner)
    def handle_event(self, ctx: StrategyContext, event: Any) -> list[OrderIntent]:
        self.ctx = ctx
        self._generated_intents.clear()

        if isinstance(event, LOBStatsEvent):
            # Apply symbol filter if configured
            if self._symbol and hasattr(event, "symbol") and event.symbol != self._symbol:
                return []
            self.on_stats(event)

        return self._generated_intents


# ---------------------------------------------------------------------------
# Numpy helpers — used by HftNativeRunner to extract arrays from signal_log
# ---------------------------------------------------------------------------
def signal_log_to_arrays(
    signal_log: list[tuple[int, float, float]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert signal_log to (timestamps, signals, mid_prices) arrays.

    Returns:
        timestamps_ns: int64 array
        signals: float64 array
        mid_prices: float64 array
    """
    if not signal_log:
        empty_i = np.zeros(0, dtype=np.int64)
        empty_f = np.zeros(0, dtype=np.float64)
        return empty_i, empty_f, empty_f

    arr = np.array(signal_log, dtype=np.float64)
    timestamps = arr[:, 0].astype(np.int64)
    signals = arr[:, 1]
    mid_prices = arr[:, 2]
    return timestamps, signals, mid_prices
