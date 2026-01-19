# observability

## Purpose
Metrics and instrumentation for runtime monitoring.

## Key Files
- `src/hft_platform/observability/metrics.py`: Prometheus metrics definitions.

## Usage
- Metrics are updated in services and validators.
- Prometheus server is started in CLI (`start_http_server`).

## Notes
- Port is controlled by `HFT_PROM_PORT` or settings.
