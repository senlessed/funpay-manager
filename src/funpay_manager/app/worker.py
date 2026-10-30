import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable

from PySide6.QtCore import QThread, Signal

from funpay_manager.utils.diagnostics import DiagnosticError, FailureCode

logger = logging.getLogger(__name__)


class BackendThread(QThread):
    """Own the asyncio loop in a worker; Qt widgets stay on the GUI thread."""

    ready = Signal()
    failed = Signal(str)
    operation_failed = Signal(str, str)
    completed = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.stop_requested = threading.Event()

    def run(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        if self.stop_requested.is_set():
            self.stop_event.set()
        self.ready.emit()
        await self.stop_event.wait()
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def submit(self, name: str, operation: Callable[[], Awaitable]) -> None:
        if self.loop is None or self.stop_event is None or self.stop_event.is_set():
            return

        async def execute():
            try:
                self.completed.emit(name, await operation())
            except Exception as error:
                # Exception bodies can include session URLs/headers. Log type only.
                code = (
                    error.diagnostic_code
                    if isinstance(error, DiagnosticError)
                    and isinstance(error.diagnostic_code, FailureCode)
                    else FailureCode.UNEXPECTED
                )
                logger.error(
                    "Backend operation %s failed: %s [%s]", name, type(error).__name__, code.value
                )
                self.failed.emit(f"Ошибка операции {name}: {type(error).__name__}")
                self.operation_failed.emit(name, code.value)

        asyncio.run_coroutine_threadsafe(execute(), self.loop)

    def stop(self) -> None:
        self.stop_requested.set()
        if self.loop and self.stop_event and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.stop_event.set)
            except RuntimeError:
                # The loop can close between the check and the thread-safe call.
                if not self.loop.is_closed():
                    raise
