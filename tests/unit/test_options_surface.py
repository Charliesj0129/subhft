"""Tests for VolSurface grid and interpolation."""

import math
from datetime import date

import pytest


def test_surface_update_and_get_exact():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    s.update(20000.0, date(2026, 4, 15), 0.20)
    assert s.get_iv(20000.0, date(2026, 4, 15)) == pytest.approx(0.20)


def test_surface_multiple_strikes():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(19500.0, d, 0.22)
    s.update(20000.0, d, 0.20)
    s.update(20500.0, d, 0.21)
    assert s.get_iv(19500.0, d) == pytest.approx(0.22)
    assert s.get_iv(20000.0, d) == pytest.approx(0.20)
    assert s.get_iv(20500.0, d) == pytest.approx(0.21)


def test_surface_interpolation_between_strikes():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(19000.0, d, 0.25)
    s.update(19500.0, d, 0.22)
    s.update(20000.0, d, 0.20)
    s.update(20500.0, d, 0.21)
    s.update(21000.0, d, 0.24)
    iv = s.get_iv(19750.0, d)
    assert 0.19 < iv < 0.23


def test_surface_stale_iv_excluded():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(20000.0, d, 0.005)  # below 0.01 threshold
    s.update(20500.0, d, 0.20)
    snap = s.snapshot()
    assert (d, 20000) not in snap
    assert (d, 20500) in snap


def test_surface_snapshot():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(20000.0, d, 0.20)
    s.update(20500.0, d, 0.21)
    snap = s.snapshot()
    assert len(snap) == 2
    assert snap[(d, 20000)] == pytest.approx(0.20)


def test_surface_get_iv_no_data_returns_nan():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    result = s.get_iv(20000.0, date(2026, 4, 15))
    assert math.isnan(result)


def test_surface_get_iv_single_point_no_interp():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(20000.0, d, 0.20)
    result = s.get_iv(20500.0, d)
    assert math.isnan(result) or result == pytest.approx(0.20)


def test_surface_skew_pctl25():
    """Percentile-based skew returns a float (renamed from skew_25d)."""
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    for strike, iv in [(19000, 0.28), (19500, 0.24), (20000, 0.20), (20500, 0.22), (21000, 0.26)]:
        s.update(float(strike), d, iv)
    skew = s.skew_pctl25(d)
    assert isinstance(skew, float)


def test_surface_butterfly_pctl25():
    """Percentile-based butterfly returns a float (renamed from butterfly_25d)."""
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    for strike, iv in [(19000, 0.28), (19500, 0.24), (20000, 0.20), (20500, 0.22), (21000, 0.26)]:
        s.update(float(strike), d, iv)
    bf = s.butterfly_pctl25(d)
    assert isinstance(bf, float)


def test_surface_no_extrapolation():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(19000.0, d, 0.25)
    s.update(20000.0, d, 0.20)
    result = s.get_iv(18000.0, d)  # outside range
    assert math.isnan(result)


def test_surface_spline_cache_invalidated_on_update():
    """Spline cache is rebuilt after an update, reflecting new IV data."""
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    for strike, iv in [(19000, 0.25), (19500, 0.22), (20000, 0.20), (20500, 0.21), (21000, 0.24)]:
        s.update(float(strike), d, iv)
    iv1 = s.get_iv(19750.0, d)  # builds and caches spline
    s.update(19500.0, d, 0.30)  # should invalidate cache
    iv2 = s.get_iv(19750.0, d)  # rebuilds spline with new data
    assert iv1 != iv2  # result should differ with updated IV
