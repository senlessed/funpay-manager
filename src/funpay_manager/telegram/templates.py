import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from funpay_manager.storage.templates import TemplateRepository
from funpay_manager.utils.templates import TemplateError
from funpay_manager.utils.text import telegram_text


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


class TemplateBot:
    """Private, owner-authorized callbacks; never handles plain topic messages."""

    def __init__(self, storage, binding, messages):
        self.repository = TemplateRepository(storage)
        self.binding, self.messages = binding, messages

    async def callback(self, bot, query, data):
        listing = re.fullmatch(r"tpls:([1-9][0-9]{0,18}):([a-f0-9]{16}):([0-9]{1,2})", data)
        preview = re.fullmatch(r"tplp:([a-f0-9]{16}):([1-9][0-9]{0,18}):([a-f0-9]{16})", data)
        action = re.fullmatch(r"(tplsend|tplcancel):([a-f0-9]{24})", data)
        if not listing and not preview and not action:
            return False
        await query.answer()
        target = query.message.chat.id
        try:
            if listing:
                local_id, route = int(listing[1]), listing[2]
                chat = await self.messages.checked_chat(local_id, route)
                templates = await self.repository.list(chat["account_id"])
                page = min(int(listing[3]), max(0, (len(templates) - 1) // 5))
                rows = [
                    [button(t["title"], f"tplp:{t['id']}:{local_id}:{route}")]
                    for t in templates[page * 5 : page * 5 + 5]
                ]
                nav = []
                if page:
                    nav.append(button("←", f"tpls:{local_id}:{route}:{page - 1}"))
                if (page + 1) * 5 < len(templates):
                    nav.append(button("→", f"tpls:{local_id}:{route}:{page + 1}"))
                if nav:
                    rows.append(nav)
                rows.append([button("История", f"h:{local_id}:{route}:0")])
                text = (
                    f"Шаблоны для {chat['username'][:100]} · ID {chat['remote_id']}\n"
                    "Выберите ответ для предпросмотра."
                )
                if not templates:
                    text += "\nПока пусто. Создайте шаблоны в окне Windows → «Шаблоны ответов»."
                await bot.send_message(
                    target,
                    telegram_text(text),
                    parse_mode=None,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                )
            elif preview:
                key, chat, text = await self.repository.preview(
                    preview[1],
                    int(preview[2]),
                    preview[3],
                    self.binding["bot_id"],
                    query.from_user.id,
                )
                shown = (
                    f"Предпросмотр · {chat['username'][:100]} · ID {chat['remote_id']}\n\n"
                    f"{text}\n\n"
                    "Отправка произойдёт только после кнопки ниже, если разрешена в Windows."
                )
                sent = await bot.send_message(
                    target,
                    shown,
                    parse_mode=None,
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                button("Отправить в FunPay", f"tplsend:{key}"),
                                button("Отмена", f"tplcancel:{key}"),
                            ]
                        ]
                    ),
                )
                await self.repository.attach(key, sent.message_id)
            else:
                operation, text = await self.repository.confirm(
                    action[2],
                    self.binding["bot_id"],
                    query.from_user.id,
                    query.message.message_id,
                    cancel=action[1] == "tplcancel",
                )
                if operation:
                    self.messages.wake.set()
                    shown = "Операция создана. Результат — в разделе «Очередь».\n\n" + text
                else:
                    shown = "Предпросмотр отменён. Текст не поставлен в очередь."
                try:
                    await bot.edit_message_text(
                        shown,
                        chat_id=target,
                        message_id=query.message.message_id,
                        parse_mode=None,
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[button("Очередь", "outbox"), button("Меню", "menu")]]
                        ),
                    )
                except Exception:
                    pass  # The durable queue result must not depend on editing its UI receipt.
        except TemplateError as error:
            await bot.send_message(target, str(error), parse_mode=None)
        except ValueError:
            await bot.send_message(
                target, "Чат недоступен. Откройте историю заново.", parse_mode=None
            )
        return True
