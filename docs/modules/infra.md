# Module: infra

## Purpose

Low-level infrastructure utilities. Currently hosts the shared ClickHouse client factory.

## Contents

- `ch_client.py` — ClickHouse client factory. Centralizes connection config
  (host, port, user, password, database) so every caller uses the same
  settings and retry behaviour.

## Used By

- `recorder/writer.py` — hot-path recording of market data and executions.
- `order/shadow_writer.py` — shadow-mode order record persistence.
- `ops/backup.py` — backup orchestration against ClickHouse.
- `monitor/_config_loader.py` — monitor TUI ClickHouse configuration.

## Notes

Not latency-critical. Float / Decimal arithmetic acceptable here; scaled-int
conversion happens at the writer boundary.
