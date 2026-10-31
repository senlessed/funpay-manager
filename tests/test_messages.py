import asyncio
import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from funpay_manager.funpay.adapter import AccountInfo, ChatInfo, Message
from funpay_manager.funpay.messaging import NotSentError
from funpay_manager.messages.service import MessageService
from funpay_manager.storage.database import MIGRATION_1, LocalStorage
from funpay_manager.storage.messages import MessageRepository


async def setup(tmp_path):
    storage = LocalStorage(tmp_path / "messages.db")
    await storage.migrate()
    await storage.set_setting("funpay_account", {"id": "42", "username": "seller"})
    repository = MessageRepository(storage)
    chats = await repository.save_chats(
        AccountInfo("42", "seller"), [ChatInfo("1", "same"), ChatInfo("2", "same")]
    )
    adapter = AsyncMock()
    adapter.get_history.return_value = [Message("11", "42", "1", "99", "hi")]
    adapter.send_text.return_value = Message("12", "42", "1", "42", "reply")

    @asynccontextmanager
    async def context(account_id):
        assert account_id == "42"
        yield adapter

    service = MessageService(storage, context)
    return storage, repository, chats, adapter, service


async def enqueue(repo, chat, source=1, text="reply", update=None):
    mapping = await repo.chat(chat.local_id)
    return await repo.enqueue(mapping, 77, 7, source, text, update)


async def test_v1_upgrade_preserves_settings(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(MIGRATION_1)
        connection.execute("PRAGMA user_version=1")
        connection.execute("INSERT INTO settings(key,value) VALUES ('theme','true')")
    storage = LocalStorage(path)
    await storage.migrate()
    assert await storage.get_setting("theme") is True
    async with storage.connect() as db:
        assert (await (await db.execute("PRAGMA user_version")).fetchone())[0] == 5


async def test_confirm_can_upgrade_unsupported_history_from_concurrent_sync(tmp_path):
    storage, repo, chats, adapter, service = await setup(tmp_path)
    await repo.save_history(
        chats[0].local_id, "42", "1", [Message("12", "42", "1", "42", "", "unsupported")]
    )
    await enqueue(repo, chats[0])
    operation = await repo.claim("42")
    await repo.confirm(operation, Message("12", "42", "1", "42", "reply"))
    assert (await repo.operations("42"))[0]["status"] == "sent"
    assert (await repo.history(chats[0].local_id))[0]["text"] == "reply"


async def test_history_dedup_direction_and_chat_isolation(tmp_path):
    storage, repo, chats, adapter, service = await setup(tmp_path)
    messages = [
        Message("9", "42", "1", "0", "system"),
        Message("10", "42", "1", "42", "out"),
        Message("11", "42", "1", "99", "in"),
    ]
    await repo.save_history(chats[0].local_id, "42", "1", messages)
    await repo.save_history(chats[0].local_id, "42", "1", messages)
    rows = await repo.history(chats[0].local_id)
    assert [row["direction"] for row in rows] == ["incoming", "outgoing", "system"]
    assert await repo.history(chats[1].local_id) == []
    with pytest.raises(ValueError):
        await repo.save_history(chats[1].local_id, "42", "1", messages)
    adapter.get_history.side_effect = TimeoutError
    _, rows, refreshed = await service.history(chats[0].local_id)
    assert len(rows) == 3 and not refreshed
    await storage.set_setting("funpay_account", {"id": "another-account"})
    with pytest.raises(ValueError):
        await service.history(chats[0].local_id)


async def test_repeat_telegram_update_is_one_operation_after_restart(tmp_path):
    storage, repo, chats, _, _ = await setup(tmp_path)
    operations = await asyncio.gather(*(enqueue(repo, chats[0], update=100) for _ in range(8)))
    assert len(set(operations)) == 1
    restarted = MessageRepository(LocalStorage(storage.path))
    assert await enqueue(restarted, chats[0], update=100) == operations[0]
    assert len(await restarted.operations("42")) == 1


async def test_claim_fifo_parallel_chats_and_unknown_block(tmp_path):
    storage, repo, chats, _, _ = await setup(tmp_path)
    first = await enqueue(repo, chats[0], 1)
    second = await enqueue(repo, chats[0], 2)
    third = await enqueue(repo, chats[1], 3)
    other_repo = MessageRepository(LocalStorage(storage.path))
    claims = await asyncio.gather(repo.claim("42"), other_repo.claim("42"))
    assert {row["operation_id"] for row in claims} == {first, third}
    await repo.finish(first, "unknown", error="test-only-timeout")
    await repo.finish(third, "sent", remote_id="13")
    assert await repo.claim("42") is None
    await repo.close_operation(first, "different-account")
    assert await repo.claim("42") is None
    await repo.close_operation(first, "42")
    assert (await repo.claim("42"))["operation_id"] == second


async def test_interrupted_send_becomes_unknown_and_is_not_retried(tmp_path):
    storage, repo, chats, adapter, service = await setup(tmp_path)
    await storage.set_setting("sending_enabled", True)
    operation = await enqueue(repo, chats[0])
    await repo.claim("42")
    await MessageRepository(LocalStorage(storage.path)).recover()
    assert (await repo.operations("42"))[0]["operation_id"] == operation
    assert (await repo.operations("42"))[0]["status"] == "unknown"
    assert not await service.process_one()
    adapter.send_text.assert_not_awaited()


@pytest.mark.parametrize(
    "failure,status", [(TimeoutError(), "unknown"), (NotSentError(), "failed"), (None, "sent")]
)
async def test_process_status_and_preserved_text(tmp_path, failure, status):
    storage, repo, chats, adapter, service = await setup(tmp_path)
    await storage.set_setting("sending_enabled", True)
    await enqueue(repo, chats[0])
    if failure:
        adapter.send_text.side_effect = failure
    assert await service.process_one()
    row = (await repo.operations("42"))[0]
    assert row["status"] == status
    assert row["text"] == "reply" and row["attempts"] == 1
    assert not await service.process_one()
    adapter.send_text.assert_awaited_once()
    if status == "sent":
        await repo.save_history(chats[0].local_id, "42", "1", [adapter.send_text.return_value])
        assert len(await repo.history(chats[0].local_id)) == 1


async def test_sending_off_does_not_call_network(tmp_path):
    _, repo, chats, adapter, service = await setup(tmp_path)
    await enqueue(repo, chats[0])
    assert not await service.process_one()
    adapter.send_text.assert_not_awaited()


async def test_reply_context_restart_cancel_and_expiry(tmp_path):
    storage, repo, chats, adapter, service = await setup(tmp_path)
    await service.prepare_reply(chats[0].local_id, 77, 7, 900)
    operation = await service.enqueue_reply(77, 7, 1, 900, "preserved while paused")
    assert operation
    with pytest.raises(ValueError):
        await service.enqueue_reply(77, 8, 2, 900, "foreign telegram chat")
    await repo.cancel_prompts(77, 7)
    with pytest.raises(ValueError):
        await service.enqueue_reply(77, 7, 2, 900, "cancelled")
    await service.prepare_reply(chats[0].local_id, 77, 7, 901)
    async with storage.connect() as db:
        await db.execute("UPDATE reply_contexts SET expires_at=0")
        await db.commit()
    with pytest.raises(ValueError):
        await service.enqueue_reply(77, 7, 3, 901, "expired")


async def test_cancel_during_write_is_unknown(tmp_path):
    storage, repo, chats, adapter, service = await setup(tmp_path)
    await storage.set_setting("sending_enabled", True)
    await enqueue(repo, chats[0])
    started = asyncio.Event()

    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()

    adapter.send_text.side_effect = blocked
    task = asyncio.create_task(service.process_one())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await repo.operations("42"))[0]["status"] == "unknown"


async def test_restore_disables_writes_and_clears_reply_prompts(tmp_path):
    storage, repo, chats, adapter, service = await setup(tmp_path)
    await storage.set_setting("sending_enabled", True)
    await enqueue(repo, chats[0])
    await repo.claim("42")
    await enqueue(repo, chats[1], source=2)
    await service.prepare_reply(chats[0].local_id, 77, 7, 900)
    backup = tmp_path / "restore.db"
    await storage.backup(backup)
    await storage.restore(backup, tmp_path / "safety.db")
    assert not await storage.get_setting("sending_enabled", False)
    assert await repo.prompt_chat(77, 7, 900) is None
    assert all(row["status"] == "unknown" for row in await repo.operations("42"))
    assert await LocalStorage(tmp_path / "safety.db").get_setting("sending_enabled") is True


async def test_stale_route_after_restore_cannot_target_reused_local_id(tmp_path):
    storage, repo, chats, _, service = await setup(tmp_path)
    backup = tmp_path / "old.db"
    await storage.backup(backup)
    extra = (await repo.save_chats(AccountInfo("42", "seller"), [ChatInfo("3", "new")]))[0]
    await storage.restore(backup, tmp_path / "safety.db")
    replacement = (await repo.save_chats(AccountInfo("42", "seller"), [ChatInfo("4", "other")]))[0]
    assert extra.local_id == replacement.local_id
    assert extra.route_key != replacement.route_key
    with pytest.raises(ValueError):
        await service.checked_chat(extra.local_id, extra.route_key)
    assert (await service.checked_chat(replacement.local_id, replacement.route_key))[
        "remote_id"
    ] == "4"


async def test_migration_two_failure_rolls_back_without_touching_v1(tmp_path, monkeypatch):
    from funpay_manager.storage import database

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(MIGRATION_1)
        connection.execute("PRAGMA user_version=1")
    monkeypatch.setattr(database, "MIGRATION_2", "CREATE TABLE partial(id); INVALID SQL;")
    with pytest.raises(sqlite3.OperationalError):
        await LocalStorage(path).migrate()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='partial'"
        ).fetchall()


async def test_telegram_reply_is_durable_deduplicated_and_owner_only(tmp_path):
    from types import SimpleNamespace as NS

    from funpay_manager.telegram.service import TelegramBotService

    storage, repo, chats, adapter, service = await setup(tmp_path)
    await storage.set_setting("sending_enabled", True)
    handler = TelegramBotService(
        {"owner_id": 7, "bot_id": 77},
        "test-token",
        AsyncMock(),
        lambda _: None,
        messages=service,
        storage=storage,
    )
    handler.bot = AsyncMock()
    handler.bot.send_message.return_value = NS(message_id=900)
    chat = chats[0]
    query = NS(
        from_user=NS(id=7, is_bot=False),
        message=NS(chat=NS(id=7, type="private")),
        data=f"reply:{chat.local_id}:{chat.route_key}",
        answer=AsyncMock(),
    )
    await handler.callback(query)
    message = NS(
        from_user=NS(id=7, is_bot=False),
        chat=NS(id=7, type="private"),
        message_id=901,
        reply_to_message=NS(message_id=900),
        text="reply",
    )
    await handler.message(message, NS(update_id=123))
    await handler.message(message, NS(update_id=123))
    assert len(await repo.operations("42")) == 1
    message.from_user.id = 8
    await handler.message(message, NS(update_id=124))
    assert len(await repo.operations("42")) == 1
    adapter.send_text.assert_not_awaited()


async def test_restore_schema_one_copy_then_migrates_safely(tmp_path):
    storage, repo, chats, adapter, service = await setup(tmp_path)
    source = tmp_path / "old.db"
    with sqlite3.connect(source) as connection:
        connection.executescript(MIGRATION_1)
        connection.execute("PRAGMA user_version=1")
        connection.execute("INSERT INTO settings(key,value) VALUES ('sending_enabled','true')")
    await storage.restore(source, tmp_path / "safety.db")
    await storage.migrate()
    assert not await storage.get_setting("sending_enabled", False)
    assert await repo.operations("42") == []
