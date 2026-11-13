# Fill identity, stale live orders and phantom claims (design, 2026-11-13)

Status: DESIGN, not implemented. Owner decision (2026-11-13): design first, then
a Tier-3 packet per item. Found while root-causing the production
`OrderOutcomeUnresolved` pages (branch `fix/telegram-alert-noise-20261113`).
Line numbers are for `origin/main` 062293d3e.

Priority: **E first** (it drops real fills), then C, then D.

## E. Real fills are dropped as "duplicates" at the day-session open (P0)

Evidence (THESHOW, read-only, engine run since 2026-11-11 21:50Z):

- `duplicate_fill_skipped` fired 5 times, all within 5 minutes of the day
  open (11-12 00:45:09Z, 00:50:06Z; 11-13 00:49:01Z, 00:49:20Z, 00:50:13Z).
- Each skipped `fill_id` exists in `hft.fills` as a fill of a *different*
  order from the previous night session, hours earlier:

  | skipped at (UTC) | fill_id | stored fill (order, side, Taipei time) |
  |---|---|---|
  | 11-12 00:45:09 | 0688B3 | R47_MAKER_TMF:6401 SELL, 11-12 03:2x |
  | 11-12 00:50:06 | 068A87 | R47_MAKER_TMF:6955 SELL, 11-12 04:xx |
  | 11-13 00:49:01 | 06FDA8 | R47_MAKER_TMF:33863 BUY, 11-13 03:xx |
  | 11-13 00:49:20 | 06FDC4 | R47_MAKER_TMF:33904 SELL, 11-13 03:xx |
  | 11-13 00:50:13 | 06FE0D | R47_MAKER_TMF:33978 BUY, 11-13 04:02 |

- The broker's id range moves backwards at the day open: per Taipei hour,
  `hft.fills` fill ids (hex -> int) were 458210-459039 at 11-13 04h and
  457958-458813 at 11-13 08h (same shape on 11-12: 428523-428836 at 04h,
  428189-429163 at 08h). The day session reissues ids the night session
  already used.
- The normalizer uses the deal's `seqno` as `fill_id` (`normalizer.py:377`);
  the router dedups on it with a 10,000-entry window persisted across
  restarts (`router.py:572`, `.state/fill_dedup_window.jsonl`). A reissued
  `seqno` therefore drops a real deal.
- Two of the five coincide with `cancel_gave_up` keys (118 at 00:45:09,
  36179 at 00:50:13): the order had filled, the deal was dropped, so no
  terminal ever arrived and the cancel "gave up". This is the root cause of
  the stall-free `cancel_gave_up` / `live_order_ttl_release` pages at the
  day open (not of 11-12 13:09Z key 19428, which had no skipped deal).
- Effect: R47's platform position and fill cash flow omit real deals; in
  sim mode broker positions are `{}`, so reconciliation cannot see it.
  Whether the live (non-paper) server reissues `seqno` the same way is
  NOT VERIFIED.

Proposed fix: dedup on the deal's identity, not the order-scoped `seqno`.

- Key = `exchange_seq` when present (the per-deal report sequence), else
  `ordno|seqno|ts|qty|price_scaled`. Check which fields the 1.7.11
  FuturesDeal payload actually carries before choosing (SDK stub:
  `tests/unit/test_shioaji_sdk_payload_boundary.py:158`; golden:
  `make shioaji-guard`).
- Migrate the persisted window: entries written with the old key must not
  collide with new keys (prefix the new key, e.g. `v2:`), and the old
  file is read once and discarded.
- Keep the defence the dedup was added for (genuine duplicate delivery,
  memory `feed_duplicate_delivery_2026`): a byte-identical redelivery must
  still be skipped.
- Tests: replay of the 11-13 00:50:13 case (night fill 06FE0D for order A,
  day deal with seqno 06FE0D for order B -> B's fill booked); genuine
  redelivery of the same deal -> skipped once; restart with an old-format
  window file.
- Files: `execution/normalizer.py` (~330-446), `execution/router.py`
  (~560-723). Tier 3; protocol conformance + `make shioaji-guard`.

## C. A stale live order is released without asking the broker

Current: `check_unconfirmed_cancels` (adapter.py:2874) retries a cancel once
at +60 s and forgets it at +120 s ("gave_up"); `sweep_stale_live_orders`
(974-1114) then releases any key older than 300 s and sends
`live_order_ttl_expired` feedback, freeing the strategy slot while the order
may still be working (2026-09-03..10-03: 4 such orders filled 5.6-27 min
later, 3 took the position to +2). After E is fixed, part of today's
gave-ups disappear (they were filled orders), so C covers the rest.

Proposed:

```
sweep: stale keys -> split
  A: no broker ids / client has no lister -> release (as today)
  B: real Trade with broker ids + lister  -> hold in live_orders,
     mark verifying, one shared lookup task (as the #708 phantom one)
verify (one list call, off loop, 15 s timeout):
  listed open, ids match exactly -> re-cancel, keep slot, new TTL
  not listed                     -> release (reason ..._verified_gone)
  lookup failed                  -> keep held; after +1800 s release
                                    with ERROR + metric (page)
```

Decisions (recommended): scope = every stale key with broker ids (one list
call per sweep regardless of count); fail closed for at most 1800 s past
the TTL (same as `_phantom_lookup_max_hold_s`), then release with an
`live_order_released_unverified` ERROR — not HALT (a lookup outage is not a
risk event); no synthesised fill on "filled" (listing returns open orders
only; a missed deal is E's problem); re-cancel cap 3 per key; HALT allows the
re-cancel; no exposure change (gateway exposure is never released on
fill/cancel). Merge the stale-verify and phantom lookups into one broker
snapshot task. Matching uses the ids `_register_broker_ids` collects
(3174-3211) via a shared `_broker_ids_of(trade)` helper with the
`_trade_bound_elsewhere` recycled-id guard.

Metrics: `live_order_stale_verify_total{outcome=found_open|gone|
lookup_failed|ambiguous|terminal_during_verify|released_unverified}`, gauge
`live_orders_held_for_verify`.

Tests: replay of key 36179 (place, ids +30 ms, cancel ok, no terminal,
retried +64 s, gave_up +125 s, sweep +303 s) with the lister returning it
open (held + re-cancel) and raising TimeoutError (held to +2100 s); terminal
or fill during verification releases exactly once; listed trade bound to
another key is not a match; reconnect invalidation clears the verify set.

## D. A late fill is claimed by a phantom by symbol+side, not identity

Current: `resolve_phantom_fill` (2053-2193) gives an unknown-`ordno` fill to
the oldest fresh phantom with the same symbol and side, pops the record and
notes the fill in its ledger; when that phantom's real Trade arrives later,
`_bind_late_trade` (1857-1919) sees it "filled" and skips the cancel. On
11-12 03:34:03.691Z phantom 6816 BUY claimed a fill although 6816's broker
`order_datetime` was 03:34:11.946Z; R47 sent 6818 and reached +2 (max_pos 1)
at 03:34:12.104Z for ~16 s. #717 added the side match, not identity.

A phantom has no broker ids by definition, so a claim can only be confirmed
later. Proposed:

```
resolve_phantom_fill: unbound phantom, same sym+side
  -> book the position to the strategy (1-strategy account),
     record TENTATIVE (ordno X, qty, ts); keep the phantom record
phantom's Trade arrives (late result or #708 lookup):
  ids contain X                 -> confirm (ledger note, count
                                   fill_claimed_after_expiry here)
  ids lack X, or order_datetime
  later than the fill ts, or
  fill had no ordno             -> misattributed: bind, track and
                                   CANCEL the working order, CRITICAL
TTL with no Trade               -> release as today, log unverified
```

Core of the fix: never skip the cancel of a working order because of an
unconfirmed claim. R47's `on_fill` still frees one same-side slot on any
fill attributed to it (`r47_maker.py:617-629`); making that order-keyed is
a strategy change, out of scope. Identity-only attribution (fill parked in
the DLQ until confirmed) is the alternative if the account will ever host
more than one strategy.

Tests: replay of the 11-12 03:34 timeline (tentative claim keeps 6816's
record; the late Trade with a different ordno triggers a cancel of 6816 and
`claim_misattributed`, no `skipped_filled`); expired-ledger claim counted
only on identity match (the 5,165 s case); existing side-match tests stay
green. `order_datetime` format (naive Asia/Taipei) needs a pinned test;
NOT VERIFIED on live payloads.

## Not verified

- Whether the live broker server reissues `seqno` across sessions (E).
- 1.7.11 deal payload fields (`exchange_seq`, `custom_field`) on real objects.
- Whether `list_trades` returns cancelled/filled trades on 1.7.11 (C).
- Cause of the 11-12 13:09Z gave-up (key 19428, no skipped deal).
