"""Diagnostics, feed status, and strategy smoke-test commands."""

from __future__ import annotations

import argparse
import json
import sys
from importlib import import_module
from pathlib import Path

from hft_platform.config.loader import load_settings


def cmd_feed_status(args: argparse.Namespace) -> None:
    print("Feed status command is lightweight; ensure service is running.")
    # Try reading Prometheus metrics if reachable
    import urllib.request

    try:
        resp = urllib.request.urlopen(f"http://localhost:{args.port}/metrics", timeout=1.5)
        body = resp.read().decode("utf-8")
        has_feed = "feed_events_total" in body
        print(f"Metrics reachable on :{args.port}; feed metric present={has_feed}")
    except Exception as exc:
        print(f"Unable to reach metrics on :{args.port}: {exc}")


def cmd_diag(args: argparse.Namespace) -> None:
    trace_file = getattr(args, "trace_file", None)
    if trace_file:
        from hft_platform.diagnostics.replay import (
            build_timeline,
            filter_traces,
            load_traces,
            render_timeline_markdown,
            summarize_trace,
        )

        records = load_traces(trace_file)
        records = filter_traces(records, trace_id=getattr(args, "trace_id", None), stage=getattr(args, "stage", None))
        if getattr(args, "timeline", False):
            timeline = build_timeline(records)
            fmt = str(getattr(args, "timeline_format", "json") or "json").strip().lower()
            out_path = getattr(args, "out", None)
            if fmt == "md":
                text = render_timeline_markdown(timeline)
                if out_path:
                    Path(out_path).write_text(text, encoding="utf-8")
                print(text)
            else:
                text = json.dumps(timeline, indent=2, ensure_ascii=False)
                if out_path:
                    Path(out_path).write_text(text, encoding="utf-8")
                print(text)
            return
        summary = summarize_trace(records)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        limit = int(getattr(args, "limit", 20) or 20)
        if limit > 0:
            print("\\nLast records:")
            for rec in sorted(records, key=lambda r: int(r.get("ts_ns", 0) or 0))[-limit:]:
                print(json.dumps(rec, ensure_ascii=False))
        return

    # Minimal diag stub
    print("Diag:")
    print("- Check metrics at http://localhost:9090/metrics")
    print("- Common fixes: verify credentials, check network, ensure queues not full.")
    print("- For trace inspection: hft diag --trace-file outputs/decision_traces/<day>.jsonl")
    print("- For timeline view: hft diag --trace-file ... --timeline --timeline-format md")


def cmd_strat_test(args: argparse.Namespace) -> None:
    settings, _ = load_settings()
    module_name = args.module or settings.get("strategy", {}).get("module", "hft_platform.strategies.simple_mm")
    class_name = args.cls or settings.get("strategy", {}).get("class", "SimpleMarketMaker")
    strategy_id = args.strategy_id or settings.get("strategy", {}).get("id", "demo")
    symbol = args.symbol or (settings.get("symbols") or ["2330"])[0]
    try:
        mod = import_module(module_name)
        cls = getattr(mod, class_name)
    except Exception as exc:
        print(f"Failed to import strategy {module_name}.{class_name}: {exc}")
        sys.exit(1)

    strat = cls(strategy_id=strategy_id)

    from hft_platform.contracts.strategy import OrderIntent
    from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
    from hft_platform.events import LOBStatsEvent
    from hft_platform.feed_adapter.normalizer import SymbolMetadata
    from hft_platform.strategy.base import StrategyContext

    def _intent_factory(**kwargs):
        kwargs.setdefault("intent_id", 1)
        kwargs.setdefault("timestamp_ns", 0)
        return OrderIntent(**kwargs)

    metadata = SymbolMetadata(settings.get("paths", {}).get("symbols"))
    price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(metadata))

    ctx = StrategyContext(
        positions={},
        strategy_id=strategy_id,
        intent_factory=_intent_factory,
        price_scaler=price_codec.scale,
        lob_source=None,
    )

    event = LOBStatsEvent(
        symbol=symbol,
        ts=0,
        imbalance=0.0,
        best_bid=99,
        best_ask=101,
        bid_depth=10,
        ask_depth=10,
    )
    intents = strat.handle_event(ctx, event)
    print(f"Strategy emitted {len(intents)} intents.")
    for it in intents:
        print(f"- {it}")
