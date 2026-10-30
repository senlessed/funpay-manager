import asyncio
import random
import time

from funpay_manager.funpay.readonly import FunPayAuthError

from .repository import SyncRepository


class FunPaySyncWorker:
    def __init__(
        self, storage, adapter_context, notification_wake, state, interval=30, batch_size=5
    ):
        self.storage, self.adapter_context = storage, adapter_context
        self.notification_wake, self.state = notification_wake, state
        self.interval, self.batch_size = max(interval, 15), min(max(batch_size, 1), 5)
        self.repository = SyncRepository(storage)
        self.wake = asyncio.Event()

    async def cycle(self):
        expected = await self.storage.get_setting("funpay_account")
        if not expected or not await self.storage.get_setting("sync_enabled", True):
            return None
        async with self.adapter_context(expected["id"]) as adapter:
            account = adapter.account
            chats = await adapter.get_chats()
            selected = await self.repository.snapshot(account, chats, self.batch_size)
            failures = 0
            for chat in selected:
                if not await self.storage.get_setting("sync_enabled", True):
                    return None
                try:
                    messages = await adapter.get_history(chat.id)
                    if await self.repository.ingest(account.id, chat, messages):
                        self.notification_wake.set()
                except FunPayAuthError:
                    raise
                except Exception:
                    failures += 1
                    await self.repository.failed_check(chat.local_id)
            if failures:
                self.state(
                    {
                        "message": (
                            f"Частично: {len(selected) - failures}/{len(selected)} чатов"
                            " · ошибка чтения"
                        ),
                        "chats_count": len(chats),
                    }
                )
                return False
            await self.repository.success(account.id)
            self.state(
                {
                    "message": f"Синхронизировано {len(selected)} из {len(chats)} доступных чатов",
                    "last_success": time.time(),
                    "chats_count": len(chats),
                }
            )
            return True

    async def run(self):
        delay = self.interval
        self.wake.set()
        while True:
            if not await self.storage.get_setting(
                "funpay_account"
            ) or not await self.storage.get_setting("sync_enabled", True):
                self.state("Приостановлена или аккаунт не настроен")
                await self.wake.wait()
                self.wake.clear()
                continue
            self.wake.clear()
            try:
                result = await self.cycle()
                delay = self.interval if result is not False else min(delay * 2, 300)
            except FunPayAuthError:
                await self.storage.set_setting("sync_enabled", False)
                await self.storage.set_setting("sending_enabled", False)
                self.state(
                    {
                        "message": "Сессия/CAPTCHA: проверьте сайт и возобновите синхронизацию",
                        "auth_paused": True,
                    }
                )
                continue
            except Exception:
                self.state("Ошибка FunPay · повтор с задержкой")
                delay = min(max(delay * 2, self.interval), 300)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay + random.uniform(0, 2))
            except TimeoutError:
                pass
