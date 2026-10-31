import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from funpay_manager.commerce_workers import AutoRaiseWorker
from funpay_manager.funpay.adapter import AccountInfo, ChatInfo
from funpay_manager.funpay.commerce import (
    Order,
    RaiseCategory,
    RaiseResult,
    parse_orders,
    parse_raise_categories,
    parse_raise_result,
)
from funpay_manager.funpay.raising import FunPayRaisingAdapter
from funpay_manager.funpay.readonly import FunPayConnectionError, FunPayReadOnlyAdapter
from funpay_manager.messages.service import MessageService
from funpay_manager.storage.database import MIGRATION_1, LocalStorage
from funpay_manager.storage.message_schema import MIGRATION_2
from funpay_manager.storage.messages import MessageRepository
from funpay_manager.storage.orders import OrdersRepository
from funpay_manager.storage.raising import RaiseRepository
from funpay_manager.storage.sync_schema import MIGRATION_3
from funpay_manager.storage.topic_schema import MIGRATION_4
from funpay_manager.topics.manager import TopicManager
from funpay_manager.topics.worker import TopicMirrorService

CAT = RaiseCategory(5, "Game", (10, 11))
HOME = (
    '<div class="promo-game-item"><div class="game-title" data-id="5">Game</div>'
    '<a href="/lots/10/">One</a><a href="/lots/11/">Two</a></div>'
)
PROFILE = (
    '<div class="user-link-name">seller</div><div><div class="offer-list-title-container">'
    '<h3><a href="/lots/10/">One</a></h3></div><a class="tc-item">offer</a></div>'
)


async def setup(tmp_path):
    storage = LocalStorage(tmp_path / "db")
    await storage.migrate()
    await storage.set_setting("funpay_account", {"id": "42", "username": "seller"})
    chats = await MessageRepository(storage).save_chats(
        AccountInfo("42", "seller"), [ChatInfo("1", "same"), ChatInfo("2", "same")]
    )
    return storage, chats


def order_html(state="Закрыт", price="1 234,50 ₽"):
    return (
        '<div class="user-link-name">seller</div><a class="tc-item">'
        + '<div class="tc-order">#ABCD1234</div><div class="media-user-name">'
        '<span data-href="https://funpay.com/users/99/">buyer</span></div>'
        f'<div class="tc-status">{state}</div><div class="tc-price">{price}</div>'
        '<div class="order-desc">description</div></a>'
    )


@pytest.mark.parametrize(
    "label,status",
    [
        ("Оплачен", "paid"),
        ("Закрыт", "closed"),
        ("Возврат", "refunded"),
        ("Новый статус", "unknown"),
    ],
)
def test_order_status_and_decimal_currency(label, status):
    rows, more = parse_orders(order_html(label))
    assert rows[0].status == status and rows[0].amount == "1234.50" and rows[0].currency == "RUB"
    assert rows[0].buyer_id == "99" and not more


def test_order_format_does_not_silently_invent_identity_or_currency():
    with pytest.raises(FunPayConnectionError):
        parse_orders(order_html().replace("/users/99/", "/unknown/"))
    with pytest.raises(FunPayConnectionError):
        parse_orders("<body>login</body>")
    assert parse_orders(order_html(price="unknown currency"))[0][0].amount is None


async def test_partial_orders_fetches_open_filter_and_peer_is_not_nickname():
    adapter = FunPayReadOnlyAdapter("test-key")
    adapter.account = AccountInfo("42", "seller")
    adapter._request = AsyncMock(
        side_effect=[
            order_html() + '<input name="continue" value="NEXT">',
            order_html("Оплачен").replace("ABCD1234", "NEW"),
            json.dumps({"chat": {"node": {"id": 1, "name": "users-99-42"}, "messages": []}}),
        ]
    )
    rows, more = await adapter.get_orders()
    assert len(rows) == 2 and not more
    assert adapter._request.call_args_list[1].args[1] == "/orders/trade?state=paid"
    await adapter.get_history("1")
    assert adapter.chat_peers == {"1": "99"}


def test_category_mapping_uses_only_published_common_lots():
    assert parse_raise_categories(HOME, PROFILE) == [RaiseCategory(5, "Game", (10,))]
    assert parse_raise_categories(HOME, PROFILE.replace("/lots/10/", "/chips/10/")) == []
    with pytest.raises(FunPayConnectionError):
        parse_raise_categories(HOME, PROFILE.replace("/lots/10/", "/lots/999/"))


@pytest.mark.parametrize(
    "payload,status,wait",
    [
        ('{"error":false}', "raised", None),
        ("not json", "unknown", None),
        ("{}", "unknown", None),
        ('{"error":true,"msg":"Подождите 2 минуты 10 секунд"}', "cooldown", 130),
        ('{"error":true,"msg":"Wait 1 hour 5 minutes"}', "cooldown", 3900),
        ('{"error":true,"msg":"Подождите позже"}', "rejected", None),
    ],
)
def test_raise_response_never_invents_cooldown(payload, status, wait):
    assert parse_raise_result(payload) == RaiseResult(status, wait)


async def test_raise_post_has_explicit_nodes_and_last_second_opt_in_check():
    adapter = FunPayRaisingAdapter("test-key")
    adapter.account = AccountInfo("42", "seller")
    adapter._csrf = "test-csrf"
    adapter.categories = AsyncMock(return_value=[CAT])
    adapter._request = AsyncMock(return_value='{"error":false}')
    deny = AsyncMock(return_value=False)
    assert (await adapter.raise_category(CAT, before_post=deny)).status == "paused"
    adapter._request.assert_not_awaited()
    allow = AsyncMock(return_value=True)
    assert (await adapter.raise_category(CAT, before_post=allow)).status == "raised"
    args = adapter._request.await_args
    assert args.args == ("POST", "/lots/raise")
    assert args.kwargs["data"]["node_ids[]"] == [10, 11]
    adapter._request.side_effect = TimeoutError
    assert (await adapter.raise_category(CAT, before_post=allow)).status == "unknown"
    assert adapter._request.await_count == 2


async def test_unowned_nodes_never_post():
    adapter = FunPayRaisingAdapter("test-key")
    adapter.categories = AsyncMock(return_value=[RaiseCategory(5, "Game", (10,))])
    adapter._request = AsyncMock()
    assert (
        await adapter.raise_category(CAT, before_post=AsyncMock(return_value=True))
    ).status == "rejected"
    adapter._request.assert_not_awaited()


async def test_preflight_network_failure_can_retry_reading_without_post():
    adapter = FunPayRaisingAdapter("test-key")
    adapter.categories = AsyncMock(side_effect=TimeoutError)
    adapter._request = AsyncMock()
    assert await adapter.raise_category(
        CAT, before_post=AsyncMock(return_value=True)
    ) == RaiseResult("paused", 60)
    adapter._request.assert_not_awaited()


async def test_auto_worker_is_default_off_and_success_is_scheduled_once(tmp_path):
    storage, _ = await setup(tmp_path)
    adapter = NS(raise_category=AsyncMock(return_value=RaiseResult("raised")))
    entered = []

    @asynccontextmanager
    async def context(account):
        entered.append(account)
        yield adapter

    worker = AutoRaiseWorker(storage, context, lambda _: None)
    await worker.cycle()
    assert entered == []
    await due(worker.repository, storage)
    await worker.cycle()
    assert (await worker.repository.rows("42"))[0]["status"] == "raised"
    assert (await worker.repository.rows("42"))[0]["next_attempt"] > time.time() + 3500
    await worker.cycle()
    adapter.raise_category.assert_awaited_once()


async def test_chat_filters_use_order_identity_not_nickname(tmp_path):
    from funpay_manager.telegram.service import TelegramBotService

    storage, chats = await setup(tmp_path)
    repo = OrdersRepository(storage)
    await repo.set_peer(chats[0].local_id, "42", "99")
    await repo.set_peer(chats[1].local_id, "42", "100")
    await repo.save(
        "42",
        [Order("A", "99", "paid", "", None, None), Order("B", "100", "closed", "", None, None)],
    )
    handler = TelegramBotService(
        {"owner_id": 7, "bot_id": 77},
        "test-key",
        AsyncMock(return_value=chats),
        lambda _: None,
        storage=storage,
        messages=MessageService(storage, AsyncMock()),
    )
    handler.bot = AsyncMock()
    message = NS(chat=NS(id=7, type="private"), message_id=1, text="old", reply_markup=None)
    query = NS(
        from_user=NS(id=7, is_bot=False), message=message, answer=AsyncMock(), data="chats:paid:0"
    )
    await handler.callback(query)
    text = handler.bot.edit_message_text.await_args.args[0]
    assert "ID 1" in text and "ID 2" not in text
    query.data = "chats:closed:0"
    await handler.callback(query)
    text = handler.bot.edit_message_text.await_args.args[0]
    assert "ID 2" in text and "ID 1" not in text


async def due(repo, storage):
    await repo.catalog("42", [CAT])
    await repo.configure("42", [5], 3600, True)
    async with storage.connect() as db:
        await db.execute("UPDATE lot_schedules SET next_attempt=0")
        await db.commit()


async def test_claim_is_atomic_cooldown_survives_toggle_and_restart(tmp_path):
    storage, _ = await setup(tmp_path)
    repo = RaiseRepository(storage)
    assert await repo.claim("42") is None
    await due(repo, storage)
    jobs = await asyncio.gather(
        repo.claim("42"), RaiseRepository(LocalStorage(storage.path)).claim("42")
    )
    job = next(j for j in jobs if j)
    assert sum(j is not None for j in jobs) == 1
    await repo.finish(job, "cooldown", 7200)
    before = (await repo.rows("42"))[0]["next_attempt"]
    await repo.configure("42", [5], 900, False)
    await repo.configure("42", [5], 900, True)
    assert (await repo.rows("42"))[0]["next_attempt"] >= before
    await RaiseRepository(LocalStorage(storage.path)).recover()
    assert await repo.claim("42") is None


async def test_restart_and_restore_never_replay_inflight_raise(tmp_path):
    storage, _ = await setup(tmp_path)
    repo = RaiseRepository(storage)
    await due(repo, storage)
    assert await repo.claim("42")
    backup = tmp_path / "backup.db"
    await storage.backup(backup)
    await repo.recover()
    assert (await repo.rows("42"))[0]["status"] == "unknown"
    assert await repo.claim("42") is None
    await storage.restore(backup, tmp_path / "safety.db")
    assert not await storage.get_setting("auto_raise_enabled")
    assert not await storage.get_setting("orders_enabled")
    assert (await repo.rows("42"))[0]["status"] == "unknown"
    assert await repo.claim("42") is None


async def test_unknown_requires_explicit_reset_and_account_guard(tmp_path):
    storage, _ = await setup(tmp_path)
    repo = RaiseRepository(storage)
    await due(repo, storage)
    job = await repo.claim("42")
    await repo.finish(job, "unknown")
    await repo.configure("42", [5], 900, True)
    assert (await repo.rows("42"))[0]["status"] == "unknown"
    await storage.set_setting("funpay_account", {"id": "other"})
    assert not await repo.authorized(job)
    with pytest.raises(ValueError):
        await repo.reset_unknown("42")
    with pytest.raises(ValueError):
        await repo.configure("42", [5], 900, True)


async def test_worker_cancel_during_post_leaves_unknown_and_does_not_retry(tmp_path):
    storage, _ = await setup(tmp_path)
    repo = RaiseRepository(storage)
    await due(repo, storage)
    entered = asyncio.Event()

    async def post(*args, **kwargs):
        assert await kwargs["before_post"]()
        entered.set()
        await asyncio.Event().wait()

    adapter = NS(raise_category=AsyncMock(side_effect=post))

    @asynccontextmanager
    async def context(account):
        yield adapter

    worker = AutoRaiseWorker(storage, context, lambda _: None)
    task = asyncio.create_task(worker.cycle())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await repo.rows("42"))[0]["status"] == "unknown"
    await worker.cycle()
    adapter.raise_category.assert_awaited_once()


async def test_order_status_open_priority_stale_and_partial_are_unknown(tmp_path):
    storage, chats = await setup(tmp_path)
    repo = OrdersRepository(storage)
    await repo.set_peer(chats[0].local_id, "42", "99")
    orders = [
        Order("A", "99", "closed", "desc", "1.00", "RUB"),
        Order("B", "99", "paid", "desc", "2.00", "RUB"),
    ]
    await repo.save("42", orders)
    assert (await repo.statuses("42"))[chats[0].local_id] == "paid"
    assert (await repo.statuses("42"))[chats[1].local_id] == "unknown"
    await repo.save("42", [orders[0]], more=True)
    assert (await repo.statuses("42"))[chats[0].local_id] == "unknown"
    # A later snapshot explicitly observes both closed; old paid cannot remain active forever.
    await repo.save("42", [orders[0], Order("B", "99", "closed", "desc", "2.00", "RUB")])
    assert (await repo.statuses("42"))[chats[0].local_id] == "closed"
    async with storage.connect() as db:
        await db.execute("UPDATE orders_cache SET observed_at=?", (time.time() - 200,))
        await db.commit()
    assert (await repo.statuses("42"))[chats[0].local_id] == "unknown"


async def test_topic_title_is_status_without_hashtag_and_checks_group(tmp_path):
    storage, chats = await setup(tmp_path)
    group = {
        "group_id": -100111,
        "owner_id": 7,
        "bot_id": 77,
        "account_id": "42",
        "binding_key": "test-binding",
    }
    await storage.set_setting("telegram_binding", {"owner_id": 7, "bot_id": 77})
    await storage.set_setting("telegram_group", group)
    await storage.set_setting("topics_enabled", True)
    manager = TopicManager(storage, MessageService(storage, AsyncMock()), asyncio.Event())
    await manager.repository.attach(chats[0].local_id, group, 101)
    await manager.orders.set_peer(chats[0].local_id, "42", "99")
    await manager.orders.save("42", [Order("A", "99", "paid", "desc", None, None)])
    bot = AsyncMock()
    bot.get_me.return_value = NS(id=77)
    bot.get_chat.return_value = NS(
        id=-100111,
        type="supergroup",
        is_forum=True,
        username=None,
        active_usernames=[],
        linked_chat_id=None,
        location=None,
    )
    bot.get_chat_member.return_value = NS(status="administrator", can_manage_topics=True)

    class Vault:
        def get(self, name):
            return "test-token"

    worker = TopicMirrorService(
        storage, manager, Vault, lambda _: None, bot_factory=lambda **kw: bot
    )
    assert await worker.drain_titles() > 0
    name = bot.edit_forum_topic.await_args.kwargs["name"]
    assert name.startswith("Открытый заказ · same") and "#" not in name
    bot.send_message.assert_not_awaited()


async def test_migration_five_rolls_back_and_upgrades_real_four(tmp_path, monkeypatch):
    from funpay_manager.storage import database

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript(MIGRATION_1 + MIGRATION_2 + MIGRATION_3 + MIGRATION_4)
        db.execute("PRAGMA user_version=4")
    actual = database.MIGRATION_5
    monkeypatch.setattr(
        database, "MIGRATION_5", "ALTER TABLE chats ADD COLUMN partial TEXT; INVALID SQL;"
    )
    with pytest.raises(sqlite3.OperationalError):
        await LocalStorage(path).migrate()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "partial" not in {r[1] for r in db.execute("PRAGMA table_info(chats)")}
    monkeypatch.setattr(database, "MIGRATION_5", actual)
    await LocalStorage(path).migrate()
