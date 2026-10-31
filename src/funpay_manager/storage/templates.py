import time
from uuid import uuid4

import aiosqlite

from funpay_manager.notifications.repository import read_setting
from funpay_manager.utils.templates import TemplateError, render_template, validate_template


class TemplateRepository:
    def __init__(self, storage):
        self.storage = storage

    async def account(self, db, expected=None):
        account = await read_setting(db, "funpay_account")
        if not account or expected is not None and expected != account["id"]:
            raise TemplateError("Аккаунт изменился. Откройте шаблоны заново.")
        return account

    async def list(self, account_id):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await self.account(db, account_id)
            async with db.execute(
                "SELECT * FROM reply_templates WHERE account_id=? ORDER BY updated_at DESC,id",
                (account_id,),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def save(self, account_id, title, body, template_id=None, revision=None):
        title, body = validate_template(title, body)
        async with self.storage.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.account(db, account_id)
            if template_id is None:
                async with db.execute(
                    "SELECT count(*) FROM reply_templates WHERE account_id=?", (account_id,)
                ) as c:
                    if (await c.fetchone())[0] >= 50:
                        raise TemplateError("Максимум 50 шаблонов на аккаунт.")
                template_id = uuid4().hex[:16]
                await db.execute(
                    "INSERT INTO reply_templates VALUES (?,?,?,?,1,?)",
                    (template_id, account_id, title, body, time.time()),
                )
            else:
                cursor = await db.execute(
                    "UPDATE reply_templates SET title=?,body=?,revision=revision+1,updated_at=? "
                    "WHERE id=? AND account_id=? AND revision=?",
                    (title, body, time.time(), template_id, account_id, revision),
                )
                if not cursor.rowcount:
                    raise TemplateError("Шаблон уже изменён или удалён. Перезагрузите список.")
                await db.execute(
                    "UPDATE template_previews SET status='cancelled' "
                    "WHERE template_id=? AND status IN ('draft','ready')",
                    (template_id,),
                )
            await db.commit()
            return template_id

    async def delete(self, account_id, template_id, revision):
        async with self.storage.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self.account(db, account_id)
            cursor = await db.execute(
                "DELETE FROM reply_templates WHERE id=? AND account_id=? AND revision=?",
                (template_id, account_id, revision),
            )
            if not cursor.rowcount:
                raise TemplateError("Шаблон уже изменён или удалён. Перезагрузите список.")
            await db.execute(
                "UPDATE template_previews SET status='cancelled' "
                "WHERE template_id=? AND status IN ('draft','ready')",
                (template_id,),
            )
            await db.commit()

    async def checked_chat(self, db, local_id, route):
        account = await self.account(db)
        async with db.execute(
            "SELECT * FROM chats WHERE id=? AND account_id=? AND route_key=?",
            (local_id, account["id"], route),
        ) as c:
            chat = await c.fetchone()
        if not chat:
            raise TemplateError("Чат недоступен или кнопка устарела. Откройте историю заново.")
        return account, dict(chat)

    async def preview(self, template_id, local_id, route, bot_id, owner_id):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            account, chat = await self.checked_chat(db, local_id, route)
            binding = await read_setting(db, "telegram_binding")
            if not binding or (binding["bot_id"], binding["owner_id"]) != (bot_id, owner_id):
                raise TemplateError("Привязка Telegram изменилась.")
            async with db.execute(
                "SELECT * FROM reply_templates WHERE id=? AND account_id=?",
                (template_id, account["id"]),
            ) as c:
                template = await c.fetchone()
            if not template:
                raise TemplateError("Шаблон удалён или недоступен.")
            text = render_template(
                template["body"],
                {
                    "buyer": chat["username"],
                    "chat_id": chat["remote_id"],
                    "seller": account.get("username"),
                },
            )
            now = time.time()
            await db.execute(
                "DELETE FROM template_previews WHERE status IN ('draft','ready','cancelled') "
                "AND expires_at<?",
                (now,),
            )
            key = uuid4().hex[:24]
            await db.execute(
                "INSERT INTO template_previews VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,'draft',NULL)",
                (
                    key,
                    template_id,
                    template["revision"],
                    account["id"],
                    local_id,
                    route,
                    bot_id,
                    owner_id,
                    text,
                    now,
                    now + 900,
                ),
            )
            await db.commit()
            return key, chat, text

    async def attach(self, key, message_id):
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE template_previews SET telegram_message_id=?,status='ready' "
                "WHERE preview_key=? AND status='draft'",
                (message_id, key),
            )
            await db.commit()

    async def confirm(self, key, bot_id, owner_id, message_id, cancel=False):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM template_previews WHERE preview_key=?", (key,)
            ) as c:
                preview = await c.fetchone()
            if not preview:
                raise TemplateError("Предпросмотр устарел. Выберите шаблон заново.")
            await self.account(db, preview["account_id"])
            binding = await read_setting(db, "telegram_binding")
            if (
                (preview["bot_id"], preview["owner_id"], preview["telegram_message_id"])
                != (bot_id, owner_id, message_id)
                or not binding
                or (binding["bot_id"], binding["owner_id"]) != (bot_id, owner_id)
            ):
                raise TemplateError("Предпросмотр относится к другой привязке или сообщению.")
            if preview["status"] == "queued":
                return preview["operation_id"], preview["text"]  # Idempotent after restart.
            if (
                preview["status"] != "ready"
                or not preview["created_at"] <= time.time() < preview["expires_at"]
            ):
                raise TemplateError("Предпросмотр устарел или отменён. Выберите шаблон заново.")
            if cancel:
                await db.execute(
                    "UPDATE template_previews SET status='cancelled' WHERE preview_key=?", (key,)
                )
                await db.commit()
                return None, preview["text"]
            if not await read_setting(db, "sending_enabled", False):
                raise TemplateError("Отправка выключена в Windows. Текст не поставлен в очередь.")
            await self.checked_chat(db, preview["chat_id"], preview["route_key"])
            async with db.execute(
                "SELECT revision FROM reply_templates WHERE id=? AND account_id=?",
                (preview["template_id"], preview["account_id"]),
            ) as c:
                template = await c.fetchone()
            if not template or template[0] != preview["revision"]:
                raise TemplateError("Шаблон изменён. Откройте новый предпросмотр.")
            operation = uuid4().hex
            await db.execute(
                "INSERT INTO outgoing_messages(operation_id,account_id,chat_id,source,bot_id,"
                "telegram_chat_id,telegram_message_id,text,status) "
                "VALUES (?,?,?,'private_bot',?,?,?,?,'queued')",
                (
                    operation,
                    preview["account_id"],
                    preview["chat_id"],
                    bot_id,
                    owner_id,
                    message_id,
                    preview["text"],
                ),
            )
            await db.execute(
                "UPDATE template_previews SET status='queued',operation_id=? WHERE preview_key=?",
                (operation, key),
            )
            await db.commit()
            return operation, preview["text"]
