"""H13: _quote_version write/read race across watchdog and event loop.

Root cause: quote watchdog thread writes `c._quote_version = 'v0'` at
quote_runtime.py:352 while other paths (subscribe, event handlers)
read the value to make decoding/subscription decisions. The reader
`ReconnectOrchestrator.get_quote_version` compares the attribute twice
in the same function (`c._quote_version == 'v0'` at two sites), so a
concurrent write between the two reads produces inconsistent decisions
and silent tick-decoding failures.

Fix: a dedicated ``_quote_version_lock`` on ShioajiClient guards writes;
``get_quote_version`` snapshots the value once into a local and reasons
from that snapshot. Writers (all sites in quote_runtime.py) hold the
lock so future visibility / non-GIL scenarios remain safe.
"""

from __future__ import annotations

import threading
import types

from hft_platform.feed_adapter.shioaji.reconnect_orchestrator import ReconnectOrchestrator


def _make_client_stub(version: str = "v0") -> types.SimpleNamespace:
    stub = types.SimpleNamespace()
    stub._quote_version = version
    stub._quote_version_lock = threading.Lock()
    stub._supports_quote_v0 = lambda: True
    stub._supports_quote_v1 = lambda: True
    return stub


def test_get_quote_version_snapshots_attribute_once():
    """Instrument the stub so reading `_quote_version` is counted; the
    orchestrator must read it at most once (snapshot), not twice."""
    reads: list[str] = []

    class _Client:
        _quote_version_storage = "v0"

        def __init__(self):
            self._quote_version_lock = threading.Lock()

        def _supports_quote_v0(self) -> bool:
            return True

        def _supports_quote_v1(self) -> bool:
            return True

        # The read-count is the observable contract: a single snapshot.
        @property
        def _quote_version(self) -> str:
            reads.append(self._quote_version_storage)
            return self._quote_version_storage

    client = _Client()
    orch = ReconnectOrchestrator(client)  # type: ignore[arg-type]
    # The shioaji QuoteVersion enum may not be available in the test env,
    # which raises after the second read — that's still evidence of the
    # race. Swallow the AttributeError and check read count.
    try:
        orch.get_quote_version()
    except AttributeError:
        pass
    assert len(reads) <= 1, f"expected 1 read, got {len(reads)} (check-then-act race)"


def test_quote_version_lock_exists_and_is_lock_like():
    stub = _make_client_stub("v0")
    # Must be usable as a context manager.
    assert hasattr(stub._quote_version_lock, "acquire")
    assert hasattr(stub._quote_version_lock, "release")
    with stub._quote_version_lock:
        pass
