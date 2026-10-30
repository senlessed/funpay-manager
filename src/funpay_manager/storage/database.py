import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .message_schema import MESSAGE_COLUMNS, MIGRATION_2

SCHEMA_VERSION = 2
# Domain tables are added by their feature migrations, rather than empty placeholders.
MIGRATION_1 = """
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY,
    event TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX audit_logs_created ON audit_logs(created_at);
CREATE TABLE sync_state (
    component TEXT PRIMARY KEY,
    cursor TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class LocalStorage:
    """Short-lived connections avoid sharing transactions between asyncio tasks."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def connect(self):
        async with self._lock, aiosqlite.connect(self.path, timeout=5) as connection:
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.execute("PRAGMA busy_timeout=5000")
            yield connection

    async def migrate(self) -> None:
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        async with self.connect() as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("BEGIN IMMEDIATE")
            try:
                async with connection.execute("PRAGMA user_version") as cursor:
                    version = (await cursor.fetchone())[0]
                if version > SCHEMA_VERSION:
                    raise RuntimeError("База создана более новой версией приложения")
                if version == 0:
                    for statement in MIGRATION_1.split(";"):
                        if statement.strip():
                            await connection.execute(statement)
                    await connection.execute("PRAGMA user_version=1")
                if version < 2:
                    for statement in MIGRATION_2.split(";"):
                        if statement.strip():
                            await connection.execute(statement)
                    await connection.execute("PRAGMA user_version=2")
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def get_setting(self, key: str, default=None):
        async with self.connect() as connection:
            async with connection.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
        return json.loads(row[0]) if row else default

    async def set_setting(self, key: str, value) -> None:
        if any(word in key.lower() for word in ("token", "secret", "golden", "password")):
            raise ValueError("Секреты должны храниться в Windows Credential Manager")
        async with self.connect() as connection:
            await connection.execute(
                "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            await connection.commit()

    async def backup(self, destination: Path) -> None:
        destination_path = await asyncio.to_thread(destination.resolve)
        source_path = await asyncio.to_thread(self.path.resolve)
        if destination_path == source_path:
            raise ValueError("Резервная копия должна иметь другой путь")
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        async with self.connect() as source, aiosqlite.connect(destination) as target:
            await source.backup(target)

    async def claim_telegram_binding(self, binding: dict, before_commit=None) -> None:
        """INSERT-only: a second setup cannot replace an existing owner."""
        async with self.connect() as connection:
            await connection.execute(
                "INSERT INTO settings(key,value) VALUES ('telegram_binding',?)",
                (json.dumps(binding),),
            )
            if before_commit:
                await before_commit()
            await connection.commit()

    async def restore(self, source: Path, safety: Path) -> None:
        """Validate first, preserve current DB, restore under the storage-wide lock.

        All consumers must use this LocalStorage instance. Future network workers
        must be stopped before invoking this maintenance operation.
        """
        paths = await asyncio.gather(
            *(asyncio.to_thread(p.resolve) for p in (self.path, source, safety))
        )
        if len(set(paths)) != 3:
            raise ValueError("Источник, рабочая база и safety backup должны различаться")
        if await asyncio.to_thread(safety.exists):
            raise ValueError("Safety backup уже существует")
        await asyncio.to_thread(safety.parent.mkdir, parents=True, exist_ok=True)
        # Read-only URI prevents silently creating a missing source.
        async with aiosqlite.connect(paths[1].as_uri() + "?mode=ro", uri=True) as backup:
            await backup.execute("BEGIN")
            async with backup.execute("PRAGMA integrity_check") as cursor:
                if await cursor.fetchall() != [("ok",)]:
                    raise ValueError("Резервная копия повреждена")
            async with backup.execute("PRAGMA user_version") as cursor:
                source_version = (await cursor.fetchone())[0]
                if source_version not in (1, SCHEMA_VERSION):
                    raise ValueError("Неподдерживаемая версия резервной копии")
            tables = {
                "settings": {"key", "value", "updated_at"},
                "audit_logs": {"id", "event", "created_at"},
                "sync_state": {"component", "cursor", "updated_at"},
            }
            if source_version == 2:
                tables.update(MESSAGE_COLUMNS)
            for table, columns in tables.items():
                async with backup.execute(f"PRAGMA table_info({table})") as cursor:
                    if not columns <= {row[1] for row in await cursor.fetchall()}:
                        raise ValueError("Неподходящая структура резервной копии")
            async with backup.execute("PRAGMA foreign_key_check") as cursor:
                if await cursor.fetchone() is not None:
                    raise ValueError("Нарушены связи в резервной копии")
            temporary = await asyncio.to_thread(tempfile.TemporaryDirectory, prefix="fptm-restore-")
            try:
                async with aiosqlite.connect(Path(temporary.name) / "staged.db") as staged:
                    await backup.backup(staged)
                    # Disable writes in the staging copy BEFORE it can replace the live DB.
                    await staged.execute("DELETE FROM settings WHERE key='sending_enabled'")
                    if source_version == 2:
                        await staged.execute(
                            "UPDATE outgoing_messages SET status='unknown', "
                            "last_error='restored_snapshot' WHERE status IN ('queued','sending')"
                        )
                        await staged.execute("DELETE FROM reply_contexts")
                    await staged.commit()
                    async with self.connect() as target, aiosqlite.connect(safety) as emergency:
                        await target.backup(emergency)
                        await staged.backup(target)
            finally:
                await asyncio.to_thread(temporary.cleanup)
