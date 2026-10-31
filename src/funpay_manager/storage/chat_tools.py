import json

import aiosqlite

from funpay_manager.notifications.repository import read_setting


class ChatToolsRepository:
    def __init__(self, storage):
        self.storage = storage

    async def search(self, query, page=0, expected_account=None):
        query = query.strip()
        if not 1 <= len(query) <= 100 or not 0 <= page <= 10000:
            raise ValueError("Invalid search")
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            account = await read_setting(db, "funpay_account")
            if not account or expected_account is not None and account["id"] != expected_account:
                raise ValueError("Account changed")
            await db.create_function("unicode_fold", 1, str.casefold, deterministic=True)
            async with db.execute(
                "SELECT id,remote_id,username,route_key FROM chats WHERE account_id=? "
                "AND (instr(unicode_fold(username),?)>0 OR instr(remote_id,?)>0) "
                "ORDER BY id DESC LIMIT 6 OFFSET ?",
                (account["id"], query.casefold(), query, page * 5),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
            return account["id"], rows[:5], len(rows) > 5

    async def checked(self, db, local_id, route):
        account = await read_setting(db, "funpay_account")
        async with db.execute("SELECT * FROM chats WHERE id=?", (local_id,)) as cursor:
            row = await cursor.fetchone()
        if (
            not row
            or not account
            or row["account_id"] != account["id"]
            or row["route_key"] != route
        ):
            raise ValueError("Stale chat")
        return dict(row)

    @staticmethod
    def note_key(chat):
        return "chat-note:" + json.dumps(
            [chat["account_id"], chat["remote_id"]], separators=(",", ":")
        )

    async def note(self, local_id, route):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            chat = await self.checked(db, local_id, route)
            return chat, await read_setting(db, self.note_key(chat), "")

    async def save_note(self, local_id, route, text):
        text = text.strip()
        if len(text) > 2000:
            raise ValueError("Note too long")
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            chat = await self.checked(db, local_id, route)
            if text:
                await db.execute(
                    "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) "
                    "DO UPDATE SET value=excluded.value,"
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                    (self.note_key(chat), json.dumps(text)),
                )
            else:
                await db.execute("DELETE FROM settings WHERE key=?", (self.note_key(chat),))
            await db.commit()
