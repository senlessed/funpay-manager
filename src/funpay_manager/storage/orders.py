import time

import aiosqlite

from funpay_manager.notifications.repository import read_setting

STATUS_LABELS = {
    "paid": "Открытый заказ",
    "closed": "Закрытый заказ",
    "refunded": "Возврат",
    "unknown": "Статус неизвестен",
}


class OrdersRepository:
    def __init__(self, storage):
        self.storage = storage

    async def save(self, account_id, orders, more=False):
        async with self.storage.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            account = await read_setting(db, "funpay_account")
            if not account or account["id"] != account_id:
                raise ValueError("Account changed")
            now = time.time()
            await db.execute(
                "UPDATE orders_cache SET status='unknown' WHERE account_id=?", (account_id,)
            )
            for position, order in enumerate(orders):
                await db.execute(
                    "INSERT INTO orders_cache VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account_id,order_id) DO UPDATE SET buyer_id=excluded.buyer_id,"
                    "status=excluded.status,description=excluded.description,"
                    "amount=excluded.amount,currency=excluded.currency,position=excluded.position,observed_at=excluded.observed_at",
                    (
                        account_id,
                        order.id,
                        order.buyer_id,
                        order.status,
                        order.description,
                        order.amount,
                        order.currency,
                        position,
                        now,
                    ),
                )
            await db.execute(
                "INSERT INTO sync_state(component,cursor) VALUES (?,?) "
                "ON CONFLICT(component) DO UPDATE SET cursor=excluded.cursor",
                ("orders-last:" + account_id, str(now)),
            )
            await db.execute(
                "INSERT INTO sync_state(component,cursor) VALUES (?,?) "
                "ON CONFLICT(component) DO UPDATE SET cursor=excluded.cursor",
                ("orders-coverage:" + account_id, "partial" if more else "complete"),
            )
            await db.commit()

    async def set_peer(self, local_id, account_id, peer):
        if (
            not isinstance(peer, str)
            or not peer.isascii()
            or not peer.isdecimal()
            or len(peer) > 32
        ):
            return False
        async with self.storage.connect() as db:
            cursor = await db.execute(
                "UPDATE chats SET buyer_id=? WHERE id=? AND account_id=? "
                "AND (buyer_id IS NULL OR buyer_id<>?)",
                (peer, local_id, account_id, peer),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def statuses(self, account_id):
        async with self.storage.connect() as db:
            if not await read_setting(db, "orders_enabled", True):
                return {}
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id,buyer_id FROM chats WHERE account_id=?", (account_id,)
            ) as c:
                chats = await c.fetchall()
            async with db.execute(
                "SELECT * FROM orders_cache WHERE account_id=? ORDER BY position", (account_id,)
            ) as c:
                orders = await c.fetchall()
            async with db.execute(
                "SELECT cursor FROM sync_state WHERE component=?",
                ("orders-coverage:" + account_id,),
            ) as c:
                coverage = await c.fetchone()
            partial = coverage is None or coverage[0] != "complete"
            by_buyer = {}
            for order in orders:
                by_buyer.setdefault(order["buyer_id"], []).append(order)
            result = {}
            for chat in chats:
                rows = by_buyer.get(chat["buyer_id"], [])
                if any(
                    not 0 <= time.time() - r["observed_at"] <= 180 or r["status"] == "unknown"
                    for r in rows
                ):
                    status = "unknown"
                else:
                    status = rows[0]["status"] if rows and not partial else "unknown"
                if any(
                    r["status"] == "paid" and 0 <= time.time() - r["observed_at"] <= 180
                    for r in rows
                ):
                    status = "paid"
                result[chat["id"]] = status
            return result

    async def title(self, chat):
        statuses = await self.statuses(chat["account_id"])
        label = STATUS_LABELS[statuses.get(chat["id"], "unknown")]
        return f"{label} · {chat['username'][:65]} · {chat['remote_id']}"[:128]
