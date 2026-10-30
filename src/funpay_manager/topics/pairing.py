import secrets
import threading

from funpay_manager.security.pairing import PairingChallenge

from .permissions import validate_group


class GroupPairing:
    def __init__(self, storage, wake):
        self.storage, self.wake = storage, wake
        self.challenge = None
        self.snapshot = None
        self.cancelled = threading.Event()
        self.error = None

    async def begin(self):
        binding = await self.storage.get_setting("telegram_binding")
        account = await self.storage.get_setting("funpay_account")
        if not binding or not account or await self.storage.get_setting("telegram_group"):
            raise ValueError(
                "Сначала подключите аккаунты; повторная привязка требует отключения группы"
            )
        self.cancelled.clear()
        self.error = None
        self.challenge = PairingChallenge()
        self.snapshot = (binding, account)
        return self.challenge.code

    def cancel(self):
        self.cancelled.set()

    async def handle(self, bot, message):
        parts = (message.text or "").split()
        if not parts or parts[0].split("@")[0] != "/bindgroup":
            return False
        challenge, snapshot = self.challenge, self.snapshot
        if (
            snapshot
            and "@" in parts[0]
            and parts[0].split("@", 1)[1].casefold() != snapshot[0]["username"].casefold()
        ):
            return True
        if (
            not challenge
            or not snapshot
            or self.cancelled.is_set()
            or not challenge.active
            or not message.from_user
            or message.from_user.is_bot
            or message.from_user.id != snapshot[0]["owner_id"]
            or message.chat.type != "supergroup"
            or getattr(message, "sender_chat", None)
            or len(parts) != 2
        ):
            return True
        if not secrets.compare_digest(parts[1].encode(), challenge.code.encode()):
            challenge.consume(parts[1])
            return True
        try:
            group = await validate_group(bot, message.chat.id, snapshot[0])
        except Exception:
            self.error = (
                "Проверьте закрытость группы, темы и права бота/владельца. "
                "Код ещё можно использовать до истечения срока."
            )
            raise
        if (
            self.cancelled.is_set()
            or self.challenge is not challenge
            or snapshot
            != (
                await self.storage.get_setting("telegram_binding"),
                await self.storage.get_setting("funpay_account"),
            )
        ):
            return True
        if not challenge.consume(parts[1]):
            return True
        import json

        config = {
            "group_id": group.id,
            "title": group.title,
            "bot_id": snapshot[0]["bot_id"],
            "owner_id": snapshot[0]["owner_id"],
            "account_id": snapshot[1]["id"],
            "binding_key": secrets.token_hex(16),
        }
        async with self.storage.connect() as db:
            await db.execute(
                "INSERT INTO settings(key,value) VALUES ('telegram_group',?)", (json.dumps(config),)
            )
            await db.execute(
                "INSERT INTO settings(key,value) VALUES ('topics_enabled','true') "
                "ON CONFLICT(key) DO UPDATE SET value='true'"
            )
            await db.commit()
        self.wake.set()
        return True
