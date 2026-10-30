import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from funpay_manager.funpay.adapter import AccountInfo, ChatInfo, Message
from funpay_manager.funpay.rate_limit import RequestGate
from funpay_manager.notifications.repository import NotificationRepository
from funpay_manager.notifications.service import NotificationService
from funpay_manager.storage.database import LocalStorage
from funpay_manager.storage.messages import MessageRepository
from funpay_manager.sync.repository import SyncRepository
from funpay_manager.sync.worker import FunPaySyncWorker


async def setup(tmp_path):
    storage = LocalStorage(tmp_path / "db")
    await storage.migrate()
    binding = {"bot_id": 77, "owner_id": 7, "username": "test_bot"}
    await storage.set_setting("funpay_account", {"id": "42", "username": "seller"})
    await storage.set_setting("telegram_binding", binding)
    account = AccountInfo("42", "seller")
    repo = SyncRepository(storage)
    chat = (await repo.snapshot(account, [ChatInfo("1", "buyer", "old")]))[0]
    return storage, binding, account, repo, chat


def msg(remote_id, author="99", chat="1", text="<incoming>"):
    return Message(str(remote_id), "42", chat, author, text)


async def notifications(storage, binding):
    return await NotificationRepository(storage).recent("42", binding)


async def test_baseline_is_silent_then_atomic_deduplicated_new_messages(tmp_path):
    storage, binding, _, repo, chat = await setup(tmp_path)
    assert await repo.ingest("42", chat, [msg(1)]) == 0
    assert await notifications(storage, binding) == []
    assert await repo.ingest("42", chat, [msg(1), msg(2), msg(3, "42"), msg(4, "0")]) == 1
    assert await repo.ingest("42", chat, [msg(1), msg(2), msg(3, "42"), msg(4, "0")]) == 0
    rows = await notifications(storage, binding)
    assert len(rows) == 1 and "<incoming>" in rows[0]["text"]
    assert rows[0]["callback_data"] == f"h:{chat.local_id}:{chat.route_key}:0"
    await LocalStorage(storage.path).migrate()
    restarted = SyncRepository(LocalStorage(storage.path))
    assert await restarted.ingest("42", chat, [msg(4, "0"), msg(5)]) == 1
    assert len(await notifications(storage, binding)) == 2


async def test_newly_discovered_chat_notifies_only_latest_and_respects_switch(tmp_path):
    storage, binding, account, repo, chat = await setup(tmp_path)
    new = (await repo.snapshot(account, [ChatInfo("2", "same name")]))[0]
    assert await repo.ingest("42", new, [msg(1, chat="2"), msg(2, chat="2")]) == 1
    assert len(await notifications(storage, binding)) == 1
    await storage.set_setting("notifications_enabled", False)
    assert await repo.ingest("42", new, [msg(2, chat="2"), msg(3, chat="2")]) == 0
    await storage.set_setting("notifications_enabled", True)
    assert await repo.ingest("42", new, [msg(3, chat="2")]) == 0
    assert len(await notifications(storage, binding)) == 1


async def test_sync_does_not_lose_notification_when_manual_history_already_saved(tmp_path):
    storage, binding, account, repo, chat = await setup(tmp_path)
    await repo.ingest("42", chat, [msg(1)])
    await MessageRepository(storage).save_history(chat.local_id, "42", "1", [msg(2)])
    assert await repo.ingest("42", chat, [msg(1), msg(2)]) == 1


async def test_bad_history_rolls_back_messages_cursor_and_events_together(tmp_path):
    storage, binding, _, repo, chat = await setup(tmp_path)
    await repo.ingest("42", chat, [msg(1)])
    with pytest.raises(ValueError):
        await repo.ingest("42", chat, [msg(2), msg(3, chat="wrong")])
    assert await notifications(storage, binding) == []
    assert len(await MessageRepository(storage).history(chat.local_id)) == 1
    assert await repo.ingest("42", chat, [msg(2)]) == 1


async def test_unmodified_preview_eventually_checked_and_new_chats_are_bounded(tmp_path):
    storage, _, account, repo, _ = await setup(tmp_path)
    chats = [ChatInfo(str(i), "same", "same preview") for i in range(1, 13)]
    visited = set()
    for _ in range(4):
        selected = await repo.snapshot(account, chats, limit=5)
        assert len(selected) <= 5
        for chat in selected:
            visited.add(chat.id)
            await repo.ingest(account.id, chat, [])
    assert visited == {chat.id for chat in chats}


async def test_worker_error_one_chat_does_not_stop_other_chats_and_closes_adapter(tmp_path):
    storage, _, account, repo, _ = await setup(tmp_path)
    adapter = AsyncMock()
    adapter.account = account
    adapter.get_chats.return_value = [ChatInfo("1", "bad"), ChatInfo("2", "good")]

    async def history(chat):
        if chat == "1":
            raise TimeoutError
        return [msg(1, chat="2")]

    adapter.get_history.side_effect = history

    @asynccontextmanager
    async def context(expected):
        try:
            yield adapter
        finally:
            await adapter.close()

    states = []
    worker = FunPaySyncWorker(storage, context, asyncio.Event(), states.append)
    assert await worker.cycle() is False
    assert "1/2" in states[-1]["message"]
    adapter.close.assert_awaited_once()


async def ready_notification(tmp_path):
    storage, binding, _, repo, chat = await setup(tmp_path)
    await repo.ingest("42", chat, [msg(1)])
    await repo.ingest("42", chat, [msg(2)])
    bot = AsyncMock()
    bot.get_me.return_value = NS(id=77)
    bot.send_message.return_value = NS(message_id=100)

    class Vault:
        def get(self, name):
            return "test-only-token"

    worker = NotificationService(
        storage, Vault, lambda _: None, bot_factory=lambda **kw: bot, send_interval=0
    )
    return storage, binding, worker, bot


async def test_notification_only_to_bound_owner_plain_text_and_never_polls(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    assert await worker.drain() is None
    args = bot.send_message.await_args
    assert args.args[0] == 7 and "<incoming>" in args.args[1]
    assert args.kwargs["parse_mode"] is None
    bot.get_updates.assert_not_awaited()
    bot.session.close.assert_awaited_once()
    assert (await notifications(storage, binding))[0]["status"] == "sent"
    await worker.drain()
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize(
    "error,status",
    [
        (TimeoutError(), "unknown"),
        (
            TelegramForbiddenError(method=SendMessage(chat_id=7, text="test"), message="test-only"),
            "failed",
        ),
    ],
)
async def test_notification_failures_do_not_blind_retry(tmp_path, error, status):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    bot.send_message.side_effect = error
    await worker.drain()
    assert (await notifications(storage, binding))[0]["status"] == status
    await worker.drain()
    bot.send_message.assert_awaited_once()


async def test_retry_after_persisted_and_new_events_cannot_bypass_it(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=7, text="test"), message="test-only", retry_after=60
    )
    assert await worker.drain() >= 60
    state = (await notifications(storage, binding))[0]
    assert state["status"] == "queued" and state["next_attempt"] > time.time()
    repo = SyncRepository(storage)
    chat = (await repo.snapshot(AccountInfo("42", "seller"), [ChatInfo("1", "buyer")]))[0]
    await repo.ingest("42", chat, [msg(3)])
    restarted = NotificationService(
        storage, worker.vault, lambda _: None, bot_factory=lambda **kw: bot, send_interval=0
    )
    assert await restarted.drain() > 0
    bot.send_message.assert_awaited_once()


async def test_mismatched_bot_does_not_leak_notification(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    bot.get_me.return_value = NS(id=99)
    await worker.drain()
    bot.send_message.assert_not_awaited()
    assert (await notifications(storage, binding))[0]["status"] == "queued"


async def test_recover_restore_do_not_replay_uncertain_notifications(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    await worker.repository.claim("42", binding)
    await worker.repository.recover()
    assert (await notifications(storage, binding))[0]["status"] == "unknown"
    await worker.drain()
    bot.send_message.assert_not_awaited()
    backup = tmp_path / "backup.db"
    await storage.backup(backup)
    await storage.restore(backup, tmp_path / "safety.db")
    assert not await storage.get_setting("sync_enabled")
    assert not await storage.get_setting("notifications_enabled")
    async with storage.connect() as db:
        assert (await (await db.execute("SELECT count(*) FROM chat_sync_state")).fetchone())[0] == 0


async def test_delivery_state_notification_commits_once_without_echo(tmp_path):
    storage, binding, account, sync, chat = await setup(tmp_path)
    repo = MessageRepository(storage)
    operation = await repo.enqueue(await repo.chat(chat.local_id), 77, 7, 100, "reply")
    row = await repo.claim("42")
    await repo.confirm(row, Message("5", "42", "1", "42", "reply"))
    assert len(await notifications(storage, binding)) == 1
    await repo.finish(operation, "unknown")
    assert len(await notifications(storage, binding)) == 1
    await sync.ingest("42", chat, [msg(5, author="42", text="reply")])
    assert len(await notifications(storage, binding)) == 1


async def test_request_gate_serializes_and_is_cancellation_safe():
    gate = RequestGate(interval=0)
    active, peak = 0, 0

    async def request():
        nonlocal active, peak
        async with gate.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*(request() for _ in range(20)))
    assert peak == 1


async def test_schema_two_upgrade_and_schema_three_rollback(tmp_path, monkeypatch):
    import sqlite3

    from funpay_manager.storage import database
    from funpay_manager.storage.message_schema import MIGRATION_2

    path = tmp_path / "v2.db"
    with sqlite3.connect(path) as db:
        db.executescript(database.MIGRATION_1 + MIGRATION_2)
        db.execute("PRAGMA user_version=2")
        db.execute("INSERT INTO settings(key,value) VALUES ('preserve','true')")
    migration = database.MIGRATION_3
    monkeypatch.setattr(database, "MIGRATION_3", "CREATE TABLE partial(id); INVALID SQL;")
    with pytest.raises(sqlite3.OperationalError):
        await LocalStorage(path).migrate()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchall()
    monkeypatch.setattr(database, "MIGRATION_3", migration)
    storage = LocalStorage(path)
    await storage.migrate()
    assert await storage.get_setting("preserve") is True


async def test_auth_failure_pauses_sync_and_writes_until_explicit_resume(tmp_path):
    from funpay_manager.funpay.readonly import FunPayAuthError

    storage, _, account, _, _ = await setup(tmp_path)
    await storage.set_setting("sending_enabled", True)
    adapter = AsyncMock()
    adapter.account = account
    adapter.get_chats.side_effect = FunPayAuthError("test-only session expired")

    @asynccontextmanager
    async def context(expected):
        try:
            yield adapter
        finally:
            await adapter.close()

    paused = asyncio.Event()

    def state(value):
        if isinstance(value, dict) and value.get("auth_paused"):
            paused.set()

    worker = FunPaySyncWorker(storage, context, asyncio.Event(), state)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(paused.wait(), 5)
    assert not await storage.get_setting("sync_enabled")
    assert not await storage.get_setting("sending_enabled")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    adapter.get_chats.assert_awaited_once()


async def test_cancel_inflight_notification_is_unknown_and_not_replayed(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    bot.send_message.side_effect = blocked
    task = asyncio.create_task(worker.drain())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await notifications(storage, binding))[0]["status"] == "unknown"
    bot.session.close.assert_awaited_once()
    await worker.drain()
    bot.send_message.assert_awaited_once()


async def test_switching_owner_does_not_send_old_owner_private_data(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    await storage.set_setting("telegram_binding", {**binding, "owner_id": 8})
    assert await worker.drain() is None
    bot.send_message.assert_not_awaited()
    assert (await notifications(storage, binding))[0]["status"] == "queued"


async def test_disable_skips_backlog_and_does_not_replay_on_enable(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    await storage.set_setting("notifications_enabled", False)
    await worker.repository.disable()
    assert await worker.drain() is None
    await storage.set_setting("notifications_enabled", True)
    await worker.drain()
    bot.send_message.assert_not_awaited()
    assert (await notifications(storage, binding))[0]["last_error"] == "disabled"


async def test_concurrent_ingest_has_one_durable_event(tmp_path):
    storage, binding, account, repo, chat = await setup(tmp_path)
    await repo.ingest("42", chat, [msg(1)])
    other = SyncRepository(LocalStorage(storage.path))
    results = await asyncio.gather(
        repo.ingest("42", chat, [msg(2)]), other.ingest("42", chat, [msg(2)])
    )
    assert sorted(results) == [0, 1]
    assert len(await notifications(storage, binding)) == 1


async def test_notification_unicode_limit_preserves_complete_characters(tmp_path):
    storage, binding, account, repo, chat = await setup(tmp_path)
    await repo.ingest("42", chat, [msg(1)])
    await repo.ingest("42", chat, [msg(2, text="😀" * 4000)])
    text = (await notifications(storage, binding))[0]["text"]
    assert len(text.encode("utf-16-le")) <= 7000
    assert text.endswith("…")


async def test_bounded_100_cycle_restart_and_notification_failure_isolation(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    repo = SyncRepository(storage)
    chat = (await repo.snapshot(AccountInfo("42", "seller"), [ChatInfo("1", "buyer")]))[0]
    # ready_notification has already queued remote ID 2.
    for remote_id in range(3, 102):
        if remote_id == 50:
            repo = SyncRepository(LocalStorage(storage.path))
        await repo.ingest("42", chat, [msg(remote_id, text="same text")])
        await repo.ingest("42", chat, [msg(remote_id, text="same text")])
    attempts = 0

    async def send(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 50:
            raise TimeoutError("test-only ambiguous delivery")
        return NS(message_id=attempts)

    bot.send_message.side_effect = send
    await worker.drain()
    await worker.drain()
    assert attempts == 100
    async with storage.connect() as db:
        async with db.execute(
            "SELECT status,count(*) FROM notifications GROUP BY status"
        ) as cursor:
            counts = dict(await cursor.fetchall())
    assert counts == {"sent": 99, "unknown": 1}


async def test_toggle_does_not_bypass_persisted_telegram_cooldown(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=7, text="test"), message="test-only", retry_after=60
    )
    await worker.drain()
    await worker.repository.disable()
    repo = SyncRepository(storage)
    chat = (await repo.snapshot(AccountInfo("42", "seller"), [ChatInfo("1", "buyer")]))[0]
    await repo.ingest("42", chat, [msg(3)])
    assert await worker.drain() > 0
    bot.send_message.assert_awaited_once()


async def test_get_me_retry_after_does_not_claim_or_send(tmp_path):
    storage, binding, worker, bot = await ready_notification(tmp_path)
    bot.get_me.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=7, text="test"), message="test-only", retry_after=60
    )
    assert await worker.drain() >= 60
    assert await worker.drain() > 0
    bot.get_me.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert (await notifications(storage, binding))[0]["status"] == "queued"
