import asyncio
import re

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramConflictError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.types import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from funpay_manager.topics.permissions import GroupPermissionError
from funpay_manager.topics.view import topic_screen
from funpay_manager.utils.text import telegram_text

from .notification_view import notification_screen


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Чаты", callback_data="chats:0")],
            [InlineKeyboardButton(text="🟢 Статус", callback_data="status")],
            [InlineKeyboardButton(text="📤 Очередь", callback_data="outbox")],
            [InlineKeyboardButton(text="🔔 Уведомления", callback_data="notifications")],
        ]
    )


class TelegramBotService:
    """One private polling consumer. Every handler verifies owner AND private chat."""

    def __init__(
        self,
        binding: dict,
        token: str,
        chats,
        state,
        bot_factory=Bot,
        messages=None,
        storage=None,
        topics=None,
        group_pairing=None,
    ):
        self.binding, self.token = binding, token
        self.chats, self.state, self.bot_factory = chats, state, bot_factory
        self.bot = None
        self.messages, self.storage = messages, storage
        self.topics, self.group_pairing = topics, group_pairing
        self.dispatcher = Dispatcher()
        self.dispatcher.message.register(self.message)
        self.dispatcher.callback_query.register(self.callback)

    def authorized(self, user, chat) -> bool:
        return bool(
            user
            and not user.is_bot
            and user.id == self.binding["owner_id"]
            and chat
            and chat.type == "private"
            and chat.id == user.id
        )

    async def message(self, message, event_update=None):
        if message.chat.type == "supergroup" and self.topics:
            if (
                not message.from_user
                or message.from_user.is_bot
                or message.from_user.id != self.binding["owner_id"]
                or getattr(message, "sender_chat", None)
            ):
                return
            try:
                if self.group_pairing and await self.group_pairing.handle(self.bot, message):
                    return
                group = await self.storage.get_setting("telegram_group")
                if not group or group["group_id"] != message.chat.id:
                    return
                await self.topics.handle_group(
                    self.bot, message, getattr(event_update, "update_id", None)
                )
            except GroupPermissionError:
                await self.storage.set_setting("topics_enabled", False)
                self.state("Группа Topics не прошла проверку; Topics приостановлены")
            except Exception:
                # No raw group text, codes, headers or Telegram errors in diagnostics.
                self.state("Не удалось обработать сообщение/команду Topics")
            return
        if (
            not self.authorized(message.from_user, message.chat)
            or not message.text
            or not message.text.strip()
        ):
            return
        if message.text.split(maxsplit=1)[0] in ("/start", "/menu", "/status", "/cancel"):
            if message.text.strip() == "/cancel" and self.messages:
                await self.messages.repository.cancel_prompts(
                    self.binding["bot_id"], message.chat.id
                )
            await self.bot.send_message(
                message.chat.id, self.status_text(), reply_markup=menu(), parse_mode=None
            )
        elif self.messages and not message.text.lstrip().startswith("/"):
            replied = getattr(message, "reply_to_message", None)
            if not replied:
                return
            try:
                operation = await self.messages.enqueue_reply(
                    self.binding["bot_id"],
                    message.chat.id,
                    message.message_id,
                    replied.message_id,
                    message.text,
                    getattr(event_update, "update_id", None),
                )
            except ValueError:
                await self.bot.send_message(
                    message.chat.id,
                    "Не поставлено в очередь. Откройте чат → «Ответить» и ответьте на запрос бота.",
                )
                return
            await self.bot.send_message(
                message.chat.id,
                f"Сохранено в очереди: {operation}.\n"
                "Это ещё не подтверждение отправки. Статус — в «Очереди».",
                reply_markup=menu(),
                parse_mode=None,
            )

    def status_text(self) -> str:
        return (
            "FunPay Telegram Manager\nTelegram: бот работает.\n"
            "Чаты и локальная история доступны после подключения FunPay.\n"
            "Отправка включается отдельно в окне Windows; по умолчанию выключена.\n"
            "Topics подключаются отдельно в Windows. Заказы и поднятие пока недоступны."
        )

    async def callback(self, query):
        message = query.message
        if not self.authorized(query.from_user, getattr(message, "chat", None)):
            return
        data = query.data or ""
        if self.topics and await topic_screen(self.topics, self.bot, query, data):
            return
        if data == "notifications" and self.storage:
            await query.answer()
            await notification_screen(self.storage, self.binding, self.bot, message.chat.id)
            return
        if self.messages and await self.message_callback(query, data):
            return
        if data not in ("menu", "status") and not (
            data.startswith("chats:")
            and data[6:].isascii()
            and data[6:].isdecimal()
            and len(data) <= 10
        ):
            await query.answer("Неизвестная кнопка")
            return
        await query.answer()
        if data in ("menu", "status"):
            text, markup = self.status_text(), menu()
        else:
            try:
                chats = await self.chats()
            except Exception:
                text, markup = (
                    (
                        "Не удалось загрузить чаты. Подключите FunPay в окне программы "
                        "или проверьте сессию и интернет."
                    ),
                    menu(),
                )
            else:
                page = min(int(data[6:]), max(0, (len(chats) - 1) // 5))
                text = "Последние доступные чаты FunPay (до 50)\n"
                for chat in chats[page * 5 : page * 5 + 5]:
                    text += f"\n{chat.username[:100]} · ID {chat.id}\n"
                    text += (chat.last_message or "Без текста")[:350] + "\n"
                if not chats:
                    text += "Чатов нет."
                buttons = []
                if page:
                    buttons.append(
                        InlineKeyboardButton(text="←", callback_data=f"chats:{page - 1}")
                    )
                if (page + 1) * 5 < len(chats):
                    buttons.append(
                        InlineKeyboardButton(text="→", callback_data=f"chats:{page + 1}")
                    )
                markup = InlineKeyboardMarkup(inline_keyboard=[buttons] if buttons else [])
                if self.messages:
                    markup.inline_keyboard[:0] = [
                        [
                            InlineKeyboardButton(
                                text=f"История: {chat.username[:40]}",
                                callback_data=f"h:{chat.local_id}:{chat.route_key}:0",
                            )
                        ]
                        for chat in chats[page * 5 : page * 5 + 5]
                        if chat.local_id and chat.route_key
                    ]
                markup.inline_keyboard.append(
                    [
                        InlineKeyboardButton(text="Обновить", callback_data=f"chats:{page}"),
                        InlineKeyboardButton(text="Меню", callback_data="menu"),
                    ]
                )
        if (
            getattr(message, "text", None) != text
            or getattr(message, "reply_markup", None) != markup
        ):
            await self.bot.edit_message_text(
                telegram_text(text),
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup,
                parse_mode=None,
            )

    async def message_callback(self, query, data):
        parts = data.split(":")
        valid = (
            data == "outbox"
            or re.fullmatch(r"outbox:[0-9]{1,5}", data)
            or re.fullmatch(r"h:[1-9][0-9]{0,18}:[a-f0-9]{16}:[0-9]{1,5}", data)
            or re.fullmatch(r"reply:[1-9][0-9]{0,18}:[a-f0-9]{16}", data)
            or re.fullmatch(r"(?:close|dismiss):[a-f0-9]{32}", data)
        )
        if not valid:
            return False
        await query.answer()
        target = query.message.chat.id
        try:
            if parts[0] == "outbox":
                queue_page = min(int(parts[1]), 10000) if len(parts) == 2 else 0
                expected = await self.storage.get_setting("funpay_account")
                operations = (
                    await self.messages.repository.operations(expected["id"], queue_page)
                    if expected
                    else []
                )
                enabled = await self.storage.get_setting("sending_enabled", False)
                text = f"Очередь · отправка {'включена' if enabled else 'выключена'}\n"
                buttons = []
                for operation in operations:
                    label = (
                        "Закрыта владельцем, без повтора"
                        if operation["last_error"] == "closed_by_owner"
                        else operation["status"]
                    )
                    text += f"\n#{operation['sequence']} · {label}\n{operation['text'][:180]}\n"
                    if operation["status"] in ("unknown", "queued") and len(buttons) < 3:
                        buttons.append(
                            [
                                InlineKeyboardButton(
                                    text=f"Закрыть #{operation['sequence']} без отправки",
                                    callback_data=f"dismiss:{operation['operation_id']}",
                                )
                            ]
                        )
                text += "\nunknown: результат неизвестен; проверьте сайт. Слепого повтора нет."
                if not operations:
                    text += "\nОчередь пуста."
                navigation = []
                if queue_page:
                    navigation.append(
                        InlineKeyboardButton(text="Новее", callback_data=f"outbox:{queue_page - 1}")
                    )
                if len(operations) == 10:
                    navigation.append(
                        InlineKeyboardButton(
                            text="Старее", callback_data=f"outbox:{queue_page + 1}"
                        )
                    )
                if navigation:
                    buttons.append(navigation)
                buttons.append(
                    [
                        InlineKeyboardButton(text="Обновить", callback_data=f"outbox:{queue_page}"),
                        InlineKeyboardButton(text="Меню", callback_data="menu"),
                    ]
                )
                markup = InlineKeyboardMarkup(inline_keyboard=buttons)
            elif parts[0] in ("dismiss", "close") and len(parts) == 2:
                if parts[0] == "dismiss":
                    text = (
                        "Закрыть операцию без отправки/повтора?\n"
                        "Текст останется в истории очереди.\n"
                        "Если результат unknown, сообщение уже могло дойти. Проверьте FunPay."
                    )
                    markup = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="Закрыть без повтора", callback_data=f"close:{parts[1]}"
                                ),
                                InlineKeyboardButton(text="Отмена", callback_data="outbox"),
                            ]
                        ]
                    )
                else:
                    expected = await self.storage.get_setting("funpay_account")
                    if expected:
                        await self.messages.repository.close_operation(parts[1], expected["id"])
                        self.messages.wake.set()
                    text, markup = "Операция закрыта, если ещё не была отправлена.", menu()
            elif parts[0] == "reply" and len(parts) == 3:
                if not await self.storage.get_setting("sending_enabled", False):
                    raise ValueError("Отправка выключена в окне Windows")
                chat = await self.messages.checked_chat(int(parts[1]), parts[2])
                prompt = await self.bot.send_message(
                    target,
                    f"Ответ для {chat['username'][:100]} · FunPay chat ID {chat['remote_id']}.\n"
                    "Ответьте на это сообщение текстом (до 4000 символов). Срок — 15 минут.\n"
                    "Текст будет поставлен в очередь отправки. Для отмены — /cancel.",
                    reply_markup=ForceReply(selective=True),
                    parse_mode=None,
                )
                await self.messages.prepare_reply(
                    chat["id"], self.binding["bot_id"], target, prompt.message_id
                )
                return True
            elif parts[0] == "h" and len(parts) == 4:
                local_id, page, route = int(parts[1]), min(int(parts[3]), 10000), parts[2]
                chat, rows, refreshed = await self.messages.history(local_id, page, route)
                text = (
                    f"{chat['username'][:100]} · чат {chat['remote_id']}\n"
                    "История: новые → старые. Даты доставки недоступны.\n"
                    + ("Обновлено с FunPay.\n" if refreshed else "Локальная копия.\n")
                )
                for row in rows:
                    content = row["text"] if row["kind"] == "text" else f"[{row['kind']}]"
                    text += f"\n#{row['remote_id']} · {row['direction']}\n{content[:450]}\n"
                if not rows:
                    text += "\nНет сохранённых сообщений."
                nav = []
                if page:
                    nav.append(
                        InlineKeyboardButton(
                            text="Новее", callback_data=f"h:{local_id}:{route}:{page - 1}"
                        )
                    )
                if len(rows) == 5:
                    nav.append(
                        InlineKeyboardButton(
                            text="Старее", callback_data=f"h:{local_id}:{route}:{page + 1}"
                        )
                    )
                markup = InlineKeyboardMarkup(inline_keyboard=[nav] if nav else [])
                markup.inline_keyboard += [
                    [
                        InlineKeyboardButton(
                            text="Обновить", callback_data=f"h:{local_id}:{route}:0"
                        ),
                        InlineKeyboardButton(
                            text="Ответить", callback_data=f"reply:{local_id}:{route}"
                        ),
                        InlineKeyboardButton(
                            text="Тема", callback_data=f"topic:{local_id}:{route}"
                        ),
                    ],
                    [InlineKeyboardButton(text="Чаты", callback_data="chats:0")],
                ]
            else:
                return False
            await self.bot.send_message(
                target, telegram_text(text), reply_markup=markup, parse_mode=None
            )
        except Exception:
            await self.bot.send_message(
                target,
                "Действие недоступно. Проверьте подключённый аккаунт "
                "и разрешение отправки в Windows.",
                reply_markup=menu(),
                parse_mode=None,
            )
        return True

    async def run(self, storage):
        bot = self.bot = self.bot_factory(
            token=self.token, default=DefaultBotProperties(link_preview_is_disabled=True)
        )
        try:
            startup_delay = 1
            while True:
                try:
                    me = await bot.get_me(request_timeout=15)
                    webhook = await bot.get_webhook_info(request_timeout=15)
                    if me.id != self.binding["bot_id"] or webhook.url:
                        self.state("Токен не соответствует привязке или включён webhook")
                        return
                    break
                except (TelegramUnauthorizedError, TelegramConflictError):
                    self.state("Невозможно запустить бота: токен отозван или конфликт")
                    return
                except TelegramRetryAfter as error:
                    self.state("Ожидание лимита Telegram")
                    await asyncio.sleep(error.retry_after)
                except Exception:
                    self.state("Не удалось проверить Telegram · повторное подключение")
                    await asyncio.sleep(startup_delay)
                    startup_delay = min(startup_delay * 2, 60)
            key = f"telegram_offset:{me.id}"
            offset = await storage.get_setting(key, self.binding.get("next_update_id"))
            delay = 1
            while True:
                try:
                    updates = await bot.get_updates(
                        offset=offset,
                        timeout=10,
                        allowed_updates=["message", "callback_query"],
                        request_timeout=20,
                    )
                    self.state("Работает · long polling")
                    for update in updates:
                        await self.dispatcher.feed_update(bot, update)
                        # Outbound text has already been committed and deduplicated by
                        # MessageService. UI replies can still repeat after a crash.
                        await storage.set_setting(key, update.update_id + 1)
                        offset = update.update_id + 1
                    delay = 1
                except (TelegramUnauthorizedError, TelegramConflictError):
                    self.state("Остановлен: токен отозван или бот уже запущен в другом месте")
                    return
                except TelegramRetryAfter as error:
                    self.state("Ожидание лимита Telegram")
                    await asyncio.sleep(error.retry_after)
                except Exception:
                    self.state("Ошибка Telegram · повторное подключение")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
        finally:
            self.token = ""
            await bot.session.close()
