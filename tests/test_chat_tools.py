from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from funpay_manager.funpay.adapter import AccountInfo, ChatInfo
from funpay_manager.messages.service import MessageService
from funpay_manager.storage.chat_tools import ChatToolsRepository
from funpay_manager.storage.database import LocalStorage
from funpay_manager.storage.messages import MessageRepository
from funpay_manager.telegram.chat_tools import NOTE_PROMPT
from funpay_manager.telegram.service import TelegramBotService


async def setup(tmp_path):
    storage = LocalStorage(tmp_path / "db")
    await storage.migrate()
    await storage.set_setting("funpay_account", {"id": "42"})
    chats = await MessageRepository(storage).save_chats(
        AccountInfo("42", "seller"), [ChatInfo(str(i), "Покупатель Ёж" + str(i)) for i in range(7)]
    )
    messages = MessageService(storage, AsyncMock())
    messages.enqueue_reply = AsyncMock()
    handler = TelegramBotService(
        {"bot_id": 77, "owner_id": 7},
        "test-token",
        AsyncMock(),
        lambda _: None,
        messages=messages,
        storage=storage,
    )
    handler.bot = AsyncMock()
    handler.bot.send_message.return_value = NS(message_id=900)
    return storage, chats, handler


def message(text, user=7, private=True, replied=None):
    return NS(
        text=text,
        from_user=NS(id=user, is_bot=False),
        chat=NS(id=user if private else -100111, type="private" if private else "supergroup"),
        message_id=901,
        reply_to_message=replied,
    )


def query(data, user=7, private=True):
    return NS(
        data=data,
        from_user=NS(id=user, is_bot=False),
        message=message("menu", user, private),
        answer=AsyncMock(),
    )


def prompt():
    return NS(message_id=900, from_user=NS(id=77), text=NOTE_PROMPT + " test")


async def test_search_unicode_pagination_literals_and_account_isolation(tmp_path):
    storage, chats, _ = await setup(tmp_path)
    repo = ChatToolsRepository(storage)
    account, first, more = await repo.search("пОкУпАтЕлЬ ёЖ")
    _, second, more2 = await repo.search("покупатель ёж", 1, account)
    assert more and not more2 and len(first) == 5 and len(second) == 2
    assert not {r["id"] for r in first} & {r["id"] for r in second}
    assert len((await repo.search("3"))[1]) == 1
    assert (await repo.search("%"))[1] == []
    await MessageRepository(storage).save_chats(
        AccountInfo("99", "other"), [ChatInfo("3", "Другой")]
    )
    assert len((await repo.search("3"))[1]) == 1
    await storage.set_setting("funpay_account", {"id": "99"})
    with pytest.raises(ValueError):
        await repo.search("3", expected_account="42")
    assert (await repo.search("Другой"))[1][0]["username"] == "Другой"


async def test_notes_persist_restore_and_never_create_outgoing_operations(tmp_path):
    storage, chats, handler = await setup(tmp_path)
    chat = chats[0]
    repo = ChatToolsRepository(storage)
    await repo.save_note(chat.local_id, chat.route_key, "<заметка>\nВернуть завтра")
    assert (
        await ChatToolsRepository(LocalStorage(storage.path)).note(chat.local_id, chat.route_key)
    )[1].startswith("<заметка>")
    backup = tmp_path / "backup.db"
    await storage.backup(backup)
    await repo.save_note(chat.local_id, chat.route_key, "changed")
    await storage.restore(backup, tmp_path / "safety.db")
    assert (await repo.note(chat.local_id, chat.route_key))[1] == "<заметка>\nВернуть завтра"
    assert await handler.messages.repository.operations("42") == []
    await repo.save_note(chat.local_id, chat.route_key, "")
    assert (await repo.note(chat.local_id, chat.route_key))[1] == ""


async def test_note_route_account_and_length_guards(tmp_path):
    storage, chats, _ = await setup(tmp_path)
    repo, chat = ChatToolsRepository(storage), chats[0]
    with pytest.raises(ValueError):
        await repo.save_note(chat.local_id, "0" * 16, "bad")
    with pytest.raises(ValueError):
        await repo.save_note(chat.local_id, chat.route_key, "x" * 2001)
    await storage.set_setting("funpay_account", {"id": "99"})
    with pytest.raises(ValueError):
        await repo.save_note(chat.local_id, chat.route_key, "wrong account")


async def test_owner_note_reply_is_saved_locally_and_consumed_before_send_queue(tmp_path):
    storage, chats, handler = await setup(tmp_path)
    chat = chats[0]
    await handler.callback(query(f"note_edit:{chat.local_id}:{chat.route_key}"))
    await handler.message(message("Секретная заметка", replied=prompt()))
    assert (await handler.chat_tools.repository.note(chat.local_id, chat.route_key))[
        1
    ] == "Секретная заметка"
    handler.messages.enqueue_reply.assert_not_awaited()
    await handler.message(message("duplicate", replied=prompt()))
    handler.messages.enqueue_reply.assert_not_awaited()
    assert (await handler.chat_tools.repository.note(chat.local_id, chat.route_key))[
        1
    ] == "Секретная заметка"


@pytest.mark.parametrize("reason", ["expired", "restart", "cancel", "account"])
async def test_invalid_note_reply_never_falls_through_to_funpay(tmp_path, reason):
    storage, chats, handler = await setup(tmp_path)
    chat = chats[0]
    await handler.callback(query(f"note_edit:{chat.local_id}:{chat.route_key}"))
    if reason == "expired":
        handler.chat_tools.clock = lambda: float("inf")
    elif reason == "restart":
        handler.chat_tools.prompts.clear()
    elif reason == "cancel":
        await handler.message(message("/cancel"))
    else:
        await storage.set_setting("funpay_account", {"id": "99"})
    await handler.message(message("must stay private", replied=prompt()))
    handler.messages.enqueue_reply.assert_not_awaited()


@pytest.mark.parametrize("user,private", [(8, True), (7, False)])
async def test_search_and_notes_are_owner_private_only(tmp_path, user, private):
    storage, chats, handler = await setup(tmp_path)
    await handler.message(message("/find покупатель", user, private))
    await handler.callback(
        query(f"note_edit:{chats[0].local_id}:{chats[0].route_key}", user, private)
    )
    handler.bot.send_message.assert_not_awaited()
    assert handler.chat_tools.prompts == {}


async def test_search_callback_expires_and_rejects_switched_account(tmp_path):
    storage, _, handler = await setup(tmp_path)
    await handler.message(message("/find покупатель"))
    token = next(iter(handler.chat_tools.searches))
    await handler.callback(query(f"find:{token}:1"))
    assert "Страница 2" in handler.bot.send_message.await_args.args[1]
    await storage.set_setting("funpay_account", {"id": "99"})
    await handler.callback(query(f"find:{token}:0"))
    assert "устарел" in handler.bot.send_message.await_args.args[1]
    handler.messages.enqueue_reply.assert_not_awaited()


async def test_note_deletion_requires_explicit_confirmation(tmp_path):
    storage, chats, handler = await setup(tmp_path)
    chat = chats[0]
    suffix = f"{chat.local_id}:{chat.route_key}"
    await handler.chat_tools.repository.save_note(chat.local_id, chat.route_key, "keep")
    await handler.callback(query("note_clear:" + suffix))
    assert (await handler.chat_tools.repository.note(chat.local_id, chat.route_key))[1] == "keep"
    await handler.callback(query("note_delete:" + suffix))
    assert (await handler.chat_tools.repository.note(chat.local_id, chat.route_key))[1] == ""
