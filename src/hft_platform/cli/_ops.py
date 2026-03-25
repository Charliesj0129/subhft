"""CLI commands: diag, feed-status, contracts-status, recorder-status, strat-test, backtest."""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import structlog

from hft_platform.config.loader import load_settings
from hft_platform.ops.flatten_gate import FlattenGate, FlattenRequest, FlattenStatus
from hft_platform.ops.manual_rearm import ManualRearmGate

logger = structlog.get_logger(__name__)


def cmd_feed_status(args: argparse.Namespace) -> None:
    print("Feed status command is lightweight; ensure service is running.")
    # Try reading Prometheus metrics if reachable
    import urllib.request

    try:
        resp = urllib.request.urlopen(f"http://localhost:{args.port}/metrics", timeout=1.5)  # nosec B310
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


def cmd_contracts_status(args: argparse.Namespace) -> None:
    import datetime as _dt

    path = Path(args.contracts)
    payload: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        sys.exit(1)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        payload["error"] = str(exc)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        sys.exit(1)
    updated_at = data.get("updated_at")
    age_s = None
    if updated_at:
        try:
            dt = _dt.datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            age_s = (_dt.datetime.now(_dt.timezone.utc) - dt).total_seconds()
        except Exception as _exc:  # noqa: BLE001
            age_s = None
    try:
        stale_after = float(os.getenv("HFT_CONTRACT_REFRESH_S", str(args.stale_after_s)))
    except ValueError:
        stale_after = float(args.stale_after_s)
    contracts = data.get("contracts", []) if isinstance(data, dict) else []
    payload.update(
        {
            "updated_at": updated_at,
            "age_s": age_s,
            "stale_after_s": stale_after,
            "stale": (age_s is None or age_s > stale_after),
            "cache_version": int(data.get("cache_version", 0)) if isinstance(data, dict) else 0,
            "contract_count": len(contracts) if isinstance(contracts, list) else 0,
        }
    )
    status_path_raw = str(getattr(args, "status_file", "") or "").strip()
    if status_path_raw:
        status_path = Path(status_path_raw)
        payload["runtime_status_path"] = str(status_path)
        if status_path.exists():
            try:
                payload["runtime_status"] = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception as exc:
                payload["runtime_status_error"] = str(exc)
    print(json.dumps(payload, indent=2, ensure_ascii=False))


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


def cmd_backtest(args: argparse.Namespace) -> None:
    if args.backtest_cmd is None:
        print("Please specify backtest subcommand (convert|run)")
        sys.exit(1)

    if args.backtest_cmd == "convert":
        try:
            from hft_platform.backtest.convert import convert_jsonl_to_npz

            convert_jsonl_to_npz(args.input, args.output, scale=args.scale)
            print(f"Converted to {args.output}")
        except Exception as exc:
            print(f"Convert failed: {exc}")
            sys.exit(1)
        return

    if args.backtest_cmd == "run":
        try:
            from hft_platform.backtest.adapter import StrategyHbtAdapter
            from hft_platform.backtest.runner import HftBacktestConfig, HftBacktestRunner
        except Exception as exc:
            print(f"Failed to import backtest modules: {exc}")
            sys.exit(1)

        # If strategy provided, use adapter; else simple buy-hold runner.
        if args.strategy_module:
            if len(args.data) > 1:
                print("Strategy bridge currently supports single-asset backtest; provide one data file.")
                sys.exit(1)
            adapter = StrategyHbtAdapter(
                data_path=args.data[0],
                strategy_module=args.strategy_module,
                strategy_class=args.strategy_class or "SimpleMarketMaker",
                strategy_id=args.strategy_id or "demo",
                symbol=args.symbol or "2330",
                tick_size=args.tick_size,
                lot_size=args.lot_size,
                maker_fee=(args.fee_maker or 0.0),
                taker_fee=(args.fee_taker or 0.0),
                partial_fill=not args.no_partial_fill,
                price_scale=args.price_scale,
                timeout=args.timeout,
                seed=int(args.seed),
            )
            adapter.run()
            print("Strategy backtest completed.")
        else:
            if len(args.data) != 1:
                print("Backtest runner currently supports one data file; run one symbol/file per invocation.")
                sys.exit(1)
            cfg = HftBacktestConfig(
                data=args.data,
                symbols=args.symbols,
                tick_sizes=args.tick_sizes or [args.tick_size],
                lot_sizes=args.lot_sizes or [args.lot_size],
                latency_entry=args.latency_entry,
                latency_resp=args.latency_resp,
                fee_maker=args.fee_maker,
                fee_taker=args.fee_taker,
                partial_fill=not args.no_partial_fill,
                strict_equity=bool(args.strict_equity),
                record_out=args.record_out,
                report=args.report,
                seed=int(args.seed),
            )
            runner = HftBacktestRunner(cfg)
            result = runner.run()
            if result is None:
                print("Backtest failed.")
                sys.exit(1)
            print(
                "Backtest completed.",
                f"run_id={result.run_id}",
                f"config={result.config_hash}",
                f"pnl={result.pnl:.2f}",
                f"synthetic={result.used_synthetic_equity}",
                f"equity_points={result.equity_points}",
            )


def cmd_recorder_status(args: argparse.Namespace) -> None:
    import time
    import urllib.request

    wal_dir: str = getattr(args, "wal_dir", None) or os.getenv("HFT_WAL_DIR", "data/wal")  # type: ignore[assignment]
    ck_host = getattr(args, "ck_host", None) or os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    ck_port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    recorder_mode = os.getenv("HFT_RECORDER_MODE", "direct")
    batcher_max = os.getenv("HFT_BATCHER_MAX_BUFFER", "2000")
    wal_batch_max = os.getenv("HFT_WAL_BATCH_MAX_ROWS", "500")
    wal_disk_min_mb = os.getenv("HFT_WAL_DISK_MIN_MB", "500")
    wal_pressure_policy = os.getenv("HFT_WAL_DISK_PRESSURE_POLICY", "drop")

    # WAL backlog scan
    wal_files: list[tuple[str, float, int]] = []  # (name, mtime, size)
    try:
        with os.scandir(wal_dir) as it:
            for entry in it:
                if entry.name.endswith(".wal") and entry.is_file():
                    st = entry.stat()
                    wal_files.append((entry.name, st.st_mtime, st.st_size))
    except FileNotFoundError:
        pass

    now = time.time()
    wal_count = len(wal_files)
    wal_total_bytes = sum(s for _, _, s in wal_files)
    oldest_age_s: float | None = None
    if wal_files:
        oldest_mtime = min(m for _, m, _ in wal_files)
        oldest_age_s = now - oldest_mtime

    def _fmt_bytes(b: int) -> str:
        if b >= 1024 * 1024:
            return f"{b / 1024 / 1024:.1f} MB"
        if b >= 1024:
            return f"{b / 1024:.1f} KB"
        return f"{b} B"

    # Disk free
    disk_free_str = "unknown"
    try:
        st_vfs = os.statvfs(wal_dir if os.path.exists(wal_dir) else ".")
        free_mb = st_vfs.f_frsize * st_vfs.f_bavail / 1024 / 1024
        if free_mb >= 1024:
            disk_free_str = f"{free_mb / 1024:.1f} GB"
        else:
            disk_free_str = f"{free_mb:.0f} MB"
    except Exception as _exc:  # noqa: BLE001
        pass

    # WAL guard status
    try:
        guard_threshold_mb = int(wal_disk_min_mb)
        disk_free_mb_val = st_vfs.f_frsize * st_vfs.f_bavail / 1024 / 1024
        guard_active = disk_free_mb_val < guard_threshold_mb
        guard_str = "ACTIVE" if guard_active else "OFF"
    except Exception as _exc:  # noqa: BLE001
        guard_str = "unknown"

    if oldest_age_s is not None:
        backlog_str = f"{wal_count} files (oldest: {oldest_age_s:.0f}s ago, total: {_fmt_bytes(wal_total_bytes)})"
    else:
        backlog_str = f"{wal_count} files"

    # ClickHouse reachability
    ck_status = "unreachable"
    try:
        resp = urllib.request.urlopen(f"http://{ck_host}:{ck_port}/ping", timeout=2.0)  # nosec B310
        if resp.status == 200:
            ck_status = "ok"
    except Exception as _exc:  # noqa: BLE001
        pass

    ck_pool = os.getenv("HFT_CLICKHOUSE_POOL_SIZE", "8")

    print("WAL Status:")
    print(f"  Mode:        {recorder_mode} (HFT_RECORDER_MODE={recorder_mode})")
    print(f"  Backlog:     {backlog_str}")
    print(f"  Disk guard:  {wal_disk_min_mb} MB min (policy={wal_pressure_policy}, free={disk_free_str}) — {guard_str}")
    print()
    print("ClickHouse:")
    print(f"  Status:      {ck_status} ({ck_host}:{ck_port})")
    print()
    print("Config:")
    print(f"  Batcher:     {batcher_max} rows/table | WAL batch: {wal_batch_max} rows | CK pool: {ck_pool} threads")


def _manual_rearm_gate(args: argparse.Namespace) -> ManualRearmGate:
    return ManualRearmGate(state_path=getattr(args, "state_path", None))


def cmd_ops_rearm_strategy(args: argparse.Namespace) -> None:
    gate = _manual_rearm_gate(args)
    try:
        gate.rearm_strategy(args.strategy_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Strategy re-armed: {args.strategy_id}")


def cmd_ops_rearm_platform(args: argparse.Namespace) -> None:
    gate = _manual_rearm_gate(args)
    gate.rearm_platform()
    print("Platform re-armed.")


def cmd_ops_autonomy_status(args: argparse.Namespace) -> None:
    gate = _manual_rearm_gate(args)
    print(json.dumps(gate.snapshot(), indent=2, ensure_ascii=False))


def _flatten_via_gate(
    scope: str,
    scope_id: str | None,
    deadline: int,
    gate: FlattenGate | None = None,
    poll_timeout_s: float = 300.0,
) -> FlattenRequest | None:
    """Submit a flatten request via FlattenGate and poll for result.

    Returns the final FlattenRequest on completion/failure, or None on timeout.
    """
    if gate is None:
        gate = FlattenGate()

    gate.submit(scope=scope, scope_id=scope_id, deadline_s=deadline)
    print(f"Flatten request submitted: scope={scope} id={scope_id} deadline={deadline}s")
    print("Waiting for running engine to process...")

    import time

    start = time.monotonic()
    while (time.monotonic() - start) < poll_timeout_s:
        req = gate.read_request()
        if req is not None and req.status in (
            FlattenStatus.COMPLETED,
            FlattenStatus.FAILED,
        ):
            return req
        time.sleep(0.5)

    print("Timeout: engine did not process flatten request within poll window.")
    return None


def cmd_ops_flatten(args: argparse.Namespace) -> None:
    """Emergency position flattening via file-based IPC."""
    scope = getattr(args, "scope", "all")
    scope_id = getattr(args, "scope_id", None)
    deadline = getattr(args, "deadline", 120)

    logger.info("ops_flatten_start", scope=scope, scope_id=scope_id, deadline=deadline)

    result = _flatten_via_gate(scope=scope, scope_id=scope_id, deadline=deadline)

    if result is None:
        print("Flatten request timed out. Check if the HFT engine is running.")
        sys.exit(1)

    if result.status == FlattenStatus.COMPLETED:
        print(
            f"Flatten completed: fully_closed={result.fully_closed} "
            f"partially_closed={result.partially_closed} "
            f"failed={result.failed}"
        )
        if result.failed_symbols:
            print(f"Failed symbols: {', '.join(result.failed_symbols)}")
    elif result.status == FlattenStatus.FAILED:
        print(f"Flatten failed: {result.error}")
        sys.exit(1)
