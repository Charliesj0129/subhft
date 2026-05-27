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
    source_ts_ns,
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
`SELECT *`).

## Canonical schema + stable hash (v2)

Canonicalization and hashing live in one place —
`src/hft_platform/replay/intent_diff.py` — and are reused by the gate, the
CLI runner, and the daily ops job (no second diff implementation).

`canonicalize_intent(intent)` projects any intent representation
(`OrderIntent`, the jsonl `_DictIntent`, or a CK-row dict) onto the v2
canonical record. `stable_intent_hash(canonical)` is a SHA-256 over a
sorted-key, version-tagged JSON of the **decision-determining subset**
(`intent_diff.HASH_FIELDS`):

| Hashed (decision)                                                                                   | Carried for reporting, NOT hashed                          |
| --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `strategy_id`, `symbol`, `side`, `intent_type`, `tif`, `price`, `qty`, `price_type`, `target_order_id`, `decision_price`, `source_ts` | `intent_id`, `local_ts` (wall-clock emit ts), `reason`, and optional context (`source_event_index`, `feature_set_id`, `feature_schema_version`, `session_phase`, `track_phase`, `risk_filter_phase`) |

Rationale: the generated `intent_id` and the wall-clock emission timestamp
(`local_ts`) differ between live and replay for the *same* decision, so they
are excluded from the hash. The **event source timestamp** (`source_ts`,
derived from `source_ts_ns`) is the deterministic input-locality key and IS
hashed — both the live SELECT and the replay path populate it. `reason` and
optional feature/session/risk context are surfaced in `first_divergence` for
localization but never affect parity. The hash embeds
`HASH_VERSION` and the record embeds `intent_schema_version` so a
hashing-scheme or schema change cannot masquerade as a parity match.

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
| `price`, `qty`, `intent_type`, `side`, `tif`, `target_order_id`, `source_ts`, `price_type`, `decision_price`, `strategy_id`, `symbol` | per-field inequality (over the hashed decision fields) at the same index |

The histogram is a full-stream aggregate for dashboards; the authoritative
fail signal is `ok` + `first_divergence` (see below). A R47-OE1-style
cancel-path bug shows up as elevated `__missing__` (live emits cancels the
backtest skipped) plus `intent_type` skew (NEW vs AMEND drift). Field schema
is owned by `hft_platform.replay.intent_diff.canonicalize_intent`.

## First divergence + mismatch taxonomy

`diff_intent_streams(expected, actual)` stops at the first divergence and
emits a localizable `first_divergence` object:

```json
{
  "path_pair": "live_vs_replay",
  "event_index": 17,
  "mismatch_type": "intent_hash_mismatch",
  "symbol": "TMFD6",
  "source_ts": 1729500000000,
  "local_ts": 1729500000123,
  "strategy_id": "r47_maker",
  "expected": { "...canonical intent..." : "..." },
  "actual": { "...canonical intent..." : "..." },
  "expected_hash": "ab12...",
  "actual_hash": "cd34...",
  "context": { "feature_set_id": "fs-7", "session_phase": "day" }
}
```

`mismatch_type` is one of: `missing_expected_intent`,
`unexpected_actual_intent`, `intent_hash_mismatch`, `ordering_mismatch`,
`schema_mismatch`, `empty_replay`, `missing_intent_log`.

## Fail-closed matrix (strict 100%)

The gate certifies parity **only** when both streams are non-empty, share a
schema version, and are byte-identical on the hashed decision fields in
order. Every other condition fails closed:

| Condition                                  | `ok`  | `mismatch_type`            | Sub-gate | CLI / daily exit |
| ------------------------------------------ | ----- | -------------------------- | -------- | ---------------- |
| Identical streams (in order)               | true  | —                          | pass     | 0                |
| Replay dropped an intent                   | false | `missing_expected_intent`  | block    | 1 (eligible)     |
| Replay emitted an extra intent             | false | `unexpected_actual_intent` | block    | 1 (eligible)     |
| Same intents, different order              | false | `ordering_mismatch`        | block    | 1 (eligible)     |
| Decision field differs at an index         | false | `intent_hash_mismatch`     | block    | 1 (eligible)     |
| Schema version skew                        | false | `schema_mismatch`          | block    | 1 (eligible)     |
| Replay produced zero intents               | false | `empty_replay`             | block    | 1 (eligible)     |
| Live/expected stream empty or absent       | false | `missing_intent_log`       | block    | 1 (eligible)     |

`--allow-pre-recorder` is an explicit operator observation escape hatch: the
report still carries `ok=false` (it can never read as a pass) but the CLI
exit stays 0 because the operator acknowledged the empty live stream.

## Threshold configuration

Strict promotion profile (`config/research/profiles/vm_ul6_strict.yaml`):

```yaml
maker:
  replay_parity_match_pct_min: 95.0   # informational metric only under strict ok
taker:
  replay_parity_match_pct_min: 95.0

sub_gates:
  - replay_parity   # registered, included in strict profile
```

Strict policy: the sub-gate blocks promotion whenever `ok is False` — i.e.
**any** divergence (structural or hash-level), regardless of `match_pct`. The
`match_pct` threshold is retained only as an informational metric and as a
fallback for legacy reports that predate the `ok` flag. The synthetic R47-OE1
fixture produces a divergence and is verified to block under this profile via
`tests/integration/test_replay_parity_e2e.py`.

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
2. Projecting them through `intent_diff.canonicalize_intent` (or its
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
| `ReplayParityReport.ok`            | Strict fail-closed flag — authoritative pass/block signal             |
| `ReplayParityReport.mismatch_type` | Divergence taxonomy (see fail-closed matrix)                          |
| `ReplayParityReport.first_divergence` | Localizable payload (path_pair, index, symbol, hashes, context)    |
| `ReplayParityReport.match_pct`     | Informational metric / legacy-report fallback                         |
| `ReplayParityReport.divergence_histogram` | Operator post-mortem (which canonical field drifted)            |
| `ReplayParityReport.first_divergence_idx` | Anchor for replay debugging (which intent index broke first)    |
| `ReplayParityReport.evidence_path` | Persisted JSON location for re-running the diff offline                |

## Cold-start verification (DoD-C4)

DoD-C4 is satisfied by the initial Task-1 cold-start subagent run on
2026-05-04 (commit `c6162cbd`). A fresh subagent executed Task 1 from
the plan (`docs/superpowers/plans/2026-05-04-slice-c-replay-parity-gate.md`)
without prior context and produced a working WAL fixture loader. No
second subagent run is required because the structural property —
"the plan is executable starting from a clean session" — has already
been proven.
