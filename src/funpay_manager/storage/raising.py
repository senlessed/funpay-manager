import json
import time

import aiosqlite

from funpay_manager.notifications.repository import read_setting


class RaiseRepository:
    def __init__(self, storage):
        self.storage = storage

    async def recover(self):
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE lot_schedules SET status='unknown',last_error='interrupted' "
                "WHERE status='sending'"
            )
            await db.execute(
                "UPDATE raise_attempts SET status='unknown',finished_at=? WHERE status='sending'",
                (time.time(),),
            )
            await db.commit()

    async def catalog(self, account_id, categories):
        async with self.storage.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            account = await read_setting(db, "funpay_account")
            if not account or account["id"] != account_id:
                raise ValueError("Account changed")
            await db.execute(
                "UPDATE lot_schedules SET available=0 WHERE account_id=?", (account_id,)
            )
            for cat in categories:
                await db.execute(
                    "INSERT INTO lot_schedules(account_id,game_id,name,nodes) VALUES (?,?,?,?) "
                    "ON CONFLICT(account_id,game_id) DO UPDATE SET name=excluded.name,available=1,"
                    "nodes=CASE WHEN lot_schedules.enabled=0 THEN excluded.nodes "
                    "ELSE lot_schedules.nodes END",
                    (account_id, cat.game_id, cat.name, json.dumps(cat.nodes)),
                )
            await db.execute(
                "UPDATE lot_schedules SET enabled=0 WHERE account_id=? AND available=0",
                (account_id,),
            )
            await db.commit()

    async def rows(self, account_id):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM lot_schedules WHERE account_id=? AND available=1 ORDER BY name",
                (account_id,),
            ) as c:
                return [dict(r) for r in await c.fetchall()]

    async def configure(self, account_id, selected, interval, enabled):
        if not 900 <= interval <= 86400:
            raise ValueError("Interval must be 15 minutes to 24 hours")
        async with self.storage.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            account = await read_setting(db, "funpay_account")
            if not account or account["id"] != account_id:
                raise ValueError("Account changed")
            async with db.execute(
                "SELECT game_id FROM lot_schedules WHERE account_id=? AND available=1",
                (account_id,),
            ) as c:
                valid = {r[0] for r in await c.fetchall()}
            if not set(selected) <= valid or enabled and not selected:
                raise ValueError("Select available categories")
            await db.execute("UPDATE lot_schedules SET enabled=0 WHERE account_id=?", (account_id,))
            for game in selected:
                await db.execute(
                    "UPDATE lot_schedules SET enabled=1,interval_seconds=?,"
                    "next_attempt=max(next_attempt,?) "
                    "WHERE account_id=? AND game_id=?",
                    (interval, time.time() + 30, account_id, game),
                )
            await db.execute(
                "INSERT INTO settings(key,value) VALUES ('auto_raise_enabled',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(bool(enabled)),),
            )
            await db.commit()

    async def claim(self, account_id):
        async with self.storage.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            account = await read_setting(db, "funpay_account")
            if (
                not account
                or account["id"] != account_id
                or not await read_setting(db, "auto_raise_enabled", False)
            ):
                return None
            async with db.execute(
                "SELECT * FROM lot_schedules WHERE account_id=? AND enabled=1 AND available=1 "
                "AND status IN ('idle','raised','cooldown','paused') AND next_attempt<=? "
                "ORDER BY next_attempt,game_id LIMIT 1",
                (account_id, time.time()),
            ) as c:
                row = await c.fetchone()
            if row is None:
                return None
            result = dict(row)
            await db.execute(
                "UPDATE lot_schedules SET status='sending',next_attempt=? "
                "WHERE account_id=? AND game_id=?",
                (time.time() + row["interval_seconds"], account_id, row["game_id"]),
            )
            cursor = await db.execute(
                "INSERT INTO raise_attempts(account_id,game_id,started_at,status) "
                "VALUES (?,?,?,'sending')",
                (account_id, row["game_id"], time.time()),
            )
            result["attempt_id"] = cursor.lastrowid
            await db.commit()
            return result

    async def authorized(self, job):
        account = await self.storage.get_setting("funpay_account")
        if (
            not account
            or account["id"] != job["account_id"]
            or not await self.storage.get_setting("auto_raise_enabled", False)
        ):
            return False
        rows = await self.rows(job["account_id"])
        return any(
            r["game_id"] == job["game_id"] and r["enabled"] and r["nodes"] == job["nodes"]
            for r in rows
        )

    async def finish(self, job, status, wait=None):
        next_time = time.time() + (wait if wait is not None else job["interval_seconds"])
        async with self.storage.connect() as db:
            await db.execute(
                "UPDATE lot_schedules SET status=?,last_error=?,next_attempt=?,"
                "last_success=CASE WHEN ?='raised' THEN ? ELSE last_success END "
                "WHERE account_id=? AND game_id=? AND status='sending'",
                (
                    status,
                    None if status in ("raised", "cooldown") else status,
                    next_time,
                    status,
                    time.time(),
                    job["account_id"],
                    job["game_id"],
                ),
            )
            await db.execute(
                "UPDATE raise_attempts SET status=?,finished_at=? WHERE id=?",
                (status, time.time(), job["attempt_id"]),
            )
            await db.commit()

    async def reset_unknown(self, account_id):
        async with self.storage.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            account = await read_setting(db, "funpay_account")
            if not account or account["id"] != account_id:
                raise ValueError("Account changed")
            await db.execute(
                "UPDATE lot_schedules SET status='idle',last_error=NULL,"
                "next_attempt=max(next_attempt,?) "
                "WHERE account_id=? AND status IN ('unknown','rejected','auth')",
                (time.time() + 60, account_id),
            )
            await db.commit()
