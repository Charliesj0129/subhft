from unittest.mock import MagicMock

from hft_platform.execution.fill_dlq import OrphanedFillDLQ


class TestDLQ:
    def test_add_count(self):
        d = OrphanedFillDLQ(100)
        d.add(MagicMock(symbol="2330"))
        assert d.count == 1

    def test_bounded(self):
        d = OrphanedFillDLQ(3)
        for i in range(5):
            d.add(MagicMock())
        assert d.count == 3

    def test_drain(self):
        d = OrphanedFillDLQ(100)
        for i in range(3):
            d.add(MagicMock())
        assert len(d.drain()) == 3
        assert d.count == 0
