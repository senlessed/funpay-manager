import asyncio
import json

from funpay_manager.funpay.commerce import RaiseCategory
from funpay_manager.funpay.readonly import FunPayAuthError
from funpay_manager.storage.orders import OrdersRepository
from funpay_manager.storage.raising import RaiseRepository


class OrdersWorker:
    def __init__(self, storage, adapter_context, topic_wake, state):
        self.storage, self.adapter_context, self.topic_wake, self.state = (
            storage,
            adapter_context,
            topic_wake,
            state,
        )
        self.repository = OrdersRepository(storage)
        self.wake = asyncio.Event()

    async def cycle(self):
        account = await self.storage.get_setting("funpay_account")
        if not account or not await self.storage.get_setting("orders_enabled", True):
            return
        async with self.adapter_context(account["id"]) as adapter:
            orders, more = await adapter.get_orders()
            await self.repository.save(account["id"], orders, more)
        self.topic_wake.set()
        self.state(
            f"Заказы обновлены: {len(orders)}" + (" · только первая страница" if more else "")
        )

    async def run(self):
        self.wake.set()
        delay = 60
        while True:
            await self.wake.wait()
            self.wake.clear()
            try:
                await self.cycle()
                delay = 60
            except FunPayAuthError:
                await self.storage.set_setting("orders_enabled", False)
                await self.storage.set_setting("auto_raise_enabled", False)
                self.state("Заказы: сессия/CAPTCHA, проверьте сайт и переподключите FunPay")
            except Exception:
                delay = min(delay * 2, 300)
                self.state("Заказы: ошибка чтения · повтор позже")
            # Also revisit titles after read failures: cached statuses can expire.
            self.topic_wake.set()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            self.wake.set()


class AutoRaiseWorker:
    def __init__(self, storage, adapter_context, state):
        self.storage, self.adapter_context, self.state = storage, adapter_context, state
        self.repository = RaiseRepository(storage)
        self.wake = asyncio.Event()

    async def cycle(self):
        account = await self.storage.get_setting("funpay_account")
        if not account or not await self.storage.get_setting("auto_raise_enabled", False):
            return
        # Authenticate before claiming: a session failure does not imply a POST.
        rows = await self.repository.rows(account["id"])
        import time

        if not any(
            r["enabled"]
            and r["status"] in ("idle", "raised", "cooldown", "paused")
            and r["next_attempt"] <= time.time()
            for r in rows
        ):
            return
        async with self.adapter_context(account["id"]) as adapter:
            job = await self.repository.claim(account["id"])
            if not job:
                return
            try:
                cat = RaiseCategory(job["game_id"], job["name"], tuple(json.loads(job["nodes"])))
                result = await adapter.raise_category(
                    cat, before_post=lambda: self.repository.authorized(job)
                )
                await self.repository.finish(job, result.status, result.wait_seconds)
                if result.status == "auth":
                    await self.storage.set_setting("auto_raise_enabled", False)
                self.state(
                    f"{job['name']}: "
                    + {
                        "raised": "поднято",
                        "cooldown": "ожидание ограничения FunPay",
                        "unknown": "результат неизвестен · категория остановлена",
                        "rejected": "отказ · категория остановлена",
                        "paused": "приостановлено",
                        "auth": "сессия недействительна",
                    }.get(result.status, "остановлено")
                )
            except asyncio.CancelledError:
                await self.repository.finish(job, "unknown")
                raise
            except FunPayAuthError:
                await self.repository.finish(job, "auth")
                await self.storage.set_setting("auto_raise_enabled", False)
                self.state("Автоподнятие выключено: проверьте сессию/CAPTCHA")
            except Exception:
                await self.repository.finish(job, "unknown")
                self.state("Результат поднятия неизвестен · категория остановлена")

    async def run(self):
        self.wake.set()
        while True:
            await self.wake.wait()
            self.wake.clear()
            try:
                await self.cycle()
            except FunPayAuthError:
                await self.storage.set_setting("auto_raise_enabled", False)
                self.state("Автоподнятие выключено: проверьте сессию/CAPTCHA")
            except Exception:
                self.state("Автоподнятие: ошибка подключения · повтор позже")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except TimeoutError:
                pass
            self.wake.set()
