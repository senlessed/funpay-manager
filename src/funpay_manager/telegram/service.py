import asyncio

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Чаты", callback_data="chats:0")],
            [InlineKeyboardButton(text="🟢 Статус", callback_data="status")],
        ]
    )


class TelegramBotService:
    """One private polling consumer. Every handler verifies owner AND private chat."""

    def __init__(self, binding: dict, token: str, chats, state, bot_factory=Bot):
        self.binding, self.token = binding, token
        self.chats, self.state, self.bot_factory = chats, state, bot_factory
        self.bot = None
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

    async def message(self, message):
        if not self.authorized(message.from_user, message.chat) or not message.text:
            return
        if message.text.split(maxsplit=1)[0] in ("/start", "/menu", "/status"):
            await self.bot.send_message(
                message.chat.id, self.status_text(), reply_markup=menu(), parse_mode=None
            )

    def status_text(self) -> str:
        return (
            "FunPay Telegram Manager\nTelegram: бот работает.\n"
            "Доступно чтение списка чатов после локального подключения FunPay.\n"
            "Ответы, заказы, Topics и поднятие пока недоступны."
        )

    async def callback(self, query):
        message = query.message
        if not self.authorized(query.from_user, getattr(message, "chat", None)):
            return
        data = query.data or ""
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
                text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup,
                parse_mode=None,
            )

    async def run(self, storage):
        bot = self.bot = self.bot_factory(token=self.token)
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
                        # No outbound FunPay operations here. Persist before acknowledging
                        # via the next getUpdates offset; UI replies can repeat after a crash.
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
