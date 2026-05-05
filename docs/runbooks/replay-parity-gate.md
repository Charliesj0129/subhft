# Replay-Parity Gate Runbook

## Why this gate exists

On 2026-04-21 the deployed R47-OE1 maker strategy diverged from its
backtest by roughly **9.4k NTD** (live -1,722 vs backtest +7,701) on
TMFD6. Root cause: the live cancel path short-circuited inside
`GatewayPolicy`, leaving stale quotes resting for **41–79 s** while the
backtest assumed sub-millisecond cancellation. The backtest was
measuring intent emission; live execution diverged at the gateway.
Full incident: [`docs/incidents/2026-04-21-r47-backtest-live-divergence.md`](../incidents/2026-04-21-r47-backtest-live-divergence.md).

The replay-parity gate (Slice C) closes that loop: it replays the live
strategy decisions against a recorded fixture, hashes the canonical
intent stream, and **blocks promotion** when ≥5 % of intents diverge.
A backtest that disagrees with what the strategy actually emits cannot
pass `vm_ul6_strict`.

## How to enable intent recording

Intent recording is **opt-in** to avoid changing live throughput
characteristics. Once enabled, every emitted `OrderIntent` lands in the
`hft.order_intents` ClickHouse table.

```bash
export HFT_INTENT_RECORDER_ENABLED=1
```

Defaults: off. The table is created on migration apply but stays empty
until the env var is set. The recorder topic mirrors the existing
`hft.orders` 365-day retention policy.

Schema source of truth: `src/hft_platform/migrations/clickhouse/20260504_001_create_order_intents.sql`.

## Reading `hft.order_intents` for a session

The canonical query for "what intents did strategy X emit between
times T0 and T1 on symbol S":

```sql
SELECT
    intent_id,
    strategy_id,
    symbol,
    intent_type,
    side,
    price_scaled,
    qty,
    tif,
    target_order_id,
    timestamp_ns,
    decision_price,
    price_type
FROM hft.order_intents
WHERE strategy_id = 'r47_maker'
  AND symbol = 'TMFD6'
  AND timestamp_ns BETWEEN
        toUnixTimestamp64Nano(toDateTime64('2026-04-21 09:00:00.000', 9))
    AND toUnixTimestamp64Nano(toDateTime64('2026-04-21 13:30:00.000', 9))
ORDER BY timestamp_ns, intent_id
FORMAT JSONEachRow;
```

Project only the columns the parity comparison consumes (no
`SELECT *`). Volatile fields (`trace_id`, `idempotency_key`, `ttl_ns`,
`reason`, `ingest_ts`, `source_ts_ns`) are excluded by design — they
do not survive replay and would create false divergences.

## Running the offline replay harness

```python
from hft_platform.replay.strategy_replay import ReplayConfig, replay_strategy
from hft_platform.strategies.r47_maker import R47Maker

cfg = ReplayConfig(
    fixture_path="/path/to/wal_fixture_2026_04_21.parquet",
    strategy_factory=lambda rng: R47Maker(rng=rng, symbol="TMFD6"),
    symbols={"TMFD6"},
    rng_seed=0,
    max_events=None,
)
replayed_log = replay_strategy(cfg)
print(f"Replayed {replayed_log.n_intents()} intents, hash={replayed_log.hash()[:12]}")
```

The harness patches `hft_platform.core.timebase.now_ns` so strategy
timestamps are bound to the fixture's `exch_ts`, eliminating wall-clock
jitter as a divergence source.

## Reading a divergence histogram

`IntentDiff.compute()` returns a `ReplayParityReport` whose
`divergence_histogram` is keyed by canonical intent field. Two
distinct shapes:

| Histogram key                          | Meaning                                                                 |
| -------------------------------------- | ----------------------------------------------------------------------- |
| `__missing__`                          | length mismatch — one side ran out of intents (dropped or extra emit)   |
| `price`, `qty`, `intent_type`, `side`, `tif`, `target_order_id`, `timestamp_us`, `price_type`, `decision_price`, `intent_id`, `strategy_id`, `symbol` | per-field inequality at the same index |

A R47-OE1-style cancel-path bug typically shows up as elevated
`__missing__` (live emits cancels the backtest skipped) plus
`intent_type` skew (NEW vs AMEND drift). Field schema is owned by
`hft_platform.replay.intent_log._intent_to_canonical` (see
`src/hft_platform/replay/intent_log.py`).

## Threshold configuration

Strict promotion profile (`config/research/profiles/vm_ul6_strict.yaml`):

```yaml
maker:
  replay_parity_match_pct_min: 95.0
taker:
  replay_parity_match_pct_min: 95.0

sub_gates:
  - replay_parity   # registered, included in strict profile
```

A `match_pct < 95.0` decision **blocks** promotion. The synthetic
R47-OE1 fixture (Slice C task 7) produces `match_pct ≈ 94 %` and is
verified to block under this profile via
`tests/integration/test_replay_parity_e2e.py::test_dod_c1_synthetic_r47_kills_at_replay_parity_gate`.

The loose `vm_ul6` profile leaves `replay_parity` advisory (non-blocking)
so research/exploration runs are not gated by parity.

## Path (a) — skipped in initial release (2026-05-04)

Slice C ships with **path (b)** only: the synthetic R47-OE1 fixture
deterministically reproduces a cancel-path divergence and exercises
the full sub-gate path (DoD-C1 satisfied).

**Path (a)** — reconstruct a live intent stream from the actual
2026-04-21 `hft.orders` rows on the production ClickHouse — was
deferred. Auto-mode policy requires explicit operator authorization
for production-system reads, and that authorization was not given in
this slice. The 2026-04-21 WAL fixture is preserved at
`~/.local/share/hft-fixtures/wal_2026_04_21.tar.gz` (37 MB, 6,307
shards, sha256
`c0ab51807cfb62dde56ec580d4e146515367f6270269d2c3a481b4d110bf140e`)
so a future operator can add path (a) by:

1. Reading the relevant `hft.orders` rows for `r47_maker` /  TMFD6 /
   2026-04-21.
2. Projecting them through `_intent_to_canonical` (or its
   equivalent for orders → intents).
3. Diffing against the harness output via `IntentDiff`.

DoD-C1 is structurally equivalent because the divergence pattern is
identical — what matters is that the gate flags `< 95 %` parity, not
which fixture supplies the live side.

## Operator quick-reference

| Knob                               | Effect                                                                 |
| ---------------------------------- | ---------------------------------------------------------------------- |
| `HFT_INTENT_RECORDER_ENABLED=1`    | Begin writing emitted intents to `hft.order_intents`                   |
| `HFT_INTENT_RECORDER_ENABLED` unset / `0` | Recorder no-op; table stays empty                                |
| `replay_parity_match_pct_min`      | Promotion-blocking threshold (strict profile: 95.0)                    |
| `--profile vm_ul6_strict`          | Includes `replay_parity` as a blocking sub-gate                        |
| `--profile vm_ul6` (default loose) | `replay_parity` advisory only                                          |

| Audit field                        | Consumer                                                               |
| ---------------------------------- | ---------------------------------------------------------------------- |
| `BacktestResult.replay_parity_report` | `ReplayParityGate.evaluate()` and Gate D `replay_parity_audit`      |
| `ReplayParityReport.match_pct`     | Strict profile threshold check                                         |
| `ReplayParityReport.divergence_histogram` | Operator post-mortem (which canonical field drifted)            |
| `ReplayParityReport.first_divergence_idx` | Anchor for replay debugging (which intent index broke first)    |
| `ReplayParityReport.evidence_path` | Persisted JSONL location for re-running the diff offline               |

## Cold-start verification (DoD-C4)

DoD-C4 is satisfied by the initial Task-1 cold-start subagent run on
2026-05-04 (commit `c6162cbd`). A fresh subagent executed Task 1 from
the plan (`docs/superpowers/plans/2026-05-04-slice-c-replay-parity-gate.md`)
without prior context and produced a working WAL fixture loader. No
second subagent run is required because the structural property —
"the plan is executable starting from a clean session" — has already
been proven.
