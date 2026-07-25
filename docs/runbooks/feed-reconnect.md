# Runbook: Feed Reconnect

## Trigger

- Prometheus alert `FeedGapCritical` fires (no tick data for > `HFT_QUOTE_NO_DATA_S` seconds).
- StormGuard transitions to STORM due to feed gap >= `HFT_STORMGUARD_FEED_GAP_HALT_S` (default 1.0s).
- Shioaji watchdog thread detects stale quotes (`HFT_QUOTE_WATCHDOG_S`, default 5s).
- Manual operator observation of zero ingest rate on Grafana dashboard.

## Impact

- **Market data pipeline halted**: No tick/bidask events flow to strategies.
- **StormGuard escalation**: Feed gap triggers STORM state; new orders are blocked.
- **Recording gap**: ClickHouse `market_data` table has a hole for the outage period.
- **Strategies go stale**: Alpha signals computed on stale LOB data produce no new intents.

## Diagnosis

### 1. Confirm feed is actually down

```bash
# Check engine logs for quote watchdog or reconnect messages
docker compose logs --tail=100 hft-engine | grep -E "watchdog|reconnect|no_data|feed_gap"

# Check Prometheus metrics
curl -s http://localhost:9090/metrics | grep -E "feed_reconnect_total|feed_gap|quote_watchdog"

# Check StormGuard state (0=NORMAL, 1=WARM, 2=STORM, 3=HALT)
curl -s http://localhost:9090/metrics | grep stormguard_mode
```

### 2. Check Shioaji API status

```bash
# Look for Shioaji SDK errors
docker compose logs --tail=200 hft-engine | grep -iE "shioaji|sj\.|ConnectionError|SSL|timeout"

# Check if login is still valid
docker compose logs --tail=50 hft-engine | grep -E "logged_in|login_failed|login_error"
```

### 3. Check network connectivity

```bash
# From inside the container
docker exec hft-engine wget --spider --timeout=5 https://api.sinopac.com 2>&1 || echo "UNREACHABLE"
```

## Resolution

### Automatic recovery (default behavior)

The `ReconnectOrchestrator` handles automatic reconnect with exponential backoff:

- Cooldown: `HFT_RECONNECT_COOLDOWN` (default 30s)
- Backoff: starts at `HFT_RECONNECT_BACKOFF_S` (default 30s), doubles up to `HFT_RECONNECT_BACKOFF_MAX_S`
- Sequence: logout -> login -> register callbacks -> subscribe basket
- Flap protection: `HFT_QUOTE_FLAP_THRESHOLD` (default 5) reconnects within `HFT_QUOTE_FLAP_WINDOW_S` (default 60s) triggers cooldown of `HFT_QUOTE_FLAP_COOLDOWN_S` (default 300s)

Wait 2-3 minutes for automatic recovery. Monitor:

```bash
docker compose logs -f hft-engine | grep -E "reconnect|Reconnecting|subscribe_basket"
```

### Manual reconnect (if automatic fails)

```bash
# Restart the engine container (preserves WAL, triggers fresh login)
docker compose restart hft-engine

# If login keeps failing, check credentials
docker exec hft-engine env | grep SHIOAJI_
```

### Scheduled reconnect

For known Shioaji session expiry, configure scheduled reconnects:

```bash
# In .env — reconnect at 08:30 and 12:30 Taipei time
HFT_RECONNECT_HOURS=8:30
HFT_RECONNECT_HOURS_2=12:30
HFT_RECONNECT_TZ=Asia/Taipei
```

### Nuclear option: full restart

```bash
docker compose down hft-engine
docker compose up -d hft-engine
```

### `code: 451, detail: Too Many Connections` during session refresh

Symptom, seen on THESHOW 2026-07-24/25: several facades log
`Session refresh: logging out` within a few hundred milliseconds of each other,
then one or more report

```
login: ... code: 451, detail: Too Many Connections.
Login retries exhausted
Session refresh failed: login unsuccessful
```

Cause: every quote facade runs its own session-refresh thread, and the pool
starts them together, so their fixed `HFT_SESSION_REFRESH_CHECK_S` sleeps stay
phase-aligned forever. The broker counts concurrent connections and rejects the
losers. On a holiday/weekend the holiday-aware branch refreshes hourly, so this
repeats every hour.

Fixed 2026-07-25 by two independent defences:

- **Jitter** — each facade's wake-up is scaled by ±`HFT_SESSION_REFRESH_JITTER_FRAC`
  (default `0.15`), so arrivals spread out instead of staying in lockstep.
- **Login slot** — a process-wide slot serialises the logout→login window and
  spaces consecutive logins by `HFT_SESSION_REFRESH_STAGGER_S` (default `5`
  seconds). If it cannot be taken within `HFT_SESSION_REFRESH_STAGGER_TIMEOUT_S`
  (default `120`), the refresh proceeds anyway and
  `shioaji_login_slot_timeouts_total` increments — a stale session is a worse
  failure than a 451 the retry path already recognises.

Check whether the fix is deployed:

```bash
curl -s localhost:9090/metrics | grep shioaji_login_slot_timeouts_total
# absent  -> engine predates the fix
# present -> deployed; a rising counter means the slot is contended for >120s,
#            which points at a genuinely slow login, not at staggering
docker compose logs hft-engine --since 24h | grep 'Staggering broker login'
```

The slot only spans a single process. Two engine containers sharing one broker
account can still collide — that is what `shioaji_session_lock_conflicts_total`
and the `.state` session lock are for.

### A facade stays logged out for hours after one failed refresh

Symptom: one `conn_id` sits at `hft_quote_conn_logged_in=0` and its
`hft_quote_conn_last_data_age_s` climbs without bound, while
`FeedState` still reports `CONNECTED` and no alert fires.

Cause (root-caused on THESHOW 2026-07-25, one facade dark for 24 h across a
whole night session): `_refresh_loop` had `c.logged_in` in its `while`
condition. A refresh logs out and then logs back in, so a login failure — the
451 above — leaves `logged_in` False, the loop condition goes false, and the
**thread exits permanently**. `do_session_refresh()`'s return value was
discarded, so nothing noticed. Recovery then depended entirely on the reconnect
orchestrator's schedule (`HFT_RECONNECT_DAYS`, `HFT_RECONNECT_HOURS*`); outside
those windows — weekends, and the Friday night session that runs past midnight
into Saturday — the facade was simply abandoned until the next process restart.

Why nothing alerted:

| Signal | Why it missed this |
|---|---|
| `FeedGapCritical` | `rate(feed_events_total)` is pool-wide; the other 3 facades kept it non-zero |
| `hft_quote_conn_subscribed_count` | reports the *configured* symbol count, so a dead facade still shows 74 |
| `ShioajiLoginFailuresDetected` | `increase(...[10m])`, so it self-resolved 10 min in while the outage ran 24 h |
| `shioaji_thread_alive{thread="session_refresh"}` | went to 0 correctly — but had no alert on it |

Fixed 2026-07-26: the loop no longer treats `logged_in` as a termination
condition and re-drives `do_session_refresh()` (which restores login,
callbacks, subscriptions and the watchdog) on each hourly wake-up until the
facade is back, counting recoveries as `session_refresh_total{result="recovered"}`.
Retry cadence is the hourly check interval and every attempt still takes the
login slot, so recovery cannot re-create the storm. Alerts
`QuoteFacadeDataStalled` and `ShioajiSessionRefreshThreadDown` now cover the
gap — both verified against this incident's data.

```bash
# is the fix deployed?
curl -s localhost:9090/metrics | grep 'session_refresh_total.*recovered'
docker compose logs hft-engine --since 24h | grep -E 'found facade logged out|Facade recovered'
```

## Rollback

No configuration rollback needed. If reconnect parameters were changed:

```bash
# Revert .env changes and restart
docker compose restart hft-engine
```

## Post-Incident

1. **Check recording gap**:
   ```bash
   docker exec clickhouse clickhouse-client \
     --password "${CLICKHOUSE_PASSWORD}" \
     --query "SELECT toStartOfMinute(toDateTime64(exch_ts/1e9, 3)) AS minute, count()
              FROM hft.market_data
              WHERE toDate(exch_ts/1e9) = today()
              GROUP BY minute ORDER BY minute"
   ```

2. **Verify WAL files were written during outage** (recorder continues if engine was up):
   ```bash
   ls -lt .wal/*.jsonl | head -10
   ```

3. **Check reconnect metrics for patterns**:
   ```bash
   curl -s http://localhost:9090/metrics | grep feed_reconnect
   ```

4. **Update monitoring thresholds** if false positives occurred.

5. **File incident report** if outage lasted > 5 minutes during trading hours.
