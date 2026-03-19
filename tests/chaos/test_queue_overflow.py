import asyncio
import pytest
@pytest.mark.chaos
class TestQueueOverflow:
    @pytest.mark.asyncio
    async def test_bounded_queue_raises_on_full(self):
        q = asyncio.Queue(maxsize=2)
        await q.put("a")
        await q.put("b")
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait("c")
        assert q.qsize() == 2
