---
name: multi-broker-ops
description: Use when setting up multi-broker support, switching between brokers (Shioaji/Fubon), configuring broker-specific credentials, or diagnosing broker failover and routing issues.
---

# Multi-Broker Operations

## When to Use This Skill

- Starting multi-broker setup for the first time
- Switching the active broker between Shioaji and Fubon
- Configuring broker-specific credentials and latency profiles
- Diagnosing broker-specific connection or order issues
- Planning dual-broker or failover architecture

---

## Broker Selection

The active broker is controlled by the `HFT_BROKER` environment variable:

```bash
# In .env or shell
export HFT_BROKER=shioaji   # default — 永豐金證券
export HFT_BROKER=fubon      # 富邦證券 TradeAPI
```

If `HFT_BROKER` is unset, the platform defaults to `shioaji`.

---

## Credential Management

Each broker has its own set of environment variables. Never mix credentials across brokers.

### Shioaji

```bash
SHIOAJI_API_KEY=<api_key>
SHIOAJI_SECRET_KEY=<secret_key>
# Optional:
HFT_SHIOAJI_SKIP_CERT=0       # 1 = skip CA cert (non-prod only)
HFT_QUOTE_VERSION=auto         # v1 to lock schema
```

### Fubon

```bash
HFT_FUBON_API_KEY=<api_key>
HFT_FUBON_PASSWORD=<password>
# Optional:
HFT_FUBON_WS_URL=<ws_endpoint>
HFT_FUBON_REST_URL=<rest_endpoint>
```

### Security Rules

- Store credentials in `.env` (git-ignored) or a secrets manager — never in source code
- Each broker's credentials are independent; changing `HFT_BROKER` does not invalidate the other set
- Rotate credentials immediately if any `.env` file is committed to version control

---

## Config Files

Per-broker configuration lives at:

```
config/base/brokers/
  shioaji.yaml    # Shioaji-specific settings (timeouts, reconnect, quote version)
  fubon.yaml      # Fubon-specific settings (WS ping interval, REST timeouts)
```

The config loader merges the broker-specific file based on `HFT_BROKER`:

```
config/base/main.yaml
  -> config/base/brokers/{HFT_BROKER}.yaml   (broker overlay)
  -> config/env/{HFT_MODE}/main.yaml          (env overlay)
  -> environment variables
  -> CLI overrides
```

---

## Switching Brokers

### Step-by-Step Procedure

```bash
# 1. Stop the running engine
uv run hft run stop
# or: make stop (if Docker-based)

# 2. Switch the broker env var
export HFT_BROKER=fubon  # or shioaji

# 3. Verify credentials are set
env | grep HFT_FUBON     # should show API_KEY and PASSWORD
# For shioaji: env | grep SHIOAJI

# 4. Validate config resolves correctly
uv run hft config preview
uv run hft config validate

# 5. Sync symbol contracts for the new broker
make sync-symbols

# 6. Restart
uv run hft run sim
# or: make start
```

### Pre-Switch Checklist

- [ ] All open positions are flat or accounted for
- [ ] Credentials for target broker are set and tested
- [ ] Broker-specific config file exists at `config/base/brokers/{broker}.yaml`
- [ ] Latency profile for target broker exists in `config/research/latency_profiles.yaml`
- [ ] Symbol list is compatible (some symbols may not be available on all brokers)

---

## Latency Profiles

Each broker MUST have a corresponding entry in `config/research/latency_profiles.yaml`. Without a latency profile, research/backtest promotion is blocked (non-promotion-ready).

```yaml
# config/research/latency_profiles.yaml
shioaji_sim_p95_v2026-03-04:
  broker: shioaji
  submit_latency_ms: 36.0
  modify_latency_ms: 38.0
  cancel_latency_ms: 35.0
  source: "measured sim API RTT P95"

fubon_sim_p95_v2026-03-11:
  broker: fubon
  submit_latency_ms: 45.0    # measure and update with real values
  modify_latency_ms: 48.0
  cancel_latency_ms: 42.0
  source: "estimated — requires measurement via shadow session"
```

### Measuring Latency for a New Broker

1. Run shadow session: `uv run hft run sim --shadow --broker fubon`
2. Collect RTT samples from order round-trips (minimum 1000 samples)
3. Compute P50, P95, P99 from samples
4. Add profile entry with P95 values and date stamp
5. Use P99 for stress test scenarios

---

## Monitoring

### Broker-Specific Health Checks

Each broker adapter exposes a health check endpoint used by the supervision system:

| Check | Shioaji | Fubon |
|-------|---------|-------|
| Session alive | `SessionRuntime.is_alive()` | Token validity check |
| Quote stream | `QuoteRuntime.watchdog` (stale tick detection) | WebSocket ping/pong status |
| Order gateway | Test order submission (sim mode) | REST health endpoint |

### Prometheus Metrics Labels

All broker-related metrics include a `broker` label for dashboard filtering:

```
hft_order_latency_seconds{broker="shioaji", side="buy"}
hft_order_latency_seconds{broker="fubon", side="buy"}
hft_quote_staleness_seconds{broker="fubon", symbol="2330"}
hft_broker_reconnect_total{broker="shioaji"}
```

---

## Failover Procedure (Manual)

When the active broker becomes unavailable:

```bash
# 1. Detect: monitor alerts or manual observation
#    - Quote stream stale > 60s
#    - Order submission failures > 3 consecutive
#    - Session login fails after max retries

# 2. Flatten positions (if possible)
uv run hft positions flatten --broker shioaji

# 3. Stop engine
uv run hft run stop

# 4. Switch broker
export HFT_BROKER=fubon

# 5. Verify and restart
uv run hft config validate
uv run hft run sim   # or 'live' if production failover
```

### Failover Checklist

- [ ] Positions flattened or hedged on the failing broker
- [ ] Target broker credentials verified (test login)
- [ ] Symbol overlap confirmed (not all symbols available on all brokers)
- [ ] Latency profile for target broker is loaded
- [ ] Alerting team notified of broker switch
- [ ] Post-failover: verify quote stream is live, test order round-trip

---

## Dual-Broker Mode (Future)

Planned capability to route different symbols or strategies to different brokers simultaneously.

### Design Sketch

```
StrategyRunner
  ├── Symbol "2330" → FubonAdapter   (lower latency for this symbol)
  └── Symbol "TX00" → ShioajiAdapter (futures on Shioaji)
```

### Requirements (Not Yet Implemented)

- Per-symbol broker assignment in `config/symbols.yaml`
- Unified position tracker aggregating across brokers
- Cross-broker risk engine (aggregate exposure)
- Separate order adapters running concurrently
- Consolidated fill reconciliation

---

## Troubleshooting

| Problem | Likely Cause | Action |
|---------|-------------|--------|
| Config validation fails after switch | Missing broker YAML | Create `config/base/brokers/{broker}.yaml` |
| Orders rejected on new broker | Credentials or permissions | Verify API key permissions; check broker agreement |
| Sharpe drops after broker switch | Different latency profile | Re-run Gate C with correct latency profile |
| Symbol not found | Not available on target broker | Check broker symbol list; update `config/symbols.yaml` |
| Quote data format mismatch | Different normalizer expected | Ensure `HFT_BROKER` selects correct normalizer path |

---

## Cross-References

| Related Skill | When to Use |
|---------------|-------------|
| `shioaji-contracts` | Shioaji session lifecycle, contract sync, quote watchdog |
| `fubon-tradeapi` | Fubon API reference, SDK usage, authentication |
| `hft-architect` | Reviewing multi-broker architecture decisions |
| `hft-strategy-dev` | Wiring broker adapters into strategy runner |
| `symbols-sync` | Symbol list management across brokers |
| `troubleshoot-metrics` | Broker-specific Prometheus metric debugging |
