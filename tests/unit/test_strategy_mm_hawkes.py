"""Unit tests for mm_hawkes strategy (numba/hftbacktest based)."""

import pytest

try:
    import numba  # noqa: F401
    from hftbacktest import GTX, LIMIT  # noqa: F401

    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

pytestmark = pytest.mark.skipif(not HAS_DEPS, reason="numba/hftbacktest not installed")


@pytest.mark.skipif(not HAS_DEPS, reason="numba/hftbacktest not installed")
def test_import_mm_hawkes():
    """Should not raise when dependencies are available."""
    from hft_platform.strategies.mm_hawkes import strategy  # noqa: F401

    assert strategy is not None


@pytest.mark.skipif(not HAS_DEPS, reason="numba/hftbacktest not installed")
def test_hawkes_tracker_instantiation():
    """HawkesTracker should instantiate without error."""
    from hft_platform.strategies.mm_hawkes import HawkesTracker

    tracker = HawkesTracker(mu=0.1, alpha=0.5, beta=1.0)
    assert tracker.mu == 0.1
    assert tracker.intensity == 0.1
