import asyncio
import time

from funpay_manager.funpay.readonly import FunPayReadOnlyAdapter
from funpay_manager.security.credentials import CredentialStore
from funpay_manager.security.profile import credential_profile
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
        self._last_chat_attempt = float("-inf")

    def vault(self):
        return self.vault_factory(profile=credential_profile(self.storage.path))

    async def start_telegram(self):
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
                binding, token, self.get_chats, lambda s: self.notify("telegram-status", s)
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
        if self.telegram_task:
            self.telegram_task.cancel()
            await asyncio.gather(self.telegram_task, return_exceptions=True)
            self.telegram_task = None
        self._chat_cache = None
        self._cache_time = 0

    async def connect_funpay(self, key: str, cancelled=None) -> dict:
        adapter = self.adapter_factory(key)
        try:
            account = await adapter.authenticate()
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError("Подключение отменено")
            await asyncio.to_thread(self.vault().set, "funpay_session", key)
            identity = {"id": account.id, "username": account.username}
            await self.storage.set_setting("funpay_account", identity)
            self._chat_cache = None
            return identity
        finally:
            await adapter.close()

    async def get_chats(self):
        async with self._lock:
            if self._chat_cache is not None and time.monotonic() - self._cache_time < 30:
                return self._chat_cache
            expected = await self.storage.get_setting("funpay_account")
            if not expected:
                raise RuntimeError("FunPay не подключён")
            key = await asyncio.to_thread(self.vault().get, "funpay_session")
            if not key:
                raise RuntimeError("Нет сессионного ключа")
            if time.monotonic() - self._last_chat_attempt < 5:
                raise RuntimeError("Подождите перед повторным запросом FunPay")
            self._last_chat_attempt = time.monotonic()
            adapter = self.adapter_factory(key)
            try:
                account = await adapter.authenticate()
                if account.id != expected["id"]:
                    raise RuntimeError("Ключ относится к другому аккаунту")
                chats = await adapter.get_chats()
                self._chat_cache, self._cache_time = chats, time.monotonic()
                return chats
            finally:
                await adapter.close()
