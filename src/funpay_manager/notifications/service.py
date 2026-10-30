import asyncio
import time

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .repository import NotificationRepository


class NotificationService:
    def __init__(self, storage, vault, state, bot_factory=Bot, send_interval=1.1):
        self.storage, self.vault, self.state, self.bot_factory = storage, vault, state, bot_factory
        self.repository = NotificationRepository(storage)
        self.wake = asyncio.Event()
        self.send_interval = send_interval

    async def drain(self):
        binding = await self.storage.get_setting("telegram_binding")
        expected = await self.storage.get_setting("funpay_account")
        if (
            not binding
            or not expected
            or not await self.storage.get_setting("notifications_enabled", True)
        ):
            return None
        due = await self.repository.pending(expected["id"], binding)
        if due is None:
            return None
        due = max(due, await self.repository.cooldown(binding))
        if due > time.time():
            return due - time.time()
        token = await asyncio.to_thread(self.vault().get, "telegram_token")
        if not token:
            raise RuntimeError("Telegram token missing")
        bot = self.bot_factory(
            token=token, default=DefaultBotProperties(link_preview_is_disabled=True)
        )
        try:
            try:
                identity = await bot.get_me(request_timeout=15)
            except TelegramRetryAfter as error:
                await self.repository.defer(binding, time.time() + error.retry_after)
                self.state("Лимит Telegram при проверке бота · ожидание")
                return max(error.retry_after, 1)
            if identity.id != binding["bot_id"]:
                self.state("Токен не соответствует привязке; отправка уведомлений остановлена")
                return None
            while True:
                event = await self.repository.claim(expected["id"], binding)
                if event is None:
                    due = await self.repository.pending(expected["id"], binding)
                    return max(0.1, due - time.time()) if due is not None else None
                try:
                    markup = None
                    if event["callback_data"]:
                        markup = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="Открыть", callback_data=event["callback_data"]
                                    )
                                ]
                            ]
                        )
                    sent = await bot.send_message(
                        binding["owner_id"],
                        event["text"],
                        parse_mode=None,
                        reply_markup=markup,
                        request_timeout=20,
                    )
                    await self.repository.finish(event["id"], "sent", remote_id=sent.message_id)
                    self.state("Уведомление доставлено")
                except TelegramRetryAfter as error:
                    await self.repository.finish(
                        event["id"],
                        "queued",
                        error="rate_limit",
                        retry_at=time.time() + error.retry_after,
                    )
                    self.state("Лимит Telegram · ожидание")
                    return max(error.retry_after, 1)
                except (TelegramForbiddenError, TelegramBadRequest):
                    await self.repository.finish(event["id"], "failed", error="rejected")
                    self.state("Telegram отклонил уведомление")
                except asyncio.CancelledError:
                    await self.repository.finish(event["id"], "unknown", error="cancelled")
                    raise
                except Exception:
                    await self.repository.finish(event["id"], "unknown", error="unconfirmed")
                    self.state("Результат уведомления неизвестен; автоматического повтора нет")
                await asyncio.sleep(self.send_interval)
        finally:
            await bot.session.close()

    async def run(self):
        self.wake.set()
        backoff = 5
        while True:
            await self.wake.wait()
            self.wake.clear()
            try:
                delay = await self.drain()
                backoff = 5
            except Exception:
                self.state("Ошибка уведомлений · повтор подключения")
                delay = backoff
                backoff = min(backoff * 2, 300)
            if delay is not None:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                self.wake.set()
