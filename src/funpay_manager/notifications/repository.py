import json
import time

import aiosqlite

from funpay_manager.utils.text import telegram_text


async def read_setting(db, key, default=None):
    async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cursor:
        row = await cursor.fetchone()
    return json.loads(row[0]) if row else default


async def queue_event(db, account_id, event_key, text, callback_data=None):
    """Called in the same transaction as the message or delivery state change."""
    if not await read_setting(db, "notifications_enabled", True):
        return False
    binding = await read_setting(db, "telegram_binding")
    expected = await read_setting(db, "funpay_account")
    if not binding or not expected or expected["id"] != account_id:
        return False
    safe_text = telegram_text(text)
    cursor = await db.execute(
        "INSERT INTO notifications(event_key,account_id,bot_id,owner_id,text,"
        "callback_data,status) VALUES (?,?,?,?,?,?,'queued') ON CONFLICT DO NOTHING",
        (event_key, account_id, binding["bot_id"], binding["owner_id"], safe_text, callback_data),
    )
    return cursor.rowcount == 1


async def queue_delivery(db, operation_id, status):
    async with db.execute(
        "SELECT sequence,account_id,bot_id,telegram_chat_id,text,source,source_owner_id "
        "FROM outgoing_messages WHERE operation_id=?",
        (operation_id,),
    ) as cursor:
        row = await cursor.fetchone()
    binding = await read_setting(db, "telegram_binding")
    if (
        not row
        or not binding
        or row[2] != binding["bot_id"]
        or (
            row[3] != binding["owner_id"]
            and not (row[5] == "topic" and row[6] == binding["owner_id"])
        )
    ):
        return
    labels = {
        "sent": "Доставка подтверждена",
        "failed": "Сообщение не отправлено",
        "unknown": "Результат отправки неизвестен. Проверьте FunPay; слепого повтора нет",
    }
    await queue_event(
        db,
        row[1],
        f"delivery:{operation_id}:{status}",
        f"Операция #{row[0]} · {labels[status]}\n\n{row[4][:1800]}",
        "outbox",
    )


async def save_cooldown(db, bot_id, owner_id, retry_at):
    key = f"tg-notification-cooldown:{bot_id}:{owner_id}"
    async with db.execute("SELECT cursor FROM sync_state WHERE component=?", (key,)) as cursor:
        row = await cursor.fetchone()
    until = max(float(row[0]) if row else 0, retry_at)
    await db.execute(
        "INSERT INTO sync_state(component,cursor) VALUES (?,?) "
        "ON CONFLICT(component) DO UPDATE SET cursor=excluded.cursor",
        (key, str(until)),
    )


class NotificationRepository:
    def __init__(self, storage):
        self.storage = storage

    async def recover(self):
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE notifications SET status='unknown',last_error='interrupted' "
                "WHERE status='sending'"
            )
            await db.commit()

    async def claim(self, account_id, binding):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            if not await read_setting(db, "notifications_enabled", True):
                await db.rollback()
                return None
            current = await read_setting(db, "telegram_binding")
            account = await read_setting(db, "funpay_account")
            if current != binding or not account or account["id"] != account_id:
                await db.rollback()
                return None
            async with db.execute(
                "SELECT * FROM notifications WHERE account_id=? AND bot_id=? "
                "AND owner_id=? AND status='queued' AND next_attempt<=? ORDER BY id LIMIT 1",
                (account_id, binding["bot_id"], binding["owner_id"], time.time()),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                await db.execute(
                    "UPDATE notifications SET status='sending',attempts=attempts+1 WHERE id=?",
                    (row["id"],),
                )
            await db.commit()
            return dict(row) if row else None

    async def finish(self, event_id, status, error=None, remote_id=None, retry_at=0):
        if status not in ("sent", "failed", "unknown", "queued"):
            raise ValueError("Invalid notification status")
        async with self.storage.connect() as db:
            cursor = await db.execute(
                "UPDATE notifications SET status=?,last_error=?,telegram_message_id=?,"
                "next_attempt=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=? AND status='sending'",
                (status, error, remote_id, retry_at, event_id),
            )
            if cursor.rowcount and status == "queued" and error == "rate_limit":
                async with db.execute(
                    "SELECT bot_id,owner_id FROM notifications WHERE id=?", (event_id,)
                ) as cursor:
                    binding = await cursor.fetchone()
                await save_cooldown(db, binding[0], binding[1], retry_at)
            await db.commit()

    async def pending(self, account_id, binding):
        async with self.storage.connect() as db:
            async with db.execute(
                "SELECT min(next_attempt) FROM notifications WHERE account_id=? "
                "AND bot_id=? AND owner_id=? AND status='queued'",
                (account_id, binding["bot_id"], binding["owner_id"]),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0]

    async def cooldown(self, binding):
        async with self.storage.connect() as db:
            async with db.execute(
                "SELECT cursor FROM sync_state WHERE component=?",
                (f"tg-notification-cooldown:{binding['bot_id']}:{binding['owner_id']}",),
            ) as cursor:
                row = await cursor.fetchone()
                return float(row[0]) if row else 0

    async def defer(self, binding, retry_at):
        async with self.storage.connect() as db:
            await save_cooldown(db, binding["bot_id"], binding["owner_id"], retry_at)
            await db.commit()

    async def disable(self):
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE notifications SET status='failed',last_error='disabled' "
                "WHERE status='queued'"
            )
            await db.commit()

    async def recent(self, account_id, binding):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM notifications WHERE account_id=? AND bot_id=? "
                "AND owner_id=? ORDER BY id DESC LIMIT 10",
                (account_id, binding["bot_id"], binding["owner_id"]),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
