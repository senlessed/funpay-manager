"""One-use local pairing challenge; no first-sender trust."""

import secrets
import time


class PairingChallenge:
    def __init__(self, ttl: float = 300, attempts: int = 100, clock=time.monotonic):
        self.code = secrets.token_urlsafe(18)
        self._clock = clock
        self.expires = clock() + ttl
        self.remaining = attempts
        self.used = False

    @property
    def active(self) -> bool:
        return not self.used and self.remaining > 0 and self._clock() < self.expires

    def accept(
        self, text: str, user_id: int, chat_id: int, private: bool, is_bot: bool = False
    ) -> bool:
        if not self.active or not private or is_bot or user_id <= 0 or chat_id != user_id:
            return False
        return (
            self.consume(text.strip().removeprefix("/start "))
            if text.strip().startswith("/start ")
            else self.consume("")
        )

    def consume(self, code: str) -> bool:
        if not self.active:
            return False
        self.remaining -= 1
        if not secrets.compare_digest(code.encode(), self.code.encode()):
            return False
        self.used = True
        return True
