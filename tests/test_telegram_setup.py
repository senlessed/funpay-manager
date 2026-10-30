import asyncio
import sqlite3
from types import SimpleNamespace as NS

import pytest

from funpay_manager.storage.database import LocalStorage
from funpay_manager.telegram.setup import SetupError, TelegramSetup


class Session:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeBot:
    def __init__(self):
        self.session = Session()
        self.webhook = ""
        self.updates = []

    async def get_me(self, **kwargs):
        return NS(id=42, username="test_bot")

    async def get_webhook_info(self, **kwargs):
        return NS(url=self.webhook)

    async def get_updates(self, **kwargs):
        return self.updates


class Vault:
    def __init__(self):
        self.saved = []

    def set(self, name, value):
        self.saved.append((name, value))


async def setup(tmp_path):
    storage = LocalStorage(tmp_path / "test.db")
    await storage.migrate()
    bots, vault = [], Vault()

    def factory(**kwargs):
        bot = FakeBot()
        bots.append(bot)
        return bot

    service = TelegramSetup(storage, factory, lambda **kw: vault)
    return service, bots, vault


def update(code, user=7, chat=7, private=True):
    return NS(
        update_id=1,
        message=NS(
            text="/start " + code,
            from_user=NS(id=user, is_bot=False),
            chat=NS(id=chat, type="private" if private else "supergroup"),
        ),
    )


async def test_binding_stores_token_only_in_vault_and_rejects_replacement(tmp_path):
    service, bots, vault = await setup(tmp_path)
    details = await service.validate("test-only-token")
    assert bots[0].session.closed
    factory = service.bot_factory

    def paired(**kwargs):
        bot = factory(**kwargs)
        bot.updates = [
            update("wrong"),
            update(details["code"], chat=-1, private=False),
            update(details["code"]),
        ]
        return bot

    service.bot_factory = paired
    binding = await service.bind()
    assert binding["owner_id"] == 7
    assert vault.saved == [("telegram_token", "test-only-token")]
    assert await service.storage.get_setting("telegram_binding") == binding
    assert service.token is None
    assert bots[-1].session.closed
    assert b"test-only-token" not in service.storage.path.read_bytes()
    with pytest.raises(SetupError):
        await service.validate("other-token")
    with pytest.raises(sqlite3.IntegrityError):
        await service.storage.claim_telegram_binding({"owner_id": 999})
    assert await service.storage.get_setting("telegram_binding") == binding


async def test_webhook_rejected_without_deletion(tmp_path):
    service, bots, vault = await setup(tmp_path)
    factory = service.bot_factory

    def with_webhook(**kwargs):
        bot = factory(**kwargs)
        bot.webhook = "https://example.invalid/webhook"
        return bot

    service.bot_factory = with_webhook
    with pytest.raises(SetupError):
        await service.validate("test-only-token")
    assert bots[0].session.closed
    assert not vault.saved


async def test_cancel_and_expiry_do_not_store(tmp_path):
    service, bots, vault = await setup(tmp_path)
    await service.validate("test-only-token")
    service.challenge.expires = 0
    with pytest.raises(SetupError):
        await service.bind()
    assert bots[-1].session.closed
    assert not vault.saved
    service.cancel()
    with pytest.raises(SetupError):
        await service.validate("test-only-token")


async def test_vault_failure_rolls_back_binding(tmp_path):
    service, bots, vault = await setup(tmp_path)
    details = await service.validate("test-only-token")
    factory = service.bot_factory

    def paired(**kwargs):
        bot = factory(**kwargs)
        bot.updates = [update(details["code"])]
        return bot

    def failed(*args):
        raise OSError("test-only vault unavailable")

    vault.set = failed
    service.bot_factory = paired
    with pytest.raises(OSError):
        await service.bind()
    assert await service.storage.get_setting("telegram_binding") is None
    assert bots[-1].session.closed


async def test_single_claim_across_storage_instances(tmp_path):
    service, _, _ = await setup(tmp_path)
    other = LocalStorage(service.storage.path)
    calls = []

    async def secure_save():
        calls.append(True)

    results = await asyncio.gather(
        service.storage.claim_telegram_binding({"owner_id": 1}, secure_save),
        other.claim_telegram_binding({"owner_id": 2}, secure_save),
        return_exceptions=True,
    )
    assert len(calls) == 1
    assert sum(isinstance(value, sqlite3.IntegrityError) for value in results) == 1


async def test_cancel_while_polling_prevents_claim(tmp_path):
    service, bots, vault = await setup(tmp_path)
    details = await service.validate("test-only-token")
    started, release = asyncio.Event(), asyncio.Event()
    factory = service.bot_factory

    def polling(**kwargs):
        bot = factory(**kwargs)

        async def get_updates(**kwargs):
            started.set()
            await release.wait()
            return [update(details["code"])]

        bot.get_updates = get_updates
        return bot

    service.bot_factory = polling
    task = asyncio.create_task(service.bind())
    await started.wait()
    service.cancel()
    release.set()
    with pytest.raises(SetupError):
        await task
    assert not vault.saved
    assert await service.storage.get_setting("telegram_binding") is None
    assert bots[-1].session.closed
