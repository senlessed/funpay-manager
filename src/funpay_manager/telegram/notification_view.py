from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from funpay_manager.notifications.repository import NotificationRepository
from funpay_manager.utils.text import telegram_text


async def notification_screen(storage, binding, bot, target):
    expected = await storage.get_setting("funpay_account")
    enabled = await storage.get_setting("notifications_enabled", True)
    rows = await NotificationRepository(storage).recent(expected["id"], binding) if expected else []
    text = f"Уведомления · {'включены' if enabled else 'выключены'}\nПоследние 10:\n"
    for row in rows:
        text += f"\n#{row['id']} · {row['status']}\n{row['text'][:180]}\n"
    if not rows:
        text += "Пока нет событий.\n"
    text += (
        "\nunknown: доставка могла состояться, автоматического повтора нет.\nНастройки — в Windows."
    )
    await bot.send_message(
        target,
        telegram_text(text),
        parse_mode=None,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Обновить", callback_data="notifications"),
                    InlineKeyboardButton(text="Меню", callback_data="menu"),
                ]
            ]
        ),
    )
