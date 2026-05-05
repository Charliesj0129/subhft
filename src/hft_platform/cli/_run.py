"""CLI commands: run, init, check, wizard."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import textwrap
from typing import Any, Dict

from structlog import get_logger

from hft_platform.config.loader import (
    detect_live_credentials,
    load_settings,
    resolve_active_strategy,
    summarize_settings,
)

from ._utils import _safe_write

logger = get_logger("cli")


def cmd_run(args: argparse.Namespace) -> None:
    os.environ.setdefault("HFT_RUNTIME_ROLE", "engine")
    runtime_role = str(os.getenv("HFT_RUNTIME_ROLE", "engine")).strip().lower().replace("-", "_")
    if runtime_role != "engine":
        logger.warning(
            "hft run invoked with non-engine runtime role; feed client creation may be disabled",
            runtime_role=runtime_role,
        )

    mode = args.mode or args.mode_flag or _resolve_default_mode()
    cli_overrides: Dict[str, Any] = {
        "mode": mode,
        "symbols": args.symbols or None,
    }

    loop_id_raw = getattr(args, "loop_id", None)
    loop_id = loop_id_raw if isinstance(loop_id_raw, str) and loop_id_raw else None
    if loop_id and args.strategy:
        print(
            "[hft run] --loop and --strategy are mutually exclusive; the loop YAML defines its own strategy.",
            file=sys.stderr,
        )
        sys.exit(2)
    if loop_id:
        cli_overrides["loop_id"] = loop_id
    elif args.strategy:
        cli_overrides["strategy"] = {
            "id": args.strategy,
            "module": args.strategy_module or "hft_platform.strategies.simple_mm",
            "class": args.strategy_class or "SimpleMarketMaker",
            "params": {},
        }
    settings, defaults = load_settings({k: v for k, v in cli_overrides.items() if v})

    _enforce_loop_trace_policy(settings)

    downgraded = None
    if settings.get("mode") == "live" and not detect_live_credentials():
        downgraded = "sim"
        settings["mode"] = "sim"
        logger.warning("No Shioaji credentials found, downgrading to sim mode")

    summary = summarize_settings(settings, downgraded_mode=downgraded)
    print(f"[hft run] {summary}")
    if downgraded:
        print("Hint: set SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY to enable live.")

    if settings.get("mode") == "replay":
        from datetime import date as _date

        from hft_platform.replay.cli_runner import run_replay_session

        session_raw = getattr(args, "session", None)
        fixture_raw = getattr(args, "fixture", None)
        if not session_raw or not fixture_raw:
            print(
                "[hft run] --mode replay requires --session YYYY-MM-DD and --fixture PATH.",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            session_date = _date.fromisoformat(session_raw)
        except ValueError:
            print(
                f"[hft run] --session must be YYYY-MM-DD (got {session_raw!r}).",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.exit(
            run_replay_session(
                settings,
                session_date=session_date,
                fixture_path=fixture_raw,
                allow_pre_recorder=bool(getattr(args, "allow_pre_recorder", False)),
            )
        )

    # Live/Sim share the same runtime pipeline; sim runs with Shioaji stub.
    from hft_platform.main import HFTSystem
    from hft_platform.observability.metrics import MetricsRegistry
    from hft_platform.observability.metrics_server import start_resilient_metrics_server

    MetricsRegistry.get()  # fully populate REGISTRY before scrape thread starts
    _prom_port = settings.get("prometheus_port", 9090)
    start_resilient_metrics_server(_prom_port)
    print(f"Prometheus metrics started on :{_prom_port}")

    system = HFTSystem(settings)
    try:
        asyncio.run(system.run())
    except KeyboardInterrupt:
        print("Interrupted, shutting down...")


def _resolve_default_mode() -> str:
    raw = str(os.getenv("HFT_MODE", "sim")).strip().lower()
    if raw == "real":
        return "live"
    if raw not in {"sim", "live", "replay"}:
        return "sim"
    return raw


def _enforce_loop_trace_policy(settings: Dict[str, Any]) -> None:
    # L5: a bound loop_id requires complete order-bearing trace chains.
    # The loop YAML must declare ``trace_policy: order_path_100pct``; we
    # then force ``HFT_DIAG_TRACE_ENABLED=1`` so the sampler opens its
    # ``enabled`` gate (default 0 per DecisionTraceSampler.from_env).
    loop_id = settings.get("loop_id")
    if not loop_id:
        return
    loop_path = os.path.join("config", "loops", f"{loop_id}.yaml")
    if not os.path.exists(loop_path):
        # _bind_loop already raises LoopBindingError before we get here when
        # called from cmd_run; this branch is defensive against direct callers.
        return
    import yaml

    with open(loop_path, "r", encoding="utf-8") as f:
        loop_cfg = yaml.safe_load(f) or {}
    policy = loop_cfg.get("trace_policy")
    if policy != "order_path_100pct":
        print(
            f"[hft run] loop_id={loop_id!r} requires "
            f"trace_policy=order_path_100pct (got {policy!r}). Refusing to start.",
            file=sys.stderr,
        )
        sys.exit(2)
    os.environ["HFT_DIAG_TRACE_ENABLED"] = "1"


def cmd_init(args: argparse.Namespace) -> None:
    """Question-lite init that drops a settings.py and a strategy skeleton."""
    strategy_id = args.strategy_id or "my_strategy"
    symbol = args.symbol or "2330"
    settings_tpl = (
        textwrap.dedent(
            f"""
        # Generated by hft init
        def get_settings():
            return {{
                "mode": "sim",
                "symbols": ["{symbol}"],
                "strategy": {{
                    "id": "{strategy_id}",
                    "module": "hft_platform.strategies.{strategy_id}",
                    "class": "Strategy",
                    "params": {{}},
                }},
                "paths": {{
                    "symbols": "config/symbols.yaml",
                    "strategy_limits": "config/base/strategy_limits.yaml",
                    "order_adapter": "config/base/order_adapter.yaml",
                }},
                "prometheus_port": 9090,
            }}
        """
        ).strip()
        + "\n"
    )

    strategy_tpl = (
        textwrap.dedent(
            f"""
        from structlog import get_logger
        from hft_platform.events import LOBStatsEvent
        from hft_platform.strategy.base import BaseStrategy

        logger = get_logger("{strategy_id}")


        class Strategy(BaseStrategy):
            default_params = {{"min_spread": 100, "qty": 1}}

            def __init__(self, strategy_id: str, **params):
                super().__init__(strategy_id)
                self.params = {{**self.default_params, **(params or {{}})}}
                self.symbols = {{"{symbol}"}}

            def on_stats(self, event: LOBStatsEvent):
                if event.symbol not in self.symbols:
                    return
                if event.spread <= self.params["min_spread"]:
                    return
                self.buy(event.symbol, event.best_bid, self.params["qty"])
                logger.info("placing order", price=event.best_bid, params=self.params)
        """
        ).strip()
        + "\n"
    )

    test_tpl = (
        textwrap.dedent(
            f"""
        import pytest
        from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
        from hft_platform.strategies.{strategy_id} import Strategy
        from hft_platform.contracts.strategy import OrderIntent
        from hft_platform.events import LOBStatsEvent
        from hft_platform.feed_adapter.normalizer import SymbolMetadata
        from hft_platform.strategy.base import StrategyContext


        def _intent_factory(**kwargs):
            kwargs.setdefault("intent_id", 1)
            kwargs.setdefault("timestamp_ns", 0)
            return OrderIntent(**kwargs)

        def test_strategy_emits_intent():
            strat = Strategy(strategy_id="{strategy_id}")
            metadata = SymbolMetadata()
            price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(metadata))
            ctx = StrategyContext(
                positions={{}},
                strategy_id="{strategy_id}",
                intent_factory=_intent_factory,
                price_scaler=price_codec.scale,
                lob_source=None,
            )
            event = LOBStatsEvent(
                symbol="{symbol}",
                ts=0,
                imbalance=0.0,
                best_bid=1000000,
                best_ask=1000200,
                bid_depth=10,
                ask_depth=10,
            )
            intents = strat.handle_event(ctx, event)
            assert intents, "strategy should emit intent on wide spread"
        """
        ).strip()
        + "\n"
    )

    _safe_write("config/settings.py", settings_tpl)
    _safe_write(f"src/hft_platform/strategies/{strategy_id}.py", strategy_tpl)
    _safe_write(f"tests/test_{strategy_id}.py", test_tpl)

    print("Initialized settings and strategy skeleton.")
    print(f"- config/settings.py\n- src/hft_platform/strategies/{strategy_id}.py\n- tests/test_{strategy_id}.py")
    print("Next steps: `hft run sim --strategy {strategy_id}` or `pytest -k test_{strategy_id}`")


def cmd_check(args: argparse.Namespace) -> None:
    settings, defaults = load_settings()
    missing = []
    if not settings.get("symbols"):
        missing.append("symbols")
    strat = resolve_active_strategy(settings)
    if not strat.get("id"):
        missing.append("strategy.id")
    if missing:
        print("Config errors:", ", ".join(missing))
        sys.exit(1)

    print("Configuration is valid.")
    if args.export:
        out = "config/exported_settings." + args.export
        if args.export == "yaml":
            try:
                import yaml

                payload = yaml.safe_dump(settings)
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                payload = json.dumps(settings, indent=2)
        else:
            payload = json.dumps(settings, indent=2)
        _safe_write(out, payload)
        print(f"Exported effective settings to {out}")


def cmd_wizard(args: argparse.Namespace) -> None:
    from hft_platform.config.wizard import run_wizard

    run_wizard()
