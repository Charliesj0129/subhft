#!/usr/bin/env python3
import json
import os
import time
from typing import Dict, List, Tuple

import clickhouse_connect
import requests
from structlog import get_logger

logger = get_logger("monitor.runtime")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _parse_metrics(text: str) -> Dict[str, List[Tuple[Dict[str, str], float]]]:
    metrics: Dict[str, List[Tuple[Dict[str, str], float]]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or " " not in line:
            continue
        name_labels, value = line.split(" ", 1)
        try:
            val = float(value.strip())
        except Exception:
            continue
        if "{" in name_labels:
            name, labels_str = name_labels.split("{", 1)
            labels_str = labels_str.rstrip("}")
            labels: Dict[str, str] = {}
            if labels_str:
                for part in labels_str.split(","):
                    if "=" not in part:
                        continue
                    k, v = part.split("=", 1)
                    labels[k] = v.strip('"')
            metrics.setdefault(name, []).append((labels, val))
        else:
            metrics.setdefault(name_labels, []).append(({}, val))
    return metrics


def _post_webhook(url: str, payload: Dict[str, object]) -> None:
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as exc:
        logger.warning("Alert webhook failed", error=str(exc))


def _query_scalar(client: clickhouse_connect.driver.Client, sql: str, parameters: Dict[str, object] | None = None):
    result = client.query(sql, parameters=parameters)
    if not result.result_rows:
        return None
    return result.result_rows[0][0]


def main() -> None:
    interval_s = _float_env("HFT_MONITOR_INTERVAL_S", 10.0)
    metrics_url = os.getenv("HFT_MONITOR_METRICS_URL", "http://localhost:9090/metrics")
    clickhouse_host = os.getenv("HFT_CLICKHOUSE_HOST") or os.getenv("CLICKHOUSE_HOST") or "clickhouse"
    clickhouse_port = _int_env("HFT_CLICKHOUSE_PORT", 8123)
    future_tol_s = _float_env("HFT_MONITOR_FUTURE_TOLERANCE_S", 60.0)
    feed_gap_warn_s = _float_env("HFT_MONITOR_FEED_GAP_WARN_S", 5.0)
    wal_rate_warn = _float_env("HFT_MONITOR_WAL_RATE_WARN", 1.0)
    ingest_rate_min = _float_env("HFT_MONITOR_INGEST_RATE_MIN", 1.0)
    max_ingest_lag_s = _float_env("HFT_MONITOR_MAX_INGEST_LAG_S", 5.0)
    webhook_url = os.getenv("HFT_MONITOR_ALERT_WEBHOOK", "")
    run_once = os.getenv("HFT_MONITOR_ONCE", "0") in {"1", "true", "yes"}

    ch_client = clickhouse_connect.get_client(host=clickhouse_host, port=clickhouse_port)
    last_wal_total: float | None = None
    last_ts = time.time()

    while True:
        now = time.time()
        alerts: List[Dict[str, object]] = []

        # --- Prometheus metrics ---
        try:
            resp = requests.get(metrics_url, timeout=5)
            resp.raise_for_status()
            metrics = _parse_metrics(resp.text)

            storm_vals = [v for _, v in metrics.get("stormguard_mode", [])]
            storm_max = max(storm_vals) if storm_vals else 0.0
            if storm_max >= 3:
                alerts.append({"type": "stormguard", "severity": "error", "value": storm_max})
            elif storm_max >= 2:
                alerts.append({"type": "stormguard", "severity": "warning", "value": storm_max})

            wal_total = 0.0
            for _, v in metrics.get("recorder_wal_writes_total", []):
                wal_total += v
            if last_wal_total is not None:
                elapsed = max(1e-6, now - last_ts)
                wal_rate = (wal_total - last_wal_total) / elapsed
                if wal_rate_warn > 0 and wal_rate >= wal_rate_warn:
                    alerts.append(
                        {"type": "wal_fallback", "severity": "warning", "rate_per_s": wal_rate}
                    )
            last_wal_total = wal_total

            feed_gaps = [v for _, v in metrics.get("feed_gap_by_symbol_seconds", [])]
            if feed_gaps:
                feed_gap_max = max(feed_gaps)
                if feed_gap_max >= feed_gap_warn_s:
                    alerts.append(
                        {"type": "feed_gap", "severity": "warning", "max_s": feed_gap_max}
                    )
        except Exception as exc:
            alerts.append({"type": "metrics_fetch", "severity": "warning", "error": str(exc)})

        # --- ClickHouse checks ---
        try:
            future_ns = int(future_tol_s * 1e9)
            future_rows = _query_scalar(
                ch_client,
                "SELECT count() FROM hft.market_data "
                "WHERE ingest_ts > toUInt64(toUnixTimestamp64Nano(now64())) + %(future_ns)s",
                parameters={"future_ns": future_ns},
            )
            if future_rows and future_rows > 0:
                alerts.append(
                    {"type": "future_rows", "severity": "warning", "count": int(future_rows)}
                )

            max_ingest_ts = _query_scalar(ch_client, "SELECT max(ingest_ts) FROM hft.market_data")
            if max_ingest_ts:
                lag_s = (time.time_ns() - int(max_ingest_ts)) / 1e9
                if max_ingest_lag_s > 0 and lag_s > max_ingest_lag_s:
                    alerts.append(
                        {"type": "ingest_lag", "severity": "warning", "lag_s": lag_s}
                    )

            window_ns = int(interval_s * 1e9)
            recent = _query_scalar(
                ch_client,
                "SELECT count() FROM hft.market_data "
                "WHERE ingest_ts >= toUInt64(toUnixTimestamp64Nano(now64())) - %(window)s",
                parameters={"window": window_ns},
            )
            if recent is not None:
                rate = float(recent) / max(1.0, interval_s)
                if ingest_rate_min > 0 and rate < ingest_rate_min:
                    alerts.append({"type": "ingest_rate", "severity": "warning", "rate": rate})
        except Exception as exc:
            alerts.append({"type": "clickhouse", "severity": "warning", "error": str(exc)})

        # --- Emit + optional webhook ---
        if alerts:
            for alert in alerts:
                logger.warning("Runtime monitor alert", **alert)
            if webhook_url:
                _post_webhook(webhook_url, {"alerts": alerts, "ts": time.time()})
        else:
            logger.info("Runtime monitor OK")

        last_ts = now
        if run_once:
            break
        time.sleep(interval_s)


if __name__ == "__main__":
    main()
