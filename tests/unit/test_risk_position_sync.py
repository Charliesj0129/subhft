"""Tests for RiskPositionSyncer (WU-05)."""

from __future__ import annotations

import threading

from hft_platform.risk.position_sync import RiskPositionSyncer


class TestRiskPositionSyncerInitialState:
    def test_initial_empty(self) -> None:
        syncer = RiskPositionSyncer()
        assert syncer.get_all_broker_positions() == {}
        assert syncer.last_sync_ts == 0.0

    def test_get_broker_qty_missing_symbol(self) -> None:
        syncer = RiskPositionSyncer()
        assert syncer.get_broker_qty("2330") is None


class TestRiskPositionSyncerUpdate:
    def test_update_stores_positions(self) -> None:
        syncer = RiskPositionSyncer()
        syncer.update(discrepancies=[], broker_map={"2330": 1000, "2317": -500})

        assert syncer.get_broker_qty("2330") == 1000
        assert syncer.get_broker_qty("2317") == -500
        assert syncer.get_broker_qty("XXXX") is None

    def test_update_overwrites_previous(self) -> None:
        syncer = RiskPositionSyncer()
        syncer.update(discrepancies=[], broker_map={"2330": 1000})
        syncer.update(discrepancies=[], broker_map={"2317": 200})

        assert syncer.get_broker_qty("2330") is None
        assert syncer.get_broker_qty("2317") == 200

    def test_last_sync_ts_updated(self) -> None:
        syncer = RiskPositionSyncer()
        assert syncer.last_sync_ts == 0.0

        syncer.update(discrepancies=[], broker_map={"2330": 1})
        assert syncer.last_sync_ts > 0


class TestRiskPositionSyncerGetAllReturnsCopy:
    def test_returns_copy(self) -> None:
        syncer = RiskPositionSyncer()
        syncer.update(discrepancies=[], broker_map={"2330": 100})

        positions = syncer.get_all_broker_positions()
        positions["2330"] = 999  # mutate the copy

        assert syncer.get_broker_qty("2330") == 100  # original unchanged


class TestRiskPositionSyncerThreadSafety:
    def test_concurrent_update_and_read(self) -> None:
        syncer = RiskPositionSyncer()
        errors: list[Exception] = []
        iterations = 200

        def writer() -> None:
            try:
                for i in range(iterations):
                    syncer.update(
                        discrepancies=[{"sym": "2330", "delta": i}],
                        broker_map={"2330": i, "2317": -i},
                    )
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(iterations):
                    syncer.get_all_broker_positions()
                    syncer.get_broker_qty("2330")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"
