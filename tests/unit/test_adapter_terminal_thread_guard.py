"""P1-3: engine-thread guard on OrderAdapter terminal-tracking helpers.

The helpers (`_record_recent_terminal`, `_prune_cancel_inflight`,
`_mark_cancel_inflight`, `_clear_cancel_inflight`, `_is_recently_terminal`)
mutate ``_recently_terminal_orders`` / ``_cancel_inflight_targets`` without a
lock because they are documented engine-loop-only. The guard makes a misuse
from another thread loud rather than silent corruption.
"""

from __future__ import annotations

import threading

import pytest

from hft_platform.order.adapter import OrderAdapter


def _make_adapter() -> OrderAdapter:
    """Build a bare adapter without invoking the (heavy) load_config path."""
    adapter = OrderAdapter.__new__(OrderAdapter)
    # Minimal slots needed for the helpers under test.
    import collections

    adapter._engine_thread_id = None
    adapter._recently_terminal_orders = collections.OrderedDict()
    adapter._recently_terminal_max = 64
    adapter._recently_terminal_ttl_s = 60.0
    adapter._cancel_inflight_targets = collections.OrderedDict()
    adapter._cancel_inflight_max = 64
    adapter._cancel_inflight_ttl_s = 30.0
    return adapter


def test_first_call_pins_engine_thread():
    adapter = _make_adapter()
    assert adapter._engine_thread_id is None
    adapter._record_recent_terminal("k1", reason="filled")
    assert adapter._engine_thread_id == threading.get_ident()


def test_same_thread_reuse_does_not_raise():
    adapter = _make_adapter()
    adapter._record_recent_terminal("k1", reason="filled")
    # Subsequent calls on the same thread must continue to work.
    adapter._mark_cancel_inflight("k2")
    adapter._clear_cancel_inflight("k2")
    assert adapter._is_recently_terminal("k1") is True
    adapter._prune_cancel_inflight()  # no raise


def test_cross_thread_call_raises_runtime_error():
    adapter = _make_adapter()
    # Pin the engine thread on the main thread.
    adapter._record_recent_terminal("k1", reason="filled")

    captured: dict[str, BaseException | None] = {"err": None}

    def _other_thread() -> None:
        try:
            adapter._mark_cancel_inflight("intruder")
        except BaseException as exc:  # noqa: BLE001
            captured["err"] = exc

    t = threading.Thread(target=_other_thread)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    err = captured["err"]
    assert isinstance(err, RuntimeError)
    assert "non-engine thread" in str(err)


@pytest.mark.parametrize(
    "method_name,args",
    [
        ("_record_recent_terminal", ("k", "filled")),
        ("_is_recently_terminal", ("k",)),
        ("_mark_cancel_inflight", ("k",)),
        ("_clear_cancel_inflight", ("k",)),
        ("_prune_cancel_inflight", ()),
    ],
)
def test_each_guarded_method_rejects_other_thread(method_name, args):
    adapter = _make_adapter()
    # Pin engine thread by a no-op call.
    adapter._mark_cancel_inflight("__seed__")
    adapter._clear_cancel_inflight("__seed__")

    captured: dict[str, BaseException | None] = {"err": None}

    def _other_thread() -> None:
        try:
            getattr(adapter, method_name)(*args)
        except BaseException as exc:  # noqa: BLE001
            captured["err"] = exc

    t = threading.Thread(target=_other_thread)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert isinstance(captured["err"], RuntimeError), method_name
