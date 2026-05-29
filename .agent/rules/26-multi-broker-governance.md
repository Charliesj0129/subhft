# Multi-Broker Governance

- All brokers satisfy `BrokerProtocol`; platform code uses the protocol, not SDKs.
- Broker SDK imports are allowed only under `feed_adapter/<broker>/`.
- `OrderAdapter` delegates broker-specific conversion to `BrokerOrderTranslator`.
- `ExecutionNormalizer` uses `BrokerExecFieldMap`; no hardcoded broker field names.
- Config lives in `config/base/brokers/<broker>.yaml`; selection via `HFT_BROKER` default `shioaji`.
- Each broker declares capabilities, auth, rate limits, and latency P50/P95/P99 for place/update/cancel in `config/research/latency_profiles.yaml`; missing profile blocks Gate D.
- Ingestion boundary scales prices to platform int x10000.
- Credentials are isolated by prefix: Shioaji `SHIOAJI_*`, Fubon `HFT_FUBON_*`.
- Each adapter has protocol conformance tests.
- SDK import failure is fail-closed: log clear error and refuse startup; never silently switch brokers.
