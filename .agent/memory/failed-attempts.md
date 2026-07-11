# Failed Attempts (do not re-walk)

Record here: approach, why it failed (evidence), what NOT to retry — the
primary guard against smaller models re-walking dead ends. Also a one-line
index of research KILL verdicts. Do NOT record: trivial first-try errors;
failures caused by a bad packet (fix the packet instead).

## Ops / platform

### `docker compose restart` on the production engine (refuted 2026-06-21 and 2026-06-22)
Immediate restart races the broker's 5-session release → transient 451,
order_client fails, quote facades come up logged-out/unsubscribed while
FeedState=CONNECTED masks it. Never retry restart-in-place. Correct: stop →
wait 60s → start → verify login flags and subscribed_count metrics, NOT
FeedState. (See successful-patterns.md.)

### Plain container restart to clear reduce-only latch (refuted 2026-06-18)
Restart RE-LATCHES (`restored_from_runtime_state`) because the reason is
persisted as non-auto-recoverable. Never restart to clear; use in-container
`hft ops rearm-platform`.

### Auto-rebuilding symbols.yaml in-process (removed 2026-05-23)
Overwrote per-connection shards in pool mode. Operators regenerate offline
after contract rolls (`make rebuild-symbols-yaml`).

## Research (KILL index — evidence in research/experiments/ and reports/)

- T1 menu COMPLETE 2026-06-05: A INCONCLUSIVE; B (vol-compression), C
  (VWAP-trend), D (intraday momentum), E (open-gap fade) all KILL; F
  (expiration V-reversal) NEEDS-MORE-DAYS (structural: 1 event/contract/month).
  None met the >10pt goal; floors were NOT relaxed.
- cd600_trailing_after_mfe KILLED 2026-05-13: D6 20-day result did not
  replicate cross-contract (C6/E6 negative). Single-contract results are not
  evidence.
- R65 lane CLOSED 2026-05-11: stable KILL on TMFD6 sub-5min orderbook-only L1
  cuts; single-day-dominance + cohort-flip mixing artifact, invariant to
  feature source.
- Multifreq fusion CF-1..CF-7 all dead 2026-06-10: state-conditioned screens
  are not first-trigger harvestable.
- Pasted TradingView strategies (2026-06-09, all NOT VALIDATED/KILL): ML-RSI,
  AI-SSMA, LuxAlgo OF-VWAP-dev (`vwap_fade` died under BBO quote-aware
  fills — trade-print backtests overstate taker edges), Liquidity Sweep JOAT
  and Mr. Market Maker (profits were no-stop artifacts; adding the script's
  own stop flips sign). Findings: `reports/liq_sweep_joat_backtest_findings.json`,
  `reports/mr_market_maker_backtest_findings.json`.
- Recurring refutation patterns to check FIRST on any new candidate: tiny
  sample (<80 events), no-stop artifact, single-day/single-contract
  dominance, long-beta-not-alpha, favorable-fill (trade-print) artifact.
