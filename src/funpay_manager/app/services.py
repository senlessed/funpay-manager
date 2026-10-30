import asyncio
import time
from contextlib import asynccontextmanager

from funpay_manager.funpay.messaging import FunPayMessagingAdapter
from funpay_manager.funpay.rate_limit import RequestGate
from funpay_manager.funpay.readonly import FunPayReadOnlyAdapter
from funpay_manager.messages.service import MessageService
from funpay_manager.notifications.service import NotificationService
from funpay_manager.security.credentials import CredentialStore
from funpay_manager.security.profile import credential_profile
from funpay_manager.sync.worker import FunPaySyncWorker
from funpay_manager.telegram.service import TelegramBotService


class ApplicationServices:
    def __init__(
        self, storage, notify, vault_factory=CredentialStore, adapter_factory=FunPayReadOnlyAdapter
    ):
        self.storage, self.notify = storage, notify
        self.vault_factory, self.adapter_factory = vault_factory, adapter_factory
        self.telegram_task = None
        self._lock = asyncio.Lock()
        self._chat_cache = None
        self._cache_time = 0
        self._cache_account = None
        self._last_chat_attempt = float("-inf")
        self.messages = MessageService(storage, self.messaging_adapter)
        self.sender_task = None
        self._lifecycle_lock = asyncio.Lock()
        self.request_gate = RequestGate()
        self.notifier = NotificationService(
            storage, self.vault, lambda state: self.notify("notification-status", state)
        )
        self.messages.notification_wake = self.notifier.wake
        self.sync = FunPaySyncWorker(
            storage,
            self.read_adapter,
            self.notifier.wake,
            lambda state: self.notify("sync-status", state),
        )
        self.sync_task = None
        self.notification_task = None

    def vault(self):
        return self.vault_factory(profile=credential_profile(self.storage.path))

    async def start_telegram(self):
        async with self._lifecycle_lock:
            await self._start_services()

    async def _start_services(self):
        if self.notification_task is None or self.notification_task.done():
            await self.notifier.repository.recover()
            self.notification_task = asyncio.create_task(self.notifier.run(), name="notifications")
        if self.sync_task is None or self.sync_task.done():
            self.sync_task = asyncio.create_task(self.sync.run(), name="funpay-sync")
        self.sync.wake.set()
        self.notifier.wake.set()
        if self.sender_task is None or self.sender_task.done():
            await self.messages.repository.recover()
            self.sender_task = asyncio.create_task(self.messages.run(), name="message-outbox")
        if self.telegram_task and not self.telegram_task.done():
            return
        binding = await self.storage.get_setting("telegram_binding")
        if not binding:
            return
        try:
            token = await asyncio.to_thread(self.vault().get, "telegram_token")
            if not token:
                self.notify("telegram-status", "Нет токена в Credential Manager")
                return
            service = TelegramBotService(
                binding,
                token,
                self.get_chats,
                lambda s: self.notify("telegram-status", s),
                messages=self.messages,
                storage=self.storage,
            )
        except Exception:
            self.notify("telegram-status", "Не удалось открыть защищённое хранилище")
            return

        async def run():
            try:
                await service.run(self.storage)
            except Exception:
                self.notify("telegram-status", "Ошибка запуска · нажмите «Переподключить»")

        self.telegram_task = asyncio.create_task(run(), name="telegram-bot")

    async def stop_telegram(self):
        async with self._lifecycle_lock:
            await self._stop_services()

    async def _stop_services(self):
        for task in (self.sync_task, self.notification_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.sync_task, self.notification_task) if task),
            return_exceptions=True,
        )
        self.sync_task = self.notification_task = None
        if self.telegram_task:
            self.telegram_task.cancel()
            await asyncio.gather(self.telegram_task, return_exceptions=True)
            self.telegram_task = None
        await self._stop_sender()
        self._chat_cache = None
        self._cache_time = 0
        self.messages.clear_history_cache()

    async def stop_sender(self):
        async with self._lifecycle_lock:
            await self._stop_sender()

    async def _stop_sender(self):
        if self.sender_task:
            self.sender_task.cancel()
            await asyncio.gather(self.sender_task, return_exceptions=True)
            self.sender_task = None

    async def set_sending(self, enabled):
        await self.storage.set_setting("sending_enabled", enabled)
        self.messages.wake.set()

    async def set_background(self, sync_enabled, notifications_enabled):
        if sync_enabled is not None:
            await self.storage.set_setting("sync_enabled", sync_enabled)
        if notifications_enabled is not None:
            await self.storage.set_setting("notifications_enabled", notifications_enabled)
        if notifications_enabled is False:
            await self.notifier.repository.disable()
        self.sync.wake.set()
        self.notifier.wake.set()
        return {
            "sync": await self.storage.get_setting("sync_enabled", True),
            "notifications": await self.storage.get_setting("notifications_enabled", True),
        }

    @asynccontextmanager
    async def read_adapter(self, expected_id):
        key = await asyncio.to_thread(self.vault().get, "funpay_session")
        if not key:
            raise RuntimeError("Нет сессионного ключа")
        adapter = self.adapter_factory(key)
        adapter.request_gate = self.request_gate
        try:
            account = await adapter.authenticate()
            if account.id != expected_id:
                raise RuntimeError("Ключ относится к другому аккаунту")
            yield adapter
        finally:
            await adapter.close()

    @asynccontextmanager
    async def messaging_adapter(self, expected_id):
        key = await asyncio.to_thread(self.vault().get, "funpay_session")
        if not key:
            raise RuntimeError("Нет сессионного ключа")
        adapter = FunPayMessagingAdapter(key)
        adapter.request_gate = self.request_gate
        try:
            account = await adapter.authenticate()
            if account.id != expected_id:
                raise RuntimeError("Сессионный ключ относится к другому аккаунту")
            yield adapter
        finally:
            await adapter.close()

    async def connect_funpay(self, key: str, cancelled=None) -> dict:
        restart_sender = self.sender_task is not None
        await self.stop_telegram()
        adapter = self.adapter_factory(key)
        adapter.request_gate = self.request_gate
        try:
            account = await adapter.authenticate()
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError("Подключение отменено")
            await self.storage.set_setting("sending_enabled", False)
            await asyncio.to_thread(self.vault().set, "funpay_session", key)
            identity = {"id": account.id, "username": account.username}
            await self.storage.set_setting("funpay_account", identity)
            self._chat_cache = None
            self.messages.clear_history_cache()
            return identity
        finally:
            await adapter.close()
            if restart_sender:
                await self.start_telegram()

    async def get_chats(self):
        async with self._lock:
            expected = await self.storage.get_setting("funpay_account")
            if not expected:
                raise RuntimeError("FunPay не подключён")
            if (
                self._chat_cache is not None
                and self._cache_account == expected["id"]
                and time.monotonic() - self._cache_time < 30
            ):
                return self._chat_cache
            key = await asyncio.to_thread(self.vault().get, "funpay_session")
            if not key:
                raise RuntimeError("Нет сессионного ключа")
            if time.monotonic() - self._last_chat_attempt < 5:
                raise RuntimeError("Подождите перед повторным запросом FunPay")
            self._last_chat_attempt = time.monotonic()
            adapter = self.adapter_factory(key)
            adapter.request_gate = self.request_gate
            try:
                account = await adapter.authenticate()
                if account.id != expected["id"]:
                    raise RuntimeError("Ключ относится к другому аккаунту")
                chats = await adapter.get_chats()
                chats = await self.messages.repository.save_chats(account, chats)
                self._chat_cache, self._cache_time = chats, time.monotonic()
                self._cache_account = account.id
                return chats
            finally:
                await adapter.close()
