# 26 — Multi-Broker Governance

## Purpose

Ensure broker integrations remain modular, testable, and compliant with HFT Constitution laws.

## Rules

### MB-01: BrokerProtocol Compliance

All broker implementations MUST satisfy `BrokerProtocol` (typing.Protocol).
No broker client may be used without implementing the full protocol surface.

### MB-02: Import Isolation

No broker-specific imports (`shioaji`, `fubon_neo`, etc.) outside `feed_adapter/<broker>/` directory.
Platform-level code (strategy, risk, recorder, gateway) MUST use `BrokerProtocol` only.
Violation = immediate code review rejection.

### MB-03: OrderTranslator Requirement

`OrderAdapter` MUST delegate to `BrokerOrderTranslator` for all order translation.
Direct broker SDK calls in OrderAdapter are forbidden.

### MB-04: Execution Field Mapping

`ExecutionNormalizer` MUST use `BrokerExecFieldMap` for field name resolution.
No hardcoded broker-specific field names (e.g., `ordno`, `custom_field`) in normalizer.

### MB-05: Configuration Structure

- Broker-specific config: `config/base/brokers/<broker_name>.yaml`
- Selection: `HFT_BROKER` env var (default: `shioaji`)
- Each config must declare: auth method, rate limits, capabilities

### MB-06: Latency Profile Requirement

Every broker integration MUST provide a latency profile in `config/research/latency_profiles.yaml`.
Missing latency profile = non-promotion-ready (Gate D blocker).
Measure: `place_order`, `update_order`, `cancel_order` at P50/P95/P99.

### MB-07: Precision Law (Cross-Broker)

ALL brokers must scale prices to `int x10000` at the ingestion boundary.
Float prices from broker SDK MUST be converted before entering platform event pipeline.

### MB-08: Credential Isolation

Each broker's credentials use distinct env var prefixes:

- Shioaji: `SHIOAJI_API_KEY`, `SHIOAJI_SECRET_KEY`
- Fubon: `HFT_FUBON_API_KEY`, `HFT_FUBON_PASSWORD`

Never share credential env vars between brokers.

### MB-09: Protocol Conformance Tests

Every new broker adapter MUST include protocol conformance tests.
Test file: `tests/unit/test_<broker>_client.py` with protocol verification.

### MB-10: Fallback Safety

If broker module import fails (SDK not installed), platform MUST:

1. Log error via structlog
2. Refuse to start with clear error message
3. Never silently fall back to a different broker
