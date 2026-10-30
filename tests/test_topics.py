import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetChat, SendMessage

from funpay_manager.funpay.adapter import AccountInfo, ChatInfo, Message
from funpay_manager.messages.service import MessageService
from funpay_manager.storage.database import LocalStorage
from funpay_manager.storage.messages import MessageRepository
from funpay_manager.topics.manager import TopicManager, TopicNeedsAttention
from funpay_manager.topics.pairing import GroupPairing
from funpay_manager.topics.permissions import (
    GroupLookupError,
    GroupPermissionError,
    pairing_failure,
    validate_group,
)
from funpay_manager.topics.repository import queue_topic_message
from funpay_manager.topics.worker import TopicMirrorService


def bot():
    result = AsyncMock()
    result.get_me.return_value = NS(id=77)
    result.get_chat.return_value = NS(
        id=-100111,
        type="supergroup",
        is_forum=True,
        title="closed",
        username=None,
        active_usernames=[],
        linked_chat_id=None,
    )

    async def member(group_id, user_id, **kwargs):
        return NS(status="creator" if user_id == 7 else "administrator", can_manage_topics=True)

    result.get_chat_member.side_effect = member
    result.create_forum_topic.return_value = NS(message_thread_id=101)
    result.send_message.return_value = NS(message_id=501)
    return result


def group_message(text="reply", user=7, group=-100111, thread=101, is_bot=False, sender_chat=None):
    return NS(
        text=text,
        from_user=NS(id=user, is_bot=is_bot),
        chat=NS(id=group, type="supergroup"),
        message_thread_id=thread,
        sender_chat=sender_chat,
        forward_origin=None,
        via_bot=None,
        message_id=500,
    )


async def setup(tmp_path, bound=True):
    storage = LocalStorage(tmp_path / "db")
    await storage.migrate()
    await storage.set_setting("funpay_account", {"id": "42", "username": "seller"})
    await storage.set_setting(
        "telegram_binding", {"bot_id": 77, "owner_id": 7, "username": "test_bot"}
    )
    group = {
        "group_id": -100111,
        "title": "closed",
        "bot_id": 77,
        "owner_id": 7,
        "account_id": "42",
        "binding_key": "test-only-binding",
    }
    if bound:
        await storage.set_setting("telegram_group", group)
        await storage.set_setting("topics_enabled", True)
    repo = MessageRepository(storage)
    chats = await repo.save_chats(
        AccountInfo("42", "seller"), [ChatInfo("1", "same name"), ChatInfo("2", "same name")]
    )
    adapter = AsyncMock()
    adapter.send_text.return_value = Message("12", "42", "1", "42", "reply")

    @asynccontextmanager
    async def context(expected):
        yield adapter

    messages = MessageService(storage, context)
    manager = TopicManager(storage, messages, asyncio.Event())
    client = bot()
    return storage, group, chats, messages, manager, client, adapter


@pytest.mark.parametrize(
    "field,value",
    [
        ("username", "public"),
        ("active_usernames", ["public"]),
        ("linked_chat_id", -100222),
        ("is_forum", False),
        ("type", "group"),
        ("location", NS(latitude=1, longitude=1)),
        ("id", -100222),
    ],
)
async def test_group_must_be_closed_forum(field, value):
    client = bot()
    setattr(client.get_chat.return_value, field, value)
    with pytest.raises(GroupPermissionError) as error:
        await validate_group(client, -100111, {"owner_id": 7, "bot_id": 77})
    expected = {
        "username": "public",
        "active_usernames": "public",
        "linked_chat_id": "linked",
        "is_forum": "forum",
        "type": "type",
        "location": "location",
        "id": "id",
    }[field]
    assert f"tg_group_{expected}" in pairing_failure(error.value)


@pytest.mark.parametrize(
    "owner_role,bot_role,manage",
    [
        ("member", "administrator", True),
        ("creator", "member", True),
        ("administrator", "administrator", False),
    ],
)
async def test_permissions_require_owner_admin_and_bot_manage_topics(owner_role, bot_role, manage):
    client = bot()

    async def member(group, user, **kwargs):
        return NS(status=owner_role if user == 7 else bot_role, can_manage_topics=manage)

    client.get_chat_member.side_effect = member
    with pytest.raises(GroupPermissionError):
        await validate_group(client, -100111, {"owner_id": 7, "bot_id": 77})


async def test_pairing_owner_code_ttl_cancel_and_one_use(tmp_path):
    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path, bound=False)
    pairing = GroupPairing(storage, asyncio.Event())
    code = await pairing.begin()
    await pairing.handle(client, group_message("/bindgroup " + code, user=8))
    client.get_chat.assert_not_awaited()
    assert await storage.get_setting("telegram_group") is None
    pairing.cancel()
    await pairing.handle(client, group_message("/bindgroup " + code))
    assert await storage.get_setting("telegram_group") is None
    code = await pairing.begin()
    pairing.challenge.expires = 0
    await pairing.handle(client, group_message("/bindgroup " + code))
    assert await storage.get_setting("telegram_group") is None
    code = await pairing.begin()
    await pairing.handle(client, group_message("/bindgroup " + code))
    saved = await storage.get_setting("telegram_group")
    assert saved["group_id"] == -100111 and saved["owner_id"] == 7
    assert await storage.get_setting("topics_enabled") is True
    await pairing.handle(client, group_message("/bindgroup " + code, group=-100222))
    assert await storage.get_setting("telegram_group") == saved
    with pytest.raises(ValueError):
        await pairing.begin()


async def test_public_group_does_not_consume_valid_code(tmp_path):
    storage, _, _, _, _, client, _ = await setup(tmp_path, bound=False)
    pairing = GroupPairing(storage, asyncio.Event())
    code = await pairing.begin()
    client.get_chat.return_value.username = "public"
    with pytest.raises(GroupPermissionError):
        await pairing.handle(client, group_message("/bindgroup " + code))
    assert pairing.challenge.active
    assert await storage.get_setting("telegram_group") is None


async def test_two_managers_create_only_one_topic_and_restart_reuses_it(tmp_path):
    storage, group, chats, messages, manager, client, _ = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(*args, **kwargs):
        entered.set()
        await release.wait()
        return NS(message_thread_id=101)

    client.create_forum_topic.side_effect = create
    task = asyncio.create_task(manager.ensure(client, chats[0].local_id))
    await asyncio.wait_for(entered.wait(), 5)
    other = TopicManager(LocalStorage(storage.path), messages, asyncio.Event())
    with pytest.raises(TopicNeedsAttention):
        await other.ensure(client, chats[0].local_id)
    release.set()
    assert await task == 101
    assert await other.ensure(client, chats[0].local_id) == 101
    client.create_forum_topic.assert_awaited_once()


async def test_unknown_topic_creation_never_blind_retries_and_can_attach(tmp_path):
    storage, group, chats, messages, manager, client, _ = await setup(tmp_path)
    client.create_forum_topic.side_effect = TimeoutError
    with pytest.raises(TopicNeedsAttention):
        await manager.ensure(client, chats[0].local_id)
    with pytest.raises(TopicNeedsAttention):
        await manager.ensure(client, chats[0].local_id)
    client.create_forum_topic.assert_awaited_once()
    assert (await manager.repository.mapping(chats[0].local_id))["status"] == "unknown"
    await manager.repository.attach(chats[0].local_id, group, 101)
    assert await manager.ensure(client, chats[0].local_id) == 101
    with pytest.raises(sqlite3.IntegrityError):
        await manager.repository.attach(chats[1].local_id, group, 101)
    with pytest.raises(ValueError):
        await manager.repository.attach(chats[1].local_id, group, 1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user": 8},
        {"group": -100222},
        {"thread": 999},
        {"is_bot": True},
        {"sender_chat": NS(id=-100111)},
        {"text": "/menu"},
    ],
)
async def test_untrusted_group_text_never_enqueues(tmp_path, kwargs):
    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path)
    await storage.set_setting("sending_enabled", True)
    await manager.repository.attach(chats[0].local_id, group, 101)
    await manager.handle_group(client, group_message(**kwargs), 100)
    assert await messages.repository.operations("42") == []
    adapter.send_text.assert_not_awaited()


async def test_group_reply_is_durable_deduplicated_and_rechecks_permissions(tmp_path):
    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path)
    await storage.set_setting("sending_enabled", True)
    await manager.repository.attach(chats[0].local_id, group, 101)
    message = group_message()
    await manager.handle_group(client, message, 100)
    await manager.handle_group(client, message, 100)
    rows = await messages.repository.operations("42")
    assert len(rows) == 1 and rows[0]["source"] == "topic"
    assert rows[0]["source_thread_id"] == 101 and rows[0]["source_owner_id"] == 7

    async def authorize(operation):
        await manager.authorize_operation(client, operation)

    messages.topic_authorizer = authorize
    client.get_chat.return_value.username = "now_public"
    await messages.process_one()
    adapter.send_text.assert_not_awaited()
    assert (await messages.repository.operations("42"))[0]["status"] == "failed"


async def test_disabled_sending_does_not_stage_normal_group_discussion(tmp_path):
    storage, group, chats, messages, manager, client, _ = await setup(tmp_path)
    await manager.repository.attach(chats[0].local_id, group, 101)
    await manager.handle_group(client, group_message(), 100)
    assert await messages.repository.operations("42") == []


async def queue_mirror(storage, chat, remote="12", direction="incoming"):
    message = Message(remote, "42", chat.id, "99" if direction == "incoming" else "42", "reply")
    async with storage.connect() as db:
        await queue_topic_message(db, chat.local_id, message, direction)
        await db.commit()


def worker(storage, manager, client):
    class Vault:
        def get(self, name):
            return "test-only-token"

    return TopicMirrorService(
        storage, manager, Vault, lambda _: None, bot_factory=lambda **kw: client
    )


async def test_mirror_posts_once_to_correct_thread_and_records_mapping(tmp_path):
    storage, group, chats, _, manager, client, _ = await setup(tmp_path)
    await queue_mirror(storage, chats[0])
    service = worker(storage, manager, client)
    assert await service.drain() > 0
    args = client.send_message.await_args
    assert args.args[0] == -100111 and args.kwargs["message_thread_id"] == 101
    assert args.kwargs["parse_mode"] is None
    client.get_updates.assert_not_awaited()
    async with storage.connect() as db:
        assert (
            await (await db.execute("SELECT count(*) FROM telegram_message_mappings")).fetchone()
        )[0] == 1
    await queue_mirror(storage, chats[0])
    await service.drain()
    client.send_message.assert_awaited_once()


async def test_missing_topic_is_invalid_and_not_automatically_recreated(tmp_path):
    storage, group, chats, _, manager, client, _ = await setup(tmp_path)
    await manager.repository.attach(chats[0].local_id, group, 101)
    await queue_mirror(storage, chats[0])
    client.send_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=-100111, text="test"), message="message thread not found"
    )
    service = worker(storage, manager, client)
    await service.drain()
    assert (await manager.repository.mapping(chats[0].local_id))["status"] == "invalid"
    await service.drain()
    client.create_forum_topic.assert_not_awaited()
    client.send_message.assert_awaited_once()


async def test_topic_origin_confirmation_does_not_echo_original_owner_text(tmp_path):
    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path)
    await manager.repository.attach(chats[0].local_id, group, 101)
    await messages.enqueue_topic(chats[0].local_id, group, 500, 101, "reply", 100)
    operation = await messages.repository.claim("42")
    await queue_mirror(storage, chats[0], direction="outgoing")
    assert await manager.repository.claim(group) is None
    await messages.repository.confirm(operation, Message("12", "42", "1", "42", "reply"))
    assert await manager.repository.claim(group) is None
    async with storage.connect() as db:
        row = await (
            await db.execute("SELECT source,telegram_message_id FROM telegram_message_mappings")
        ).fetchone()
    assert row == ("topic", 500)


async def test_public_group_pauses_topics_without_exposing_message(tmp_path):
    storage, group, chats, _, manager, client, _ = await setup(tmp_path)
    await queue_mirror(storage, chats[0])
    client.get_chat.return_value.username = "public"
    await worker(storage, manager, client).drain()
    client.create_forum_topic.assert_not_awaited()
    client.send_message.assert_not_awaited()
    assert not await storage.get_setting("topics_enabled")


async def test_unknown_mirror_is_not_retried_without_private_owner_confirmation(tmp_path):
    from funpay_manager.telegram.service import TelegramBotService

    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path)
    await queue_mirror(storage, chats[0])
    client.send_message.side_effect = TimeoutError
    service = worker(storage, manager, client)
    await service.drain()
    event = (await manager.repository.deliveries(chats[0].local_id, group))[0]
    assert event["status"] == "unknown"
    async with storage.connect() as db:
        await db.execute(
            "UPDATE sync_state SET cursor='0' WHERE component LIKE 'tg-topic-cooldown:%'"
        )
        await db.commit()
    await service.drain()
    client.send_message.assert_awaited_once()
    client.send_message.side_effect = None
    handler = TelegramBotService(
        {"owner_id": 7, "bot_id": 77},
        "test-only-token",
        AsyncMock(),
        lambda _: None,
        messages=messages,
        storage=storage,
        topics=manager,
    )
    handler.bot = client
    query = NS(
        from_user=NS(id=7, is_bot=False),
        message=NS(chat=NS(id=7, type="private")),
        data=f"mirror_retry:{event['delivery_key']}",
        answer=AsyncMock(),
    )
    await handler.callback(query)
    assert (await manager.repository.delivery(event["delivery_key"], group))["status"] == "unknown"
    query.data = f"mirror_confirm:{event['delivery_key']}"
    query.from_user.id = 8
    await handler.callback(query)
    assert (await manager.repository.delivery(event["delivery_key"], group))["status"] == "unknown"
    query.from_user.id = 7
    await handler.callback(query)
    assert (await manager.repository.delivery(event["delivery_key"], group))["status"] == "queued"
    adapter.send_text.assert_not_awaited()


async def test_cancel_during_creation_is_unknown(tmp_path):
    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path)
    entered = asyncio.Event()

    async def create(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    client.create_forum_topic.side_effect = create
    task = asyncio.create_task(manager.ensure(client, chats[0].local_id))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await manager.repository.mapping(chats[0].local_id))["status"] == "unknown"
    with pytest.raises(TopicNeedsAttention):
        await manager.ensure(client, chats[0].local_id)
    client.create_forum_topic.assert_awaited_once()


async def test_cancel_binding_while_checking_group_does_not_save(tmp_path):
    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path, bound=False)
    pairing = GroupPairing(storage, asyncio.Event())
    code = await pairing.begin()
    entered, release = asyncio.Event(), asyncio.Event()
    details = client.get_chat.return_value

    async def get_chat(*args, **kwargs):
        entered.set()
        await release.wait()
        return details

    client.get_chat.side_effect = get_chat
    task = asyncio.create_task(pairing.handle(client, group_message("/bindgroup " + code)))
    await asyncio.wait_for(entered.wait(), 5)
    pairing.cancel()
    release.set()
    await task
    assert await storage.get_setting("telegram_group") is None


async def test_restore_pauses_topics_and_never_replays_old_group_deliveries(tmp_path):
    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path)
    await queue_mirror(storage, chats[0])
    await manager.repository.reserve(chats[0].local_id, group)
    backup = tmp_path / "backup.db"
    await storage.backup(backup)
    await storage.restore(backup, tmp_path / "safety.db")
    assert not await storage.get_setting("topics_enabled")
    assert (await manager.repository.mapping(chats[0].local_id))["status"] == "unknown"
    assert (await manager.repository.deliveries(chats[0].local_id, group))[0]["status"] == "unknown"
    await worker(storage, manager, client).drain()
    client.send_message.assert_not_awaited()


async def test_switching_funpay_account_does_not_leak_into_previous_group(tmp_path):
    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path)
    await queue_mirror(storage, chats[0])
    await storage.set_setting("funpay_account", {"id": "different"})
    await worker(storage, manager, client).drain()
    client.get_me.assert_not_awaited()
    client.send_message.assert_not_awaited()


async def test_private_bot_send_and_funpay_echo_create_one_mirror(tmp_path):
    storage, group, chats, messages, manager, client, adapter = await setup(tmp_path)
    operation_id = await messages.repository.enqueue(
        await messages.repository.chat(chats[0].local_id), 77, 7, 99, "reply"
    )
    operation = await messages.repository.claim("42")
    await messages.repository.confirm(operation, Message("12", "42", "1", "42", "reply"))
    await queue_mirror(storage, chats[0], direction="outgoing")
    assert len(await manager.repository.deliveries(chats[0].local_id, group)) == 1
    await worker(storage, manager, client).drain()
    client.send_message.assert_awaited_once()
    assert (await messages.repository.operations("42"))[0]["operation_id"] == operation_id


async def test_schema_three_upgrade_rollback_preserves_old_database(tmp_path, monkeypatch):
    from funpay_manager.storage import database
    from funpay_manager.storage.message_schema import MIGRATION_2
    from funpay_manager.storage.sync_schema import MIGRATION_3

    path = tmp_path / "v3.db"
    with sqlite3.connect(path) as db:
        db.executescript(database.MIGRATION_1 + MIGRATION_2 + MIGRATION_3)
        db.execute("PRAGMA user_version=3")
    migration = database.MIGRATION_4
    monkeypatch.setattr(
        database, "MIGRATION_4", "ALTER TABLE accounts ADD COLUMN partial TEXT; INVALID SQL;"
    )
    with pytest.raises(sqlite3.OperationalError):
        await LocalStorage(path).migrate()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "partial" not in {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
    monkeypatch.setattr(database, "MIGRATION_4", migration)
    await LocalStorage(path).migrate()


@pytest.mark.parametrize("state", ["absent", "expired", "cancelled", "basic_group", "bad_code"])
async def test_pairing_feedback_is_private_safe_and_throttled(tmp_path, state):
    storage, _, _, _, _, client, _ = await setup(tmp_path, bound=False)
    pairing = GroupPairing(storage, asyncio.Event())
    code = await pairing.begin() if state != "absent" else "test-only-inactive-code"
    msg = group_message("/bindgroup@test_bot " + code)
    if state == "expired":
        pairing.challenge.expires = 0
    elif state == "cancelled":
        pairing.cancel()
    elif state == "basic_group":
        msg.chat.type = "group"
    elif state == "bad_code":
        msg.text = "/bindgroup@test_bot wrong-test-code"
    assert await pairing.handle(client, msg)
    assert await pairing.handle(client, msg)
    assert await storage.get_setting("telegram_group") is None
    client.send_message.assert_awaited_once()
    call = client.send_message.await_args
    assert call.args[0] == 7
    assert code not in call.args[1] and "wrong-test-code" not in call.args[1]
    assert call.kwargs["parse_mode"] is None
    client.get_chat.assert_not_awaited()


async def test_pairing_does_not_reply_to_foreign_anon_forwarded_or_other_bot_command(tmp_path):
    storage, _, _, _, _, client, _ = await setup(tmp_path, bound=False)
    pairing = GroupPairing(storage, asyncio.Event())
    code = await pairing.begin()
    foreign = group_message("/bindgroup@test_bot " + code, user=8)
    anonymous = group_message("/bindgroup@test_bot " + code, sender_chat=NS(id=-100111))
    forwarded = group_message("/bindgroup@test_bot " + code)
    forwarded.forward_origin = NS(type="user")
    for msg in (foreign, anonymous, forwarded, group_message("/bindgroup@other_bot " + code)):
        assert await pairing.handle(client, msg)
    client.send_message.assert_not_awaited()
    client.get_chat.assert_not_awaited()
    assert pairing.challenge.active


async def test_pairing_success_survives_failure_of_private_confirmation(tmp_path):
    storage, _, _, _, _, client, _ = await setup(tmp_path, bound=False)
    pairing = GroupPairing(storage, asyncio.Event())
    code = await pairing.begin()
    client.send_message.side_effect = TimeoutError
    assert await pairing.handle(client, group_message("/bindgroup@TEST_BOT " + code))
    assert await storage.get_setting("telegram_group")
    assert pairing.error is None
    assert all(call.args[0] == 7 for call in client.send_message.await_args_list)


async def test_real_dispatcher_routes_addressed_group_command_to_pairing(tmp_path):
    from aiogram import Bot
    from aiogram.types import Update

    from funpay_manager.telegram.service import TelegramBotService

    storage, _, _, _, manager, mock, _ = await setup(tmp_path, bound=False)
    pairing = GroupPairing(storage, asyncio.Event())
    code = await pairing.begin()
    client = Bot(token="77:test-only-token")
    client.get_chat = mock.get_chat
    client.get_chat_member = mock.get_chat_member
    client.send_message = mock.send_message
    handler = TelegramBotService(
        await storage.get_setting("telegram_binding"),
        "test-only-token",
        AsyncMock(),
        lambda _: None,
        storage=storage,
        topics=manager,
        group_pairing=pairing,
    )
    handler.bot = client
    command = "/bindgroup@test_bot"
    update = Update.model_validate(
        {
            "update_id": 100,
            "message": {
                "message_id": 10,
                "date": 0,
                "chat": {"id": -100111, "type": "supergroup", "title": "test", "is_forum": True},
                "from": {"id": 7, "is_bot": False, "first_name": "test"},
                "text": command + " " + code,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
            },
        }
    )
    try:
        await handler.dispatcher.feed_update(client, update)
        assert await storage.get_setting("telegram_group")
        assert "привязана" in mock.send_message.await_args.args[1]
        assert all(call.args[0] == 7 for call in mock.send_message.await_args_list)
    finally:
        await client.session.close()


@pytest.mark.parametrize("stage", ["chat", "owner", "bot"])
@pytest.mark.parametrize("kind", ["network", "bad_request"])
async def test_api_check_failure_identifies_stage_without_raw_response(stage, kind):
    client = bot()
    method = GetChat(chat_id=-100111)
    error_type = TelegramNetworkError if kind == "network" else TelegramBadRequest
    original = error_type(method=method, message="sensitive-token-or-response")
    member = client.get_chat_member.side_effect
    if stage == "chat":
        client.get_chat.side_effect = original
    else:

        async def check(group_id, user_id, **kwargs):
            if user_id == (7 if stage == "owner" else 77):
                raise original
            return await member(group_id, user_id, **kwargs)

        client.get_chat_member.side_effect = check
    with pytest.raises(GroupLookupError) as error:
        await validate_group(client, -100111, {"owner_id": 7, "bot_id": 77})
    text = pairing_failure(error.value)
    assert f"tg_group_{stage}_{kind}" in text
    assert "sensitive" not in text
    assert not isinstance(error.value, GroupPermissionError)


async def test_group_rate_limit_is_preserved_for_mirror_cooldown():
    client = bot()
    original = TelegramRetryAfter(
        method=GetChat(chat_id=-100111), message="sensitive", retry_after=9
    )
    client.get_chat.side_effect = original
    with pytest.raises(TelegramRetryAfter) as error:
        await validate_group(client, -100111, {"owner_id": 7, "bot_id": 77})
    assert error.value is original
    assert "tg_group_rate_limit" in pairing_failure(original)
    assert "sensitive" not in pairing_failure(original)


async def test_actual_aiogram_chat_model_with_private_forum_passes():
    from aiogram.types import ChatFullInfo

    client = bot()
    client.get_chat.return_value = ChatFullInfo.model_validate(
        {
            "id": -100111,
            "type": "supergroup",
            "title": "private",
            "is_forum": True,
            "accent_color_id": 0,
            "max_reaction_count": 11,
            "accepted_gift_types": {
                "unlimited_gifts": False,
                "limited_gifts": False,
                "unique_gifts": False,
                "premium_subscription": False,
                "gifts_from_channels": False,
            },
        }
    )
    await validate_group(client, -100111, {"owner_id": 7, "bot_id": 77})


async def test_pairing_reports_actual_failed_right_and_preserves_challenge(tmp_path):
    storage, _, _, _, _, client, _ = await setup(tmp_path, bound=False)
    pairing = GroupPairing(storage, asyncio.Event())
    code = await pairing.begin()
    client.get_chat_member.side_effect = lambda *args, **kwargs: NS(
        status="administrator", can_manage_topics=False
    )
    with pytest.raises(GroupPermissionError):
        await pairing.handle(client, group_message("/bindgroup@test_bot " + code))
    assert "tg_group_manage_topics" in pairing.error
    assert "администратор" in pairing.error
    assert "управление темами: не подтверждено" in pairing.error
    assert pairing.challenge.active
    assert code not in client.send_message.await_args.args[1]
