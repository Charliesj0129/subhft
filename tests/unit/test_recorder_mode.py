"""Tests for CE3-01: RecorderMode enum and get_recorder_mode()."""
import os

import pytest

from hft_platform.recorder.mode import RecorderMode, get_recorder_mode


def test_default_mode_is_direct(monkeypatch):
    monkeypatch.delenv("HFT_RECORDER_MODE", raising=False)
    monkeypatch.delenv("HFT_DISABLE_CLICKHOUSE", raising=False)
    assert get_recorder_mode() == RecorderMode.DIRECT


def test_explicit_direct(monkeypatch):
    monkeypatch.setenv("HFT_RECORDER_MODE", "direct")
    monkeypatch.delenv("HFT_DISABLE_CLICKHOUSE", raising=False)
    assert get_recorder_mode() == RecorderMode.DIRECT


def test_explicit_wal_first(monkeypatch):
    monkeypatch.setenv("HFT_RECORDER_MODE", "wal_first")
    monkeypatch.delenv("HFT_DISABLE_CLICKHOUSE", raising=False)
    assert get_recorder_mode() == RecorderMode.WAL_FIRST


def test_disable_clickhouse_maps_to_wal_first(monkeypatch):
    monkeypatch.setenv("HFT_DISABLE_CLICKHOUSE", "1")
    assert get_recorder_mode() == RecorderMode.WAL_FIRST


def test_invalid_value_falls_back_to_direct(monkeypatch):
    monkeypatch.setenv("HFT_RECORDER_MODE", "unknown_value")
    monkeypatch.delenv("HFT_DISABLE_CLICKHOUSE", raising=False)
    assert get_recorder_mode() == RecorderMode.DIRECT


def test_enum_values():
    assert RecorderMode.DIRECT.value == "direct"
    assert RecorderMode.WAL_FIRST.value == "wal_first"
