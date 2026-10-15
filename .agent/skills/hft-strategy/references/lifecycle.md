# Alpha-to-production lifecycle

Read when scaffolding, shadow-running, or preparing a promotion. Everything
that reaches live is user-driven and subject to the loop_v1 L11 freeze.

1. **Research and scaffold.** `make research-scaffold ALPHA=r<N>_<name>` creates
   `research/alphas/<id>/` (manifest, signal, tests). Float is fine in research
   only. Validate on the most recent data first; a monotonically increasing IC
   is trend contamination. Use `hft-alpha-research`.
2. **Gates A-C.** `make research ALPHA=<id> OWNER=<you> DATA=<path>` or
   `uv run hft alpha validate`. A: manifest, data fields, complexity. B: unit
   tests. C: backtest and scorecard with a latency profile (see `validation-gate`).
3. **Runtime strategy.** `src/hft_platform/strategies/<id>.py`, built per `sdk.md`.
4. **Config.** Register in the live registry only when approved; set limits in
   `config/base/strategy_limits.yaml` (start at 1 lot).
5. **Shadow in sim.** `HFT_ORDER_SHADOW_MODE=1` and `HFT_ORDER_MODE=sim`; run
   `uv run hft run sim` (or `make start-engine`); watch `make logs` and
   `make callback-latency-report`. Evaluate fill rate, slippage, max position,
   zero HALT events against the thresholds in the gate runbook.
6. **Gates D-E.** `hft alpha promote --alpha-id <id> --owner <you> ...` writes the
   canary config; E needs enough shadow sessions (`min_shadow_sessions`).
7. **Canary.** `hft alpha canary status|evaluate`, `make canary-snapshot`,
   `make canary-evaluate`. `enable`, `graduate`, and `rollback` subcommands do not exist.
8. **Live.** Only on the user's explicit instruction: shadow reviewed, `max_pos`
   1 on day one, latency profile documented (Gate D blocker), `make pre-market-check`
   green, and the activation SOP `docs/runbooks/live-trading-activation-sop.md`.
   Rollback is a config change plus a controlled stop/start per `41-deployment.md`.

Anti-patterns: skipping shadow; `max_pos` above 1 on the first live day; going
live without a latency profile; treating backtest PnL as live PnL without
modeling broker RTT (P95 at minimum).
