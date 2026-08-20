"""The broker session's account id must actually reach the fill path.

``BrokerProtocol.get_default_account_id`` carries an explicit contract: its
value "must match the ``account_id`` value that ``ExecutionNormalizer``
resolves from fill callbacks, so that recovery keys align with live fill keys
in ``PositionStore``".  ``ExecutionRouter`` built its normalizer with two
positional arguments and never passed one, so ``_default_account_id`` was
permanently ``""`` and step 3 of the documented resolution chain was dead
code.  A fill arriving without its own account field went straight to the
reject branch -- ``fill_rejected_missing_account_id``, no PositionStore
update, no fill event -- which is a silent position/reconciliation gap rather
than a loud failure.

The value cannot be read at construction time: the router is built before the
broker session authenticates, and ``get_default_account_id`` answers ``""``
until then.  It is therefore wired as a provider and resolved on first use.
"""

from __future__ import annotations

import asyncio
import time

from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.router import ExecutionRouter


def _symbols_cfg(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")
    return cfg


def _fill(seqno: str) -> RawExecEvent:
    return RawExecEvent(
        "deal",
        {
            "seqno": seqno,
            "ordno": "O1",
            "code": "AAA",
            "action": "Buy",
            "quantity": 1,
            "price": 1.00,
            "ts": 1,
            # account_id intentionally omitted -- this is the shape that was
            # being dropped.
        },
        time.time_ns(),
    )


def test_router_hands_the_broker_account_provider_to_its_normalizer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    router = ExecutionRouter(
        bus=None,
        raw_queue=asyncio.Queue(),
        order_id_map={},
        position_store=None,
        terminal_handler=lambda *_: None,
        default_account_id_provider=lambda: "SJ-ACCT-007",
    )
    event = router.normalizer.normalize_fill(_fill("F1"))
    assert event is not None, "fill was rejected -- the provider never reached the normalizer"
    assert event.account_id == "SJ-ACCT-007"


def test_account_id_is_resolved_after_login_not_at_construction(tmp_path, monkeypatch) -> None:
    """The session authenticates after the router is built."""
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    session = {"account": ""}  # not logged in yet
    norm = ExecutionNormalizer(default_account_id_provider=lambda: session["account"])

    assert norm.normalize_fill(_fill("F2")) is None  # pre-login: still fail-closed

    session["account"] = "SJ-ACCT-007"
    event = norm.normalize_fill(_fill("F3"))
    assert event is not None
    assert event.account_id == "SJ-ACCT-007"


def test_resolved_account_is_cached_so_the_fill_path_stays_off_the_sdk(tmp_path, monkeypatch) -> None:
    """Fills run on the broker thread; the provider must be called at most once."""
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    calls = {"n": 0}

    def provider() -> str:
        calls["n"] += 1
        return "SJ-ACCT-007"

    norm = ExecutionNormalizer(default_account_id_provider=provider)
    for i in range(5):
        assert norm.normalize_fill(_fill(f"F{i}")) is not None
    assert calls["n"] == 1


def test_an_explicit_payload_account_still_wins_over_the_session_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer(default_account_id_provider=lambda: "SJ-ACCT-007")
    raw = _fill("F9")
    raw.data["account_id"] = "live-account-99"
    event = norm.normalize_fill(raw)
    assert event is not None
    assert event.account_id == "live-account-99"


def test_a_broker_session_error_stays_fail_closed(tmp_path, monkeypatch) -> None:
    """Mid-reconnect the SDK can raise; a fill must be rejected, not mis-attributed."""
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))

    def provider() -> str:
        raise RuntimeError("session not ready")

    norm = ExecutionNormalizer(default_account_id_provider=provider)
    assert norm.normalize_fill(_fill("F10")) is None


def test_no_provider_keeps_the_previous_reject_behaviour(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer()
    assert norm.normalize_fill(_fill("F11")) is None
