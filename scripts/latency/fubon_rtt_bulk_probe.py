"""Safe bulk order-API RTT probe for the Fubon (fubon-neo) TEST environment.

Counterpart of `shioaji_rtt_bulk_probe.py`: fires N place/cancel cycles with LIMIT ROD 1-lot
orders priced FAR from market so they cannot fill, and writes JSON with the same
schema (per-op count / errors / mean / std / P50 / P90 / P95 / P99 / max, bootstrap
95% CI for P50 and P95, raw samples in microseconds) so the two brokers can be
compared directly.

Everything goes through the platform's own `FubonClientFacade` (login, execution callbacks,
order gateway); the SDK is never imported or called here directly. Credentials are read
by the session runtime from the existing `HFT_FUBON_*` environment variables and are never
logged or written to the report.

Safety design
-------------

1. **Test environment only**: the probe refuses to start unless `HFT_ORDER_MODE` is not
   live and `HFT_FUBON_TEST_URL` passes the session runtime's `validate_test_url` guard AND names
   `wss://neoapitest.fbs.com.tw`. The check runs before the SDK is constructed (constructing
   it opens the trading websocket).
2. **Far-from-market price**: Buy at reference - offset, Sell at reference + offset
   (default 300 ticks = 300 index points). `--price` pins an explicit scaled-int (x10000)
   price instead and needs a single side (one price cannot be far for both).
3. **Immediate cancel**: every place is followed by a cancel in the same iteration.
   Orders whose cancel failed are cancelled again on exit (try/finally).
4. **Fill circuit-breaker**: any deal callback, any reported filled lot, or a cancel
   the broker refuses as "already filled" aborts the probe at once.
5. **Failure circuit-breaker**: five consecutive failed place_order calls abort.
6. **Rate respect**: `--sleep` (default 0.3 s) between cycles.

Measured operations
-------------------

* `place_order`: wall time of `facade.place_order` (blocks until the broker answers).
* `submitted_ack`: time from the START of the place_order call to the arrival of the
  order callback (`set_on_futopt_order`) that carries the order number. Fubon's place_order
  blocks, unlike Shioaji's, so the shioaji probe's "after place returns" clock would
  read ~0 here; start-of-call is the full submit-to-ack latency a strategy waits for.
  Errors count iterations with no ack within `--ack-timeout`.
* `cancel_order`: wall time of `facade.cancel_order`.

As in the shioaji probe, a missing sample counts as one error for that op.

Reference price
---------------

Without `--price` the reference is the last trade from the REST intraday quote
(`FubonSnapshotFetcher`, which needs `sdk.init_realtime()`; that call reaches the market-data
service, a host the URL guard above does not cover). Pass `--price` to avoid it.

Invocation
----------

Offline (mocked SDK, synthetic latencies, never connects)::

    uv run python scripts/latency/fubon_rtt_bulk_probe.py --dry-run \
        --iters 20 --sleep 0.01 --out /tmp/fubon_rtt_dry.json

Test environment (credentials and `HFT_FUBON_TEST_URL` already exported)::

    uv run python scripts/latency/fubon_rtt_bulk_probe.py \
        --symbol TXFL6 --iters 50 --offset-ticks 300 \
        --out outputs/fubon_rtt_test_bulk_$(date +%Y%m%d_%H%M).json

Exit codes: 0 clean, 2 refused / could not start, 3 aborted (fill or failure streak),
4 an order could not be cancelled on exit (ids are logged; cancel them by hand).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import json
import math
import os
import random
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import numpy as np
import structlog

from hft_platform.core import timebase
from hft_platform.feed_adapter.fubon import facade as facade_module
from hft_platform.feed_adapter.fubon.execution_callbacks import FubonCallbackRegistrationError
from hft_platform.feed_adapter.fubon.facade import FubonClientFacade
from hft_platform.feed_adapter.fubon.order_codec import PRICE_SCALE
from hft_platform.feed_adapter.fubon.order_gateway import FubonCancelNoOp
from hft_platform.feed_adapter.fubon.session_runtime import (
    ENV_ACCOUNT,
    ENV_API_KEY,
    ENV_CERT_PASS,
    ENV_CERT_PATH,
    ENV_ORDER_MODE,
    ENV_PASSWORD,
    ENV_PERSONAL_ID,
    ENV_TEST_URL,
    FubonSimSafetyError,
    is_live_order_mode,
    validate_test_url,
)
from hft_platform.feed_adapter.fubon.snapshot_fetcher import FubonSnapshotFetcher

log = structlog.get_logger("scripts.latency.fubon_rtt_bulk_probe")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_HOST = "neoapitest.fbs.com.tw"
_QTY = 1
_USER_DEF = "RTTPROBE"
_BROKER_CONFIG = {"broker": "fubon"}
_ACK_CAPACITY = 1024
_MAX_CONSECUTIVE_PLACE_ERRORS = 5
_QUOTE_REFRESH_EVERY = 10
_DEFAULT_TICK_SCALED = PRICE_SCALE  # one index point
_DEFAULT_DRY_RUN_SYMBOL = "TXFL6"

_EXIT_OK = 0
_EXIT_REFUSED = 2
_EXIT_ABORTED = 3
_EXIT_LEFTOVER = 4

_SUBMITTED_ACK_DEFINITION = "place_order call start -> order callback carrying the order number"


class ProbeRefusedError(RuntimeError):
    """The probe must not run (unsafe target, bad arguments, no session)."""


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------


def validate_probe_target() -> str:
    """Return the test URL, or raise unless the SDK would only ever reach the Fubon TEST host.

    The session runtime's `validate_test_url` already refuses an empty / non-ws / production URL;
    this adds the not-live check (live mode builds the SDK with the vendor default = production)
    and pins the host to the official test environment.
    """
    if is_live_order_mode():
        raise ProbeRefusedError(f"{ENV_ORDER_MODE} is live: the SDK would target production")
    try:
        url = validate_test_url(os.environ.get(ENV_TEST_URL))
    except FubonSimSafetyError as exc:
        raise ProbeRefusedError(str(exc)) from exc
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "wss" or host != _TEST_HOST:
        raise ProbeRefusedError(f"{ENV_TEST_URL} must be wss://{_TEST_HOST}, got scheme={parts.scheme!r} host={host!r}")
    return url


# ---------------------------------------------------------------------------
# Stats (same method and field names as shioaji_rtt_bulk_probe.py)
# ---------------------------------------------------------------------------


@dataclass
class OpStats:
    op: str
    count: int
    errors: int
    mean_us: float
    std_us: float
    p50_us: float
    p90_us: float
    p95_us: float
    p99_us: float
    max_us: float
    p50_ci95_us: tuple[float, float]
    p95_ci95_us: tuple[float, float]


def _bootstrap_percentile_ci(
    samples: np.ndarray, q: float, n_boot: int = 1000, alpha: float = 0.05
) -> tuple[float, float]:
    if samples.size < 10:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed=42)
    boots = rng.choice(samples, size=(n_boot, samples.size), replace=True)
    vals = np.percentile(boots, q * 100, axis=1)
    lo = float(np.percentile(vals, 100 * alpha / 2))
    hi = float(np.percentile(vals, 100 * (1 - alpha / 2)))
    return (lo, hi)


def _summarize(op: str, samples_us: list[int], errors: int) -> OpStats:
    if not samples_us:
        return OpStats(op, 0, errors, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, (0.0, 0.0), (0.0, 0.0))
    arr = np.asarray(samples_us, dtype=np.float64)
    return OpStats(
        op=op,
        count=int(arr.size),
        errors=errors,
        mean_us=float(arr.mean()),
        std_us=float(arr.std(ddof=0)),
        p50_us=float(np.percentile(arr, 50)),
        p90_us=float(np.percentile(arr, 90)),
        p95_us=float(np.percentile(arr, 95)),
        p99_us=float(np.percentile(arr, 99)),
        max_us=float(arr.max()),
        p50_ci95_us=_bootstrap_percentile_ci(arr, 0.5),
        p95_ci95_us=_bootstrap_percentile_ci(arr, 0.95),
    )


# ---------------------------------------------------------------------------
# Pricing (scaled int x10000 end to end, Law 4)
# ---------------------------------------------------------------------------


def _points_to_scaled(points: Any) -> int | None:
    """Convert a raw quote price in index points to a x10000 int via its 4-decimal text.

    `None` for anything that is not a positive finite number (no reference, no order).
    """
    if isinstance(points, bool) or not isinstance(points, int | float) or not math.isfinite(points) or points <= 0:
        return None
    whole, _, frac = f"{points:.4f}".partition(".")
    return int(whole) * PRICE_SCALE + int(frac or "0")


def price_for_side(side: str, reference_scaled: int, offset_ticks: int, tick_scaled: int) -> int:
    """Far-from-market scaled price: Buy below the reference, Sell above it (never below one tick)."""
    if side == "Buy":
        return max(tick_scaled, reference_scaled - offset_ticks * tick_scaled)
    return reference_scaled + offset_ticks * tick_scaled


class FixedPriceSource:
    """Explicit `--price` (scaled int)."""

    __slots__ = ("_price",)

    def __init__(self, price_scaled: int) -> None:
        self._price = price_scaled

    def __call__(self, side: str) -> int | None:
        return self._price


class QuotePriceSource:
    """Reference from the REST intraday quote (last trade), refreshed every few cycles.

    A failed refresh keeps the previous reference: with a multi-hundred-tick offset a
    few cycles of staleness cannot bring the order near the market. With no reference
    at all it answers `None` and the cycle is counted as an error.
    """

    __slots__ = ("_sdk", "_symbol", "_offset_ticks", "_tick_scaled", "_refresh_every", "_calls", "_reference")

    def __init__(
        self,
        sdk: Any,
        symbol: str,
        offset_ticks: int,
        tick_scaled: int,
        refresh_every: int = _QUOTE_REFRESH_EVERY,
    ) -> None:
        self._sdk = sdk
        self._symbol = symbol
        self._offset_ticks = offset_ticks
        self._tick_scaled = tick_scaled
        self._refresh_every = max(1, refresh_every)
        self._calls = 0
        self._reference: int | None = None

    def _fetch_reference(self) -> int | None:
        sdk = self._sdk
        if getattr(sdk, "marketdata", None) is None:
            try:
                sdk.init_realtime()
            except Exception as exc:
                log.warning("fubon_rtt_probe_realtime_init_failed", error_type=type(exc).__name__)
                return None
        snapshots = FubonSnapshotFetcher(sdk).fetch_snapshots([self._symbol])
        if not snapshots:
            return None
        return _points_to_scaled(snapshots[0].get("close"))

    def __call__(self, side: str) -> int | None:
        if self._reference is None or self._calls % self._refresh_every == 0:
            fresh = self._fetch_reference()
            if fresh is not None:
                self._reference = fresh
            elif self._reference is not None:
                log.warning("fubon_rtt_probe_quote_refresh_failed_keeping_previous", symbol=self._symbol)
        self._calls += 1
        if self._reference is None:
            return None
        return price_for_side(side, self._reference, self._offset_ticks, self._tick_scaled)


# ---------------------------------------------------------------------------
# Execution callbacks (run on SDK threads)
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class ExecTracker:
    """Records when the order callback for each order number arrived, and whether anything filled.

    The platform's callback adapter calls `on_order(state, payload)` and `on_deal(payload)` on SDK
    threads. A callback may arrive before `place_order` has returned, so arrivals are kept by
    order number and looked up afterwards. The map is bounded (oldest evicted first).
    """

    __slots__ = ("_cond", "_acks", "_capacity", "_fill_seen")

    def __init__(self, capacity: int = _ACK_CAPACITY) -> None:
        self._cond = threading.Condition()
        self._acks: OrderedDict[str, int] = OrderedDict()
        self._capacity = capacity
        self._fill_seen = False

    @property
    def fill_seen(self) -> bool:
        return self._fill_seen

    def on_order(self, state: Any, payload: Any) -> None:
        arrived_ns = time.perf_counter_ns()
        if not isinstance(payload, dict):
            return
        block = payload.get("order")
        filled = _as_int(block.get("filled_qty")) if isinstance(block, dict) else 0
        with self._cond:
            if filled > 0:
                self._fill_seen = True
            if state == "order":
                for key in (payload.get("order_id"), payload.get("seq_no")):
                    text = "" if key is None else str(key)
                    if text and text not in self._acks:
                        self._acks[text] = arrived_ns
                        if len(self._acks) > self._capacity:
                            self._acks.popitem(last=False)
            self._cond.notify_all()

    def on_deal(self, payload: Any) -> None:
        with self._cond:
            self._fill_seen = True
            self._cond.notify_all()

    def _first_ack(self, keys: tuple[str, ...]) -> int | None:
        hits = [self._acks[key] for key in keys if key in self._acks]
        return min(hits) if hits else None

    def wait_ack(self, keys: tuple[str, ...], timeout_s: float) -> int | None:
        """perf_counter_ns arrival of the order callback for any of *keys*; None on timeout.

        Returns early (possibly None) when a fill is reported, so the caller can abort promptly.
        """
        if not keys:
            return None
        with self._cond:
            self._cond.wait_for(lambda: self._first_ack(keys) is not None or self._fill_seen, timeout=timeout_s)
            return self._first_ack(keys)


# ---------------------------------------------------------------------------
# Probe core
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    symbol: str
    side: str
    offset_ticks: int
    tick_scaled: int
    explicit_price: int | None
    market_type: str
    iters: int
    warmup: int
    sleep_s: float
    ack_timeout_s: float
    qty: int = _QTY


@dataclass(slots=True)
class _Cycle:
    place_us: int | None = None
    ack_us: int | None = None
    cancel_us: int | None = None
    filled: bool = False


def _order_keys(order: Any) -> tuple[str, ...]:
    keys = []
    for name in ("order_no", "seq_no"):
        value = getattr(order, name, None)
        if value is not None and str(value).strip():
            keys.append(str(value))
    return tuple(keys)


def _order_label(order: Any) -> str:
    keys = _order_keys(order)
    return keys[0] if keys else "unknown"


class RttProbe:
    """Runs warm-up + measured place/ack/cancel cycles and always sweeps what it left behind."""

    def __init__(
        self,
        facade: Any,
        config: ProbeConfig,
        price_source: Callable[[str], int | None],
        tracker: ExecTracker,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._facade = facade
        self._config = config
        self._price_source = price_source
        self._tracker = tracker
        self._sleep = sleep
        self._open: list[Any] = []
        self._place_error_streak = 0
        self.place_samples: list[int] = []
        self.submitted_ack_samples: list[int] = []
        self.cancel_samples: list[int] = []
        self.place_errors = 0
        self.submitted_ack_errors = 0
        self.cancel_errors = 0
        self.iters_recorded = 0
        self.fills_aborted = 0
        self.abort_reason: str | None = None
        self.leftovers_cancelled = 0
        self.leftovers_filled = 0
        self.leftovers_failed = 0
        self.leftover_failed_ids: list[str] = []

    @property
    def open_orders(self) -> tuple[Any, ...]:
        return tuple(self._open)

    def run(self) -> None:
        """Warm up, measure, and (in a finally) cancel every order this probe still has open."""
        cfg = self._config
        sides = ("Buy", "Sell") if cfg.side == "alternate" else (cfg.side,)
        try:
            for i in range(cfg.warmup):
                if not self._step(sides[i % len(sides)], record=False):
                    return
                self._sleep(cfg.sleep_s)
            for i in range(cfg.iters):
                if not self._step(sides[i % len(sides)], record=True):
                    return
                self._sleep(cfg.sleep_s)
        finally:
            self.sweep_leftovers()

    def _step(self, side: str, *, record: bool) -> bool:
        """One cycle plus the breaker checks; False means stop the probe."""
        cycle = self._cycle(side)
        if cycle.filled:
            self.fills_aborted += 1
            self.abort_reason = "fill"
            log.error("fubon_rtt_probe_fill_abort", side=side)
            return False
        self._place_error_streak = self._place_error_streak + 1 if cycle.place_us is None else 0
        if record:
            self._record(cycle)
        if self._place_error_streak >= _MAX_CONSECUTIVE_PLACE_ERRORS:
            self.abort_reason = "consecutive_place_errors"
            log.error("fubon_rtt_probe_failure_streak_abort", streak=self._place_error_streak)
            return False
        return True

    def _record(self, cycle: _Cycle) -> None:
        self.iters_recorded += 1
        if cycle.place_us is None:
            self.place_errors += 1
        else:
            self.place_samples.append(cycle.place_us)
        if cycle.ack_us is None:
            self.submitted_ack_errors += 1
        else:
            self.submitted_ack_samples.append(cycle.ack_us)
        if cycle.cancel_us is None:
            self.cancel_errors += 1
        else:
            self.cancel_samples.append(cycle.cancel_us)

    def _forget(self, order: Any) -> None:
        for idx, candidate in enumerate(self._open):
            if candidate is order:
                del self._open[idx]
                return

    def _cycle(self, side: str) -> _Cycle:
        cfg = self._config
        price = self._price_source(side)
        if price is None:
            return _Cycle()
        t0 = time.perf_counter_ns()
        try:
            order = self._facade.place_order(
                symbol=cfg.symbol,
                price=price,
                qty=cfg.qty,
                side=side,
                tif="ROD",
                price_type="LMT",
                order_type="Auto",
                market_type=cfg.market_type,
                user_def=_USER_DEF,
            )
        except Exception as exc:
            log.warning("fubon_rtt_probe_place_failed", error_type=type(exc).__name__, error=str(exc)[:200])
            return _Cycle()
        place_us = (time.perf_counter_ns() - t0) // 1000
        self._open.append(order)
        if self._tracker.fill_seen or _as_int(getattr(order, "filled_lot", None)) > 0:
            return _Cycle(place_us=place_us, filled=True)

        ack_ns = self._tracker.wait_ack(_order_keys(order), cfg.ack_timeout_s)
        ack_us = None if ack_ns is None else max(0, (ack_ns - t0) // 1000)
        if self._tracker.fill_seen:
            return _Cycle(place_us=place_us, ack_us=ack_us, filled=True)
        return self._cancel(order, place_us, ack_us)

    def _cancel(self, order: Any, place_us: int, ack_us: int | None) -> _Cycle:
        t1 = time.perf_counter_ns()
        try:
            outcome = self._facade.cancel_order(order)
        except Exception as exc:
            log.warning(
                "fubon_rtt_probe_cancel_failed",
                order=_order_label(order),
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            return _Cycle(place_us=place_us, ack_us=ack_us)
        cancel_us = (time.perf_counter_ns() - t1) // 1000
        self._forget(order)
        if isinstance(outcome, FubonCancelNoOp):
            return _Cycle(place_us=place_us, ack_us=ack_us, filled=True)
        return _Cycle(place_us=place_us, ack_us=ack_us, cancel_us=cancel_us)

    def sweep_leftovers(self) -> None:
        """Cancel every order this probe placed and has not seen cancelled (one pass, never raises)."""
        remaining: list[Any] = []
        for order in list(self._open):
            label = _order_label(order)
            try:
                outcome = self._facade.cancel_order(order)
            except Exception as exc:
                self.leftovers_failed += 1
                self.leftover_failed_ids.append(label)
                remaining.append(order)
                log.error(
                    "fubon_rtt_probe_leftover_cancel_failed",
                    order=label,
                    error_type=type(exc).__name__,
                    error=str(exc)[:200],
                )
                continue
            if isinstance(outcome, FubonCancelNoOp):
                self.leftovers_filled += 1
                if self.abort_reason is None:
                    self.abort_reason = "fill"
                    self.fills_aborted += 1
                log.error("fubon_rtt_probe_leftover_already_filled", order=label)
            else:
                self.leftovers_cancelled += 1
        self._open = remaining
        if remaining:
            log.error("fubon_rtt_probe_orders_left_open", orders=list(self.leftover_failed_ids))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _utc_iso(epoch_ns: int) -> str:
    seconds, nanos = divmod(epoch_ns, 1_000_000_000)
    stamp = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=nanos // 1000)
    return stamp.isoformat().replace("+00:00", "Z")


def build_report(
    config: ProbeConfig,
    probe: RttProbe,
    *,
    mode: str,
    elapsed_s: float,
    interrupted: bool,
    epoch_ns: int,
) -> dict[str, Any]:
    place_stats = _summarize("place_order", probe.place_samples, probe.place_errors)
    ack_stats = _summarize("submitted_ack", probe.submitted_ack_samples, probe.submitted_ack_errors)
    cancel_stats = _summarize("cancel_order", probe.cancel_samples, probe.cancel_errors)
    return {
        "meta": {
            "mode": mode,
            "synthetic": mode == "dry-run",
            "broker": "fubon",
            "sdk_host": _TEST_HOST,
            "symbol": config.symbol,
            "contract_code": config.symbol,
            "market_type": config.market_type,
            "qty": config.qty,
            "side": config.side,
            "offset_ticks": config.offset_ticks,
            "tick_scaled": config.tick_scaled,
            "explicit_price_scaled": config.explicit_price,
            "iters_requested": config.iters,
            "iters_recorded": probe.iters_recorded,
            "warmup": config.warmup,
            "sleep_between_cycles_s": config.sleep_s,
            "ack_timeout_s": config.ack_timeout_s,
            "submitted_ack_definition": _SUBMITTED_ACK_DEFINITION,
            "elapsed_s": round(elapsed_s, 2),
            "fills_aborted": probe.fills_aborted,
            "abort_reason": probe.abort_reason,
            "interrupted": interrupted,
            "leftovers_cancelled": probe.leftovers_cancelled,
            "leftovers_filled": probe.leftovers_filled,
            "leftovers_failed": probe.leftovers_failed,
            "ts_utc": _utc_iso(epoch_ns),
        },
        "place_order": asdict(place_stats),
        "submitted_ack": asdict(ack_stats),
        "cancel_order": asdict(cancel_stats),
        "raw": {
            "place_samples_us": probe.place_samples,
            "submitted_ack_samples_us": probe.submitted_ack_samples,
            "cancel_samples_us": probe.cancel_samples,
        },
    }


def _log_summary(report: dict[str, Any], out_path: Path) -> None:
    meta = report["meta"]
    log.info(
        "fubon_rtt_probe_done",
        mode=meta["mode"],
        symbol=meta["symbol"],
        elapsed_s=meta["elapsed_s"],
        abort_reason=meta["abort_reason"],
        leftovers_failed=meta["leftovers_failed"],
        report=str(out_path),
    )
    for key in ("place_order", "submitted_ack", "cancel_order"):
        stats = report[key]
        log.info(
            "fubon_rtt_probe_op",
            op=key,
            n=stats["count"],
            errors=stats["errors"],
            p50_ms=round(stats["p50_us"] / 1000, 1),
            p95_ms=round(stats["p95_us"] / 1000, 1),
            p99_ms=round(stats["p99_us"] / 1000, 1),
            max_ms=round(stats["max_us"] / 1000, 1),
            p95_ci95_ms=[round(v / 1000, 1) for v in stats["p95_ci95_us"]],
        )


# ---------------------------------------------------------------------------
# Dry run: mocked SDK (tests/unit/fubon_mock_helper.py), synthetic latencies, no network
# ---------------------------------------------------------------------------

_DRY_RUN_URL = f"wss://{_TEST_HOST}/dry-run"
_DRY_PRODUCT = "DRYPROD"
_DRY_EXPIRY = "209912"
_DRY_REFERENCE_POINTS = 22000.0


def _load_mock_helper() -> Any:
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        return importlib.import_module("tests.unit.fubon_mock_helper")
    except ImportError as exc:
        raise ProbeRefusedError(f"--dry-run needs tests/unit/fubon_mock_helper.py: {exc}") from exc


class _SimulatedBroker:
    """The broker side of the mocked SDK: deterministic synthetic delays, never touches a network."""

    def __init__(self, helper: Any, seed: int = 7) -> None:
        self._helper = helper
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._seq = 0
        self._on_order: Callable[..., Any] | None = None
        self._on_changed: Callable[..., Any] | None = None

    def set_on_order(self, callback: Callable[..., Any]) -> None:
        self._on_order = callback

    def set_on_changed(self, callback: Callable[..., Any]) -> None:
        self._on_changed = callback

    def _delay_s(self, low_ms: float, high_ms: float) -> float:
        with self._lock:
            return self._rng.uniform(low_ms, high_ms) / 1000

    @staticmethod
    def _deliver(callback: Callable[..., Any] | None, report: Any, delay_s: float) -> None:
        if callback is None:
            return
        timer = threading.Timer(delay_s, callback, args=(0, report))
        timer.daemon = True
        timer.start()

    def _report(self, order: Any, order_no: str, seq_no: str) -> SimpleNamespace:
        return SimpleNamespace(
            order_no=order_no,
            seq_no=seq_no,
            symbol=_DRY_PRODUCT,
            expiry_date=_DRY_EXPIRY,
            buy_sell=getattr(order, "buy_sell", "Buy"),
            price=getattr(order, "price", None),
            lot=getattr(order, "lot", None),
            filled_lot=0,
            status=10,
            function_type=0,
            user_def=getattr(order, "user_def", None),
        )

    def place(self, account: Any, py_order: Any, unblock: Any = None) -> Any:
        with self._lock:
            self._seq += 1
            seq = self._seq
        order_no = f"DRY{seq:05d}"
        seq_no = f"{seq:011d}"
        place_delay = self._delay_s(2.0, 6.0)
        ack_delay = place_delay + self._delay_s(-1.5, 2.0)
        self._deliver(self._on_order, self._report(py_order, order_no, seq_no), max(0.0, ack_delay))
        time.sleep(place_delay)
        return self._helper.ok(self._helper.make_order_result(order_no=order_no, seq_no=seq_no, filled_lot=0))

    def cancel(self, account: Any, order_res: Any, unblock: Any = None) -> Any:
        order_no = str(getattr(order_res, "order_no", ""))
        seq_no = str(getattr(order_res, "seq_no", ""))
        cancel_delay = self._delay_s(2.0, 5.0)
        self._deliver(self._on_changed, self._report(None, order_no, seq_no), cancel_delay)
        time.sleep(cancel_delay)
        return self._helper.ok(self._helper.make_order_result(order_no=order_no, seq_no=seq_no, filled_lot=0))


class _DryRunSdkFactory:
    """Stand-in for the `FubonSDK` class: autospec'd SDK whose order API is the simulated broker."""

    def __init__(self, helper: Any, symbol: str) -> None:
        self._helper = helper
        self._symbol = symbol
        self.broker = _SimulatedBroker(helper)
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        helper = self._helper
        inspect.signature(helper.FubonSDKSpec).bind(*args, **kwargs)
        self.calls.append((args, kwargs))
        sdk = helper.make_fubon_sdk_mock()
        login_result = helper.ok([helper.make_account()])
        sdk.login.return_value = login_result
        sdk.apikey_login.return_value = login_result
        sdk.set_on_futopt_order.side_effect = self.broker.set_on_order
        sdk.set_on_order_futopt_changed.side_effect = self.broker.set_on_changed
        sdk.futopt.place_order.side_effect = self.broker.place
        sdk.futopt.cancel_order.side_effect = self.broker.cancel
        sdk.futopt.convert_symbol.side_effect = helper.make_convert_symbol(
            extra={(_DRY_PRODUCT, _DRY_EXPIRY): self._symbol}
        )
        marketdata = MagicMock()
        marketdata.rest_client.futopt.intraday.quote.return_value = {
            "lastTrade": {"price": _DRY_REFERENCE_POINTS, "size": 1},
        }
        sdk.marketdata = marketdata
        return sdk


@contextlib.contextmanager
def _dry_run_patches(symbol: str) -> Iterator[_DryRunSdkFactory]:
    """Swap the SDK class for the mock and the credentials for dummies; restore both on exit."""
    helper = _load_mock_helper()
    helper.install_fubon_neo_mock()
    factory = _DryRunSdkFactory(helper, symbol)
    dummies = {
        ENV_TEST_URL: _DRY_RUN_URL,
        ENV_PERSONAL_ID: "DRYRUN",
        ENV_PASSWORD: "dry-run",
        ENV_CERT_PATH: "dry-run.pfx",
    }
    cleared = (ENV_ORDER_MODE, ENV_API_KEY, ENV_CERT_PASS, ENV_ACCOUNT)
    saved = {name: os.environ.get(name) for name in (*dummies, *cleared)}
    saved_sdk = facade_module.FubonSDK
    try:
        for name in cleared:
            os.environ.pop(name, None)
        os.environ.update(dummies)
        facade_module.FubonSDK = factory
        yield factory
    finally:
        facade_module.FubonSDK = saved_sdk
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe bulk Fubon TEST-environment order-API RTT probe.")
    parser.add_argument("--dry-run", action="store_true", help="mocked SDK, synthetic latencies, never connects")
    parser.add_argument("--iters", type=int, default=50, help="measured iterations")
    parser.add_argument("--warmup", type=int, default=5, help="warmup iterations discarded")
    parser.add_argument("--sleep", type=float, default=0.3, help="sleep (s) between cycles - rate-limit guard")
    parser.add_argument(
        "--symbol", default=None, help="trading symbol as listed (e.g. TXFL6); required unless --dry-run"
    )
    parser.add_argument("--offset-ticks", type=int, default=300, help="far-from-market offset from the reference")
    parser.add_argument("--tick-scaled", type=int, default=_DEFAULT_TICK_SCALED, help="tick size, scaled x10000")
    parser.add_argument("--price", type=int, default=None, help="explicit LIMIT price, scaled int x10000 (one side)")
    parser.add_argument("--side", choices=["Buy", "Sell", "alternate"], default="alternate")
    parser.add_argument("--market-type", choices=["Future", "FutureNight"], default="Future", help="session")
    parser.add_argument("--ack-timeout", type=float, default=2.0, help="seconds to wait for the order callback")
    parser.add_argument("--out", required=True, help="output JSON path")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> ProbeConfig:
    symbol = args.symbol or (_DEFAULT_DRY_RUN_SYMBOL if args.dry_run else None)
    if not symbol:
        raise ProbeRefusedError("--symbol is required (the front-month code rolls; no default is guessed)")
    if args.iters < 1 or args.warmup < 0 or args.sleep < 0 or args.ack_timeout <= 0:
        raise ProbeRefusedError("--iters >= 1, --warmup >= 0, --sleep >= 0 and --ack-timeout > 0 are required")
    if args.offset_ticks < 1 or args.tick_scaled < 1:
        raise ProbeRefusedError("--offset-ticks and --tick-scaled must be >= 1 (offset 0 would price at the market)")
    if args.price is not None:
        if args.price <= 0:
            raise ProbeRefusedError("--price must be a positive scaled int (x10000)")
        if args.side == "alternate":
            raise ProbeRefusedError("--price needs --side Buy or Sell: one price cannot be far from market for both")
    return ProbeConfig(
        symbol=symbol,
        side=args.side,
        offset_ticks=args.offset_ticks,
        tick_scaled=args.tick_scaled,
        explicit_price=args.price,
        market_type=args.market_type,
        iters=args.iters,
        warmup=args.warmup,
        sleep_s=args.sleep,
        ack_timeout_s=args.ack_timeout,
    )


def _connect(facade: Any, tracker: ExecTracker) -> None:
    """Log in and wire the execution callbacks; no order may be placed on a session blind to fills."""
    if not facade.login():
        raise ProbeRefusedError("Fubon login failed (reason is in the fubon_login_failed log line)")
    try:
        facade.set_execution_callbacks(tracker.on_order, tracker.on_deal)
    except FubonCallbackRegistrationError as exc:
        raise ProbeRefusedError(f"cannot see order/fill callbacks: {exc}") from exc


def _make_price_source(facade: Any, config: ProbeConfig) -> Callable[[str], int | None]:
    if config.explicit_price is not None:
        return FixedPriceSource(config.explicit_price)
    sdk = getattr(facade, "_sdk", None)  # the facade has no public quote/snapshot accessor
    if sdk is None:
        raise ProbeRefusedError("no SDK on the facade; cannot fetch a reference quote (pass --price)")
    return QuotePriceSource(sdk, config.symbol, config.offset_ticks, config.tick_scaled)


def _exit_code(probe: RttProbe) -> int:
    """Leftover orders outrank an abort: an order still open on the broker needs a human first."""
    if probe.leftovers_failed:
        return _EXIT_LEFTOVER
    if probe.abort_reason is not None:
        return _EXIT_ABORTED
    return _EXIT_OK


def _run_probe(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    out_path = Path(args.out)
    with contextlib.ExitStack() as stack:
        if args.dry_run:
            stack.enter_context(_dry_run_patches(config.symbol))
        validate_probe_target()
        if facade_module.FubonSDK is None:
            raise ProbeRefusedError("fubon_neo is not installed (use --dry-run to exercise the probe offline)")
        facade = FubonClientFacade(symbols_path=None, broker_config=_BROKER_CONFIG)
        tracker = ExecTracker()
        interrupted = False
        try:
            _connect(facade, tracker)
            probe = RttProbe(facade, config, _make_price_source(facade, config), tracker)
            started_ns = time.perf_counter_ns()
            try:
                probe.run()
            except KeyboardInterrupt:
                interrupted = True
                log.warning("fubon_rtt_probe_interrupted")
            elapsed_s = (time.perf_counter_ns() - started_ns) / 1e9
        finally:
            facade.close(logout=True)

    report = build_report(
        config,
        probe,
        mode="dry-run" if args.dry_run else "test",
        elapsed_s=elapsed_s,
        interrupted=interrupted,
        epoch_ns=timebase.now_ns(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))
    _log_summary(report, out_path)

    return _exit_code(probe)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return _run_probe(args)
    except ProbeRefusedError as exc:
        log.error("fubon_rtt_probe_refused", reason=str(exc))
        return _EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
