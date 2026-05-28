# C14 — R47 Maker on TXF Rolling Front-Month

Round R6 candidate. Promoted past T2 APPROVE (first of the run).

## What this is

Running the deployed R47 maker strategy on whichever TXF futures
contract is currently front-month. TXF front-month rolls through the
B/C/D/E quarterly cycle; this strategy tracks the rotation rather than
pinning to a fixed symbol.

R47 strategy code at `research/alphas/r47_maker_pivot/` is **not
modified** — this module imports and composes R47's sub-states
(`_PEState`, `_QueueState`, `_MFGState`) into a new
`TxfFrontMonthMaker` that conforms to the `MakerStrategy` protocol.

## Rollover rule

**Volume-crossover with calendar fallback.**

The active front-month on trade day `N` is the TXF contract with the
greatest aggregate traded volume on day `N-1`, restricted to contracts
whose calendar window covers `N`. If no prior-day volume data is
available (e.g. first day of the backtest, or a data gap), fall back
to the calendar rule.

| Contract | Calendar window (from T1 §3.3) |
| -------- | ------------------------------- |
| TXFB6 | 2026-01-26 → 2026-02-25 |
| TXFC6 | 2026-02-26 → 2026-03-18 |
| TXFD6 | 2026-03-19 → 2026-04-14 |

See `frontmonth.py::FrontMonthSelector` for the implementation and
`detect_rollover_days()` to enumerate boundary days.

### Boundary behaviour

On a rollover day: the outgoing contract is flattened at the close of
the previous day, and quoting resumes on the incoming contract from
the session open. No cross-contract carry. The research harness /
backtest driver is responsible for inserting the flattening trade(s)
at the correct price (mid at session close of the outgoing contract)
— this cost is part of the T5 scorecard, per T2 P2 WARN requirement.

### Why volume-crossover (rather than pure calendar)

- Matches empirical market behaviour (see T1 §3.3 where TXFB6/TXFC6/
  TXFD6 rotations were measured on volume crossover dates).
- Calendar rules risk quoting a contract on its illiquid listing day
  or overtrading during a formally-valid-but-practically-dead tail.

Pure calendar fallback is acceptable for research but would need
upgrading to true exchange expiry calendars before production use.

## Parameters

Defaults are the R47 structural-optimum configuration, with `spread_threshold_pts` lowered from 5 to 3 to match TXF's tighter front-month spread distribution.

See `manifest.yaml` for the full list.

## Backtest setup (for T5)

1. Load 56+ days of TXF front-month data from CK covering the Jan-Feb
   TXFB6 window, the Feb-Mar TXFC6 window, and the Mar-Apr TXFD6
   window.
2. Drive `MakerEngine` with `TxfFrontMonthMaker`, calling
   `set_active_symbol(sym)` on every date boundary where `sym`
   changes.
3. Report pooled PnL across the three front-month segments with the
   rollover-day cost included.

## T2 feedback addressed

- P2 WARN (rollover engineering): `frontmonth.py` supplies both a
  volume-based and calendar-based selector, with a `flatten_position()`
  API hook that forces the outgoing position to zero before the switch.
- T2 informational guidance (per-contract PnL reporting): the
  `MakerEngine` already emits per-day `daily_pnl` rows; the T5 driver
  will partition these by active symbol for the per-contract report.

## Files

| File | Purpose |
| ---- | ------- |
| `impl.py` | `TxfFrontMonthMaker` (MakerStrategy) + `C14Alpha` (AlphaProtocol shim) |
| `frontmonth.py` | Front-month selector + rollover detection helpers |
| `manifest.yaml` | AlphaManifest fields + cost model + governance links |
| `tests/test_c14.py` | Unit tests: scaled int, monotonic time, rollover, gap |
| `README.md` | This document |
