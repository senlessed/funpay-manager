import asyncio
from contextlib import asynccontextmanager


class RequestGate:
    """Serialize FunPay requests across sessions and space their start times."""

    def __init__(self, interval: float = 2):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last_start = float("-inf")

    @asynccontextmanager
    async def slot(self):
        async with self._lock:
            loop = asyncio.get_running_loop()
            remaining = self.interval - (loop.time() - self._last_start)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_start = loop.time()
            yield
