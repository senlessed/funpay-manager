import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from .permissions import GroupPermissionError, validate_group
from .repository import TopicRepository


class TopicNeedsAttention(RuntimeError):
    """Creation or binding needs explicit local-owner recovery."""


class TopicManager:
    def __init__(self, storage, messages, wake):
        self.storage, self.messages, self.wake = storage, messages, wake
        self.repository = TopicRepository(storage)
        self._lock = asyncio.Lock()

    async def configuration(self):
        group = await self.storage.get_setting("telegram_group")
        binding = await self.storage.get_setting("telegram_binding")
        account = await self.storage.get_setting("funpay_account")
        if (
            not group
            or not binding
            or not account
            or not await self.storage.get_setting("topics_enabled", False)
            or group["account_id"] != account["id"]
            or (group["owner_id"], group["bot_id"]) != (binding["owner_id"], binding["bot_id"])
        ):
            raise GroupPermissionError("Topics отключены или привязаны к другому аккаунту")
        return group

    async def ensure(self, bot, local_id, expected_group=None):
        async with self._lock:
            group = await self.configuration()
            if expected_group is not None and group != expected_group:
                raise GroupPermissionError("Привязка группы изменилась")
            chat = await self.messages.checked_chat(local_id)
            await validate_group(bot, group["group_id"], group)
            if await self.configuration() != group:
                raise GroupPermissionError("Привязка группы изменилась во время проверки")
            try:
                create, row = await self.repository.reserve(local_id, group)
            except ValueError as error:
                raise TopicNeedsAttention("Тема относится к другой привязке") from error
            if not create:
                if row["status"] == "ready":
                    return row["thread_id"]
                raise TopicNeedsAttention("Нужна ручная привязка/подтверждение новой темы")
            try:
                topic = await bot.create_forum_topic(
                    group["group_id"],
                    name=f"{chat['username'][:90]} · {chat['remote_id']}"[:128],
                    request_timeout=20,
                )
                if topic.message_thread_id <= 1:
                    raise ValueError("Invalid thread ID")
                await self.repository.set_state(local_id, "ready", topic.message_thread_id)
                return topic.message_thread_id
            except TelegramRetryAfter as error:
                await self.repository.set_state(local_id, "pending")
                await self.repository.defer(group, error.retry_after)
                raise
            except TelegramBadRequest:
                await self.repository.set_state(local_id, "pending")
                raise
            except asyncio.CancelledError:
                await self.repository.set_state(local_id, "unknown")
                raise
            except Exception as error:
                await self.repository.set_state(local_id, "unknown")
                raise TopicNeedsAttention("Результат создания темы неизвестен") from error

    async def authorize_operation(self, bot, operation):
        group = await self.configuration()
        if (
            operation["telegram_chat_id"] != group["group_id"]
            or operation["source_binding_key"] != group["binding_key"]
            or operation["source_owner_id"] != group["owner_id"]
        ):
            raise GroupPermissionError("Исходящая операция относится к другой привязке")
        await validate_group(bot, group["group_id"], group)
        mapping = await self.repository.mapping(operation["chat_id"])
        if (
            not mapping
            or mapping["status"] != "ready"
            or mapping["binding_key"] != group["binding_key"]
            or mapping["thread_id"] != operation["source_thread_id"]
        ):
            raise GroupPermissionError("Тема больше не привязана к этому чату")

    async def handle_group(self, bot, message, update_id=None):
        if (
            not message.from_user
            or message.from_user.is_bot
            or getattr(message, "sender_chat", None)
            or getattr(message, "forward_origin", None)
            or getattr(message, "via_bot", None)
        ):
            return
        group = await self.configuration()
        if (
            message.from_user.id != group["owner_id"]
            or message.chat.id != group["group_id"]
            or message.chat.type != "supergroup"
            or not getattr(message, "message_thread_id", None)
        ):
            return
        text = (message.text or "").strip()
        if not text:
            return
        await validate_group(bot, group["group_id"], group)
        if text.startswith("/attach "):
            parts = text.split()
            if len(parts) != 3 or not parts[1].isascii() or not parts[1].isdecimal():
                return
            chat = await self.messages.checked_chat(int(parts[1]), parts[2])
            await self.repository.attach(chat["id"], group, message.message_thread_id)
            self.wake.set()
            await bot.send_message(
                group["group_id"],
                "[Служебно] Тема привязана к чату " + chat["remote_id"],
                message_thread_id=message.message_thread_id,
            )
            return
        if text.startswith("/"):
            return
        local_id = await self.repository.chat_for_thread(group, message.message_thread_id)
        if local_id is None:
            return
        if not await self.storage.get_setting("sending_enabled", False):
            await bot.send_message(
                group["group_id"],
                "[Служебно] Отправка выключена в Windows. Текст не поставлен в очередь.",
                message_thread_id=message.message_thread_id,
            )
            return
        await self.messages.enqueue_topic(
            local_id, group, message.message_id, message.message_thread_id, message.text, update_id
        )
