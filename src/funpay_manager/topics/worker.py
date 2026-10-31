import asyncio
import time

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from .manager import TopicNeedsAttention
from .permissions import GroupPermissionError, validate_group


class TopicMirrorService:
    def __init__(self, storage, manager, vault, state, bot_factory=Bot):
        self.storage, self.manager, self.vault = storage, manager, vault
        self.state, self.bot_factory = state, bot_factory
        self.wake = manager.wake
        self.repository = manager.repository

    async def drain(self):
        try:
            group = await self.manager.configuration()
        except GroupPermissionError:
            return None
        cooldown = await self.repository.cooldown(group)
        if cooldown:
            return cooldown
        token = await asyncio.to_thread(self.vault().get, "telegram_token")
        if not token:
            raise RuntimeError("Token missing")
        bot = self.bot_factory(
            token=token, default=DefaultBotProperties(link_preview_is_disabled=True)
        )
        try:
            me = await bot.get_me(request_timeout=15)
            if me.id != group["bot_id"]:
                raise GroupPermissionError("Bot ID mismatch")
            while True:
                if await self.manager.configuration() != group:
                    return None
                event = await self.repository.claim(group)
                if event is None:
                    return None
                posting = False
                try:
                    thread_id = await self.manager.ensure(
                        bot, event["chat_id"], expected_group=group
                    )
                    await validate_group(bot, group["group_id"], group)
                    if await self.manager.configuration() != group:
                        raise GroupPermissionError("Привязка группы изменилась")
                    posting = True
                    # Mark cooldown before the send so a crash/restart cannot burst a group.
                    await self.repository.defer(group, 3.2)
                    mapped = await self.repository.mirror_mapping(event, group)
                    if mapped:
                        if mapped["thread_id"] != thread_id:
                            raise TopicNeedsAttention("Mirror belongs to a previous topic")
                        await bot.edit_message_text(
                            event["text"],
                            chat_id=group["group_id"],
                            message_id=mapped["telegram_message_id"],
                            parse_mode=None,
                            request_timeout=20,
                        )
                        sent = None
                    else:
                        sent = await bot.send_message(
                            group["group_id"],
                            event["text"],
                            message_thread_id=thread_id,
                            parse_mode=None,
                            request_timeout=20,
                        )
                    await self.repository.finish(
                        event["id"],
                        "sent",
                        sent=sent,
                        group=group,
                        thread_id=thread_id,
                        expected_text=event["text"],
                    )
                    self.state("Сообщение доставлено в тему")
                except TelegramRetryAfter as error:
                    await self.repository.defer(group, error.retry_after)
                    await self.repository.finish(
                        event["id"], "queued", "rate_limit", time.time() + error.retry_after
                    )
                    return max(error.retry_after, 1)
                except GroupPermissionError:
                    await self.repository.finish(event["id"], "queued", "permissions")
                    await self.storage.set_setting("topics_enabled", False)
                    self.state("Topics приостановлены: проверьте закрытость группы и права")
                    return None
                except TopicNeedsAttention:
                    await self.repository.finish(event["id"], "queued", "topic_attention")
                    self.state("Тема требует ручной привязки/восстановления")
                    continue
                except TelegramBadRequest as error:
                    problem = error.message.lower()
                    if "message is not modified" in problem:
                        await self.repository.finish(
                            event["id"], "sent", expected_text=event["text"]
                        )
                        continue
                    missing = any(
                        marker in problem
                        for marker in (
                            "message thread not found",
                            "topic_deleted",
                            "topic_closed",
                            "message_thread_id_invalid",
                        )
                    )
                    if missing:
                        await self.repository.set_state(event["chat_id"], "invalid")
                    await self.repository.finish(
                        event["id"],
                        "queued" if missing else "failed",
                        "topic_missing" if missing else "rejected",
                    )
                except asyncio.CancelledError:
                    await self.repository.finish(
                        event["id"], "unknown" if posting else "queued", "cancelled"
                    )
                    raise
                except Exception:
                    await self.repository.finish(
                        event["id"], "unknown" if posting else "queued", "unconfirmed"
                    )
                    self.state("Доставка в тему не подтверждена")
                    return 30
                cooldown = await self.repository.cooldown(group)
                if cooldown:
                    return cooldown
        finally:
            await bot.session.close()

    async def run(self):
        self.wake.set()
        delay = 5
        while True:
            await self.wake.wait()
            self.wake.clear()
            try:
                next_delay = await self.drain()
                delay = 5
            except GroupPermissionError:
                await self.storage.set_setting("topics_enabled", False)
                self.state("Topics приостановлены: группа или bot ID не прошли проверку")
                next_delay = None
            except TelegramRetryAfter as error:
                group = await self.storage.get_setting("telegram_group")
                if group:
                    await self.repository.defer(group, error.retry_after)
                next_delay = max(error.retry_after, 1)
            except Exception:
                self.state("Ошибка Topics · повтор подключения")
                next_delay, delay = delay, min(delay * 2, 300)
            if next_delay is not None:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=next_delay)
                except TimeoutError:
                    pass
                self.wake.set()
