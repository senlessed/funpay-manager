import time
from dataclasses import replace
from uuid import uuid4

import aiosqlite

from funpay_manager.notifications.repository import queue_delivery
from funpay_manager.topics.repository import queue_topic_message


class MessageRepository:
    def __init__(self, storage):
        self.storage = storage

    async def save_chats(self, account, chats):
        result = []
        async with self.storage.connect() as db:
            await db.execute(
                "INSERT INTO accounts(id,username) VALUES (?,?) ON CONFLICT(id) "
                "DO UPDATE SET username=excluded.username",
                (account.id, account.username),
            )
            for chat in chats:
                await db.execute(
                    "INSERT INTO chats(account_id,remote_id,username,route_key) VALUES (?,?,?,?) "
                    "ON CONFLICT(account_id,remote_id) DO UPDATE SET "
                    "username=excluded.username",
                    (account.id, chat.id, chat.username, uuid4().hex[:16]),
                )
                async with db.execute(
                    "SELECT id,route_key FROM chats WHERE account_id=? AND remote_id=?",
                    (account.id, chat.id),
                ) as cursor:
                    local_id, route_key = await cursor.fetchone()
                result.append(replace(chat, local_id=local_id, route_key=route_key))
            await db.commit()
        return result

    async def chat(self, local_id: int):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM chats WHERE id=?", (local_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def save_history(self, local_id: int, account_id: str, remote_chat: str, messages):
        async with self.storage.connect() as db:
            async with db.execute(
                "SELECT 1 FROM chats WHERE id=? AND account_id=? AND remote_id=?",
                (local_id, account_id, remote_chat),
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise ValueError("Invalid chat mapping")
            for message in messages:
                if message.account_id != account_id or message.chat_id != remote_chat:
                    raise ValueError("Message belongs to a different chat/account")
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
                        local_id,
                        message.id,
                        message.author_id,
                        message.text,
                        message.kind,
                        direction,
                    ),
                )
            await db.commit()

    async def history(self, local_id: int, page: int = 0):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM messages WHERE chat_id=? ORDER BY "
                "length(remote_id) DESC,remote_id DESC LIMIT 5 OFFSET ?",
                (local_id, min(max(page, 0), 10000) * 5),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def add_prompt(self, bot_id, telegram_chat, prompt_id, local_id):
        async with self.storage.connect() as db:
            await db.execute("DELETE FROM reply_contexts WHERE expires_at<?", (time.time(),))
            await db.execute(
                "INSERT INTO reply_contexts VALUES (?,?,?,?,?)",
                (bot_id, telegram_chat, prompt_id, local_id, time.time() + 900),
            )
            await db.commit()

    async def prompt_chat(self, bot_id, telegram_chat, prompt_id):
        async with self.storage.connect() as db:
            async with db.execute(
                "SELECT chat_id FROM reply_contexts WHERE bot_id=? AND "
                "telegram_chat_id=? AND prompt_id=? AND expires_at>?",
                (bot_id, telegram_chat, prompt_id, time.time()),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def cancel_prompts(self, bot_id, telegram_chat):
        async with self.storage.connect() as db:
            await db.execute(
                "DELETE FROM reply_contexts WHERE bot_id=? AND telegram_chat_id=?",
                (bot_id, telegram_chat),
            )
            await db.commit()

    async def enqueue(
        self,
        chat,
        bot_id,
        telegram_chat,
        telegram_message,
        text,
        update_id=None,
        source="private_bot",
        thread_id=None,
        binding_key=None,
        owner_id=None,
    ):
        if source not in ("private_bot", "topic"):
            raise ValueError("Unsupported message source")
        async with self.storage.connect() as db:
            await db.execute(
                "INSERT INTO outgoing_messages(operation_id,account_id,chat_id,source,"
                "bot_id,telegram_chat_id,telegram_message_id,source_update_id,text,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,'queued') ON CONFLICT DO NOTHING",
                (
                    uuid4().hex,
                    chat["account_id"],
                    chat["id"],
                    source,
                    bot_id,
                    telegram_chat,
                    telegram_message,
                    update_id,
                    text,
                ),
            )
            async with db.execute(
                "SELECT operation_id FROM outgoing_messages WHERE bot_id=? AND "
                "telegram_chat_id=? AND telegram_message_id=?",
                (bot_id, telegram_chat, telegram_message),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ValueError("Telegram update conflicts with a different message")
            if source == "topic":
                await db.execute(
                    "UPDATE outgoing_messages SET source_thread_id=?,source_binding_key=?,"
                    "source_owner_id=? WHERE operation_id=? AND source_thread_id IS NULL",
                    (thread_id, binding_key, owner_id, row[0]),
                )
            await db.commit()
            return row[0]

    async def recover(self):
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE outgoing_messages SET status='unknown',last_error='interrupted' "
                "WHERE status='sending'"
            )
            await db.commit()

    async def claim(self, account_id: str):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT o.*,c.remote_id FROM outgoing_messages o JOIN chats c ON c.id=o.chat_id "
                "WHERE o.account_id=? AND o.status='queued' AND NOT EXISTS "
                "(SELECT 1 FROM outgoing_messages p WHERE p.chat_id=o.chat_id "
                "AND p.sequence<o.sequence "
                "AND p.status IN ('queued','sending','unknown')) ORDER BY o.sequence LIMIT 1",
                (account_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                await db.execute(
                    "UPDATE outgoing_messages SET status='sending',attempts=attempts+1,"
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE operation_id=?",
                    (row["operation_id"],),
                )
            await db.commit()
            return dict(row) if row else None

    async def has_ready(self, account_id):
        async with self.storage.connect() as db:
            async with db.execute(
                "SELECT 1 FROM outgoing_messages o WHERE account_id=? "
                "AND status='queued' AND NOT EXISTS (SELECT 1 FROM outgoing_messages p "
                "WHERE p.chat_id=o.chat_id AND p.sequence<o.sequence "
                "AND p.status IN ('queued','sending','unknown')) LIMIT 1",
                (account_id,),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def finish(self, operation_id, status, remote_id=None, error=None):
        if status not in ("sent", "failed", "unknown"):
            raise ValueError("Invalid final status")
        async with self.storage.connect() as db:
            cursor = await db.execute(
                "UPDATE outgoing_messages SET status=?,remote_message_id=?,last_error=?,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE operation_id=? AND status='sending'",
                (status, remote_id, error, operation_id),
            )
            if cursor.rowcount:
                await queue_delivery(db, operation_id, status)
            await db.commit()

    async def operations(self, account_id: str, page: int = 0):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outgoing_messages WHERE account_id=? "
                "ORDER BY sequence DESC LIMIT 10 OFFSET ?",
                (account_id, min(max(page, 0), 10000) * 10),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def confirm(self, operation, message):
        async with self.storage.connect() as db:
            async with db.execute(
                "SELECT author_id,text FROM messages WHERE chat_id=? AND remote_id=?",
                (operation["chat_id"], message.id),
            ) as cursor:
                existing = await cursor.fetchone()
                if existing and existing != (message.author_id, message.text):
                    raise ValueError("Remote ID conflicts with stored history")
            await db.execute(
                "INSERT INTO messages(chat_id,remote_id,author_id,text,kind,direction) "
                "VALUES (?,?,?,?,?,'outgoing') ON CONFLICT(chat_id,remote_id) DO NOTHING",
                (operation["chat_id"], message.id, message.author_id, message.text, message.kind),
            )
            cursor = await db.execute(
                "UPDATE outgoing_messages SET status='sent',remote_message_id=?,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE operation_id=? AND status='sending'",
                (message.id, operation["operation_id"]),
            )
            if cursor.rowcount:
                await queue_delivery(db, operation["operation_id"], "sent")
                await queue_topic_message(db, operation["chat_id"], message, "outgoing", operation)
            await db.commit()

    async def close_operation(self, operation_id, account_id):
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE outgoing_messages SET status='failed',last_error='closed_by_owner',"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE operation_id=? AND account_id=? AND status IN ('queued','unknown')",
                (operation_id, account_id),
            )
            await db.commit()
