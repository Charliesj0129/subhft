# 26 — Multi-Broker Governance

Ensure broker integrations remain modular, testable, and compliant with Core Laws.

## Rules

### MB-01: BrokerProtocol Compliance

All broker implementations MUST satisfy `BrokerProtocol` (typing.Protocol). No broker client usable without full protocol surface.

### MB-02: Import Isolation

No broker-specific imports (`shioaji`, `fubon_neo`, etc.) outside `feed_adapter/<broker>/`. Platform code (strategy, risk, recorder, gateway) uses `BrokerProtocol` only. Violation = code review rejection.

### MB-03: OrderTranslator Requirement

`OrderAdapter` MUST delegate to `BrokerOrderTranslator` for all order translation. Direct broker SDK calls in `OrderAdapter` are forbidden.

### MB-04: Execution Field Mapping

`ExecutionNormalizer` MUST use `BrokerExecFieldMap` for field resolution. No hardcoded broker-specific field names (e.g., `ordno`) in normalizer.

### MB-05: Configuration Structure

- Broker config: `config/base/brokers/<broker_name>.yaml`
- Selection: `HFT_BROKER` env var (default `shioaji`)
- Each config declares: auth method, rate limits, capabilities

### MB-06: Latency Profile Requirement

Every broker MUST provide a latency profile in `config/research/latency_profiles.yaml` covering `place_order`, `update_order`, `cancel_order` at P50/P95/P99. Missing profile = Gate D blocker.

### MB-07: Precision Law (Cross-Broker)

All brokers MUST scale prices to `int x10000` at the ingestion boundary before entering the platform event pipeline.

### MB-08: Credential Isolation

Distinct env var prefixes per broker; never share:
- Shioaji: `SHIOAJI_API_KEY`, `SHIOAJI_SECRET_KEY`
- Fubon: `HFT_FUBON_API_KEY`, `HFT_FUBON_PASSWORD`

### MB-09: Protocol Conformance Tests

Every broker adapter MUST include `tests/unit/test_<broker>_client.py` with protocol verification.

### MB-10: Fallback Safety

If broker SDK import fails, platform MUST: (1) log error via `structlog`, (2) refuse to start with clear error, (3) never silently fall back to another broker.
