# Hot-path broad-except inventory

Snapshot of `origin/main` at `53f8b815` (2026-10-16). Answers candidate-inventory items N89 ("94 silent broad
excepts on the hot path, unclassified") and N11 ("148 handlers; HFT-D001 has a gap").

Nothing in `src/` changed. The scan is read-only and re-runnable:

```
python scripts/audit_broad_except.py                      # summary table
python scripts/audit_broad_except.py --list all --shape silent
python scripts/audit_broad_except.py --format json
```

Line numbers below are for `53f8b815`. Branches that are in flight move some of them (see "Already covered").

## 1. What is counted

A **broad** handler is a bare `except:`, `except Exception`, `except BaseException`, or a tuple containing one of
them. A **silent** handler is a broad handler whose whole body is one of `pass`, `...`, `continue`, `break`, bare
`return`, `return None` (the N89 definition). A handler that returns a constant other than `None` is reported
separately as `fallback`; one that logs, counts or re-raises is not silent.

The N89 and N11 numbers both reproduce:

| Number | Where it came from | Reproduced |
|---|---|---|
| 201 | N89: silent broad handlers in all of `src/hft_platform` | 201 (160 `pass`, 27 `return None`, 5 bare `return`, 9 `continue`, 0 `break`) |
| 94 | N89: the same, in the hot-path packages (order, risk, execution, gateway, strategy, feed_adapter, feature, engine) | 94 (93 plus one tuple catch at `gateway/service.py:227`) |
| 148 | N11: order 37, services 35, recorder 20, strategy 18, feed_adapter 12, execution 11, risk 10, gateway 3, feature 2 | 148 (tuple catches excluded) |
| 41 | HFT-D001 gap: silent broad handlers whose body is not `pass`/`...` | 41 |

N11's 148 and N89's 94 are different package scopes, not two counts of one thing. N11 adds `services` and
`recorder` (55 handlers) and leaves out the one tuple catch. HFT-D001 (`scripts/check_discipline.py:129`) already
reports every `pass`/`...` body; what it cannot see is the 41 that `return`, `return None` or `continue`.

Everything below counts silent handlers unless it says otherwise.

## 2. All broad handlers

1,082 broad handlers in `src/hft_platform`. 201 are silent, 20 more fall back to a constant, 237 only write a DEBUG
line, 455 log at INFO or above, 36 re-raise, 132 do something else (assign, call, return a computed value).
Only 1 counts without also logging.

| Group | Packages | Broad | Silent | Fallback | Debug only | Silent, no comment |
|---|---|---|---|---|---|---|
| money | order, risk, gateway, execution | 158 | 62 | 7 | 18 | 19 |
| market-data | feed_adapter, engine, feature, strategy | 296 | 32 | 2 | 92 | 8 |
| services | services | 215 | 35 | 2 | 54 | 16 |
| recorder | recorder | 132 | 20 | 1 | 33 | 9 |
| other | every other package, plus 1 at the package root | 281 | 52 | 8 | 40 | 13 |
| **all** | | **1,082** | **201** | **20** | **237** | **65** |

Silent handlers by package: order 37, services 35, recorder 20, strategy 18, ops 14, alpha 14, feed_adapter 12,
execution 11, risk 10, gateway 4, replay 4, observability 4, notifications 4, backtest 3, feature 2, reports 2,
cli 2, bot 2, ipc 1, diagnostics 1, contracts 1.

The DEBUG-only column is the next place to look after the silent ones: 237 handlers leave a line that production
log levels drop. `rg -c operation_fallback src` (172 sites, all `logger.debug`) is a large part of it.

## 3. The 94 hot-path handlers, classified

Method: the script splits each handler by what the `try` body touches. 49 of the 94 wrap only a metric call
(`inc`, `set`, `labels`, `observe`); I read all 49 `try` lines and they are metric updates. The other 45 I read
with ten lines of context each and sorted by hand.

| Class | Count | Meaning |
|---|---|---|
| Metrics / observability protection | 64 (49 + 15 read by hand) | The failed call is a counter, gauge, log or audit write. Swallowing is the right call; the cost is that a broken metric is invisible. |
| Cleanup, probe, degrade | 17 | Shutdown cleanup, import or attribute probes, optional features. Failure picks a fallback. |
| Money path | 13 | The failure changes order, fill or dedup state, or hides which path the order took. |

### 3.1 Money path (13)

| Site | What is lost | Status |
|---|---|---|
| `order/adapter.py:560` `_begin_dispatch_ticket` | `begin_dispatch` raised: order went out with no StormGuard ticket | fixed on `fix/stormguard-ticket-fail-closed` (fail closed; unmerged) |
| `order/adapter.py:596` `_end_dispatch_ticket` | `end_dispatch` raised: a HALT during dispatch goes unseen | fixed on `fix/stormguard-ticket-fail-closed` |
| `execution/fill_dlq.py:122` | a fill that cannot be serialised is dropped from the DLQ snapshot | covered by #578 (`.unserializable` sidecar) |
| `execution/fill_dlq.py:162` | an unreadable DLQ line is skipped on reload | covered by #578 (skipped lines counted, ERROR log) |
| `order/adapter.py:812` | rejection feedback dropped; strategy's pending counter stays up | `fix/adapter-feedback-and-feature-errors-counted` (counted, sampled WARNING; unmerged) |
| `gateway/dedup.py:224` | Rust `release` raised (only with `HFT_DEDUP_RUST=1`; the default is off): key is gone from the Python map but stays reserved in the Rust store, so a retry of that key is deduped | **open** (F1) |
| `order/adapter.py:4530`, `:4537` | `_dedup_release` raised while cancelling the API worker: reservation stays | **open** (F1) |
| `execution/router.py:244` | an unreadable line in the fill-dedup file is skipped on restore | **open** (F2) |
| `order/adapter.py:2024` | an unreadable line in the order-id map file is skipped on restore | **open** (F2) |
| `order/adapter.py:702` | contract resolver raised; the order falls back to the config alias, and the sibling counter `order_contract_code_resolution_total` then counts it as `alias_fallback` (or `symbol_raw`), exactly as it does for a contract the resolver simply does not know; it has no error source, so a raise cannot be told from a miss | **open** (F3) |
| `execution/router.py:784` | phantom resolver raised during shutdown drain; the fill goes to the orphan DLQ and the reason is lost | **open** (F4) |
| `strategy/runner.py:995` | Rust tracker raised in `get_positions_by_strategy`; falls back to the Python path with no signal | **open** (F5) |

### 3.2 Cleanup, probe, degrade (17)

| Site | What happens | Judgement |
|---|---|---|
| `feed_adapter/shioaji/client.py:1049` | old API `logout()` raised during `recreate_api` | `fix/feed-session-silent-handlers-visible` (WARNING + counter; unmerged) |
| `feature/engine.py:106` | `_top_qty` returns `None` on any error | `fix/adapter-feedback-and-feature-errors-counted` (counted; unmerged) |
| `feed_adapter/shioaji/quote_connection_pool.py:416` | tagging `conn_id` onto a facade | harmless; the comment says why |
| `feed_adapter/shioaji/quote_connection_pool.py:796` | `facade.close()` on stop | acceptable; shutdown |
| `feed_adapter/shioaji/quote_connection_pool.py:1376` | `_unsubscribe_symbol` during a universe reload | roll path: a failure leaves a stale subscription. Worth a counter; hold until after the 2026-10-21 contract roll (F7) |
| `gateway/service.py:227` | `await` of a cancelled expiry task (tuple catch) | idiomatic shutdown |
| `feature/parity.py:49` | importing the Rust module to probe availability | acceptable; the caller handles `False` |
| `feed_adapter/shioaji/_compat.py:199` | `getattr(api, attr)` on a property that raises | documented in the docstring |
| `feed_adapter/shioaji/_solace_env.py:54` | importing `shioaji.config` | documented; returns `None` |
| `strategy/compat.py:31`, `:55`, `:68` | the set-id, schema-version and profile-id probes raised | `:55` **open** (F6): a `None` schema version skips the `feature_schema_too_old` check without raising an issue. `:31` and `:68` already surface: a `None` set id raises `feature_set_unavailable` and a `None` profile id raises the `feature_profile_unavailable` warning (each when the strategy declares the requirement) |
| `order/adapter.py:849`, `:2669`, `:3792` | explanation assembler call failed | observability-only by design (whether the assembler stays is an open decision); the adapter-feedback branch gave one a written reason |
| `order/adapter.py:821` | audit writer failed | the audit trail loses a row with no count; low risk, listed in F1 |
| `strategy/base.py:196` | `publish_state` sink raised | observability |

### 3.3 Metrics / observability protection (64)

The 49 metric-only `try` blocks plus 15 found by hand: `execution/reconciliation.py` (`:279`, `:732`, `:853`,
`:881`, `:1174`), `feed_adapter/shioaji/quote_runtime.py` (`:266`, `:279`, `:292`), `gateway/service.py`
(`:636`, `:694`), `order/adapter.py` (`:4544`, `:4553`), `risk/storm_guard.py` (`:449`, `:813`),
`strategy/runner.py:1387`. All follow the pattern "a counter must never break the order path".
`--list all --shape silent` prints the 49 with `metrics-guard` in the last-but-one column.

One thing this class cannot show: whether the metric exists. A silent `except` around a metric whose name is
misspelled never fires and never says so. A dead-metrics scan (defined but never set, incremented or observed) is
the check for that, not this file.

## 4. Follow-up branches

Each is small and independent. None is started. F1, F3, F4, F5 touch the order, gateway, execution or strategy
paths (Tier 3).

| # | Branch | Sites | Change |
|---|---|---|---|
| F1 | `fix/dedup-release-failure-visible` | `gateway/dedup.py:224`, `order/adapter.py:4530`, `:4537`, `:821` | log + counter on release and audit failures; no behaviour change. Should follow `fix/dispatch-abort-release-funnel`, which owns the release funnel |
| F2 | `fix/state-restore-skipped-lines-counted` | `execution/router.py:244`, `order/adapter.py:2024` | count and log skipped lines on restore, as #578 did for the fill DLQ |
| F3 | `fix/contract-resolver-fallback-visible` | `order/adapter.py:702` | add a `resolver_error` source to `order_contract_code_resolution_total`, WARNING sampled |
| F4 | `fix/shutdown-drain-phantom-resolver-visible` | `execution/router.py:784` | log the exception type next to the orphan-DLQ add |
| F5 | `fix/strategy-rust-position-fallback-visible` | `strategy/runner.py:995` | count Rust-to-Python fallbacks, WARNING once |
| F6 | `fix/strategy-compat-schema-probe-visible` | `strategy/compat.py:55` | raise a `feature_schema_unavailable` issue when the schema probe fails, matching the set-id and profile-id cases |
| F7 | `fix/pool-universe-reload-unsubscribe-visible` | `feed_adapter/shioaji/quote_connection_pool.py:1376` | after the 2026-10-21 roll; same file as `fix/roll-gate-settlement-date-parsing` |
| F8 | `chore/discipline-d001-broad-handlers` | `scripts/check_discipline.py` | Do-NOT-Edit, needs an explicit decision: extend D001 to the 41 and add a `# hft: silent-ok <reason>` marker |

Not in scope here: the 35 `services` and 20 `recorder` handlers that N11 counts. `fix/services-silent-handlers-visible` and `fix/recorder-wal-silent-paths-visible`
cover some of them; the rest are not hand-classified. Run `--list services --shape silent` and
`--list recorder --shape silent` for the starting list.

## 5. Limits

- The scan is syntactic. A `try` body that wraps a metric inside a helper looks like "other"; that is why the 15
  were read by hand. The `try_kind` column is a hint, not a verdict.
- It does not follow calls: a handler that calls `self._note_failure(exc)` is reported as `other`, not `silent`.
  That is the intended behaviour (the failure is handled), but it also means a helper that itself swallows the
  exception is not found.
- A comment is detected on the `except` line, the first body line or the line above. A comment two lines up is not
  seen.
- `tests/` is not scanned; test files are skipped by name (`test_*.py`) only.
- A file that does not parse is skipped without a message. All 391 files under `src/hft_platform` parse on Python
  3.12 (checked), but an older interpreter would drop any file that uses newer syntax and under-count.
