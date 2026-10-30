from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AccountInfo:
    id: str
    username: str


@dataclass(frozen=True)
class ChatInfo:
    id: str
    username: str
    last_message: str | None = None
    unread: bool | None = None


@dataclass(frozen=True)
class Message:
    id: str
    account_id: str
    chat_id: str
    author_id: str
    text: str


class FunPayAdapter(Protocol):
    async def authenticate(self) -> AccountInfo: ...

    async def get_chats(self) -> list[ChatInfo]: ...

    async def get_history(self, chat_id: str) -> list[Message]: ...

    async def send_text(self, chat_id: str, text: str) -> Message: ...

    async def close(self) -> None: ...
