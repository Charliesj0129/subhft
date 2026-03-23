# Live Trading Activation SOP

## Prerequisites

- CA certificate file (`Sinopac.pfx`) in `./certs/`
- `.env` configured with `CA_CERT_PATH`, `CA_PASSWORD`, `SHIOAJI_ACTIVATE_CA=1`, `HFT_ORDER_NO_CA=0`
- Sufficient margin in Shioaji futures account (>50,000 NTD for MXF)
- All CI checks passing on deployed commit
- `config/symbols.yaml` has correct `point_value`/`tick_size`/`price_scale` for target futures

## Stage A: Local CA Activation

```bash
export HFT_MODE=real
export HFT_ORDER_MODE=sim
export SHIOAJI_ACTIVATE_CA=1
export HFT_ORDER_NO_CA=0
export CA_CERT_PATH=./certs/Sinopac.pfx
export CA_PASSWORD=<your_password>

uv run hft run real
```

**Pass:** Log shows `ca_activated=true`
**Fail:** Check cert path, password, person_id. Verify cert not expired.

## Stage B: Local Sim Order Flow

Same config as A. Enable a strategy that generates OrderIntents.

**Pass:** OrderAdapter places sim order → callback → PositionStore updated → reconciliation 0 discrepancies
**Fail:** Debug order path before proceeding

## Stage C: Docker Sim Order Flow

```bash
# Ensure .env has CA vars set (SHIOAJI_ACTIVATE_CA=1, HFT_ORDER_NO_CA=0)
# Ensure certs/Sinopac.pfx exists
docker compose up -d hft-engine
docker compose logs -f hft-engine | grep -i "ca_activated\|order_mode"
```

**Pass:** `ca_activated=true` in logs, sim orders flowing
**Fail:** Check docker-compose env passthrough, cert volume mount

## Stage D: First Live Trade (DAY SESSION ONLY)

**CRITICAL: Only execute during 08:45-13:45 TST**

```bash
# Check margin FIRST
# Via Shioaji API: api.margin(futopt_account) → available_margin > 50,000

# Set live mode in .env:
#   HFT_MODE=real
#   HFT_ORDER_MODE=live
#   HFT_ORDER_NO_CA=0
#   SHIOAJI_ACTIVATE_CA=1
docker compose up -d hft-engine

# Verify safety guard passed and CA activated:
docker compose logs hft-engine | grep -E "LIVE ORDER MODE|ca_activated"
# Expected: "LIVE ORDER MODE ACTIVE" + "ca_activated=true"
```

**Monitoring:** `make monitor-remote` must be active

**Pass criteria:**
1. Order placed → exchange confirms (check logs for `order_placed`)
2. Fill callback received (check logs for `fill_received`)
3. PositionStore updated (check `/status` endpoint)
4. Reconciliation: `api.list_positions(futopt_account)` matches local

**Fail:** Kill switch immediately. Set `HFT_ORDER_MODE=sim`, restart.

**Post-trade:**
- Close position within 5 minutes (avoid overnight exposure during validation)
- Verify ClickHouse: `SELECT * FROM hft.orders WHERE toDate(ts/1e9) = today()`
- Verify WAL clean: `ls .wal/*.jsonl | wc -l` (should be 0 or decreasing)
- Run reconciliation: local PositionStore vs `api.list_positions(futopt_account)`

## Stage E: Multi-Day Soak (3 days)

Run automated strategy with 1-lot MXF, day session only.

**Daily checklist:**
- [ ] Zero position discrepancies at close
- [ ] Zero orphaned fills
- [ ] StormGuard stayed NORMAL (or correctly escalated)
- [ ] ClickHouse recorded all trades
- [ ] daily_loss_limit not breached (or correctly triggered rejection)

**Pass:** 3 clean days → production ready for day session.

## Night Session Activation (after day session validated)

1. Set `HFT_STORMGUARD_FEED_GAP_HALT_S=60` (night session lower liquidity)
2. Run 1-lot MXF during night session (15:00-05:00 TST)
3. Monitor for 3 clean nights before production use

## Contract Rollover

When current month contract expires:
1. Close all positions before settlement day (T-1)
2. Update `config/symbols.yaml` with new month codes (e.g., MXFD6 → MXFE6)
3. Restart engine: `docker compose up -d hft-engine`
4. Verify new contracts load: check logs for contract symbols

## Emergency Procedures

**Immediate HALT:**
```bash
# Option 1: Kill switch file
echo '{"reason": "manual_halt"}' > .runtime/kill_switch

# Option 2: Set sim mode
# In .env: HFT_ORDER_MODE=sim
docker compose restart hft-engine

# Option 3: Stop engine entirely
docker compose stop hft-engine
```

**After HALT:**
1. Check position status: `curl http://localhost:8080/status`
2. Verify no orphaned orders: check broker dashboard
3. Run reconciliation before resuming
