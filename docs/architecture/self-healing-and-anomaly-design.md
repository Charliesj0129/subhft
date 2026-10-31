# Self-healing and anomaly detection: what exists, what does not, and a design

Status: design note, nothing in it is implemented. Written 2026-10-18 against `origin/main` `53f8b815`.
Docs only. Candidate inventory B4 and B10 (plan item R16).

The roadmap lists two items under Track C: a "self-healing framework v1" (`ROADMAP.md:66`, estimated 15K lines)
and an "anomaly detection engine" that replaces fixed thresholds with EWMA or an isolation forest
(`ROADMAP.md:129`, estimated 20K lines). This note says what the platform already does about
faults, where the existing pieces disagree with each other, what happened to the first attempt at a healing
framework, and what to build instead. Every number comes from the code at the commit above; the probe in
section 4 re-runs offline and its output is embedded.

## Short answers

1. **Is there a self-healing framework?** No. A `healing/` package (orchestrator, playbook, actions) was added in
   five commits on 2026-04-16 and deleted in one commit on 2026-04-17 as dead code (the message says it had no
   inbound imports). While it existed, only two actions were ever registered: `wait` and `log_warn`. What is left is
   `config/base/healing_playbook.yaml`: 10 playbooks, 18 distinct action names, 16 of them with no implementation
   anywhere, and no code that reads the file.
2. **Does the platform react to faults at all?** Yes, and more than the roadmap implies. Probes report 11 fault
   reasons on every supervisor tick; the controller puts the platform in reduce-only and clears 10 of the 11 by
   itself after 60 s of quiet. StormGuard has its own state machine and `.trigger_halt(` appears 20 times in
   8 files. A strategy that raises is quarantined. Section 3 lists them.
3. **Do these layers agree with each other?** Not everywhere. The offline probe found: (a) the optional monitor
   records "manual re-arm required" for 8 reasons the controller clears by itself, and "auto" for a reason the
   controller latches; (b) two reasons the probes emit, and every margin-critical reason, land in the metric
   label `unknown`; (c) 6 of the 22 allowed reason codes have no string literal anywhere else in `src/`;
   (d) `feed_gap_exceeded` is listed as auto-recoverable but nothing emits it. Separately, one threshold has two
   defaults (RSS 2048 MB in one file, 3072 MB in the file that actually configures it).
4. **What should be built?** Not an orchestrator. One fault table that the controller, the monitor and the metric
   labels all derive from, plus a test that fails when they disagree (section 6). Automatic repair stays out of
   money paths.
5. **And the EWMA engine?** As a second, advisory layer beside the fixed thresholds, not a replacement:
   shadow-only, off the hot path, a handful of signals, promoted only after it has been compared with real
   incidents (section 7). Its hardest prerequisite is data: metric history lives in the host Prometheus, which
   this note did not read.

## 1. What exists today

```ascii
 probes, every supervisor tick
 PlatformDegradeInputs.reduce_only_reasons()          platform_inputs.py
   11 reasons: feed gap / pending / flapping, queue depth, RSS, Redis,
   WAL backlog, recorder DATA_LOSS / DEGRADED, reconciliation drift /
   unverifiable
        |
        v   SystemService._update_platform_degrade_state       system.py
 PlatformDegradeController.enter_reduce_only_async(reason)
        |   allow_intent(): NEW that opens risk is refused;
        |   CANCEL / AMEND / FORCE_FLAT stay allowed
        |
        +-- reason in _AUTO_RECOVERABLE_REASONS (10 of the 11)
        |     check_auto_recovery_async clears it after 60 s of quiet
        |
        '-- latched until an operator re-arm request is consumed:
              recorder_data_loss (a probe reason), broker_unavailable and
              margin_critical_* (both come from the monitor only)

 AutonomyMonitor, only if HFT_AUTONOMY_MONITOR_ENABLED=1    bootstrap.py
   reads the same probes plus StormGuard, broker and margin, and adds:
     HALT                 -> flatten_all
     recorder DATA_LOSS   -> trigger_halt
     broker down > 300 s  -> reduce-only
     margin critical      -> reduce-only
     recon drift streak 2 -> reduce-only
   writes its own evidence record: manual_rearm_required = (rearm == "manual")

 StormGuard  NORMAL -> WARM -> STORM -> HALT                 storm_guard.py
   fed by drawdown, loop lag, order RTT, feed gap and 20 trigger_halt calls
   optional: DriftBurstDetector (HFT_STORMGUARD_DRIFT_BURST_ENABLED=1)

 StrategyHealthGovernor.quarantine_async                     runner.py
   a strategy that raises is stopped; only a re-arm request restarts it
```

Where each box lives: `src/hft_platform/ops/platform_inputs.py:139`, `src/hft_platform/services/system.py:1501`,
`src/hft_platform/ops/platform_degrade.py:114`, `src/hft_platform/ops/platform_degrade.py:439`, `src/hft_platform/ops/platform_degrade.py:316`,
`src/hft_platform/services/system.py:1831`, `src/hft_platform/services/bootstrap.py:1477`,
`src/hft_platform/ops/autonomy_monitor.py:244`, `src/hft_platform/risk/storm_guard.py:98`, `src/hft_platform/strategy/runner.py:1253`.

The controller's cooldown is `HFT_PLATFORM_AUTO_RECOVERY_COOLDOWN_S`, default 60 (`src/hft_platform/ops/platform_degrade.py:521`);
auto-recovery itself is on unless `HFT_PLATFORM_AUTO_RECOVERY_ENABLED` is set to something other than 1
(`src/hft_platform/ops/platform_degrade.py:519`). Recovery needs every active reason to be auto-recoverable, none
of them reported again, and the cooldown to run out (`src/hft_platform/ops/platform_degrade.py:316` onward).

The `tests/chaos/test_playbook_*.py` files and `docs/runbooks/chaos-playbook-index.md` use the word "playbook" for
chaos tests that check this existing behaviour. They are not the runtime playbooks of the removed framework.

## 2. The first attempt: built on 2026-04-16, deleted on 2026-04-17

The design is in section C3 of the Track C autonomy spec (`docs/superpowers/specs/2026-04-15-track-c-autonomy-foundation-design.md:266`): a seven-step
orchestrator (classify, diagnose, look up a playbook, risk check, execute, verify, record), 13 playbook rows, a
Telegram approve/reject flow and a ClickHouse `healing_log` table. The plan is
`docs/superpowers/plans/2026-04-16-track-c-autonomy-foundation.md`.

| commit | date | what |
|---|---|---|
| `2904a8e2` | 2026-04-16 | add FaultEvent, FaultCategory, FaultSeverity, RiskLevel models |
| `968ad941` | 2026-04-16 | add ActionRegistry for named repair action callables |
| `083cee42` | 2026-04-16 | add HealingPlaybook with YAML-driven fault-repair matching (this commit added the YAML) |
| `a188a3e5` | 2026-04-16 | add HealingOrchestrator with playbook-driven repair execution |
| `2a9c7b57` | 2026-04-16 | AutonomyMonitor emits FaultEvent when HFT_HEALING_ENABLED=1 |
| `f6630799` | 2026-04-17 | remove dead modules (healing/, data_quality/, eod_recon, burst_detector, etc.); its message says the deleted files had zero inbound imports; it also removed the monitor's healing branch |

At the commit before the removal, the orchestrator was constructed nowhere in `src/`, `config/` or `scripts/`. The
only outside reference was one rule in `AutonomyMonitor` (broker down more than 5 minutes): when
`HFT_HEALING_ENABLED` was set and a `fault_callback` argument had been passed, it built a `FaultEvent` and called
the callback instead of recording its normal decision. No code passed a callback. The removal commit took that
branch out of the monitor and deleted its test.

What the package held: 5 files and 370 lines (`__init__.py` 1, `actions.py` 40, `fault.py` 37, `orchestrator.py` 183,
`playbook.py` 109). The spec's `classifier.py`, `diagnoser.py` and `result.py` were never written. The only
actions ever registered were the two built-ins, `wait` and `log_warn` (`git show f6630799^:src/hft_platform/healing/actions.py`).
Nothing named `healing_log`, `HFT_HEALING_ENABLED`, `FaultEvent` or `HealingOrchestrator` remains in `src/`, `config/`,
`scripts/` or the `Makefile`; the only docs that mention them are the spec and the plan.

What is left behind is `config/base/healing_playbook.yaml`: 10 playbooks and 20 action steps using 18 distinct
action names. Only `wait` (2 steps) and `log_warn` (1 step) were ever implemented. The other 16 names
(`unsubscribe_symbol`, `resubscribe_symbol`, `logout_broker`, `relogin_broker`, `resubscribe_all`,
`reconcile_positions`, `enter_reduce_only`, `alert_and_wait_approval`, `switch_to_wal_only`,
`disable_live_monitor`, `archive_old_wal`, `compress_logs`, `requery_broker_positions`, `auto_correct_positions`,
`refresh_contract_cache`, `resolve_aliases`) were only names. No file in `src/`, `scripts/`, `tests/`, `config/`,
`Makefile` or `pyproject.toml` mentions the YAML.

The lesson is not that the idea was wrong. The package was written bottom-up (models, registry, playbook,
orchestrator, then a monitor hook) in one day, the only hook sat behind an opt-in flag and an argument nobody
passed, and a day later a search for callers found none. Section 6 turns that into a rule.

## 3. Fault to action, as built

"Self-clears" means the controller removes the reason after the probe has stopped reporting it for the cooldown
(60 s). "Latched" means it stays until an operator re-arm request is consumed. Thresholds are the defaults passed
by `src/hft_platform/services/bootstrap.py:384`; the host values were not read.

| fault | detector | default and env var | action | ends |
|---|---|---|---|---|
| no feed for 600 s | `src/hft_platform/ops/platform_inputs.py:146` | 600 s, `HFT_PLATFORM_REDUCE_ONLY_FEED_GAP_S` | reduce-only | self-clears |
| reconnect pending while the calendar says the session is open | `src/hft_platform/ops/platform_inputs.py:160` | 60 s, `HFT_PLATFORM_REDUCE_ONLY_RECONNECT_PENDING_S` | reduce-only | self-clears |
| quote connection flapping | `src/hft_platform/ops/platform_inputs.py:163` | 5 events, `HFT_PLATFORM_REDUCE_ONLY_RECONNECT_FLAP_BUDGET` (0 disables) | reduce-only | self-clears |
| deepest queue of 5 to 7 | `src/hft_platform/ops/platform_inputs.py:178` | 5000, `HFT_PLATFORM_REDUCE_ONLY_QUEUE_DEPTH` | reduce-only | self-clears |
| process memory | `src/hft_platform/ops/platform_inputs.py:183` | 3072 MB, `HFT_PLATFORM_REDUCE_ONLY_RSS_MB` (see G5) | reduce-only | self-clears |
| Redis unhealthy | `src/hft_platform/ops/platform_inputs.py:187` | none | reduce-only | self-clears |
| WAL files waiting | `src/hft_platform/ops/platform_inputs.py:192` | 200 files, `HFT_PLATFORM_REDUCE_ONLY_WAL_BACKLOG_FILES` | reduce-only | self-clears |
| recorder DEGRADED or CRITICAL | `src/hft_platform/ops/platform_inputs.py:210` | none | reduce-only | self-clears |
| recorder DATA_LOSS | `src/hft_platform/ops/platform_inputs.py:208` | boot grace 60 s, `HFT_RECORDER_DATA_LOSS_BOOT_GRACE_S` | reduce-only; with the monitor also HALT (`src/hft_platform/ops/autonomy_monitor.py:293`) | latched |
| reconciliation drift or unverifiable | `src/hft_platform/ops/platform_inputs.py:134`, `src/hft_platform/ops/platform_inputs.py:136` | state of the reconciliation service | reduce-only | self-clears |
| broker down more than 300 s | `src/hft_platform/ops/autonomy_monitor.py:226` (monitor only) | 300 s, hard-coded | reduce-only | latched in the controller |
| margin critical | `src/hft_platform/ops/autonomy_monitor.py:191` (monitor only) | set by the margin monitor | reduce-only | latched in the controller |
| a strategy raises | `src/hft_platform/strategy/runner.py:1253` | any exception | that strategy is quarantined | re-arm request |
| drawdown, loop lag, order RTT, feed gap | `src/hft_platform/risk/storm_guard.py:21` | drawdown -50/-100/-200 bps (`src/hft_platform/risk/storm_guard.py:22`); loop lag 5/20 ms (`src/hft_platform/risk/storm_guard.py:36`); order RTT 1500/2500 ms (`src/hft_platform/risk/storm_guard.py:87`); feed gap 1.0 s to STORM (`src/hft_platform/risk/storm_guard.py:90`) | WARM, STORM or HALT | cooldowns, `HFT_STORMGUARD_STORM_COOLDOWN_S` 30 s and `HFT_STORMGUARD_HALT_COOLDOWN_S` 60 s |

The 20 `.trigger_halt(` call sites, grouped. One of them, `src/hft_platform/risk/validators.py:1105`, only forwards, so 19 places decide to halt.
Session-lease loss also halts, through `src/hft_platform/services/bootstrap.py:831`.

| group | sites |
|---|---|
| a queue or buffer is overflowing | `src/hft_platform/engine/event_bus.py:379` (rate in a window), `src/hft_platform/engine/event_bus.py:686` and `src/hft_platform/engine/event_bus.py:789` (per-consumer count), `src/hft_platform/risk/engine.py:559`, `src/hft_platform/strategy/runner.py:1675`, `src/hft_platform/services/market_data.py:1909`, `src/hft_platform/services/system.py:581`, `src/hft_platform/services/system.py:2807`, `src/hft_platform/services/system.py:2818` |
| an operator or an outside flag | `src/hft_platform/services/system.py:594` and `src/hft_platform/services/system.py:2077` (kill-switch file), `src/hft_platform/services/system.py:2103` (Redis flag `hft:emergency_halt`) |
| a component keeps crashing | `src/hft_platform/services/system.py:1484`, `src/hft_platform/services/system.py:2155` |
| positions cannot be trusted | `src/hft_platform/services/system.py:926`, `src/hft_platform/execution/reconciliation.py:411` (reconciliation unavailable after consecutive failures), `src/hft_platform/execution/reconciliation.py:1200` (critical discrepancies; sets the reconciliation hold first) |
| money | `src/hft_platform/risk/engine.py:1201` |
| data integrity | `src/hft_platform/ops/autonomy_monitor.py:293` (monitor only) |

Also optional: the drift-burst detector, off unless `HFT_STORMGUARD_DRIFT_BURST_ENABLED` is set
(`src/hft_platform/services/bootstrap.py:813`); it can only escalate StormGuard, never lower the state
(`update_with_lob`, `src/hft_platform/risk/storm_guard.py:745`).

## 4. Where the layers disagree

The probe below builds the controller and the monitor with stand-ins for the metrics registry and the evidence
writer, feeds them the reasons the code can emit, and prints what each says. It needs the project's Python
dependencies, no network, and writes nothing. Run from the repository root.

```python
"""Read-only probe of the fault table: what the monitor says, what the controller does, what the metric shows.

Offline: no broker, no network, no file written (metrics and the evidence writer are stubs).
Run from the repository root:  python -I probe.py
"""
import logging
import pathlib
import sys
from unittest.mock import MagicMock

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
sys.path.insert(0, "src")

from hft_platform.contracts.strategy import StormGuardState  # noqa: E402
from hft_platform.core import timebase  # noqa: E402
from hft_platform.ops.autonomy import _ALLOWED_REASON_CODES, AutonomyTransition  # noqa: E402
from hft_platform.ops.autonomy_monitor import AutonomyMonitor  # noqa: E402
from hft_platform.ops.platform_degrade import _AUTO_RECOVERABLE_REASONS, PlatformDegradeController  # noqa: E402

COOLDOWN_S = 60
T0 = 1_000_000_000_000
EMITTED = [
    "feed_reconnect_unhealthy",
    "feed_reconnect_pending",
    "feed_reconnect_flapping",
    "queue_depth_exceeded",
    "rss_unhealthy",
    "redis_unhealthy",
    "wal_backlog_unhealthy",
    "clickhouse_unhealthy",
    "reconciliation_drift",
    "reconciliation_unverifiable",
    "recorder_data_loss",
    "broker_unavailable",
    "margin_critical_95%",
]

print("1. Controller: does the reason clear itself once the probe stops reporting it? (cooldown 60 s)")
print(f"{'reason':30s} {'manual_rearm_required':>21s}  after two ticks 61 s apart")
for r in EMITTED:
    c = PlatformDegradeController(metrics=MagicMock(), evidence_writer=MagicMock(), auto_recovery_cooldown_s=COOLDOWN_S)
    transition = c.enter_reduce_only(reason=r)
    c.check_auto_recovery(current_reasons=[], now_ns=T0)  # starts the cooldown unless something blocks it
    c.check_auto_recovery(current_reasons=[], now_ns=T0 + (COOLDOWN_S + 1) * 1_000_000_000)
    print(f"{r:30s} {str(transition.manual_rearm_required):>21s}  {'still reduce-only' if c.reduce_only_active else 'cleared'}")


def monitor(probe_reasons: list[str], broker_down: bool) -> AutonomyMonitor:
    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL
    degrade = MagicMock()
    degrade.reduce_only_active = False
    inputs = MagicMock()
    inputs.reduce_only_reasons.return_value = probe_reasons
    broker = MagicMock()
    broker.is_connected.return_value = not broker_down
    m = AutonomyMonitor(storm_guard=storm_guard, platform_degrade=degrade, platform_inputs=inputs, broker_client=broker)
    if broker_down:
        m._broker_was_connected = False
        m._broker_disconnect_since_ns = timebase.now_ns() - 301_000_000_000
    return m


print()
print("2. Monitor (opt-in): the decision it records for the same reasons")
print(f"{'probe reason':24s} {'rule':22s} {'action':18s} {'rearm':>7s}  controller says auto-recoverable")
for r in [x for x in EMITTED if x in _AUTO_RECOVERABLE_REASONS or x == "recorder_data_loss"]:
    for d in monitor([r], broker_down=False)._evaluate():
        print(f"{r:24s} {d.rule_name:22s} {d.action:18s} {d.rearm:>7s}  {d.reason in _AUTO_RECOVERABLE_REASONS}")
for d in monitor([], broker_down=True)._evaluate():
    print(f"{'(broker down > 300 s)':24s} {d.rule_name:22s} {d.action:18s} {d.rearm:>7s}  {d.reason in _AUTO_RECOVERABLE_REASONS}")

print()
print("3. Metric label: autonomy_transitions_total{reason} for each emitted reason")
unknown = [r for r in EMITTED if AutonomyTransition.enter_platform_reduce_only(r).metric_reason == "unknown"]
print("reasons that collapse to 'unknown':", unknown)

print()
print("4. Allowed reason codes: files under src/ (other than ops/autonomy.py) holding the code as a string literal")
sources = {p: p.read_text(encoding="utf-8") for p in pathlib.Path("src").rglob("*.py") if p.as_posix() != "src/hft_platform/ops/autonomy.py"}
none_found = []
for code in sorted(_ALLOWED_REASON_CODES):
    n = sum(1 for text in sources.values() if f'"{code}"' in text or f"'{code}'" in text)
    if n == 0:
        none_found.append(code)
print(f"{len(_ALLOWED_REASON_CODES)} allowed codes; no string literal elsewhere in src/ for:", none_found)
```

```text
1. Controller: does the reason clear itself once the probe stops reporting it? (cooldown 60 s)
reason                         manual_rearm_required  after two ticks 61 s apart
feed_reconnect_unhealthy                       False  cleared
feed_reconnect_pending                         False  cleared
feed_reconnect_flapping                        False  cleared
queue_depth_exceeded                           False  cleared
rss_unhealthy                                  False  cleared
redis_unhealthy                                False  cleared
wal_backlog_unhealthy                          False  cleared
clickhouse_unhealthy                           False  cleared
reconciliation_drift                           False  cleared
reconciliation_unverifiable                    False  cleared
recorder_data_loss                              True  still reduce-only
broker_unavailable                              True  still reduce-only
margin_critical_95%                             True  still reduce-only

2. Monitor (opt-in): the decision it records for the same reasons
probe reason             rule                   action               rearm  controller says auto-recoverable
feed_reconnect_unhealthy feed_gap_majority      enter_reduce_only   manual  True
feed_reconnect_pending   feed_gap_majority      enter_reduce_only   manual  True
feed_reconnect_flapping  feed_gap_majority      enter_reduce_only   manual  True
queue_depth_exceeded     queue_depth_exceeded   enter_reduce_only   manual  True
rss_unhealthy            rss_unhealthy          enter_reduce_only   manual  True
redis_unhealthy          redis_unhealthy        enter_reduce_only   manual  True
wal_backlog_unhealthy    persistence_failure    enter_reduce_only   manual  True
clickhouse_unhealthy     clickhouse_unhealthy   enter_reduce_only   manual  True
recorder_data_loss       recorder_data_loss     trigger_halt        manual  False
(broker down > 300 s)    broker_disconnect      enter_reduce_only     auto  False

3. Metric label: autonomy_transitions_total{reason} for each emitted reason
reasons that collapse to 'unknown': ['reconciliation_unverifiable', 'margin_critical_95%']

4. Allowed reason codes: files under src/ (other than ops/autonomy.py) holding the code as a string literal
22 allowed codes; no string literal elsewhere in src/ for: ['manual_operator', 'memory_pressure', 'pnl_peak_drawdown', 'pnl_soft_limit', 'session_close_only', 'strategy_reject_spike']
```

**G1. The monitor's re-arm label contradicts the controller.** `MonitorDecision.rearm` (`src/hft_platform/ops/autonomy_monitor.py:32`)
is used for one thing: the `manual_rearm_required` field of the evidence record the monitor writes
(`src/hft_platform/ops/autonomy_monitor.py:433`). What actually happens is decided by the controller from
`_AUTO_RECOVERABLE_REASONS` (`src/hft_platform/ops/platform_degrade.py:26`, used at `src/hft_platform/ops/platform_degrade.py:152`).
The probe shows 8 infrastructure reasons where the monitor says "manual" and the controller clears them, and the
broker-disconnect decision (`src/hft_platform/ops/autonomy_monitor.py:233`) where the monitor says "auto" and the controller latches. The
controller also writes its own record when it enters reduce-only, so an entry that the monitor triggers can be
recorded twice with different values. I did not trace what reads this field.

**G2. Reasons that fall into the `unknown` metric label.** `autonomy_transitions_total` maps a reason to a label
through `_ALLOWED_REASON_CODES` (`src/hft_platform/ops/autonomy.py:7`) and returns `unknown` for anything else
(`src/hft_platform/ops/autonomy.py:47`). `reconciliation_unverifiable` is emitted and auto-recoverable but not in the set.
`margin_critical_{ratio}` bakes the ratio into the string (`src/hft_platform/ops/autonomy_monitor.py:191`), so
every value maps to `unknown`; the probe prints the 95% case. A dashboard or alert grouped by reason cannot tell
these apart.

**G3. Six allowed codes with no literal in `src/`.** The probe's section 4 is a literal-string scan, so a code built
at run time would not show up there. As scanned, nothing else in `src/` contains `manual_operator`,
`memory_pressure`, `pnl_peak_drawdown`, `pnl_soft_limit`, `session_close_only` or `strategy_reject_spike`. Also,
`feed_gap_exceeded` sits in the auto-recoverable set (`src/hft_platform/ops/platform_degrade.py:30`) and is not mentioned anywhere else
in `src/`: an entry nothing emits.

**G4. The monitor is opt-in, and several behaviours exist only inside it.** Without
`HFT_AUTONOMY_MONITOR_ENABLED=1` (`src/hft_platform/services/bootstrap.py:1477`), the following are not running: flatten on HALT
(`src/hft_platform/ops/autonomy_monitor.py:259`), HALT on recorder DATA_LOSS, reduce-only after 300 s without a broker, reduce-only on
margin critical, and the second reconciliation path (drift streak of 2, `src/hft_platform/ops/autonomy_monitor.py:336`). Other code may cover
some of these; only the monitor was traced. The host value of the flag was not read.

**G5. One threshold, two defaults.** `PlatformDegradeInputs.rss_threshold_mb` defaults to 2048
(`src/hft_platform/ops/platform_inputs.py:80`), while the only production construction site passes 3072
(`src/hft_platform/services/bootstrap.py:393`). Code or a test that builds `PlatformDegradeInputs` directly gets a different
limit than the engine.

**G6. Four hand-kept lists of the same reasons.** The controller's auto-recoverable set, the allowed metric codes,
the monitor's `_INFRA_REASON_MAP` (`src/hft_platform/ops/autonomy_monitor.py:305`) and the strings the probes append are separate lists.
G1 to G3 are what drift between hand-kept lists looks like.

## 5. The 2026-04-15 playbook, row by row

The spec table has 13 rows (for example `docs/superpowers/specs/2026-04-15-track-c-autonomy-foundation-design.md:352`). The functions below exist today; whether and where
they are called, and under which flag, was not traced, so this table claims nothing about the 13 behaviours.

| spec row | a function that does the step exists | note |
|---|---|---|
| feed gap, resubscribe | `src/hft_platform/feed_adapter/shioaji/subscription_manager.py:293` | callers not traced |
| session expired, logout and relogin | `src/hft_platform/feed_adapter/shioaji/reconnect_orchestrator.py:57` | its docstring: logout, login, callbacks, subscribe, with cooldown and backoff; callers not traced |
| position drift, auto-correct | `src/hft_platform/execution/reconciliation.py:1133` | see 6.4: this must not be an automatic step |
| contract alias stale, refresh | `src/hft_platform/feed_adapter/shioaji/contracts_runtime.py:859` | callers not traced |
| ClickHouse down, WAL only | the recorder already falls back to the WAL (AGENTS.md architecture section) | not a repair action; the fallback is the normal path |
| disk space, archive WAL | not looked for | |
| broker down more than 5 min, reduce-only then wait for approval | the monitor rule at G1 | the spec says CONFIRM (a human approves); the monitor labels it "auto" |

## 6. Proposal: one fault table, no orchestrator

**6.1 One table.** A single frozen table (a Python module under `src/hft_platform/ops/`, name open) with one row
per reason: the string a probe emits, who emits it (probe, monitor or governor), whether it clears by itself,
its metric code, and the monitor rule and action if any. From it, derive `_AUTO_RECOVERABLE_REASONS`,
`_ALLOWED_REASON_CODES`, `_INFRA_REASON_MAP` and `MonitorDecision.rearm`. Python, not YAML: the removed
framework's YAML is a file that no code reads, which is how config drifts from behaviour.

**6.2 A consistency test.** Five assertions. Once the table exists, assertions 2 to 5 fail on today's code (they are the
probe output turned into tests):

1. every string `reduce_only_reasons()` can append is a row (drive each branch with fakes, as the probe does);
2. every row marked as emitted by a probe has a branch that emits it (catches `feed_gap_exceeded`);
3. no row maps to the metric code `unknown` (catches `reconciliation_unverifiable`, `margin_critical_*`);
4. the monitor's `rearm` equals the controller's classification (catches G1);
5. the defaults in `PlatformDegradeInputs` equal what bootstrap passes (catches G5).

**6.3 Order of work.** Smallest and safest first, each its own change: the table and test with no behaviour change
(assertions 2 to 5 are marked as expected failures until their fixes land); then G5; then G1 (the evidence record becomes
truthful); then G2 and G3 (new metric codes). Renaming `margin_critical_{ratio}` to a fixed reason string changes
a string that `_restore_manual_rearm_state` (`src/hft_platform/ops/platform_degrade.py:536`) reads back after a restart, so
that one is a separate, reviewed change. All of this is on the reduce-only control path, so Tier 3.

**6.4 What stays manual.** No automatic repair on a money path. The spec marks large position drift and a disk
under 1 GB as CONFIRM, and the one automatic row that touches positions is "auto-correct". The platform has had
a reconciliation that deleted its own positions (a local memory note,
`reconciliation_deleted_the_platforms_own_positions.md`, records 19 deletions). Latches that exist on purpose stay:
`recorder_data_loss`, `broker_unavailable` (spec: CONFIRM) and margin critical.

**6.5 A rule taken from the history.** An action is added in the same change as its first caller and a test
that goes through that caller. If the YAML is ever revived, a test must load it with the same loader production
uses and check that every action name resolves in the registry.

## 7. Anomaly detection (EWMA)

The roadmap line says the engine replaces fixed thresholds. The record argues for adding a band beside them:
`src/hft_platform/risk/storm_guard.py:82` in the StormGuard thresholds says the order-RTT limits were set from an April
P99 of 185 ms and then widened by the operator to 1500/2500 ms because live tails had reached 1.7 to 2.3 s and the breaker
fired about 100 times a day. A fixed number hand-tuned after the fact is the case a baseline helps with. But the
same history is also why fixed caps must stay: a baseline that follows a slowly degrading system would follow it
down.

**7.1 Shape.** Off the hot path, in the supervisor tick, reading existing in-process metric samples (the helper
`src/hft_platform/ops/platform_inputs.py:31` already reads one). No new per-tick work, no new instrumentation on the hot path.

**7.2 First signals.** Ones with a slow baseline and an existing fixed threshold: event-loop probe lag, order RTT
peak, deepest queue, WAL backlog files, RSS. Not price signals.

**7.3 Algorithm.** Per signal, an EWMA mean and variance, kept separately for the day and night session (their
distributions differ); score z = (x - mean) / max(sd, floor); alarm after k consecutive ticks above a z limit;
no baseline update while the signal is in alarm, so an outage does not become the new normal; a warm-up period
after every restart, in which the detector reports nothing. Output is a gauge `anomaly_zscore{signal}` and a counter.
**It takes no action.** An isolation forest is left out on purpose: it needs training data, a labelled incident set
does not exist, and an operator woken at 03:00 needs a reason in one line.

**7.4 Promotion gate (all three):** a shadow period of at least N trading days (N chosen before it starts);
the false-alarm rate on those days; and every incident in the incident record flagged no later than the fixed
threshold. Only then may a signal tighten a cap. It never loosens one.

**7.5 The existing drift-burst detector is not the base.** `DriftBurstDetector` is a price-microstructure test
(drift-burst t-statistic of log returns, window `src/hft_platform/risk/drift_burst_detector.py:42`, burst threshold
`src/hft_platform/risk/drift_burst_detector.py:43`) for one instrument. Its history is the argument: fed a rotating
symbol it produced a platform-wide HALT that depended on dict order, and latched on a far-month contract it fired
about 346 times a day; both are documented in `src/hft_platform/services/system.py:305` onward. What to reuse is its guards
(reset when the series changes, a variance floor `src/hft_platform/risk/drift_burst_detector.py:46`, a cooldown), not its statistic.

**7.6 Data.** An in-process EWMA has no history at start. Seeding it, and judging it against past incidents,
needs the metric history in the host Prometheus. Reading that is a read-only host action that was not requested
for this note.

**7.7 Test plan (offline).** Replay synthetic series through the detector: a step, a ramp, a single spike, noise
with a fixed seed. Assert detection latency for the step and ramp, silence on stationary noise, no baseline
update during an alarm, and silence during warm-up. Run it against recorded Prometheus samples once they are
available.

## 8. Not verified

- Who calls the repair functions in section 5, and under which flags; therefore which of the 13 spec rows already
  have an owner.
- Whether anything reads `manual_rearm_required` from the evidence records (G1).
- Host values: `HFT_AUTONOMY_MONITOR_ENABLED`, the `HFT_PLATFORM_REDUCE_ONLY_*` limits, `HFT_STORMGUARD_DRIFT_BURST_ENABLED`.
  No host was contacted.
- Prometheus history (section 7.6).
- Whether any of the six reason codes in G3 is built at run time; the probe only scans for string literals.
- The chaos playbook tests (`tests/chaos/test_playbook_*.py`, six files) and the unit tests for the monitor and controller were not run for this note.
- `make agent-docs-check` was not run: no governing document was changed.

## 9. Decisions for Charlie

| decision | my suggestion |
|---|---|
| Build a healing orchestrator at all? | No. Build the fault table and its test (6.1, 6.2) |
| Should `broker_unavailable` latch? The spec says CONFIRM, the controller latches, the monitor says "auto" | Keep the latch; fix the monitor's label |
| Enable `HFT_AUTONOMY_MONITOR_ENABLED`? It adds flatten on HALT, HALT on DATA_LOSS and the broker and margin rules | Not before G1 to G3 are fixed and a drill has run; check the host value first (needs a read-only host grant) |
| Fix the `unknown` labels (G2) | Yes; the `margin_critical_*` rename is its own reviewed change (6.3) |
| `config/base/healing_playbook.yaml` | Remove it (nothing reads it, 16 of 18 actions never existed), or keep it only if you want the history in the tree |
| Position-drift auto-correct (spec row, `_auto_correct_drift`) | Never an automatic step; confirm this rule |
| EWMA: go for shadow-only on the five signals in 7.2, and request read-only Prometheus access | Yes to shadow-only; the access request is yours to grant |

## Sources

`ROADMAP.md`; `docs/superpowers/specs/2026-04-15-track-c-autonomy-foundation-design.md`; `docs/superpowers/plans/2026-04-16-track-c-autonomy-foundation.md`;
`config/base/healing_playbook.yaml`; `docs/runbooks/chaos-playbook-index.md`; `src/hft_platform/ops/platform_inputs.py`; `src/hft_platform/ops/platform_degrade.py`; `src/hft_platform/ops/autonomy_monitor.py`; `src/hft_platform/ops/autonomy.py`;
`src/hft_platform/services/bootstrap.py`; `src/hft_platform/services/system.py`; `src/hft_platform/risk/storm_guard.py`; `src/hft_platform/risk/drift_burst_detector.py`; `src/hft_platform/strategy/runner.py`; `src/hft_platform/execution/reconciliation.py`; `src/hft_platform/engine/event_bus.py`; `src/hft_platform/risk/engine.py`; `src/hft_platform/services/market_data.py`; `src/hft_platform/risk/validators.py`.
