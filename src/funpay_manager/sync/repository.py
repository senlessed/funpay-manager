import time

import aiosqlite

from funpay_manager.notifications.repository import queue_event, read_setting
from funpay_manager.storage.messages import MessageRepository


class SyncRepository:
    def __init__(self, storage):
        self.storage = storage
        self.messages = MessageRepository(storage)

    async def snapshot(self, account, chats, limit=5):
        saved = await self.messages.save_chats(account, chats)
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            key = f"fp-baseline:{account.id}"
            async with db.execute("SELECT 1 FROM sync_state WHERE component=?", (key,)) as cursor:
                initial = await cursor.fetchone() is None
            states = []
            for chat in saved:
                await db.execute(
                    "INSERT INTO chat_sync_state(chat_id,baseline_pending) VALUES (?,?) "
                    "ON CONFLICT(chat_id) DO NOTHING",
                    (chat.local_id, int(initial)),
                )
                async with db.execute(
                    "SELECT * FROM chat_sync_state WHERE chat_id=?", (chat.local_id,)
                ) as cursor:
                    state = dict(await cursor.fetchone())
                    state["chat"] = chat
                    state["changed"] = (
                        state["preview"] != chat.last_message or state["unread"] != chat.unread
                    )
                    states.append(state)
            await db.execute(
                "INSERT INTO sync_state(component,cursor) VALUES (?,'1') "
                "ON CONFLICT(component) DO NOTHING",
                (key,),
            )
            await db.commit()
        # Reserve two slots for oldest checks so unchanged previews do not starve.
        oldest = sorted(states, key=lambda state: (state["last_checked"], state["chat_id"]))
        selected = oldest[: min(2, limit)]
        selected_ids = {state["chat_id"] for state in selected}
        remaining = sorted(
            (state for state in states if state["chat_id"] not in selected_ids),
            key=lambda state: (not state["changed"], state["last_checked"], state["chat_id"]),
        )
        return [state["chat"] for state in (selected + remaining[: limit - len(selected)])]

    async def ingest(self, account_id, chat, messages):
        """History, watermark and notification events commit together."""
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            active = await read_setting(db, "funpay_account")
            if (
                not active
                or active["id"] != account_id
                or not await read_setting(db, "sync_enabled", True)
            ):
                await db.rollback()
                return 0
            async with db.execute(
                "SELECT * FROM chat_sync_state WHERE chat_id=?", (chat.local_id,)
            ) as cursor:
                state = await cursor.fetchone()
            async with db.execute(
                "SELECT account_id,remote_id FROM chats WHERE id=?", (chat.local_id,)
            ) as cursor:
                mapping = await cursor.fetchone()
            if (
                state is None
                or mapping is None
                or mapping["account_id"] != account_id
                or mapping["remote_id"] != chat.id
            ):
                raise ValueError("Invalid sync chat mapping")
            watermark = int(state["watermark"])
            ordered = sorted(messages, key=lambda message: int(message.id))
            new = [message for message in ordered if int(message.id) > watermark]
            if not state["initialized"]:
                new = [] if state["baseline_pending"] else ordered[-1:]
            new_ids = {message.id for message in new}
            notifications = 0
            for message in ordered:
                if message.account_id != account_id or message.chat_id != chat.id:
                    raise ValueError("History belongs to another account/chat")
                direction = (
                    "system"
                    if message.author_id == "0"
                    else "outgoing"
                    if message.author_id == account_id
                    else "unknown"
                    if message.author_id == "-1"
                    else "incoming"
                )
                await db.execute(
                    "INSERT INTO messages(chat_id,remote_id,author_id,text,kind,direction) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id,remote_id) DO NOTHING",
                    (
                        chat.local_id,
                        message.id,
                        message.author_id,
                        message.text,
                        message.kind,
                        direction,
                    ),
                )
                if message.id in new_ids and direction == "incoming":
                    content = message.text if message.kind == "text" else f"[{message.kind}]"
                    title = (
                        "Новое сообщение"
                        if state["initialized"]
                        else "Сообщение в обнаруженном чате"
                    )
                    text = (
                        f"{title} · {chat.username[:100]}\nЧат {chat.id} · #{message.id}\n\n"
                        f"{content[:2700]}"
                    )
                    notifications += int(
                        await queue_event(
                            db,
                            account_id,
                            f"incoming:{account_id}:{chat.id}:{message.id}",
                            text,
                            f"h:{chat.local_id}:{chat.route_key}:0",
                        )
                    )
            if ordered:
                watermark = max(watermark, int(ordered[-1].id))
            await db.execute(
                "UPDATE chat_sync_state SET watermark=?,initialized=1,baseline_pending=0,"
                "preview=?,unread=?,last_checked=? WHERE chat_id=?",
                (str(watermark), chat.last_message, chat.unread, time.time(), chat.local_id),
            )
            await db.commit()
            return notifications

    async def success(self, account_id):
        async with self.storage.connect() as db:
            await db.execute(
                "INSERT INTO sync_state(component,cursor) VALUES (?,?) "
                "ON CONFLICT(component) DO UPDATE SET cursor=excluded.cursor,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (f"fp-last-success:{account_id}", str(time.time())),
            )
            await db.commit()

    async def failed_check(self, local_id):
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE chat_sync_state SET last_checked=? WHERE chat_id=?", (time.time(), local_id)
            )
            await db.commit()
