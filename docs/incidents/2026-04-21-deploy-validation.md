# R47 Fix — Deployment & Validation Runbook

Commits to deploy:
- `dc7d23f6` fix(r47): stale-quote reconciler + cooldown + inflight tracking + metrics
- `930be1e4` feat(backtest): LatencyProfile injection for MakerEngine (D5)

Full context: `docs/incidents/2026-04-21-r47-backtest-live-divergence.md`.

## Pre-deploy (local)

Already verified on main:
- 127 R47-related tests pass (incl. 16 new)
- 26 MakerEngine tests pass
- Strategy smoke-loads with new config (stale_quote_max_scaled=30000, cooldown=1000 ms, metric refs OK)
- Full unit suite: 12147 pass / 13 pre-existing failures unrelated

## Remote deploy (manual — per `feedback_no_auto_deploy`)

On the local dev machine (this one):

```bash
git push origin main
```

On the remote trading host (`charl@100.91.176.126`, dir `~/subhft`):

```bash
cd ~/subhft
git fetch origin
git log HEAD..origin/main --oneline | head   # confirm dc7d23f6 + 930be1e4 present
git pull --ff-only origin main
make test                                      # local sanity — should pass
make stop                                      # halt current hft-engine
make start                                     # relaunch with new config
docker compose logs -f hft-engine | head -100  # watch startup
```

Confirm in startup log:
```
R47MakerStrategy initialized ... spread_pts=5
R47_MAKER_TMF ... stale_quote_max_scaled=30000 ... _QUOTE_COOLDOWN_NS=1000000000
```

## Validation — first 30 minutes after market open

### A. Metrics (Prometheus at `:9090/metrics`)

```bash
curl -s http://localhost:9090/metrics | grep -E 'strategy_stale_cancels|strategy_inflight_oids|cancel_already_terminal'
```

Expected:
- `hft_strategy_stale_cancels_total{strategy="R47_MAKER_TMF",side="BUY"}` — **non-zero within first minute** (confirms reconciler runs under gate block)
- `hft_strategy_inflight_oids{strategy="R47_MAKER_TMF",side="BUY"}` — **≤ 1 in steady state** (cooldown 1000 ms eliminates stacking)
- `hft_order_cancel_already_terminal_total` — rate should be **< 1/min** (down from ~23/hr today)

### B. ClickHouse order lifetime

```sql
-- Order lifetime p95/max for today — should drop from 7s/41s to <500ms/<2s
WITH subs AS (
  SELECT order_id, min(ingest_ts) AS sub_ts, side
  FROM hft.orders WHERE toDate(ingest_ts/1e9)=today()
    AND strategy_id='R47_MAKER_TMF' AND status='SUBMITTED'
  GROUP BY order_id, side
), terms AS (
  SELECT order_id, min(ingest_ts) AS term_ts
  FROM hft.orders WHERE toDate(ingest_ts/1e9)=today()
    AND strategy_id='R47_MAKER_TMF' AND status IN ('CANCELLED','FAILED','FILLED')
  GROUP BY order_id
)
SELECT side, count() AS n,
       round(quantile(0.5)((t.term_ts-s.sub_ts)/1e6),0) AS p50_ms,
       round(quantile(0.95)((t.term_ts-s.sub_ts)/1e6),0) AS p95_ms,
       round(max((t.term_ts-s.sub_ts)/1e6),0) AS max_ms
FROM subs s INNER JOIN terms t USING (order_id)
GROUP BY side;
```

Pre-fix baseline (2026-04-21):
- BUY p50=315, p95=7057, max=41323
- SELL p50=460, p95=32362, max=78736

Target post-fix:
- Any side p95 < 1500 ms (~2× cooldown)
- Any side max < 5000 ms

### C. PnL smoke

After 30 min of live quoting:

```sql
-- Net fills + realized PnL (pts, NTD)
SELECT count() AS fills,
       round(sum(if(side='SELL', price_scaled, -price_scaled))/1e6, 2) AS gross_pts,
       round(sum(if(side='SELL', price_scaled, -price_scaled))/1e5, 0) AS gross_ntd
FROM hft.fills WHERE toDate(ts_local/1e9)=today() AND strategy_id='R47_MAKER_TMF';
```

Expected: flat-to-positive. A bad day (continued directional drift) can still go negative; the success criterion is **no repeat of 41-second stale orders**, not absolute profit.

## Rollback (if anything looks wrong)

```bash
cd ~/subhft
git reset --hard 261b4435       # pre-fix HEAD (261b4435 'prior-session work on Bugs #29-#32')
make stop && make start
```

Memory retains the baseline since lifetimes have been measured; any post-rollback degradation is directly comparable.

## Follow-up if fix validates

1. Consider raising `stale_quote_max_ticks` from 3 → 5 after 1 week of clean data
   (3 is conservative; 5 matches 5-pt gate threshold for symmetry).
2. Schedule D3b (full intent-id contract change) as a proper cross-cutting
   refactor — cleaner than the current `_active_*_oid` + `_inflight_*_oids`
   dual tracking, and necessary if other strategies face similar issues.
3. Use D5 `LatencyProfile.shioaji_p95()` on R47 replay of 2026-04-21
   vs the fixed strategy: expect replay to show post-fix PnL ≥ 0.
