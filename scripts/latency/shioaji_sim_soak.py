"""Credentialed SIM soak for the shioaji 1.5.3 upgrade (PR #367 runtime evidence).

Closes the only residuals that static analysis and offline tests cannot:

  * **Latency P50/P95/P99** for place / update / cancel against the real 1.5.3
    Rust transport (Gate D input). Reuses the safe far-from-market probe core
    from ``shioaji_rtt_bulk_probe`` and ADDS a measured ``update_order`` leg the
    bulk probe lacks.
  * **Real-payload Decimal parity** — every live tick/bidask callback payload is
    piped through the production ``MarketDataNormalizer`` and asserted to land as
    a positive scaled int, with the Rust-fallback rate recorded. This closes the
    synthetic gap left by the offline boundary test using REAL 1.5.3 objects.
  * **Reconnect timing** — ``set_event_callback`` is registered and event codes
    12 (Reconnecting) / 13 (Reconnected) plus the first post-reconnect tick are
    timestamped if a reconnect occurs during the window (observational; a sim
    transport disconnect cannot be forced from the client side).
  * **Resource growth** — RSS / threads / fds sampled from ``/proc`` over the
    window; linear-fit slope + max flag a leak.

SAFETY (fail-closed, non-negotiable):
  * ``simulation=True`` ONLY. Refuses to start if the session does not resolve
    to simulation, or if ANY ``SHIOAJI_CA_*`` (live-cert) env var is present.
  * Credentials come from ``SHIOAJI_API_KEY`` / ``SHIOAJI_SECRET_KEY`` env ONLY,
    never CLI args, never logged. The JSON report contains no credentials.
  * Far-from-market limit prices + immediate cancel + fill circuit-breaker, all
    inherited from the probe core. Aborts on any unexpected fill.

This script imports ``shioaji`` and is meant to run in the isolated 1.5.3
harness venv (see scripts/shioaji_153_harness/). It is NOT imported by any
hot-path code.

Invocation (operator supplies creds via env; never echoed)::

    export SHIOAJI_API_KEY=...  SHIOAJI_SECRET_KEY=...   # simulation creds
    PYTHONPATH=src python scripts/latency/shioaji_sim_soak.py \\
        --minutes 30 --out outputs/shioaji_153_sim_soak_20260617_1200.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"numpy not available: {exc}") from exc

try:
    import shioaji as sj
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"shioaji not available: {exc}") from exc

# Reuse the audited probe core verbatim — do not reimplement order safety.
_PROBE_DIR = Path(__file__).resolve().parent
if str(_PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(_PROBE_DIR))
from shioaji_rtt_bulk_probe import (  # noqa: E402
    OpStats,
    _build_futures_order,
    _ordno_ready,
    _snapshot_bidask,
    _summarize,
    _trade_filled,
    _wait_for_ordno,
)

_REPO_ROOT = _PROBE_DIR.parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from hft_platform.events import BidAskEvent, TickEvent  # noqa: E402
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer  # noqa: E402


# --------------------------------------------------------------------------- #
# Safety: refuse anything that isn't a credentialed simulation session.
# --------------------------------------------------------------------------- #
def _assert_sim_only() -> None:
    live_ca = sorted(k for k in os.environ if k.startswith("SHIOAJI_CA"))
    if live_ca:
        raise SystemExit(
            f"REFUSING: live CA env vars present ({live_ca}); this soak is "
            "simulation-only. Unset them and rerun."
        )
    if not os.getenv("SHIOAJI_API_KEY") or not os.getenv("SHIOAJI_SECRET_KEY"):
        raise SystemExit("Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY (simulation creds) in env.")


# --------------------------------------------------------------------------- #
# Real-payload Decimal parity tracker.
# --------------------------------------------------------------------------- #
class _ParityTracker:
    """Pipes real 1.5.3 callback payloads through the normalizer and records
    whether Decimal prices land as positive scaled ints, plus fallback / error
    counts. Thread-safe (callbacks fire from the SDK's quote thread)."""

    def __init__(self, normalizer: MarketDataNormalizer) -> None:
        self._n = normalizer
        self._lock = threading.Lock()
        self.ticks = 0
        self.bidasks = 0
        self.decimal_priced = 0
        self.scaled_ok = 0
        self.dropped = 0
        self.exceptions = 0
        self.first_tick_perf_ns = 0
        self.sample_schema: dict[str, Any] = {}

    @staticmethod
    def _attr(obj: Any, name: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def on_tick(self, exchange: Any, tick: Any) -> None:
        try:
            close = self._attr(tick, "close")
            with self._lock:
                self.ticks += 1
                if self.first_tick_perf_ns == 0:
                    self.first_tick_perf_ns = time.perf_counter_ns()
                if isinstance(close, Decimal):
                    self.decimal_priced += 1
                if not self.sample_schema:
                    self.sample_schema = {
                        "close_type": type(close).__name__,
                        "code": str(self._attr(tick, "code")),
                    }
            payload = {
                "code": self._attr(tick, "code"),
                "close": close,
                "volume": self._attr(tick, "volume") or 0,
                "total_volume": self._attr(tick, "total_volume") or 0,
                "ts": time.perf_counter_ns(),
                "simtrade": self._attr(tick, "simtrade") or 0,
                "intraday_odd": 0,
            }
            ev = self._n.normalize_tick(payload)
            with self._lock:
                if isinstance(ev, TickEvent) and isinstance(ev.price, int) and ev.price > 0:
                    self.scaled_ok += 1
                else:
                    self.dropped += 1
        except Exception:  # noqa: BLE001 — a raising callback would tear down the feed.
            with self._lock:
                self.exceptions += 1

    def on_bidask(self, exchange: Any, bidask: Any) -> None:
        try:
            bid_price = self._attr(bidask, "bid_price") or []
            with self._lock:
                self.bidasks += 1
                if bid_price and isinstance(bid_price[0], Decimal):
                    self.decimal_priced += 1
            payload = {
                "code": self._attr(bidask, "code"),
                "ts": time.perf_counter_ns(),
                "bid_price": bid_price,
                "bid_volume": self._attr(bidask, "bid_volume") or [],
                "ask_price": self._attr(bidask, "ask_price") or [],
                "ask_volume": self._attr(bidask, "ask_volume") or [],
            }
            ev = self._n.normalize_bidask(payload)
            with self._lock:
                if isinstance(ev, BidAskEvent):
                    ok = all(int(p) > 0 for p, _ in list(ev.bids) + list(ev.asks)) if (
                        len(ev.bids) or len(ev.asks)
                    ) else True
                    self.scaled_ok += 1 if ok else 0
                    self.dropped += 0 if ok else 1
                else:
                    self.dropped += 1
        except Exception:  # noqa: BLE001
            with self._lock:
                self.exceptions += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = self.ticks + self.bidasks
            return {
                "ticks": self.ticks,
                "bidasks": self.bidasks,
                "events_total": events,
                "decimal_priced": self.decimal_priced,
                "scaled_ok": self.scaled_ok,
                "dropped": self.dropped,
                "exceptions": self.exceptions,
                "rust_fallback_tick": _counter(self._n._rust_fallback_tick),
                "rust_fallback_bidask": _counter(self._n._rust_fallback_bidask),
                "parity_holds": self.exceptions == 0 and self.dropped == 0 and events > 0,
                "sample_schema": self.sample_schema,
            }


def _counter(child: Any) -> float:
    try:
        return float(child._value.get())  # type: ignore[attr-defined]
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# /proc-based resource sampler (dependency-free on Linux/WSL).
# --------------------------------------------------------------------------- #
class _ResourceSampler(threading.Thread):
    def __init__(self, interval_s: float) -> None:
        super().__init__(daemon=True)
        self._interval = interval_s
        # NB: must NOT be named ``_stop`` — that shadows ``threading.Thread._stop``
        # (a CPython-internal method ``join()`` invokes), which makes ``join()``
        # raise ``TypeError: 'Event' object is not callable``.
        self._stop_evt = threading.Event()
        self.t_s: list[float] = []
        self.rss_kb: list[int] = []
        self.threads: list[int] = []
        self.fds: list[int] = []

    @staticmethod
    def _rss_kb() -> int:
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except Exception:
            pass
        return 0

    @staticmethod
    def _count(path: str) -> int:
        try:
            return len(os.listdir(path))
        except Exception:
            return 0

    def run(self) -> None:
        t0 = time.perf_counter()
        while not self._stop_evt.is_set():
            self.t_s.append(time.perf_counter() - t0)
            self.rss_kb.append(self._rss_kb())
            self.threads.append(self._count("/proc/self/task"))
            self.fds.append(self._count("/proc/self/fd"))
            self._stop_evt.wait(self._interval)

    def stop(self) -> None:
        self._stop_evt.set()

    def summary(self) -> dict[str, Any]:
        def slope(ys: list[int]) -> float:
            if len(ys) < 2:
                return 0.0
            return float(np.polyfit(np.asarray(self.t_s), np.asarray(ys, dtype=np.float64), 1)[0])

        return {
            "samples": len(self.t_s),
            "rss_kb_first": self.rss_kb[0] if self.rss_kb else 0,
            "rss_kb_last": self.rss_kb[-1] if self.rss_kb else 0,
            "rss_kb_max": max(self.rss_kb) if self.rss_kb else 0,
            "rss_kb_per_s_slope": slope(self.rss_kb),
            "threads_max": max(self.threads) if self.threads else 0,
            "threads_per_s_slope": slope(self.threads),
            "fds_max": max(self.fds) if self.fds else 0,
            "fds_per_s_slope": slope(self.fds),
        }


# --------------------------------------------------------------------------- #
# Latency: place -> wait-ordno -> update(price) -> cancel, all measured.
# --------------------------------------------------------------------------- #
def _resolve_probe_price(
    api: Any, contract: Any, side: str, offset_ticks: int, price_mode: str
) -> float | int | None:
    """A valid, far-from-market limit price for the probe.

    ``snapshot`` anchors on the live bid/ask (needs an OPEN market).
    ``fixed`` derives the price from the contract's daily limit band, so it works
    while the market is CLOSED (休市): no live quote is needed and a fill is
    impossible — the order rests 2 ticks inside the floor (Buy) / ceiling (Sell).
    This lets place/update/cancel latency be measured without contending for the
    live market-data feed (which a shared person-ID would).
    """
    if price_mode == "fixed":
        lo = getattr(contract, "limit_down", None)
        hi = getattr(contract, "limit_up", None)
        ref = getattr(contract, "reference", None)
        try:
            if side == "Buy":
                floor = float(lo) if lo else (float(ref) * 0.9 if ref else None)
                return None if floor is None else int(round(floor)) + 2
            ceil = float(hi) if hi else (float(ref) * 1.1 if ref else None)
            return None if ceil is None else int(round(ceil)) - 2
        except Exception:  # noqa: BLE001
            return None
    bidask = _snapshot_bidask(api, contract)
    if bidask is None:
        return None
    bid, ask = bidask
    return max(1, bid - offset_ticks) if side == "Buy" else ask + offset_ticks


def _probe_place_update_cancel(
    api: Any, contract: Any, sj_: Any, qty: int, side: str, offset_ticks: int,
    price_mode: str = "snapshot",
) -> tuple[int | None, int | None, int | None, int | None, Any]:
    """Returns (place_us, submitted_ack_us, modify_us, cancel_us, trade)."""
    price = _resolve_probe_price(api, contract, side, offset_ticks, price_mode)
    if price is None:
        return (None, None, None, None, None)
    order = _build_futures_order(sj_, api, side, price, qty)

    t0 = time.perf_counter_ns()
    try:
        trade = api.place_order(contract, order)
        place_us = (time.perf_counter_ns() - t0) // 1000
    except Exception:
        return (None, None, None, None, None)
    if _trade_filled(trade):
        return (place_us, None, None, None, trade)

    submitted_ack_us = _wait_for_ordno(api, trade, timeout_s=2.0)
    if not _ordno_ready(trade):
        return (place_us, submitted_ack_us, None, None, trade)

    # Modify: nudge one tick to measure the update_order ack the bulk probe never
    # captured. In ``fixed`` mode the limit rests at the band edge, so nudge it
    # TOWARD mid (still far from any fill) to stay inside the daily price band.
    if price_mode == "fixed":
        new_price = price + 1 if side == "Buy" else max(1, price - 1)
    else:
        new_price = max(1, price - 1) if side == "Buy" else price + 1
    modify_us: int | None = None
    t1 = time.perf_counter_ns()
    try:
        api.update_order(trade=trade, price=new_price)
        modify_us = (time.perf_counter_ns() - t1) // 1000
    except Exception:
        modify_us = None

    cancel_us: int | None = None
    t2 = time.perf_counter_ns()
    try:
        api.cancel_order(trade)
        cancel_us = (time.perf_counter_ns() - t2) // 1000
    except Exception:
        cancel_us = None
    return (place_us, submitted_ack_us, modify_us, cancel_us, trade)


def _build_normalizer(codes: list[tuple[str, str]]) -> tuple[MarketDataNormalizer, str]:
    """Temp symbols.yaml for the soak basket, all scaled x10000."""
    import tempfile

    lines = ["symbols:"]
    for code, exch in codes:
        lines += [f"  - code: '{code}'", f"    exchange: '{exch}'", "    price_scale: 10000"]
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    tmp.write("\n".join(lines) + "\n")
    tmp.flush()
    tmp.close()
    return MarketDataNormalizer(tmp.name), tmp.name


def _stock_contract(api: Any, code: str) -> Any:
    try:
        return api.Contracts.Stocks[code]
    except Exception:
        return None


def _futures_contract(api: Any, symbol: str) -> Any:
    try:
        return api.Contracts.Futures[symbol]
    except Exception:
        pass
    for fb in ("TXFR1", "TXFR2", "MXFR1"):
        try:
            c = getattr(api.Contracts.Futures.TXF, fb, None)
            if c is not None:
                return c
        except Exception:
            continue
    return None


def _run_order_loop(api: Any, future: Any, args: Any) -> dict[str, Any]:
    """Fire place->update->cancel cycles until the deadline; collect latencies.

    Aborts immediately on any unexpected fill (the far-from-market price makes a
    fill impossible, so a fill means something is wrong)."""
    place_s: list[int] = []
    ack_s: list[int] = []
    modify_s: list[int] = []
    cancel_s: list[int] = []
    place_err = ack_err = modify_err = cancel_err = fills_aborted = 0
    deadline = time.perf_counter() + args.minutes * 60.0
    sides = ["Buy", "Sell"]
    i = 0
    max_cycles = max(0, int(getattr(args, "max_cycles", 0) or 0))
    t_start = time.perf_counter()
    while time.perf_counter() < deadline:
        if max_cycles and i >= max_cycles:
            break
        side = sides[i % 2]
        p, a, m, c, trade = _probe_place_update_cancel(
            api, future, sj, args.qty, side, args.offset_ticks, args.price_mode
        )
        if _trade_filled(trade):
            fills_aborted += 1
            print(f"!! ABORT iter {i}: unexpected fill — stopping order loop.", file=sys.stderr)
            break
        for val, samples, err_name in (
            (p, place_s, "place_err"),
            (a, ack_s, "ack_err"),
            (m, modify_s, "modify_err"),
            (c, cancel_s, "cancel_err"),
        ):
            if val is None:
                if err_name == "place_err":
                    place_err += 1
                elif err_name == "ack_err":
                    ack_err += 1
                elif err_name == "modify_err":
                    modify_err += 1
                else:
                    cancel_err += 1
            else:
                samples.append(val)
        i += 1
        time.sleep(args.sleep)
    return {
        "place_s": place_s, "ack_s": ack_s, "modify_s": modify_s, "cancel_s": cancel_s,
        "place_err": place_err, "ack_err": ack_err, "modify_err": modify_err, "cancel_err": cancel_err,
        "fills_aborted": fills_aborted, "cycles": i, "elapsed_s": time.perf_counter() - t_start,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Credentialed shioaji 1.5.3 SIM soak.")
    ap.add_argument("--minutes", type=float, default=30.0, help="soak duration")
    ap.add_argument("--stock", default="2330")
    ap.add_argument("--future", default="TXFR1")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--offset-ticks", type=int, default=300)
    ap.add_argument("--sleep", type=float, default=0.5, help="sleep between order cycles (rate-limit guard)")
    ap.add_argument(
        "--price-mode", choices=("snapshot", "fixed"), default="snapshot",
        help="snapshot=anchor on live bid/ask (needs an OPEN market); fixed=derive from the "
             "contract daily limit band so latency can be probed during 休市/closed market "
             "(no live quote needed, fill impossible)",
    )
    ap.add_argument(
        "--max-cycles", type=int, default=0,
        help="cap the number of order cycles (0=unlimited, bounded only by --minutes)",
    )
    ap.add_argument("--resource-interval", type=float, default=5.0)
    ap.add_argument("--out", required=True, help="output JSON path (under outputs/, gitignored)")
    args = ap.parse_args()

    _assert_sim_only()

    api = sj.Shioaji(simulation=True)
    # Defensive: confirm the constructed session is actually simulation.
    if getattr(api, "simulation", True) is False:
        raise SystemExit("REFUSING: Shioaji session did not resolve to simulation=True.")

    api.login(
        api_key=os.environ["SHIOAJI_API_KEY"],
        secret_key=os.environ["SHIOAJI_SECRET_KEY"],
        contracts_timeout=60000,
    )

    # Silence the SDK's default order-state stdout print — it echoes broker_id /
    # account_id. The probe reads ``trade.status`` directly, so a no-op order
    # callback is safe and keeps account identifiers out of logs.
    try:
        api.set_order_callback(lambda *_a, **_k: None)
    except Exception:  # noqa: BLE001
        pass

    stock = _stock_contract(api, args.stock)
    future = _futures_contract(api, args.future)
    if future is None:
        raise SystemExit(f"Cannot resolve futures contract {args.future!r}")

    basket: list[tuple[str, str]] = []
    if stock is not None:
        basket.append((str(getattr(stock, "code", args.stock)), "TSE"))
    basket.append((str(getattr(future, "code", args.future)), "TAIFEX"))
    normalizer, cfg_path = _build_normalizer(basket)
    tracker = _ParityTracker(normalizer)

    # Reconnect-event observation.
    reconnect_events: list[dict[str, Any]] = []

    def _on_event(resp_code: Any, event_code: Any, info: Any, event: Any) -> None:
        try:
            reconnect_events.append(
                {"perf_ns": time.perf_counter_ns(), "event_code": int(event_code)}
            )
        except Exception:  # noqa: BLE001
            pass

    # Register all four v1 callbacks + the event callback through the proxy.
    quote = api.quote
    registered: list[str] = []
    for setter, handler in (
        ("set_on_tick_stk_v1_callback", tracker.on_tick),
        ("set_on_tick_fop_v1_callback", tracker.on_tick),
        ("set_on_bidask_stk_v1_callback", tracker.on_bidask),
        ("set_on_bidask_fop_v1_callback", tracker.on_bidask),
    ):
        fn = getattr(quote, setter, None)
        if fn is not None:
            fn(handler)
            registered.append(setter)
    ev_setter = getattr(quote, "set_event_callback", None)
    if ev_setter is not None:
        ev_setter(_on_event)
        registered.append("set_event_callback")

    # Subscribe basket, version v1.
    v1 = sj.constant.QuoteVersion.v1
    if stock is not None:
        quote.subscribe(stock, quote_type=sj.constant.QuoteType.Tick, version=v1)
        quote.subscribe(stock, quote_type=sj.constant.QuoteType.BidAsk, version=v1)
    quote.subscribe(future, quote_type=sj.constant.QuoteType.Tick, version=v1)
    quote.subscribe(future, quote_type=sj.constant.QuoteType.BidAsk, version=v1)

    sampler = _ResourceSampler(args.resource_interval)
    sampler.start()
    loop = _run_order_loop(api, future, args)
    sampler.stop()
    sampler.join(timeout=2.0)

    place_stats = _summarize("place_order", loop["place_s"], loop["place_err"])
    ack_stats = _summarize("submitted_ack", loop["ack_s"], loop["ack_err"])
    modify_stats = _summarize("update_order", loop["modify_s"], loop["modify_err"])
    cancel_stats = _summarize("cancel_order", loop["cancel_s"], loop["cancel_err"])
    elapsed_s = loop["elapsed_s"]
    i = loop["cycles"]
    fills_aborted = loop["fills_aborted"]

    # Reconnect timing (observational).
    codes = reconnect_events
    reconnecting = next((e for e in codes if e["event_code"] == 12), None)
    reconnected = next((e for e in codes if e["event_code"] == 13), None)
    recon = {
        "exercised": bool(reconnecting or reconnected),
        "event_codes_seen": sorted({e["event_code"] for e in codes}),
        "recover_ms": (
            (reconnected["perf_ns"] - reconnecting["perf_ns"]) / 1e6
            if reconnecting and reconnected
            else None
        ),
        "quote_no_data_s_threshold": float(os.getenv("HFT_QUOTE_NO_DATA_S", "30")),
        "note": "Sim transport disconnect cannot be forced client-side; recorded if it occurred.",
    }

    parity = tracker.snapshot()
    resources = sampler.summary()

    def ms(s: OpStats) -> dict[str, float]:
        return {"p50_ms": s.p50_us / 1000, "p95_ms": s.p95_us / 1000, "p99_ms": s.p99_us / 1000, "max_ms": s.max_us / 1000, "n": s.count, "errors": s.errors}

    report = {
        "meta": {  # NB: no credentials, ever.
            "sdk_version": sj.__version__,
            "mode": "sim",
            "stock": args.stock,
            "future": args.future,
            "qty": args.qty,
            "offset_ticks": args.offset_ticks,
            "minutes": args.minutes,
            "price_mode": args.price_mode,
            "max_cycles": getattr(args, "max_cycles", 0),
            "contract_bands": {
                "reference": str(getattr(future, "reference", None)),
                "limit_up": str(getattr(future, "limit_up", None)),
                "limit_down": str(getattr(future, "limit_down", None)),
            },
            "elapsed_s": round(elapsed_s, 1),
            "order_cycles": i,
            "fills_aborted": fills_aborted,
            "callbacks_registered": registered,
        },
        "latency_ms": {
            "place_order": ms(place_stats),
            "submitted_ack": ms(ack_stats),
            "update_order": ms(modify_stats),
            "cancel_order": ms(cancel_stats),
        },
        "latency_full": {
            "place_order": asdict(place_stats),
            "submitted_ack": asdict(ack_stats),
            "update_order": asdict(modify_stats),
            "cancel_order": asdict(cancel_stats),
        },
        "decimal_parity": parity,
        "reconnect": recon,
        "resources": resources,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))
    Path(cfg_path).unlink(missing_ok=True)

    # Draft latency_profiles.yaml block (sim is a FLOOR; live ≈10× sim).
    draft = _draft_profile_block(sj.__version__, place_stats, modify_stats, cancel_stats)
    draft_path = out_path.with_suffix(".profile.yaml")
    draft_path.write_text(draft)

    print(f"\nshioaji {sj.__version__} SIM soak — {elapsed_s:.0f}s, {i} order cycles, aborted={fills_aborted}")
    for label, s in (("place", place_stats), ("ack", ack_stats), ("update", modify_stats), ("cancel", cancel_stats)):
        print(f"  {label:<8} n={s.count:<4} P50={s.p50_us/1000:7.1f}ms P95={s.p95_us/1000:7.1f}ms P99={s.p99_us/1000:7.1f}ms")
    print(f"  decimal_parity: events={parity['events_total']} decimal={parity['decimal_priced']} "
          f"scaled_ok={parity['scaled_ok']} dropped={parity['dropped']} exc={parity['exceptions']} "
          f"fallback(tick/bidask)={parity['rust_fallback_tick']}/{parity['rust_fallback_bidask']} "
          f"holds={parity['parity_holds']}")
    print(f"  reconnect: exercised={recon['exercised']} codes={recon['event_codes_seen']} recover_ms={recon['recover_ms']}")
    print(f"  resources: rss_slope={resources['rss_kb_per_s_slope']:.2f}kb/s threads_max={resources['threads_max']} "
          f"fds_slope={resources['fds_per_s_slope']:.3f}/s")
    print(f"\nReport:  {out_path}")
    print(f"Draft profile:  {draft_path}")

    try:
        api.logout()
    except Exception:
        pass
    return 0


def _draft_profile_block(version: str, place: OpStats, modify: OpStats, cancel: OpStats) -> str:
    name = f"shioaji_sim_p95_v2026-06-17_sdk{version.replace('.', '')}"
    return (
        f"# DRAFT — paste into config/research/latency_profiles.yaml ONLY on GO.\n"
        f"# Sim is a FLOOR; live submitted_ack ≈10x sim (see r47_maker measured profile).\n"
        f"  {name}:\n"
        f'    description: "Shioaji {version} sim API RTT P95 — 2026-06-17 (measured)"\n'
        f'    source: "scripts/latency/shioaji_sim_soak.py"\n'
        f"    submit_ack_latency_ms: {place.p95_us / 1000:.1f}\n"
        f"    modify_ack_latency_ms: {modify.p95_us / 1000:.1f}\n"
        f"    cancel_ack_latency_ms: {cancel.p95_us / 1000:.1f}\n"
        f"    local_decision_pipeline_latency_us: 250\n"
        f'    sdk_version: "{version}"\n'
        f'    measurement_date: "2026-06-17"\n'
    )


if __name__ == "__main__":
    sys.exit(main())
