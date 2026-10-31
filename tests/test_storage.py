import asyncio
import sqlite3

import pytest

from funpay_manager.storage.database import LocalStorage


async def test_migration_restart_and_settings(tmp_path):
    path = tmp_path / "data.db"
    storage = LocalStorage(path)
    await storage.migrate()
    await storage.set_setting("dark_theme", True)
    restarted = LocalStorage(path)
    await restarted.migrate()
    assert await restarted.get_setting("dark_theme") is True
    assert await restarted.get_setting("absent", "default") == "default"
    async with restarted.connect() as connection:
        for pragma, expected in (
            ("user_version", 6),
            ("journal_mode", "wal"),
            ("foreign_keys", 1),
            ("busy_timeout", 5000),
        ):
            async with connection.execute(f"PRAGMA {pragma}") as cursor:
                assert (await cursor.fetchone())[0] == expected


async def test_concurrent_settings_and_consistent_backup(tmp_path):
    storage = LocalStorage(tmp_path / "source.db")
    await storage.migrate()
    await asyncio.gather(*(storage.set_setting(f"key-{i}", i) for i in range(20)))
    destination = tmp_path / "backup.db"
    await storage.backup(destination)
    backup = LocalStorage(destination)
    assert await backup.get_setting("key-19") == 19
    async with backup.connect() as connection:
        async with connection.execute("PRAGMA integrity_check") as cursor:
            assert (await cursor.fetchone())[0] == "ok"
    with pytest.raises(ValueError):
        await storage.backup(storage.path)


async def test_future_schema_is_not_modified(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError):
        await LocalStorage(path).migrate()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


async def test_migration_rolls_back_on_failure(tmp_path, monkeypatch):
    from funpay_manager.storage import database

    monkeypatch.setattr(database, "MIGRATION_1", "CREATE TABLE partial(id); INVALID SQL;")
    path = tmp_path / "failed.db"
    with pytest.raises(sqlite3.OperationalError):
        await LocalStorage(path).migrate()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='partial'"
        ).fetchall()


@pytest.mark.parametrize("key", ["telegram_token", "golden_key", "password", "secret"])
async def test_secrets_rejected(tmp_path, key):
    storage = LocalStorage(tmp_path / "data.db")
    await storage.migrate()
    with pytest.raises(ValueError):
        await storage.set_setting(key, "test-only")


async def test_restore_preserves_safety_and_restores_settings(tmp_path):
    storage = LocalStorage(tmp_path / "current.db")
    await storage.migrate()
    await storage.set_setting("dark_theme", True)
    await storage.backup(tmp_path / "old.db")
    await storage.set_setting("dark_theme", False)
    await storage.restore(tmp_path / "old.db", tmp_path / "safety.db")
    assert await storage.get_setting("dark_theme") is True
    assert await LocalStorage(tmp_path / "safety.db").get_setting("dark_theme") is False
    await storage.migrate()


@pytest.mark.parametrize("kind", ["corrupt", "missing", "future", "foreign"])
async def test_restore_rejects_invalid_without_changing_current(tmp_path, kind):
    storage = LocalStorage(tmp_path / "current.db")
    await storage.migrate()
    await storage.set_setting("keep", 42)
    source = tmp_path / "source.db"
    if kind == "corrupt":
        source.write_bytes(b"not sqlite")
    elif kind in ("future", "foreign"):
        with sqlite3.connect(source) as connection:
            connection.execute(f"PRAGMA user_version={99 if kind == 'future' else 1}")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        await storage.restore(source, tmp_path / "safety.db")
    assert await storage.get_setting("keep") == 42
    assert not (tmp_path / "safety.db").exists()
