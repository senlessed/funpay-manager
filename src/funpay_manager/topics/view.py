import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from funpay_manager.utils.text import telegram_text


async def topic_screen(manager, bot, query, data):
    retry = re.fullmatch(r"(mirror_retry|mirror_confirm):([a-f0-9]{24})", data)
    if retry:
        await query.answer()
        try:
            group = await manager.configuration()
            event = await manager.repository.delivery(retry[2], group)
            if not event:
                raise ValueError("Delivery not found")
            await manager.messages.checked_chat(event["chat_id"])
            if retry[1] == "mirror_confirm":
                await manager.repository.retry_delivery(retry[2], group)
                manager.wake.set()
                text, buttons = "Зеркало повторно поставлено в очередь по вашему подтверждению.", []
            else:
                text = (
                    "Повторить сообщение бота в теме? При unknown оно могло уже появиться.\n"
                    "Возможен дубль в Telegram. В FunPay этот повтор ничего не отправляет."
                )
                buttons = [
                    [
                        InlineKeyboardButton(
                            text="Подтвердить повтор зеркала",
                            callback_data=f"mirror_confirm:{retry[2]}",
                        )
                    ]
                ]
            await bot.send_message(
                query.message.chat.id,
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=buttons
                    + [[InlineKeyboardButton(text="Меню", callback_data="menu")]]
                ),
            )
        except Exception:
            await bot.send_message(
                query.message.chat.id, "Зеркало недоступно: проверьте текущую группу и аккаунт."
            )
        return True
    matched = re.fullmatch(
        r"(topic|topic_new|topic_confirm):([1-9][0-9]{0,18}):([a-f0-9]{16})", data
    )
    if not matched:
        return False
    await query.answer()
    action, local_id, route = matched[1], int(matched[2]), matched[3]
    try:
        chat = await manager.messages.checked_chat(local_id, route)
        group = await manager.configuration()
        mapping = await manager.repository.mapping(local_id)
        buttons = []
        if action == "topic_new":
            text = (
                "Создать новую тему для этого чата?\n"
                "При прежнем неизвестном результате тема могла уже появиться — проверьте группу.\n"
                "Старая тема останется в Telegram, но перестанет маршрутизировать ответы."
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Подтвердить создание",
                        callback_data=f"topic_confirm:{local_id}:{route}",
                    )
                ]
            )
        else:
            if action == "topic_confirm":
                if mapping:
                    await manager.repository.set_state(local_id, "pending")
                thread_id = await manager.ensure(bot, local_id)
                manager.wake.set()
                mapping = await manager.repository.mapping(local_id)
                text = f"Создана/привязана тема {thread_id}."
            else:
                text = f"{chat['username'][:100]} · чат {chat['remote_id']}\n"
            text += (
                f"\nСостояние: {mapping['status'] if mapping else 'ещё не создана'}\n"
                "Для привязки существующей темы отправьте внутри неё:\n"
                f"/attach {local_id} {route}\n"
                "Команда работает только для владельца в привязанной закрытой группе."
            )
            for event in await manager.repository.deliveries(local_id, group):
                text += f"\nЗеркало #{event['remote_message_id']}: {event['status']}"
                if event["status"] in ("unknown", "failed"):
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                text=f"Повторить зеркало #{event['remote_message_id']}…",
                                callback_data=f"mirror_retry:{event['delivery_key']}",
                            )
                        ]
                    )
            if (
                mapping
                and mapping["status"] == "ready"
                and str(group["group_id"]).startswith("-100")
            ):
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text="Открыть тему",
                            url=f"https://t.me/c/{str(group['group_id'])[4:]}/{mapping['thread_id']}",
                        )
                    ]
                )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Создать новую тему…", callback_data=f"topic_new:{local_id}:{route}"
                    )
                ]
            )
        buttons.append(
            [InlineKeyboardButton(text="Назад", callback_data=f"h:{local_id}:{route}:0")]
        )
        await bot.send_message(
            query.message.chat.id,
            telegram_text(text),
            parse_mode=None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        await bot.send_message(
            query.message.chat.id,
            "Не удалось выполнить действие с темой.\n"
            "Проверьте привязку, права и результат в группе.\n"
            "При таймауте не создавайте вторую тему без проверки существующей.",
        )
    return True
