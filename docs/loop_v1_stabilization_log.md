# Loop_v1 Stabilization Log

> **Append-only.** Every trading day in stabilization gets one entry. Charter amendments and incident postmortems append to dedicated sections at the bottom.

## How to use this log

- One entry per trading day, in chronological order.
- Use the schema below — do not freeform.
- Numbers come from `outputs/replay/<session>/report.json` and `hft.order_explanations`.
- Mark phase transitions with a `## Phase Transition: <date>` heading inline with the daily entries.

## Daily entry schema

```markdown
### YYYY-MM-DD — Phase: <sim|shadow|live> — Day N/<phase_total>

- **match_pct**: <float> (threshold: 99 sim / 95 shadow / 95 live)
- **eligibility**: <eligible|pre_recorder|no_fixture|strategy_unbuildable>
- **n_live_intents**: <int>
- **n_replayed_intents**: <int>
- **divergence_count**: <int>
- **first_divergence_idx**: <int|null>
- **net_pnl_ntd**: <float> (broker-confirmed cost; null in sim/shadow)
- **drawdown_ntd**: <float>
- **turnover_rt**: <int> (round-trips)
- **fill_rate**: <float> (fills/intents)
- **engine_restarts**: <int>
- **incidents**: <P0_count>/<P1_count>/<P2_count>/<P3_count>
- **notes**: free-text, one paragraph max
```

## Stabilization clock

Track here when each phase starts/ends. Update inline on transition.

| Phase  | Start date  | End date    | Days   | Status      |
|--------|-------------|-------------|--------|-------------|
| Sim    | _pending_   | _pending_   | 0/5    | not started |
| Shadow | _pending_   | _pending_   | 0/10   | not started |
| Live   | _pending_   | _pending_   | 0/30   | not started |

## Daily Entries

<!-- Append daily entries here, newest at the bottom. -->

_No entries yet. Stabilization clock will start when the operator runs the first daily-replay-diff job and adds an entry._

## Incidents

<!-- Per-incident postmortems. Each has: ID, severity, date, description, root cause, fix, follow-ups. -->

_No incidents recorded._

## Charter Amendments

<!-- Per-amendment: date, PR link, why, what changed, who approved. -->

_No amendments. Charter is at v1._

## Final Disposition

<!-- Filled only at end of stabilization (success or rollback). One section. -->

_Stabilization in progress. Final disposition will be recorded here when the Live phase exit criteria are met or stabilization is abandoned._
