"""``resolve_order_mode`` writes to ``os.environ``, and monkeypatch cannot undo it.

Found 2026-09-07 at 99% of a full ``pytest tests/unit tests/integration -n 8``
run: ``tests/integration/test_recon_mismatch_drill.py::
test_recon_small_mismatch_detected`` failed with ``assert 0 == 1``, and passed
when its file was run alone.

```
  worker gw4
    ...
    test_bootstrap_lifecycle.py       monkeypatch.setenv(HFT_ORDER_SIMULATION)
      -> resolve_order_mode()         os.environ["HFT_ORDER_MODE"] = "sim"
      -> teardown                     monkeypatch restores HFT_ORDER_SIMULATION
                                      X  HFT_ORDER_MODE was never recorded
    ...
    test_recon_mismatch_drill.py      ReconciliationService() reads sim
      -> local != 0, broker == 0      -> "not comparable", 0 discrepancies
      -> assert len(...) == 1         FAILED
```

The drill is the regression cover for the position-truth path, so the leak did
not make it fail loudly -- it made it assert nothing, on whichever shard
collected a bootstrap test first.

The write-back itself is correct production behaviour: ``ReconciliationService``
and ``StartupRecon`` both read ``HFT_ORDER_MODE`` from the environment at
construction time, so normalizing a legacy or aliased value in place is how
they see the canonical one. The test harness is what has to isolate it --
``tests/conftest.py::_restore_order_mode_env``.
"""

import os
from pathlib import Path

import pytest

from hft_platform.services.bootstrap import resolve_order_mode

_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def test_resolve_order_mode_writes_back_past_monkeypatch():
    """The legacy path assigns a variable monkeypatch never recorded."""
    sentinel = object()
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("HFT_ORDER_MODE", raising=False)
        mp.setenv("HFT_ORDER_SIMULATION", "1")
        assert resolve_order_mode() == "sim"
        assert os.environ.get("HFT_ORDER_MODE") == "sim"

    # monkeypatch has now undone everything it recorded. HFT_ORDER_SIMULATION
    # is gone; HFT_ORDER_MODE is not, because bootstrap set it directly.
    assert os.environ.get("HFT_ORDER_SIMULATION", sentinel) is sentinel
    assert os.environ.get("HFT_ORDER_MODE") == "sim", (
        "if this stops leaking, resolve_order_mode no longer writes back and the conftest fixture below can go with it"
    )


def test_conftest_restores_the_order_mode_env_after_every_test():
    """The autouse fixture that contains the leak must stay autouse.

    Source-level because a function-scoped fixture's teardown cannot be
    observed from inside the test it wraps, and an ordering-dependent pair of
    tests is exactly the fragility this is fixing.
    """
    source = _CONFTEST.read_text()
    idx = source.find("def _restore_order_mode_env(")
    assert idx != -1, "tests/conftest.py lost the HFT_ORDER_MODE restore fixture"
    decorator = source.rfind("@pytest.fixture", 0, idx)
    assert "autouse=True" in source[decorator:idx], "the restore fixture is no longer autouse"
    assert 'os.environ.pop("HFT_ORDER_MODE", None)' in source[idx:], (
        "the fixture must delete the variable when it was unset before the test, not merely overwrite it"
    )
