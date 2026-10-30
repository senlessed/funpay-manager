import time
from uuid import uuid4

import aiosqlite

from funpay_manager.notifications.repository import read_setting
from funpay_manager.utils.text import telegram_text


async def queue_topic_message(db, chat_id, message, direction, operation=None):
    group = await read_setting(db, "telegram_group")
    binding = await read_setting(db, "telegram_binding")
    if (
        not group
        or not binding
        or not await read_setting(db, "topics_enabled", False)
        or group["account_id"] != message.account_id
        or (group["bot_id"], group["owner_id"]) != (binding["bot_id"], binding["owner_id"])
    ):
        return False
    if (
        operation
        and operation.get("source") == "topic"
        and operation.get("source_binding_key") == group["binding_key"]
    ):
        await db.execute(
            "INSERT INTO telegram_message_mappings VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (
                chat_id,
                message.id,
                group["binding_key"],
                group["group_id"],
                operation["source_thread_id"],
                operation["telegram_message_id"],
                "topic",
            ),
        )
        await db.execute(
            "UPDATE telegram_topic_deliveries SET status='sent',last_error='already_visible' "
            "WHERE chat_id=? AND remote_message_id=? AND binding_key=? AND status='queued'",
            (chat_id, message.id, group["binding_key"]),
        )
        return False
    async with db.execute(
        "SELECT 1 FROM telegram_message_mappings WHERE chat_id=? AND "
        "remote_message_id=? AND binding_key=?",
        (chat_id, message.id, group["binding_key"]),
    ) as cursor:
        if await cursor.fetchone():
            return False
    content = message.text if message.kind == "text" else f"[{message.kind}]"
    text = telegram_text(
        f"{'Покупатель' if direction == 'incoming' else 'Продавец'} · #{message.id}\n{content}"
    )
    cursor = await db.execute(
        "INSERT INTO telegram_topic_deliveries(delivery_key,chat_id,remote_message_id,"
        "binding_key,direction,text,status) VALUES (?,?,?,?,?,?,'queued') "
        "ON CONFLICT DO NOTHING",
        (uuid4().hex[:24], chat_id, message.id, group["binding_key"], direction, text),
    )
    return cursor.rowcount == 1


class TopicRepository:
    def __init__(self, storage):
        self.storage = storage

    async def deliveries(self, chat_id, group):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM telegram_topic_deliveries WHERE chat_id=? "
                "AND binding_key=? ORDER BY id DESC LIMIT 5",
                (chat_id, group["binding_key"]),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def delivery(self, delivery_key, group):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM telegram_topic_deliveries WHERE delivery_key=? AND binding_key=?",
                (delivery_key, group["binding_key"]),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def retry_delivery(self, delivery_key, group):
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE telegram_topic_deliveries SET status='queued',next_attempt=0,"
                "last_error='manual_retry' WHERE delivery_key=? AND binding_key=? "
                "AND status IN ('unknown','failed')",
                (delivery_key, group["binding_key"]),
            )
            await db.commit()

    async def mapping(self, chat_id):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM telegram_topic_mappings WHERE chat_id=?", (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def reserve(self, chat_id, group):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM telegram_topic_mappings WHERE chat_id=?", (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row and (
                row["binding_key"] != group["binding_key"] or row["group_id"] != group["group_id"]
            ):
                raise ValueError("Mapping belongs to another group binding")
            create = row is None or row["status"] == "pending"
            if row is None:
                await db.execute(
                    "INSERT INTO telegram_topic_mappings(chat_id,binding_key,group_id,status) "
                    "VALUES (?,?,?,'creating')",
                    (chat_id, group["binding_key"], group["group_id"]),
                )
            elif create:
                await db.execute(
                    "UPDATE telegram_topic_mappings SET status='creating' WHERE chat_id=?",
                    (chat_id,),
                )
            await db.commit()
            return create, dict(row) if row else None

    async def set_state(self, chat_id, state, thread_id=None):
        if state not in ("pending", "ready", "invalid", "unknown"):
            raise ValueError("Invalid topic state")
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE telegram_topic_mappings SET status=?,thread_id=coalesce(?,thread_id),"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE chat_id=?",
                (state, thread_id, chat_id),
            )
            await db.commit()

    async def attach(self, chat_id, group, thread_id):
        if thread_id <= 1:
            raise ValueError("General topic cannot be attached")
        async with self.storage.connect() as db:
            await db.execute(
                "INSERT INTO telegram_topic_mappings"
                "(chat_id,binding_key,group_id,thread_id,status) "
                "VALUES (?,?,?,?,'ready') ON CONFLICT(chat_id) DO UPDATE SET "
                "binding_key=excluded.binding_key,group_id=excluded.group_id,"
                "thread_id=excluded.thread_id,status='ready'",
                (chat_id, group["binding_key"], group["group_id"], thread_id),
            )
            await db.commit()

    async def chat_for_thread(self, group, thread_id):
        async with self.storage.connect() as db:
            async with db.execute(
                "SELECT chat_id FROM telegram_topic_mappings WHERE group_id=? "
                "AND binding_key=? AND thread_id=? AND status='ready'",
                (group["group_id"], group["binding_key"], thread_id),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def recover(self):
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE telegram_topic_mappings SET status='unknown' WHERE status='creating'"
            )
            await db.execute(
                "UPDATE telegram_topic_deliveries SET status='unknown',last_error='interrupted' "
                "WHERE status='sending'"
            )
            await db.commit()

    async def claim(self, group):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT d.*,c.username FROM telegram_topic_deliveries d JOIN chats c "
                "ON c.id=d.chat_id WHERE d.binding_key=? AND c.account_id=? "
                "AND d.status='queued' AND d.next_attempt<=? AND NOT EXISTS "
                "(SELECT 1 FROM telegram_topic_mappings m WHERE m.chat_id=d.chat_id "
                "AND (m.status IN ('creating','unknown','invalid') "
                "OR m.binding_key!=d.binding_key)) AND NOT "
                "(d.direction='outgoing' AND EXISTS (SELECT 1 FROM outgoing_messages o "
                "WHERE o.chat_id=d.chat_id AND o.status IN ('sending','unknown'))) "
                "ORDER BY d.id LIMIT 1",
                (group["binding_key"], group["account_id"], time.time()),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                await db.execute(
                    "UPDATE telegram_topic_deliveries SET status='sending' WHERE id=?", (row["id"],)
                )
            await db.commit()
            return dict(row) if row else None

    async def finish(
        self, event_id, status, error=None, retry_at=0, sent=None, group=None, thread_id=None
    ):
        async with self.storage.connect() as db:
            if sent and group:
                async with db.execute(
                    "SELECT chat_id,remote_message_id FROM telegram_topic_deliveries WHERE id=?",
                    (event_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                await db.execute(
                    "INSERT INTO telegram_message_mappings VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT DO NOTHING",
                    (
                        row[0],
                        row[1],
                        group["binding_key"],
                        group["group_id"],
                        thread_id,
                        sent.message_id,
                        "mirror",
                    ),
                )
            await db.execute(
                "UPDATE telegram_topic_deliveries SET status=?,last_error=?,next_attempt=? "
                "WHERE id=? AND status='sending'",
                (status, error, retry_at, event_id),
            )
            await db.commit()

    async def cooldown(self, group):
        async with self.storage.connect() as db:
            async with db.execute(
                "SELECT cursor FROM sync_state WHERE component=?",
                (f"tg-topic-cooldown:{group['bot_id']}:{group['group_id']}",),
            ) as cursor:
                row = await cursor.fetchone()
                return max(0, float(row[0]) - time.time()) if row else 0

    async def defer(self, group, seconds):
        key = f"tg-topic-cooldown:{group['bot_id']}:{group['group_id']}"
        async with self.storage.connect() as db:
            async with db.execute(
                "SELECT cursor FROM sync_state WHERE component=?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
            until = max(float(row[0]) if row else 0, time.time() + seconds)
            await db.execute(
                "INSERT INTO sync_state(component,cursor) VALUES (?,?) "
                "ON CONFLICT(component) DO UPDATE SET cursor=excluded.cursor",
                (key, str(until)),
            )
            await db.commit()
