"""Parse watchlist.yaml and merge with symbols.yaml for name/product_type."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from structlog import get_logger

from hft_platform.monitor._types import MonitorConfig, WatchlistSymbol

logger = get_logger("monitor.config")


def load_watchlist(
    watchlist_path: str | Path | None = None,
    symbols_path: str | Path | None = None,
) -> MonitorConfig:
    """Load watchlist config, enriching with symbol metadata from symbols.yaml."""
    if watchlist_path is None:
        watchlist_path = os.getenv("HFT_WATCHLIST_PATH", "config/watchlist.yaml")
    if symbols_path is None:
        symbols_path = os.getenv("HFT_SYMBOLS_PATH", "config/symbols.yaml")

    wl_path = Path(watchlist_path)
    if not wl_path.exists():
        raise FileNotFoundError(f"Watchlist not found: {wl_path}")

    with open(wl_path) as f:
        raw = yaml.safe_load(f) or {}

    # Load symbol metadata lookup
    sym_meta: dict[str, dict[str, str]] = {}
    sym_path = Path(symbols_path)
    if sym_path.exists():
        with open(sym_path) as f:
            sym_data = yaml.safe_load(f) or {}
        for entry in sym_data.get("symbols", []):
            code = str(entry.get("code", ""))
            sym_meta[code] = {
                "name": entry.get("name", code),
                "product_type": entry.get("product_type", "stock"),
            }

    # Parse monitor settings
    monitor_cfg = raw.get("monitor", {})
    poll_interval = float(monitor_cfg.get("poll_interval_s", 2.0))
    warmup_ticks = int(monitor_cfg.get("warmup_ticks", 64))
    stale_threshold = float(monitor_cfg.get("stale_threshold_s", 6.0))
    no_data_warn_s = float(monitor_cfg.get("no_data_warn_s", 10.0))
    max_retries = int(monitor_cfg.get("max_retries", 20))
    source = (
        (os.getenv("HFT_MONITOR_SOURCE") or os.getenv("MONITOR_SOURCE") or str(monitor_cfg.get("source", "clickhouse")))
        .strip()
        .lower()
    )
    batch_limit_per_symbol = int(
        os.getenv("HFT_MONITOR_BATCH_LIMIT_PER_SYMBOL")
        or os.getenv("MONITOR_BATCH_LIMIT_PER_SYMBOL")
        or monitor_cfg.get("batch_limit_per_symbol", 200)
    )
    replay_ticks = int(
        os.getenv("HFT_MONITOR_REPLAY_TICKS")
        or os.getenv("MONITOR_REPLAY_TICKS")
        or monitor_cfg.get("replay_ticks", warmup_ticks)
    )

    ch_host = os.getenv("HFT_CLICKHOUSE_HOST", monitor_cfg.get("ch_host", "localhost"))
    ch_port = int(os.getenv("HFT_CLICKHOUSE_PORT", str(monitor_cfg.get("ch_port", 8123))))
    ch_user = (
        os.getenv("HFT_CLICKHOUSE_USER")
        or os.getenv("HFT_CLICKHOUSE_USERNAME")
        or os.getenv("CLICKHOUSE_USER")
        or monitor_cfg.get("ch_user", "default")
    )
    ch_password = (
        os.getenv("HFT_CLICKHOUSE_PASSWORD") or os.getenv("CLICKHOUSE_PASSWORD") or monitor_cfg.get("ch_password", "")
    )
    redis_host = (
        os.getenv("HFT_MONITOR_REDIS_HOST") or os.getenv("REDIS_HOST") or monitor_cfg.get("redis_host", "localhost")
    )
    redis_port = int(
        os.getenv("HFT_MONITOR_REDIS_PORT") or os.getenv("REDIS_PORT") or str(monitor_cfg.get("redis_port", 6379))
    )
    redis_password = (
        os.getenv("HFT_MONITOR_REDIS_PASSWORD")
        or os.getenv("HFT_REDIS_PASSWORD")
        or os.getenv("REDIS_PASSWORD")
        or monitor_cfg.get("redis_password", "")
    )
    redis_key_prefix = str(
        os.getenv("HFT_MONITOR_REDIS_KEY_PREFIX") or monitor_cfg.get("redis_key_prefix", "monitor:l1")
    )
    redis_ring_size = int(os.getenv("HFT_MONITOR_REDIS_RING_SIZE") or monitor_cfg.get("redis_ring_size", 256))
    promotions_dir = monitor_cfg.get("promotions_dir", "config/strategy_promotions")
    data_source = (os.getenv("HFT_MONITOR_DATA_SOURCE") or str(monitor_cfg.get("data_source", "auto"))).strip().lower()
    hybrid_backfill_interval_s = float(
        os.getenv("HFT_MONITOR_HYBRID_BACKFILL_INTERVAL_S") or monitor_cfg.get("hybrid_backfill_interval_s", 30.0)
    )

    # Parse symbols
    symbols: list[WatchlistSymbol] = []
    raw_symbols = raw.get("symbols", [])
    if not raw_symbols:
        raise ValueError("watchlist.yaml: no symbols defined")

    for entry in raw_symbols:
        code = str(entry.get("code", ""))
        if not code:
            logger.warning("skipping symbol with empty code")
            continue

        meta = sym_meta.get(code, {})
        name = meta.get("name", code)
        product_type = meta.get("product_type", "stock")
        alpha_ids = tuple(str(a) for a in entry.get("alpha_ids", []))
        if not alpha_ids:
            logger.warning("skipping symbol with no alphas", code=code)
            continue

        if code not in sym_meta:
            # Infer product_type from code pattern: alpha prefix → future/option, numeric → stock
            product_type = _infer_product_type(code)
            name = code
            logger.warning("symbol_not_in_symbols_yaml_using_fallback", code=code, product_type=product_type)

        symbols.append(
            WatchlistSymbol(
                code=code,
                name=name,
                product_type=product_type,
                alpha_ids=alpha_ids,
            )
        )

    if not symbols:
        raise ValueError("watchlist.yaml: all symbols were invalid")

    if not any(symbol.alpha_ids for symbol in symbols):
        raise ValueError("watchlist.yaml: no alpha_ids configured")

    logger.info(
        "watchlist loaded",
        n_symbols=len(symbols),
        symbols=[s.code for s in symbols],
    )

    return MonitorConfig(
        symbols=tuple(symbols),
        source=source,
        poll_interval_s=poll_interval,
        warmup_ticks=warmup_ticks,
        stale_threshold_s=stale_threshold,
        no_data_warn_s=no_data_warn_s,
        max_retries=max_retries,
        batch_limit_per_symbol=batch_limit_per_symbol,
        replay_ticks=replay_ticks,
        ch_host=ch_host,
        ch_port=ch_port,
        ch_user=str(ch_user),
        ch_password=str(ch_password),
        redis_host=str(redis_host),
        redis_port=redis_port,
        redis_password=str(redis_password),
        redis_key_prefix=redis_key_prefix,
        redis_ring_size=redis_ring_size,
        promotions_dir=promotions_dir,
        data_source=data_source,
        hybrid_backfill_interval_s=hybrid_backfill_interval_s,
    )


def _infer_product_type(code: str) -> str:
    """Infer product_type from symbol code pattern."""
    if code.isdigit():
        return "stock"
    # Futures/options: TX, MX, TM, TXO, etc.
    if any(code.upper().startswith(p) for p in ("TX", "MX", "TM", "SI", "ZE")):
        return "future"
    if "O" in code.upper() and any(c.isdigit() for c in code):
        return "option"
    return "future"  # default for unknown alpha-prefix codes
