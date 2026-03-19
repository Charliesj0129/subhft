import asyncio

import pytest


@pytest.mark.chaos
class TestOverflow:
    @pytest.mark.asyncio
    async def test_full(self):
        q = asyncio.Queue(maxsize=2)
        await q.put("a")
        await q.put("b")
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait("c")
        assert q.qsize() == 2
