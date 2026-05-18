"""Tests for Shioaji Solace callback arity shim (B fix, 2026-05-18).

Reproduces the prod crash pattern where libsolclient calls
``SolClient.*_callback_wrap`` with 5 positional args while shioaji 1.2.9
declares 4, raising TypeError → pybind11 terminate → SIGABRT (engine
crash-loop ~hourly).
"""

from __future__ import annotations

import sys
import types

import pytest


def _install_fake_solace_module(monkeypatch: pytest.MonkeyPatch) -> type:
    """Install a fake shioaji.backend.solace.api with a strict-arity SolClient.

    The fake module mirrors the prod failure: each ``*_callback_wrap`` is
    defined with self + 3 positional args. Calling with 4 raises TypeError
    before the shim runs.
    """

    class SolClient:
        def onreply_callback_wrap(self, a, b, c):  # noqa: ANN001
            return ("onreply", a, b, c)

        def reply_callback_wrap(self, a, b, c):  # noqa: ANN001
            return ("reply", a, b, c)

        def event_callback_wrap(self, a, b, c):  # noqa: ANN001
            return ("event", a, b, c)

        def msg_callback_wrap(self, a, b, c):  # noqa: ANN001
            return ("msg", a, b, c)

        def p2p_callback_wrap(self, a, b, c):  # noqa: ANN001
            return ("p2p", a, b, c)

        def session_down_callback_wrap(self, a, b, c):  # noqa: ANN001
            return ("down", a, b, c)

    # Build fake module hierarchy.
    sj_mod = types.ModuleType("shioaji")
    backend_mod = types.ModuleType("shioaji.backend")
    solace_pkg = types.ModuleType("shioaji.backend.solace")
    api_mod = types.ModuleType("shioaji.backend.solace.api")
    api_mod.SolClient = SolClient  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "shioaji", sj_mod)
    monkeypatch.setitem(sys.modules, "shioaji.backend", backend_mod)
    monkeypatch.setitem(sys.modules, "shioaji.backend.solace", solace_pkg)
    monkeypatch.setitem(sys.modules, "shioaji.backend.solace.api", api_mod)
    return SolClient


def test_strict_arity_raises_without_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check: bare SolClient crashes with 4 user-provided args (5 incl self)."""
    SolClient = _install_fake_solace_module(monkeypatch)
    inst = SolClient()
    with pytest.raises(TypeError, match=r"takes 4 positional arguments but 5 were given"):
        inst.onreply_callback_wrap(1, 2, 3, 4)  # type: ignore[call-arg]


def test_shim_absorbs_extra_positional_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the shim is applied, the same 5-arg call must succeed and the
    first 3 user args are forwarded to the original wrap."""
    SolClient = _install_fake_solace_module(monkeypatch)

    # Reset the module-level guard so the shim re-applies on this fake.
    from hft_platform.feed_adapter.shioaji import client as _client

    monkeypatch.setattr(_client, "_SOLACE_ARITY_SHIM_APPLIED", False, raising=False)
    _client._apply_solace_arity_shim()

    inst = SolClient()
    # No exception, and extras silently dropped.
    result = inst.onreply_callback_wrap(1, 2, 3, "extra")  # type: ignore[call-arg]
    assert result == ("onreply", 1, 2, 3)


def test_shim_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling _apply_solace_arity_shim twice must not double-wrap."""
    SolClient = _install_fake_solace_module(monkeypatch)
    from hft_platform.feed_adapter.shioaji import client as _client

    monkeypatch.setattr(_client, "_SOLACE_ARITY_SHIM_APPLIED", False, raising=False)
    _client._apply_solace_arity_shim()
    first_wrap = SolClient.onreply_callback_wrap

    # Second apply must early-out (guard flag set).
    _client._apply_solace_arity_shim()
    assert SolClient.onreply_callback_wrap is first_wrap
    assert getattr(first_wrap, "_hft_arity_shim", False) is True


def test_shim_covers_all_callback_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    """All 6 wrap methods that libsolclient invokes must be shimmed so a
    signature change on any one of them cannot crash the engine."""
    SolClient = _install_fake_solace_module(monkeypatch)
    from hft_platform.feed_adapter.shioaji import client as _client

    monkeypatch.setattr(_client, "_SOLACE_ARITY_SHIM_APPLIED", False, raising=False)
    _client._apply_solace_arity_shim()

    inst = SolClient()
    expected = {
        "onreply_callback_wrap": "onreply",
        "reply_callback_wrap": "reply",
        "event_callback_wrap": "event",
        "msg_callback_wrap": "msg",
        "p2p_callback_wrap": "p2p",
        "session_down_callback_wrap": "down",
    }
    for attr, tag in expected.items():
        fn = getattr(inst, attr)
        # Each must accept the surprise 4th arg without raising.
        out = fn(10, 20, 30, "surprise")
        assert out == (tag, 10, 20, 30)
        assert getattr(getattr(SolClient, attr), "_hft_arity_shim", False) is True


def test_shim_skips_when_solace_module_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the Solace backend module is unimportable (e.g. test env without
    the wheel), the shim must no-op and set the applied flag so the engine
    still boots."""
    # Ensure no fake module is registered so the import inside the shim fails.
    for mod in [
        "shioaji.backend.solace.api",
        "shioaji.backend.solace",
        "shioaji.backend",
    ]:
        monkeypatch.delitem(sys.modules, mod, raising=False)

    from hft_platform.feed_adapter.shioaji import client as _client

    monkeypatch.setattr(_client, "_SOLACE_ARITY_SHIM_APPLIED", False, raising=False)
    _client._apply_solace_arity_shim()
    assert _client._SOLACE_ARITY_SHIM_APPLIED is True
