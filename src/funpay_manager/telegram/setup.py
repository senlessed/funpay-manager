import asyncio
import hashlib
import threading

from aiogram import Bot

from funpay_manager.security.credentials import CredentialStore
from funpay_manager.security.pairing import PairingChallenge
from funpay_manager.storage.database import LocalStorage


class SetupError(RuntimeError):
    """Never include credentials or remote exception bodies."""


class TelegramSetup:
    def __init__(self, storage: LocalStorage, bot_factory=Bot, credential_factory=CredentialStore):
        self.storage = storage
        self.bot_factory = bot_factory
        self.credential_factory = credential_factory
        self.cancelled = threading.Event()
        self.token: str | None = None
        self.challenge: PairingChallenge | None = None
        self.identity: dict | None = None

    def cancel(self) -> None:
        self.cancelled.set()
        self.token = None

    async def validate(self, token: str) -> dict:
        if self.cancelled.is_set() or await self.storage.get_setting("telegram_binding"):
            raise SetupError("Setup unavailable")
        bot = self.bot_factory(token=token)
        try:
            me = await bot.get_me(request_timeout=15)
            webhook = await bot.get_webhook_info(request_timeout=15)
            if webhook.url:
                # Do not destroy another service's webhook automatically.
                raise SetupError("Webhook enabled")
            if self.cancelled.is_set():
                raise SetupError("Cancelled")
            self.token = token
            self.identity = {"bot_id": me.id, "username": me.username}
            self.challenge = PairingChallenge()
            return {**self.identity, "code": self.challenge.code}
        finally:
            await bot.session.close()

    async def bind(self) -> dict:
        token, challenge = self.token, self.challenge
        if not token or not challenge or not self.identity:
            raise SetupError("Validate first")
        bot = self.bot_factory(token=token)
        offset = None
        try:
            while challenge.active and not self.cancelled.is_set():
                updates = await bot.get_updates(
                    offset=offset, timeout=3, allowed_updates=["message"], request_timeout=10
                )
                for update in updates:
                    offset = update.update_id + 1
                    message = update.message
                    if not message or not message.text or not message.from_user:
                        continue
                    user = message.from_user
                    if self.cancelled.is_set():
                        raise SetupError("Cancelled")
                    if challenge.accept(
                        message.text,
                        user.id,
                        message.chat.id,
                        message.chat.type == "private",
                        user.is_bot,
                    ):
                        binding = {**self.identity, "owner_id": user.id}
                        profile = hashlib.sha256(
                            str(self.storage.path.absolute()).casefold().encode()
                        ).hexdigest()[:24]
                        vault = self.credential_factory(profile=profile)
                        # Persist owner only after secure storage succeeds. No bot replies
                        # or customer actions occur during pairing.
                        await self.storage.claim_telegram_binding(
                            binding,
                            lambda vault=vault: asyncio.to_thread(
                                vault.set, "telegram_token", token
                            ),
                        )
                        return binding
                if not updates:
                    await asyncio.sleep(0.1)
            raise SetupError("Pairing expired or cancelled")
        finally:
            self.token = None
            self.challenge = None
            await bot.session.close()
