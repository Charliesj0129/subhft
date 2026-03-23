"""Tests for adaptive column width profiles."""

from hft_platform.monitor._renderer import compute_column_profile


def test_narrow_terminal():
    cp = compute_column_profile(100)
    assert cp.show_drivers is False
    assert cp.show_spark is False
    assert cp.name_width == 8


def test_normal_terminal():
    cp = compute_column_profile(150)
    assert cp.show_drivers is True
    assert cp.show_spark is True
    assert cp.name_width == 10


def test_wide_terminal():
    cp = compute_column_profile(200)
    assert cp.spark_width == 30
    assert cp.name_width == 20
