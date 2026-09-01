import os
import sys

os.environ.setdefault("HFT_EVENT_MODE", "event")
os.environ.setdefault("HFT_GATEWAY_STARTUP_HOLDOFF_S", "0")

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Add src/ first so compiled hft_platform.rust_core takes priority.
for candidate in (SRC, ROOT):
    path = str(candidate)
    if path not in sys.path:
        sys.path.insert(0, path)

# The rust_core/ directory at ROOT is Cargo source, not a Python package.
# If Python accidentally imported it as a namespace package (before the
# compiled extension was loaded), evict it so the real module can be found.

_rust_ns = sys.modules.get("rust_core")
if _rust_ns is not None and getattr(_rust_ns, "__spec__", None) is not None:
    if getattr(_rust_ns.__spec__, "origin", None) is None:
        # Namespace package — evict so hft_platform.rust_core alias can fill in
        del sys.modules["rust_core"]

# ---------------------------------------------------------------------------
# WU-T01: Shared factory functions and fixtures
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock

import numpy as np
import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.events import BidAskEvent, MetaData, TickEvent

# Default values (Precision Law: all prices scaled x10000)
_DEFAULT_SYMBOL = "2330"
_DEFAULT_PRICE = 5_000_000  # 500.0 * 10000
_DEFAULT_TS_NS = 1_700_000_000_000_000_000


def make_order_intent(**overrides) -> OrderIntent:
    """Create an OrderIntent with sensible defaults. Override any field via kwargs."""
    defaults = {
        "intent_id": 1,
        "strategy_id": "test_strategy",
        "symbol": _DEFAULT_SYMBOL,
        "intent_type": IntentType.NEW,
        "side": Side.BUY,
        "price": _DEFAULT_PRICE,
        "qty": 1,
        "tif": TIF.LIMIT,
        "target_order_id": None,
        "timestamp_ns": _DEFAULT_TS_NS,
        "source_ts_ns": _DEFAULT_TS_NS,
        "reason": "",
        "trace_id": "",
        "idempotency_key": "",
        "ttl_ns": 0,
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def make_fill_event(**overrides) -> FillEvent:
    """Create a FillEvent with sensible defaults. Override any field via kwargs."""
    defaults = {
        "fill_id": "FILL-001",
        "account_id": "ACC-001",
        "order_id": "ORD-001",
        "strategy_id": "test_strategy",
        "symbol": _DEFAULT_SYMBOL,
        "side": Side.BUY,
        "qty": 1,
        "price": _DEFAULT_PRICE,
        "fee": 0,
        "tax": 0,
        "ingest_ts_ns": _DEFAULT_TS_NS,
        "match_ts_ns": _DEFAULT_TS_NS,
    }
    defaults.update(overrides)
    return FillEvent(**defaults)


def make_order_command(**overrides) -> OrderCommand:
    """Create an OrderCommand with sensible defaults. Override any field via kwargs."""
    intent_overrides = overrides.pop("intent", None)
    if intent_overrides is None:
        intent = make_order_intent()
    elif isinstance(intent_overrides, dict):
        intent = make_order_intent(**intent_overrides)
    else:
        # Already an OrderIntent instance
        intent = intent_overrides

    defaults = {
        "cmd_id": 1,
        "intent": intent,
        "deadline_ns": _DEFAULT_TS_NS + 1_000_000_000,  # 1s after default ts
        "storm_guard_state": StormGuardState.NORMAL,
        "created_ns": _DEFAULT_TS_NS,
    }
    defaults.update(overrides)
    return OrderCommand(**defaults)


def make_tick_event(**overrides) -> TickEvent:
    """Create a TickEvent with sensible defaults. Override any field via kwargs."""
    meta_overrides = overrides.pop("meta", None)
    if meta_overrides is None:
        meta = MetaData(seq=1, source_ts=_DEFAULT_TS_NS, local_ts=_DEFAULT_TS_NS)
    elif isinstance(meta_overrides, dict):
        meta_defaults = {"seq": 1, "source_ts": _DEFAULT_TS_NS, "local_ts": _DEFAULT_TS_NS, "topic": ""}
        meta_defaults.update(meta_overrides)
        meta = MetaData(**meta_defaults)
    else:
        meta = meta_overrides

    defaults = {
        "meta": meta,
        "symbol": _DEFAULT_SYMBOL,
        "price": _DEFAULT_PRICE,
        "volume": 100,
        "total_volume": 1000,
        "bid_side_total_vol": 500,
        "ask_side_total_vol": 500,
        "is_simtrade": False,
        "is_odd_lot": False,
    }
    defaults.update(overrides)
    return TickEvent(**defaults)


def make_bidask_event(**overrides) -> BidAskEvent:
    """Create a BidAskEvent with sensible defaults. Override any field via kwargs."""
    meta_overrides = overrides.pop("meta", None)
    if meta_overrides is None:
        meta = MetaData(seq=1, source_ts=_DEFAULT_TS_NS, local_ts=_DEFAULT_TS_NS)
    elif isinstance(meta_overrides, dict):
        meta_defaults = {"seq": 1, "source_ts": _DEFAULT_TS_NS, "local_ts": _DEFAULT_TS_NS, "topic": ""}
        meta_defaults.update(meta_overrides)
        meta = MetaData(**meta_defaults)
    else:
        meta = meta_overrides

    # Default 5-level book: bids descending, asks ascending from default price
    tick_size = 1_000  # 0.1 * 10000
    if "bids" not in overrides:
        bids = np.array(
            [[_DEFAULT_PRICE - i * tick_size, 100] for i in range(5)],
            dtype=np.int64,
        )
    else:
        bids = overrides.pop("bids")

    if "asks" not in overrides:
        asks = np.array(
            [[_DEFAULT_PRICE + (i + 1) * tick_size, 100] for i in range(5)],
            dtype=np.int64,
        )
    else:
        asks = overrides.pop("asks")

    defaults = {
        "meta": meta,
        "symbol": _DEFAULT_SYMBOL,
        "bids": bids,
        "asks": asks,
        "stats": None,
        "fused_stats": None,
        "is_snapshot": False,
    }
    defaults.update(overrides)
    return BidAskEvent(**defaults)


@pytest.fixture(autouse=True)
def _isolate_autonomy_state_paths(tmp_path, monkeypatch):
    """Keep autonomy state and evidence writes inside the test's tmp dir.

    ``manual_rearm.DEFAULT_RUNTIME_STATE_PATH`` and
    ``evidence.DEFAULT_AUTONOMY_EVIDENCE_DIR`` are CWD-relative, so any test that
    builds a real ``PlatformDegradeController``/``ManualRearmGate`` without an
    explicit ``state_path`` writes ``outputs/production_rollout/autonomy/`` under
    whatever directory pytest was launched from. On 2026-07-08 a test run on the
    production host left ``reason="test_reason"`` plus phantom ``strat1``/
    ``strat_a`` latches in ``/home/charl/subhft``, where ``hft ops
    autonomy-status`` then reported them as real operator-facing state.
    """
    from hft_platform.ops import evidence as _evidence
    from hft_platform.ops import manual_rearm as _manual_rearm
    from hft_platform.ops import platform_degrade as _platform_degrade

    base = tmp_path / "autonomy_state"
    monkeypatch.setattr(_manual_rearm, "DEFAULT_RUNTIME_STATE_PATH", base / "runtime_state.json")
    monkeypatch.setattr(_evidence, "DEFAULT_AUTONOMY_EVIDENCE_DIR", base)
    _evidence.reset_shared_autonomy_evidence_writer()
    # The degrade controller is a process-wide singleton holding the platform's
    # own REDUCE_ONLY/HALT latch, and a latch is designed to survive -- so it
    # survives the test that set it. ``tests/spec/test_resilience.py`` drives a
    # real ``HFTSystem`` to HALT and leaves the platform in REDUCE_ONLY for the
    # rest of the process: every later test that dispatches a NEW order gets it
    # DLQ'd with ``reason="platform_reduce_only"``. Alphabetical collection put
    # ``tests/e2e`` before ``tests/spec``, so a full run hid it and naming a
    # tier explicitly did not.
    _platform_degrade.reset_shared_platform_degrade_controller()
    try:
        yield
    finally:
        _evidence.reset_shared_autonomy_evidence_writer()
        _platform_degrade.reset_shared_platform_degrade_controller()


@pytest.fixture(autouse=True)
def _isolate_execution_state_paths(tmp_path, monkeypatch):
    """Keep the ``.state/`` durability files inside the test's tmp dir.

    Sibling of ``_isolate_autonomy_state_paths``, for the same reason one level
    over: the fill-dedup window, position checkpoint, order-id map, gateway
    dedup window and leader lease all default to CWD-relative ``.state/``
    paths, so every test that builds a real execution path writes into the
    checkout pytest was launched from.

    These files are *durability* state, and durability is the point: they
    survive the process. So they also survive the pytest run. A fill id a test
    replays -- ``SEQ001`` in ``tests/e2e/test_04_execution_plane.py`` -- lands
    in ``.state/fill_dedup_window.jsonl`` and is deduplicated on the next run,
    so the test passes exactly once on a clean tree and fails on every run
    after that. The failure looks like a flake and is not: it is the previous
    run's evidence doing its job.
    """
    # Deliberately NOT created: several tests assert their ``tmp_path`` is
    # empty, and a directory this fixture made would be a file they never
    # wrote. Every writer below does ``mkdir(parents=True)`` on first use, so
    # the directory appears only when a test actually persists something.
    base = tmp_path / "exec_state"
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(base / "fill_dedup_window.jsonl"))
    monkeypatch.setenv("HFT_DEDUP_PERSIST_PATH", str(base / "dedup_window.jsonl"))
    monkeypatch.setenv("HFT_POSITION_CHECKPOINT_PATH", str(base / "position_checkpoint.json"))
    monkeypatch.setenv("HFT_ORDER_ID_MAP_PERSIST_PATH", str(base / "order_id_map.jsonl"))
    monkeypatch.setenv("HFT_GATEWAY_LEADER_LEASE_PATH", str(base / "gateway_leader.lock"))


@pytest.fixture(autouse=True)
def _isolate_wal_and_state_dirs(tmp_path, monkeypatch):
    """Keep the WAL and state *directories* out of the checkout too.

    ``_isolate_execution_state_paths`` names five individual files, but the
    two directories they live beside are read straight from the environment
    with a CWD-relative default -- ``HFT_WAL_DIR`` (``.wal``), its market-data
    sibling ``HFT_MD_WAL_DIR``, and ``HFT_STATE_DIR`` (``.state``, home of the
    execution-overflow DLQ). A full unit run wrote 350 KB of real batch files
    and a DLQ line into the working tree.

    That is not just litter. Both directories are shared mutable state keyed
    only by the working directory, so two pytest processes in one checkout
    interfere: on 2026-08-26 a unit run and a non-unit run started seconds
    apart reported 13 failures that neither produced alone.

    Tests that exercise these paths deliberately set the same variables
    themselves, which still wins -- ``monkeypatch``/``patch.dict`` applied
    inside the test overrides what this fixture set up.
    """
    wal = tmp_path / "wal"
    state = tmp_path / "state"
    monkeypatch.setenv("HFT_WAL_DIR", str(wal))
    monkeypatch.setenv("HFT_MD_WAL_DIR", str(wal))
    monkeypatch.setenv("HFT_STATE_DIR", str(state))
    monkeypatch.setenv("HFT_CONTRACT_REFRESH_STATUS_PATH", str(state / "contract_refresh_status.json"))


@pytest.fixture(autouse=True)
def _disarm_loop_stall_watchdog(monkeypatch):
    """Never let a test start a watchdog that can ``os._exit`` the test process.

    ``HFTSystem.run()`` starts a real :class:`LoopStallWatchdog` on a daemon OS
    thread (``services/system.py``, threshold ``HFT_LOOP_STALL_KILL_S``, default
    60 s). Its production ``on_stall`` is ``os._exit(70)`` — by design, so a
    starved live engine dies and the container restarts it.

    Any test that calls ``run()`` without reaching the ``stop_async()`` that
    stops the watchdog (e.g. because ``stop_async`` is mocked) leaks that thread.
    It outlives the test, and ~60 s later force-exits **pytest itself**: the run
    dies mid-collection with exit 70, no summary line, no failing test name, and
    the blame lands on whichever unrelated test happened to be executing. That
    is exactly how CI failed on 2026-07-26 with 14k passing tests and zero F/E.

    Disabling it globally is safe: ``stall_kill_s <= 0`` short-circuits
    ``check_once`` and ``start()``, and the watchdog's own tests
    (``tests/unit/test_loop_watchdog.py``) construct it directly with an explicit
    threshold and an injected ``on_stall``, so they never read this env var.
    """
    monkeypatch.setenv("HFT_LOOP_STALL_KILL_S", "0")


@pytest.fixture(autouse=True)
def _reset_broker_login_slot(monkeypatch):
    """Clear the process-wide broker login slot between tests.

    ``_infra`` keeps the last login-release timestamp in module state so facades
    can space their re-logins. Without a reset, one test's release makes the next
    test's ``acquire_login_slot`` sleep out the remaining gap — a real multi-second
    stall that looks like flakiness rather than the shared state it is.

    The reset only covers *between* tests. The default 5 s gap
    (``HFT_SESSION_REFRESH_STAGGER_S``) still bites *within* a test that logs in
    more than once: the second and later acquisitions each ``time.sleep`` the full
    gap. ``test_reconnect_chaos_three_consecutive_login_timeouts_backoff_reaches_cap``
    reconnects three times and so burned 10 real seconds, blowing CI's
    ``--timeout=10`` even though nothing was wrong with the code under test.
    Zeroing the gap keeps the serialisation semantics (the lock still orders
    logins) while removing the wall-clock pacing, which is broker-protection
    behaviour, not logic any unit test should pay for. Tests that assert on the
    pacing set the interval explicitly — see
    ``test_session_refresh_passes_configured_stagger_settings_to_slot``.
    """
    monkeypatch.setenv("HFT_SESSION_REFRESH_STAGGER_S", "0")

    from hft_platform.feed_adapter.shioaji import _infra as _shioaji_infra

    _shioaji_infra.reset_login_slot_for_tests()
    try:
        yield
    finally:
        _shioaji_infra.reset_login_slot_for_tests()


@pytest.fixture()
def mock_metrics() -> MagicMock:
    """Return a MagicMock that can stand in for MetricsRegistry."""
    mock = MagicMock()
    mock.name = "MockMetricsRegistry"
    return mock
