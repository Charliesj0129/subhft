# Production Launch Checklist — TXFD6 + queue_imbalance

Scope: 1 alpha (queue_imbalance, weight=0.1), 1 broker (Shioaji), 1 symbol (TXFD6 Mini-TAIEX futures).

## T-1 Day: Pre-Launch Verification

### Infrastructure
- [ ] ClickHouse running and healthy: `SELECT 1` on port 8123
- [ ] Redis running and healthy: `redis-cli ping`
- [ ] Prometheus scraping targets: check `http://localhost:9091/targets`
- [ ] Grafana dashboards loading: `http://localhost:3000/api/health`
- [ ] Alertmanager configured with production receivers: verify `alertmanager.prod.yml` is mounted
- [ ] WAL directory exists and has sufficient disk space (>5 GB free)
- [ ] Docker containers healthy: `docker compose ps` — all show `Up (healthy)`

### Configuration
- [ ] `config/env/prod/main.yaml` — broker=shioaji, symbols=[TXFD6], mode=live
- [ ] `config/env/prod/strategies.yaml` — queue_imbalance enabled, weight=0.1
- [ ] `config/env/prod/risk.yaml` — max_position_lots=2, daily_loss_limit=50000
- [ ] Promotion YAML valid: `config/strategy_promotions/20260301/queue_imbalance.yaml`
- [ ] YAML syntax valid (run validation commands below)

### Credentials and Secrets
- [ ] `SHIOAJI_API_KEY` set (not empty, not placeholder)
- [ ] `SHIOAJI_SECRET_KEY` set (not empty, not placeholder)
- [ ] `CA_PASSWORD` set for CA certificate activation
- [ ] `SHIOAJI_CA_PATH` points to valid `.pfx` file
- [ ] Alert webhook tokens set: `ALERTMANAGER_TELEGRAM_BOT_TOKEN`, `ALERTMANAGER_LINE_NOTIFY_TOKEN`
- [ ] No secrets committed to git: `git diff --cached -- '*.yaml' '*.yml' | grep -iE 'key|secret|password|token'` returns empty

### Broker Connectivity
- [ ] Shioaji login succeeds in sim mode: `HFT_MODE=sim uv run hft run sim` (brief test)
- [ ] CA certificate activation succeeds
- [ ] TXFD6 contract resolution works: contract object returned for Mini-TAIEX futures
- [ ] Quote subscription for TXFD6 returns data within 5 seconds during market hours

### Risk Limits Verification
- [ ] max_position_lots = 2 (confirmed in risk.yaml)
- [ ] max_order_size = 1 (confirmed in risk.yaml)
- [ ] daily_loss_limit = 50000 NTD (confirmed in risk.yaml)
- [ ] StormGuard halt_threshold = 50000 NTD
- [ ] StormGuard auto_recover = false (manual recovery required)
- [ ] Circuit breaker consecutive_reject_limit = 5
- [ ] Position reconciliation enabled (interval=30s, halt_on_divergence=true)

## T-0: Launch Day

### Pre-Market (before 08:45 TST)

1. **Start infrastructure**
   ```bash
   docker compose up -d clickhouse redis prometheus grafana alertmanager
   docker compose ps  # verify all healthy
   ```

2. **Verify alert routing**
   ```bash
   # Send test alert to verify webhook delivery
   curl -X POST http://localhost:9093/-/reload
   # Check alertmanager status
   curl -s http://localhost:9093/api/v2/status | python3 -m json.tool
   ```

3. **Start engine in dry-run mode first**
   ```bash
   # Dry run: connect to broker, subscribe quotes, but do NOT send orders
   HFT_MODE=sim HFT_ENV=prod uv run hft run sim
   # Verify: quotes arriving, no order errors, metrics publishing
   curl -s http://localhost:9090/metrics | grep hft_
   ```

4. **Switch to live mode**
   ```bash
   # Stop sim run, then start live
   HFT_ENV=prod uv run hft run live
   ```

5. **Verify live startup** (within first 60 seconds)
   - [ ] Feed events arriving: `rate(feed_events_total[30s]) > 0`
   - [ ] LOB engine processing: `lob_process_latency_ns` histogram has data
   - [ ] Strategy running: `strategy_latency_ns` histogram has data
   - [ ] No StormGuard HALT: `stormguard_mode != 3`
   - [ ] Position = 0 at start: confirm flat
   - [ ] Recorder writing: `recorder_insert_batches_total` incrementing

### During Trading Hours (08:45 - 13:45 TST)

**Monitor continuously via Grafana or CLI:**

```bash
# Quick health check
curl -s http://localhost:9090/metrics | grep -E 'stormguard_mode|portfolio_drawdown|order_actions_total'
```

**Escalation thresholds:**
| Metric | Warning | Critical (HALT) |
|--------|---------|-----------------|
| Daily PnL loss | > 30,000 NTD | > 50,000 NTD |
| Position | > 1 lot | > 2 lots |
| Order reject rate | > 5% | > 10% |
| Feed gap | > 3s | > 5s |
| Event loop lag | > 3ms | > 5ms |

### Post-Market (after 13:45 TST)

1. **Verify clean shutdown**
   - [ ] All positions flat (or intentionally held)
   - [ ] No pending orders
   - [ ] WAL fully drained (wal_backlog_files = 0)
   - [ ] ClickHouse data consistent: row counts match expected

2. **Review session metrics**
   ```bash
   # Check daily PnL
   curl -s http://localhost:9090/metrics | grep portfolio_realized_pnl
   # Check fill count
   curl -s http://localhost:9090/metrics | grep fill_events_total
   # Check reject count
   curl -s http://localhost:9090/metrics | grep order_reject_total
   ```

3. **Save session report**
   - Screenshot or export Grafana dashboard for the trading day
   - Record: PnL, fill count, reject count, max position, max drawdown

## Rollback Procedure

If any critical issue occurs during live trading:

1. **Immediate**: StormGuard should auto-HALT on threshold breach
2. **Manual halt**: If StormGuard does not trigger:
   ```bash
   # Stop the engine immediately
   docker compose stop hft-engine
   ```
3. **Cancel open orders**: Verify no pending orders with broker
4. **Flatten positions**: If positions remain, manually close via broker terminal
5. **Investigate**: Review logs, metrics, and alerts before restarting
6. **Post-incident**: Create incident report following `docs/operations/incident-response-protocol.md`

## YAML Validation Commands

```bash
python3 -c "import yaml; yaml.safe_load(open('config/env/prod/main.yaml')); print('main.yaml OK')"
python3 -c "import yaml; yaml.safe_load(open('config/env/prod/strategies.yaml')); print('strategies.yaml OK')"
python3 -c "import yaml; yaml.safe_load(open('config/env/prod/risk.yaml')); print('risk.yaml OK')"
```
