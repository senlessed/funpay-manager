import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from funpay_manager.funpay.adapter import ChatInfo
from funpay_manager.storage.database import LocalStorage
from funpay_manager.telegram.service import TelegramBotService


def service():
    result = TelegramBotService(
        {"owner_id": 7, "bot_id": 42},
        "test-only-token",
        AsyncMock(return_value=[ChatInfo(str(i), "<buyer>", "<message>") for i in range(7)]),
        lambda _: None,
    )
    result.bot = AsyncMock()
    return result


def message(user=7, chat=7, private=True, bot=False, text="/menu"):
    return NS(
        from_user=NS(id=user, is_bot=bot),
        chat=NS(id=chat, type="private" if private else "supergroup"),
        text=text,
        message_id=4,
        reply_markup=None,
    )


@pytest.mark.parametrize(
    "msg",
    [
        message(user=8, chat=8),
        message(chat=-100, private=False),
        message(bot=True),
        message(chat=8),
    ],
)
async def test_messages_and_callbacks_reject_untrusted(msg):
    handler = service()
    await handler.message(msg)
    callback = NS(from_user=msg.from_user, message=msg, data="chats:0", answer=AsyncMock())
    await handler.callback(callback)
    handler.bot.send_message.assert_not_awaited()
    handler.bot.edit_message_text.assert_not_awaited()
    handler.chats.assert_not_awaited()


async def test_owner_menu_and_pagination_are_plaintext():
    handler = service()
    await handler.message(message())
    assert handler.bot.send_message.await_args.kwargs["parse_mode"] is None
    callback = NS(
        from_user=message().from_user, message=message(), data="chats:1", answer=AsyncMock()
    )
    await handler.callback(callback)
    result = handler.bot.edit_message_text.await_args
    assert "ID 5" in result.args[0]
    assert "ID 0" not in result.args[0]
    assert result.kwargs["parse_mode"] is None
    assert "<buyer>" in result.args[0]
    handler.chats.reset_mock()
    callback.data = "chats:../../secret"
    await handler.callback(callback)
    handler.chats.assert_not_awaited()


async def test_startup_rejects_mismatched_bot_and_closes_session(tmp_path):
    storage = LocalStorage(tmp_path / "db")
    await storage.migrate()
    bot = AsyncMock()
    bot.get_me.return_value = NS(id=99)
    handler = TelegramBotService(
        {"owner_id": 7, "bot_id": 42},
        "test-only-token",
        AsyncMock(),
        lambda _: None,
        lambda **kw: bot,
    )
    await handler.run(storage)
    bot.get_updates.assert_not_awaited()
    bot.session.close.assert_awaited_once()


async def test_polling_commits_offset_and_closes_on_cancellation(tmp_path):
    storage = LocalStorage(tmp_path / "db")
    await storage.migrate()
    bot, observed = AsyncMock(), asyncio.Event()
    bot.get_me.return_value = NS(id=42)
    bot.get_webhook_info.return_value = NS(url="")

    async def updates(**kwargs):
        if kwargs["offset"] is None:
            return [NS(update_id=100)]
        observed.set()
        await asyncio.Event().wait()

    bot.get_updates.side_effect = updates
    handler = TelegramBotService(
        {"owner_id": 7, "bot_id": 42},
        "test-only-token",
        AsyncMock(),
        lambda _: None,
        lambda **kw: bot,
    )
    handler.dispatcher.feed_update = AsyncMock()
    task = asyncio.create_task(handler.run(storage))
    await asyncio.wait_for(observed.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert await storage.get_setting("telegram_offset:42") == 101
    bot.session.close.assert_awaited_once()


async def test_emoji_heavy_chat_previews_stay_under_telegram_text_limit():
    handler = service()
    handler.chats.return_value = [ChatInfo(str(i), "😀" * 100, "😀" * 350) for i in range(5)]
    callback = NS(
        from_user=message().from_user, message=message(), data="chats:0", answer=AsyncMock()
    )
    await handler.callback(callback)
    text = handler.bot.edit_message_text.await_args.args[0]
    assert len(text.encode("utf-16-le")) <= 7000
