---
name: broker-integration
description: "Multi-broker layer: BrokerProtocol, HFT_BROKER selection, Shioaji and Fubon adapters, credentials, latency profiles, switching and failover. Use when editing feed_adapter broker packages, adding a broker, or planning failover. Not for Shioaji SDK API questions or market-data internals."
---

# Broker integration

## Invariants (governance)

- Every broker satisfies the protocols in `feed_adapter/protocol.py`
  (`BrokerClientProtocol`, `BrokerOrderCodec`). Platform code uses the protocol,
  never an SDK. SDK imports exist only under `feed_adapter/<broker>/`
  (`make shioaji-guard`, discipline HFT-A001).
- `OrderAdapter` delegates broker-specific conversion to the broker's order
  translator; `ExecutionNormalizer` uses a broker field map
  (`BrokerExecFieldMap`); no hard-coded broker field names.
- Config: `config/base/brokers/<broker>.yaml` (Do-NOT-Edit without
  instruction). Selection: `HFT_BROKER` (default `shioaji`).
- Each broker declares capabilities, auth, rate limits, and place/update/cancel
  latency P50/P95/P99 in `config/research/latency_profiles.yaml`; a missing
  profile blocks Gate D.
- The ingestion boundary scales prices to platform int x10000.
- Credentials are isolated by prefix: Shioaji `SHIOAJI_*`, Fubon `HFT_FUBON_*`
  (`HFT_FUBON_API_KEY`, `HFT_FUBON_PASSWORD`, `HFT_FUBON_CERT_PATH`,
  `HFT_FUBON_ACCOUNT`). Never print or commit them.
- SDK import failure is fail-closed: log clearly, refuse startup, never silently
  switch brokers.
- Each adapter has protocol conformance tests with the SDK mocked.

## How a broker is wired

`services/bootstrap.py` (`_build_broker_clients`) validates `HFT_BROKER`
against `_VALID_BROKERS` (`shioaji`, `fubon`) and lazily imports the facade:
`ShioajiClientFacade` (default) or `FubonClientFacade`, one facade per role
(quotes and orders are separate sessions). `feed_adapter/broker_registry.py`
exists but nothing calls `register_broker`; adding a broker means extending
bootstrap and `_VALID_BROKERS`. Do not trust older docs that say brokers
auto-register.

Adding a broker: package `feed_adapter/<broker>/` with session, quote, order,
account runtimes and a facade; a field map and order codec; a guarded SDK import;
an optional dependency in `pyproject.toml` (pins need approval); config YAML;
latency profile; conformance tests; update this skill.

## Shioaji

Cap of 5 sessions per account: orders and quotes are separate facades and
sessions, and a restart races the broker's session release (stop, wait 60 s,
start). Adapter pieces live in `feed_adapter/shioaji/` (session, quote,
reconnect orchestrator, tick dispatcher, order gateway). SDK behavior and
version differences: `docs/runbooks/shioaji-version-diff.md`; contract refresh:
`docs/runbooks/shioaji-contract-refresh-operations.md`.

## Switching and failover (manual, never automatic)

1. Detect: quote stream stale, repeated order failures, login exhausted.
2. Flatten or account for positions: `uv run hft ops flatten --scope all`
   (options `--scope {all,strategy,track}`, `--deadline`).
3. Stop the engine (local: `make stop`; production: stop -> 60 s -> start per
   `41-deployment.md`; switching the live broker is a user-approved cutover).
4. Set `HFT_BROKER`, verify credentials exist (names only, never values),
   `uv run hft config validate`, `uv run hft config sync` for the new broker's symbols.
5. Confirm the target's latency profile and symbol overlap, then start in sim
   first. Verify quote stream and an order round trip afterwards.

Dual-broker routing is not implemented.

## Verify

```bash
make test-file FILE=tests/unit/<adapter conformance test>
make shioaji-guard
make dependency-boundary
```

## Done when

Conformance tests pass for the touched adapter with and without the SDK
importable, `make shioaji-guard` is clean, and no credential or SDK type leaked
across the boundary.

## References

- `references/fubon.md` — read when working on `feed_adapter/fubon/`.
