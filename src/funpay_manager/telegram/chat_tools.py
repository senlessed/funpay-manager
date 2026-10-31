import re
import time
from uuid import uuid4

from aiogram.types import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

from funpay_manager.storage.chat_tools import ChatToolsRepository
from funpay_manager.utils.text import telegram_text

NOTE_PROMPT = "[Личная заметка]"


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


class PrivateChatTools:
    """Called only after the service's owner/private authorization guard."""

    def __init__(self, storage, binding, clock=time.monotonic):
        self.repository = ChatToolsRepository(storage)
        self.binding, self.clock = binding, clock
        self.searches, self.prompts = {}, {}

    def remember(self, cache, key, value):
        while len(cache) >= 32:
            del cache[next(iter(cache))]
        cache[key] = (*value, self.clock() + 900)

    async def send(self, bot, target, text, rows=None, **kwargs):
        return await bot.send_message(
            target,
            telegram_text(text),
            parse_mode=None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
            **kwargs,
        )

    async def search_page(self, bot, target, token, page):
        query, account, expires = self.searches[token]
        if self.clock() >= expires:
            raise ValueError("Expired search")
        _, chats, more = await self.repository.search(query, page, account)
        text = f"Поиск локальных чатов: {query}\nСтраница {page + 1}\n"
        rows = []
        for chat in chats:
            text += f"\n{chat['username'][:100]} · ID {chat['remote_id']}"
            rows.append(
                [
                    button(
                        f"Открыть: {chat['username'][:35]}", f"h:{chat['id']}:{chat['route_key']}:0"
                    )
                ]
            )
        if not chats:
            text += "\nСовпадений нет. Поиск охватывает только уже загруженные чаты."
        nav = []
        if page:
            nav.append(button("←", f"find:{token}:{page - 1}"))
        if more:
            nav.append(button("→", f"find:{token}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("Новый поиск", "search"), button("Меню", "menu")])
        await self.send(bot, target, text, rows)

    async def message(self, bot, message):
        text = message.text.strip()
        command = text.split(maxsplit=1)[0]
        if command == "/cancel":
            self.prompts.clear()
            return False
        if command == "/find":
            query = text.partition(" ")[2].strip()
            try:
                account, _, _ = await self.repository.search(query)
                token = uuid4().hex[:16]
                self.remember(self.searches, token, (query, account))
                await self.search_page(bot, message.chat.id, token, 0)
            except ValueError:
                await self.send(
                    bot,
                    message.chat.id,
                    "Введите /find ник или ID (1–100 символов). FunPay должен быть подключён.",
                )
            return True
        replied = getattr(message, "reply_to_message", None)
        if not replied or text.startswith("/"):
            return False
        context = self.prompts.get(replied.message_id)
        author = getattr(replied, "from_user", None)
        old_prompt = (
            author
            and author.id == self.binding["bot_id"]
            and (getattr(replied, "text", "") or "").startswith(NOTE_PROMPT)
        )
        if not context and not old_prompt:
            return False
        if not context or self.clock() >= context[-1]:
            await self.send(
                bot, message.chat.id, "Запрос заметки истёк. Откройте чат → «Заметка» заново."
            )
            return True
        local_id, route, _ = context
        try:
            if not text or len(text) > 2000:
                raise ValueError("Invalid note")
            await self.repository.save_note(local_id, route, text)
            self.prompts.pop(replied.message_id, None)
            await self.send(
                bot,
                message.chat.id,
                "Личная заметка сохранена. В FunPay ничего не отправлено.",
                [[button("Заметка", f"note:{local_id}:{route}")]],
            )
        except ValueError:
            await self.send(
                bot,
                message.chat.id,
                "Заметка не сохранена. Максимум 2000 символов; проверьте текущий аккаунт и чат.",
            )
        return True

    async def callback(self, bot, query, data):
        found = re.fullmatch(r"find:([a-f0-9]{16}):([0-9]{1,5})", data)
        note = re.fullmatch(
            r"(note|note_edit|note_clear|note_delete):([1-9][0-9]{0,18}):([a-f0-9]{16})", data
        )
        if data != "search" and not found and not note:
            return False
        await query.answer()
        target = query.message.chat.id
        try:
            if data == "search":
                await self.send(
                    bot,
                    target,
                    "Напишите /find ник или ID чата.\n"
                    "Можно искать часть ника, в том числе кириллицу. Поиск локальный, "
                    "по уже загруженным чатам подключённого аккаунта.",
                )
            elif found:
                await self.search_page(bot, target, found[1], int(found[2]))
            else:
                action, local_id, route = note[1], int(note[2]), note[3]
                chat, value = await self.repository.note(local_id, route)
                if action == "note_edit":
                    sent = await bot.send_message(
                        target,
                        f"{NOTE_PROMPT} {chat['username'][:100]}\n"
                        "Ответьте на это сообщение заметкой (до 2000 символов, 15 минут).\n"
                        "Она сохраняется только локально. Для отмены — /cancel.",
                        parse_mode=None,
                        reply_markup=ForceReply(force_reply=True),
                    )
                    self.remember(self.prompts, sent.message_id, (local_id, route))
                elif action == "note_clear":
                    await self.send(
                        bot,
                        target,
                        "Удалить личную заметку этого чата?",
                        [
                            [
                                button("Удалить", f"note_delete:{local_id}:{route}"),
                                button("Отмена", f"note:{local_id}:{route}"),
                            ]
                        ],
                    )
                else:
                    if action == "note_delete":
                        await self.repository.save_note(local_id, route, "")
                        value = ""
                    await self.send(
                        bot,
                        target,
                        f"Личная заметка · {chat['username'][:100]}\n\n{value or 'Пока пусто.'}",
                        [
                            [
                                button("Изменить", f"note_edit:{local_id}:{route}"),
                                button("Удалить…", f"note_clear:{local_id}:{route}"),
                            ],
                            [button("История", f"h:{local_id}:{route}:0")],
                        ],
                    )
        except (ValueError, KeyError):
            await self.send(
                bot, target, "Запрос устарел или аккаунт изменился. Откройте чат или поиск заново."
            )
        return True
