import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from funpay_manager.funpay.adapter import AccountInfo, ChatInfo
from funpay_manager.messages.service import MessageService
from funpay_manager.storage.database import LocalStorage
from funpay_manager.storage.messages import MessageRepository
from funpay_manager.storage.templates import TemplateRepository
from funpay_manager.telegram.service import TelegramBotService
from funpay_manager.utils.templates import TemplateError, render_template


async def setup(tmp_path):
    storage = LocalStorage(tmp_path / "db")
    await storage.migrate()
    await storage.set_setting("funpay_account", {"id": "42", "username": "Seller"})
    await storage.set_setting("telegram_binding", {"bot_id": 77, "owner_id": 7})
    await storage.set_setting("sending_enabled", True)
    chat = (
        await MessageRepository(storage).save_chats(
            AccountInfo("42", "Seller"), [ChatInfo("1", "Buyer")]
        )
    )[0]
    repo = TemplateRepository(storage)
    template = await repo.save("42", "Приветствие", "Привет, {buyer}! {seller}, чат {chat_id}.")
    return storage, chat, repo, template


async def preview(chat, repo, template, attach=True):
    key, _, text = await repo.preview(template, chat.local_id, chat.route_key, 77, 7)
    if attach:
        await repo.attach(key, 900)
    return key, text


@pytest.mark.parametrize(
    "body",
    ["{buyer.__class__}", "{buyer[0]}", "{buyer!r}", "{buyer:>20}", "{unknown}", "{}", "{buyer"],
)
def test_variables_are_strict_and_do_not_execute(body):
    with pytest.raises(TemplateError):
        render_template(body, {"buyer": "name"})


def test_literal_braces_and_values_are_not_recursively_interpreted():
    assert render_template("{{x}} {buyer}", {"buyer": "{seller}"}) == "{x} {seller}"
    with pytest.raises(TemplateError):
        render_template("{buyer}", {"buyer": "😀" * 1500})
    with pytest.raises(TemplateError):
        render_template("{seller}", {})


async def test_template_crud_revisions_and_account_isolation(tmp_path):
    storage, chat, repo, template = await setup(tmp_path)
    assert (await repo.list("42"))[0]["revision"] == 1
    await repo.save("42", "Other", "text", template, 1)
    assert (await repo.list("42"))[0]["revision"] == 2
    with pytest.raises(TemplateError):
        await repo.save("42", "stale", "text", template, 1)
    await storage.set_setting("funpay_account", {"id": "99"})
    with pytest.raises(TemplateError):
        await repo.list("42")
    with pytest.raises(TemplateError):
        await repo.delete("42", template, 2)
    assert await repo.list("99") == []


async def test_preview_is_inert_and_send_is_durable_atomic_and_idempotent(tmp_path):
    storage, chat, repo, template = await setup(tmp_path)
    key, text = await preview(chat, repo, template)
    assert await MessageRepository(storage).operations("42") == []
    assert text == "Привет, Buyer! Seller, чат 1."
    results = await asyncio.gather(
        repo.confirm(key, 77, 7, 900),
        TemplateRepository(LocalStorage(storage.path)).confirm(key, 77, 7, 900),
    )
    assert results[0] == results[1]
    rows = await MessageRepository(storage).operations("42")
    assert len(rows) == 1 and rows[0]["text"] == text and rows[0]["source"] == "private_bot"
    await MessageRepository(storage).close_operation(rows[0]["operation_id"], "42")
    assert (await repo.confirm(key, 77, 7, 900))[0] == rows[0]["operation_id"]
    assert len(await MessageRepository(storage).operations("42")) == 1


@pytest.mark.parametrize(
    "reason", ["draft", "expired", "clock_back", "account", "route", "disabled", "actor", "message"]
)
async def test_send_rejects_invalid_preview_context(tmp_path, reason):
    storage, chat, repo, template = await setup(tmp_path)
    key, _ = await preview(chat, repo, template, attach=reason != "draft")
    if reason in ("expired", "clock_back", "route"):
        async with storage.connect() as db:
            if reason == "expired":
                await db.execute("UPDATE template_previews SET expires_at=0")
            elif reason == "clock_back":
                await db.execute("UPDATE template_previews SET created_at=?", (time.time() + 100,))
            else:
                await db.execute("UPDATE chats SET route_key=?", ("0" * 16,))
            await db.commit()
    if reason == "account":
        await storage.set_setting("funpay_account", {"id": "99"})
    if reason == "disabled":
        await storage.set_setting("sending_enabled", False)
    with pytest.raises(TemplateError):
        await repo.confirm(
            key, 77, 8 if reason == "actor" else 7, 901 if reason == "message" else 900
        )
    assert await MessageRepository(storage).operations("42") == []


@pytest.mark.parametrize("action", ["edit", "delete", "cancel"])
async def test_edit_delete_and_cancel_invalidate_old_preview(tmp_path, action):
    storage, chat, repo, template = await setup(tmp_path)
    key, _ = await preview(chat, repo, template)
    if action == "edit":
        await repo.save("42", "Changed", "new text", template, 1)
    elif action == "delete":
        await repo.delete("42", template, 1)
    else:
        await repo.confirm(key, 77, 7, 900, cancel=True)
    with pytest.raises(TemplateError):
        await repo.confirm(key, 77, 7, 900)
    assert await MessageRepository(storage).operations("42") == []


async def test_restore_keeps_templates_and_cancels_unsubmitted_previews(tmp_path):
    storage, chat, repo, template = await setup(tmp_path)
    key, _ = await preview(chat, repo, template)
    backup = tmp_path / "backup.db"
    await storage.backup(backup)
    await repo.delete("42", template, 1)
    await storage.restore(backup, tmp_path / "safety.db")
    assert len(await repo.list("42")) == 1
    await storage.set_setting("sending_enabled", True)
    with pytest.raises(TemplateError):
        await repo.confirm(key, 77, 7, 900)
    assert await MessageRepository(storage).operations("42") == []


def query(data, user=7, private=True, message_id=900):
    return NS(
        data=data,
        from_user=NS(id=user, is_bot=False),
        answer=AsyncMock(),
        message=NS(
            chat=NS(id=user if private else -100111, type="private" if private else "supergroup"),
            message_id=message_id,
        ),
    )


async def test_bot_preview_then_explicit_send_uses_exact_snapshot_even_if_receipt_fails(tmp_path):
    storage, chat, repo, template = await setup(tmp_path)
    messages = MessageService(storage, AsyncMock())
    handler = TelegramBotService(
        {"bot_id": 77, "owner_id": 7},
        "test-only-token",
        AsyncMock(),
        lambda _: None,
        messages=messages,
        storage=storage,
    )
    handler.bot = AsyncMock()
    handler.bot.send_message.return_value = NS(message_id=900)
    await handler.callback(query(f"tplp:{template}:{chat.local_id}:{chat.route_key}"))
    shown = handler.bot.send_message.await_args
    assert "Привет, Buyer! Seller, чат 1." in shown.args[1]
    assert shown.kwargs["parse_mode"] is None
    send_data = shown.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert await messages.repository.operations("42") == []
    handler.bot.edit_message_text.side_effect = TimeoutError
    await handler.callback(query(send_data))
    await handler.callback(query(send_data))
    operations = await messages.repository.operations("42")
    assert len(operations) == 1 and operations[0]["text"] == "Привет, Buyer! Seller, чат 1."
    assert messages.wake.is_set()


@pytest.mark.parametrize("user,private", [(8, True), (7, False)])
async def test_templates_remain_owner_private_only(tmp_path, user, private):
    storage, chat, repo, template = await setup(tmp_path)
    handler = TelegramBotService(
        {"bot_id": 77, "owner_id": 7},
        "test-only-token",
        AsyncMock(),
        lambda _: None,
        messages=MessageService(storage, AsyncMock()),
        storage=storage,
    )
    handler.bot = AsyncMock()
    await handler.callback(
        query(f"tplp:{template}:{chat.local_id}:{chat.route_key}", user, private)
    )
    handler.bot.send_message.assert_not_awaited()
    assert await MessageRepository(storage).operations("42") == []


async def test_limit_and_paginated_picker(tmp_path):
    storage, chat, repo, template = await setup(tmp_path)
    for i in range(49):
        await repo.save("42", f"Ответ {i}", "Текст {buyer}")
    with pytest.raises(TemplateError):
        await repo.save("42", "51", "text")
    handler = TelegramBotService(
        {"bot_id": 77, "owner_id": 7},
        "test-only-token",
        AsyncMock(),
        lambda _: None,
        messages=MessageService(storage, AsyncMock()),
        storage=storage,
    )
    handler.bot = AsyncMock()
    await handler.callback(query(f"tpls:{chat.local_id}:{chat.route_key}:9"))
    markup = handler.bot.send_message.await_args.kwargs["reply_markup"]
    picks = [
        b for row in markup.inline_keyboard for b in row if b.callback_data.startswith("tplp:")
    ]
    assert len(picks) == 5
    assert all(len(b.callback_data.encode()) <= 64 for b in picks)
    assert await MessageRepository(storage).operations("42") == []


async def test_migration_six_rolls_back_and_upgrades_five(tmp_path, monkeypatch):
    import sqlite3

    from funpay_manager.storage import database
    from funpay_manager.storage.commerce_schema import MIGRATION_5
    from funpay_manager.storage.message_schema import MIGRATION_2
    from funpay_manager.storage.sync_schema import MIGRATION_3
    from funpay_manager.storage.topic_schema import MIGRATION_4

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            database.MIGRATION_1 + MIGRATION_2 + MIGRATION_3 + MIGRATION_4 + MIGRATION_5
        )
        db.execute("PRAGMA user_version=5")
    actual = database.MIGRATION_6
    monkeypatch.setattr(database, "MIGRATION_6", "CREATE TABLE partial(id INTEGER); INVALID SQL;")
    with pytest.raises(sqlite3.OperationalError):
        await LocalStorage(path).migrate()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone() is None
    monkeypatch.setattr(database, "MIGRATION_6", actual)
    await LocalStorage(path).migrate()


async def test_confirmed_template_uses_existing_sender_and_exact_visible_text(tmp_path):
    from contextlib import asynccontextmanager

    from funpay_manager.funpay.adapter import Message

    storage, chat, repo, template = await setup(tmp_path)
    key, text = await preview(chat, repo, template)
    await repo.confirm(key, 77, 7, 900)
    adapter = NS(send_text=AsyncMock(return_value=Message("12", "42", "1", "42", text)))

    @asynccontextmanager
    async def context(account):
        assert account == "42"
        yield adapter

    sender = MessageService(storage, context)
    assert await sender.process_one()
    adapter.send_text.assert_awaited_once_with("1", text)
    assert (await sender.repository.operations("42"))[0]["status"] == "sent"
