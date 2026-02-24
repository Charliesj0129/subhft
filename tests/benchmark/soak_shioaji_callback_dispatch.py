from __future__ import annotations

import argparse
import json
import time
from typing import Any

from hft_platform.feed_adapter import shioaji_client as mod


class _Client:
    __slots__ = ("code", "calls")

    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    def _enqueue_tick(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1


def _reset_registry() -> None:
    with mod.CLIENT_REGISTRY_LOCK:
        mod.CLIENT_REGISTRY.clear()
        mod.CLIENT_REGISTRY_BY_CODE.clear()
        mod.CLIENT_REGISTRY_SNAPSHOT = ()
        mod.CLIENT_REGISTRY_BY_CODE_SNAPSHOT = {}
        mod.TOPIC_CODE_CACHE.clear()


def main() -> int:
    p = argparse.ArgumentParser(description="Soak benchmark for Shioaji callback dispatch routing.")
    p.add_argument("--clients", type=int, default=64)
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--miss-rate", type=float, default=0.01, help="Fraction of dispatches using unparsable topic.")
    p.add_argument("--strict", action="store_true", help="Enable strict route-miss drop mode during soak.")
    p.add_argument(
        "--fallback-mode",
        default="wildcard",
        choices=("wildcard", "broadcast", "none"),
        help="Fallback target set when route miss occurs and strict=false.",
    )
    args = p.parse_args()

    _reset_registry()
    clients: list[_Client] = []
    for i in range(args.clients):
        code = f"{2300 + i}"
        c = _Client(code)
        clients.append(c)
        mod._registry_register(c)
        mod._registry_rebind_codes(c, [code])

    target = clients[len(clients) // 2]
    good_topic = f"Q/TSE/{target.code}"
    bad_topic = "UNPARSEABLE@@@"
    quote = type("Quote", (), {"code": target.code})()

    old_strict = mod._ROUTE_MISS_STRICT
    old_log_every = mod._ROUTE_MISS_LOG_EVERY
    old_fallback_mode = getattr(mod, "_ROUTE_MISS_FALLBACK_MODE", "wildcard")
    mod._ROUTE_MISS_STRICT = bool(args.strict)
    mod._ROUTE_MISS_FALLBACK_MODE = str(args.fallback_mode)
    mod._ROUTE_MISS_LOG_EVERY = 10_000_000
    try:
        deadline = time.perf_counter() + max(0.1, args.seconds)
        dispatches = 0
        miss_inputs = 0
        t0 = time.perf_counter()
        while time.perf_counter() < deadline:
            if args.miss_rate > 0 and (dispatches % max(1, int(1.0 / args.miss_rate)) == 0):
                mod.dispatch_tick_cb(bad_topic, object())
                miss_inputs += 1
            else:
                mod.dispatch_tick_cb(good_topic, quote)
            dispatches += 1
        elapsed = time.perf_counter() - t0
    finally:
        mod._ROUTE_MISS_STRICT = old_strict
        mod._ROUTE_MISS_FALLBACK_MODE = old_fallback_mode
        mod._ROUTE_MISS_LOG_EVERY = old_log_every

    total_calls = sum(c.calls for c in clients)
    payload = {
        "clients": args.clients,
        "seconds": args.seconds,
        "strict": bool(args.strict),
        "fallback_mode": str(args.fallback_mode),
        "miss_rate": args.miss_rate,
        "dispatches": dispatches,
        "miss_inputs": miss_inputs,
        "elapsed_s": elapsed,
        "dispatches_per_sec": dispatches / max(elapsed, 1e-9),
        "us_per_dispatch": elapsed / max(dispatches, 1) * 1e6,
        "calls_per_dispatch": total_calls / max(dispatches, 1),
        "target_calls": target.calls,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
