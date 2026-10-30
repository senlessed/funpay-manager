import secrets
import threading
import time

from funpay_manager.security.pairing import PairingChallenge

from .permissions import validate_group


class GroupPairing:
    def __init__(self, storage, wake):
        self.storage, self.wake = storage, wake
        self.challenge = None
        self.snapshot = None
        self.cancelled = threading.Event()
        self.error = None
        self._notice_at = float("-inf")

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

    async def notice(self, bot, owner_id, text, *, force=False):
        # Reply only to the known owner privately, never publish diagnostics or
        # pairing state into an unbound group. Do not flood on repeated updates.
        if not force and time.monotonic() - self._notice_at < 5:
            return
        self._notice_at = time.monotonic()
        try:
            await bot.send_message(owner_id, text, parse_mode=None, request_timeout=10)
        except Exception:
            # Delivery of feedback must not roll back an already committed binding.
            pass

    async def handle(self, bot, message):
        parts = (message.text or "").split()
        if not parts or parts[0].split("@")[0] != "/bindgroup":
            return False
        binding = await self.storage.get_setting("telegram_binding")
        if (
            not binding
            or not message.from_user
            or message.from_user.is_bot
            or message.from_user.id != binding["owner_id"]
            or getattr(message, "sender_chat", None)
            or getattr(message, "forward_origin", None)
            or getattr(message, "via_bot", None)
        ):
            return True
        challenge, snapshot = self.challenge, self.snapshot
        if (
            "@" in parts[0]
            and parts[0].split("@", 1)[1].casefold() != binding["username"].casefold()
        ):
            return True
        if message.chat.type != "supergroup":
            self.error = "Включите темы в закрытой группе Telegram и повторите команду там."
            await self.notice(bot, binding["owner_id"], self.error)
            return True
        if await self.storage.get_setting("telegram_group"):
            await self.notice(
                bot,
                binding["owner_id"],
                "Группа уже привязана. Для другой группы сначала отключите текущую в Windows.",
            )
            return True
        if not challenge or not snapshot or self.cancelled.is_set() or not challenge.active:
            self.error = (
                "Нет активного кода привязки. Создайте новый код в Windows и оставьте "
                "окно с кодом открытым до завершения привязки."
            )
            await self.notice(bot, binding["owner_id"], self.error)
            return True
        if len(parts) != 2:
            self.error = "Скопируйте полную команду с кодом из окна привязки Windows."
            await self.notice(bot, binding["owner_id"], self.error)
            return True
        if not secrets.compare_digest(parts[1].encode(), challenge.code.encode()):
            challenge.consume(parts[1])
            self.error = "Код не совпадает. Скопируйте команду из текущего окна привязки Windows."
            await self.notice(bot, binding["owner_id"], self.error)
            return True
        self.error = "Команда получена. Проверяются группа и права администратора…"
        await self.notice(bot, binding["owner_id"], self.error)
        try:
            group = await validate_group(bot, message.chat.id, snapshot[0])
        except Exception:
            self.error = (
                "Привязка не выполнена. Нужна закрытая группа с темами без публичного username, "
                "геолокации и связанного канала. Вы и бот должны быть администраторами; "
                "боту нужно право управления темами. "
                "Код ещё можно использовать до истечения срока."
            )
            await self.notice(bot, binding["owner_id"], self.error, force=True)
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
        self.error = None
        await self.notice(
            bot,
            binding["owner_id"],
            "Группа Topics привязана. В окне Windows можно завершить настройку. "
            "Зеркалирование включено; отправка в FunPay разрешается отдельно.",
            force=True,
        )
        return True
