import asyncio

from funpay_manager.funpay.messaging import NotSentError
from funpay_manager.storage.messages import MessageRepository


class MessageService:
    def __init__(self, storage, adapter_context):
        self.storage = storage
        self.repository = MessageRepository(storage)
        self.adapter_context = adapter_context
        self._history_lock = asyncio.Lock()
        self._history_times = {}
        self.wake = asyncio.Event()

    async def checked_chat(self, local_id, route_key=None):
        expected = await self.storage.get_setting("funpay_account")
        chat = await self.repository.chat(local_id)
        if (
            not chat
            or not expected
            or chat["account_id"] != expected["id"]
            or route_key is not None
            and chat["route_key"] != route_key
        ):
            raise ValueError("Чат не принадлежит подключённому аккаунту")
        return chat

    def clear_history_cache(self):
        self._history_times.clear()

    async def history(self, local_id, page=0, route_key=None):
        chat = await self.checked_chat(local_id, route_key)
        refreshed = False
        if page == 0:
            async with self._history_lock:
                loop = asyncio.get_running_loop()
                if loop.time() - self._history_times.get(local_id, float("-inf")) >= 10:
                    self._history_times[local_id] = loop.time()
                    try:
                        async with self.adapter_context(chat["account_id"]) as adapter:
                            messages = await adapter.get_history(chat["remote_id"])
                            await self.repository.save_history(
                                local_id, chat["account_id"], chat["remote_id"], messages
                            )
                        refreshed = True
                    except Exception:
                        pass  # Local history remains available during outages.
        return chat, await self.repository.history(local_id, page), refreshed

    async def prepare_reply(self, local_id, bot_id, telegram_chat, prompt_id):
        await self.checked_chat(local_id)
        await self.repository.add_prompt(bot_id, telegram_chat, prompt_id, local_id)

    async def enqueue_reply(
        self, bot_id, telegram_chat, telegram_message, prompt_id, text, update_id=None
    ):
        if not text.strip() or len(text) > 4000:
            raise ValueError("Сообщение должно содержать 1–4000 символов")
        local_id = await self.repository.prompt_chat(bot_id, telegram_chat, prompt_id)
        if local_id is None:
            raise ValueError("Ответьте на актуальный запрос «Ответить»")
        chat = await self.checked_chat(local_id)
        # Keep text even when sending has been paused since the prompt was created.
        operation = await self.repository.enqueue(
            chat, bot_id, telegram_chat, telegram_message, text, update_id
        )
        self.wake.set()
        return operation

    async def process_one(self):
        if not await self.storage.get_setting("sending_enabled", False):
            return False
        expected = await self.storage.get_setting("funpay_account")
        if not expected or not await self.repository.has_ready(expected["id"]):
            return False
        # Authentication failures must not turn a queued message into an in-flight write.
        async with self.adapter_context(expected["id"]) as adapter:
            if not await self.storage.get_setting("sending_enabled", False):
                return False
            operation = await self.repository.claim(expected["id"])
            if operation is None:
                return False
            operation_id = operation["operation_id"]
            try:
                message = await adapter.send_text(operation["remote_id"], operation["text"])
                if (
                    message.account_id != operation["account_id"]
                    or message.chat_id != operation["remote_id"]
                    or message.author_id != operation["account_id"]
                    or message.text != operation["text"]
                    or message.kind != "text"
                    or not message.id.isascii()
                    or not message.id.isdecimal()
                    or int(message.id) <= 0
                ):
                    raise ValueError("Подтверждение относится к другому сообщению")
                await self.repository.confirm(operation, message)
            except NotSentError:
                await self.repository.finish(operation_id, "failed", error="not_sent")
            except asyncio.CancelledError:
                await self.repository.finish(operation_id, "unknown", error="cancelled")
                raise
            except Exception:
                await self.repository.finish(operation_id, "unknown", error="unconfirmed")
            return True

    async def run(self):
        self.wake.set()
        delay = 5
        while True:
            await self.wake.wait()
            self.wake.clear()
            try:
                while await self.process_one():  # noqa: ASYNC110 -- deliberate request rate limit
                    await asyncio.sleep(2)
                delay = 5
            except Exception:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                self.wake.set()
                delay = min(delay * 2, 60)
