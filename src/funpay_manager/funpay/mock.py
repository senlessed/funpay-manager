from .adapter import AccountInfo, ChatInfo, Message


class MockFunPayAdapter:
    """Explicit in-memory test adapter; never used implicitly in production."""

    def __init__(self):
        self.account = AccountInfo("mock-account", "Тестовый продавец")
        self.chats = {"mock-chat": ChatInfo("mock-chat", "Тестовый покупатель")}
        self.messages: list[Message] = []
        self.closed = False

    async def authenticate(self) -> AccountInfo:
        if self.closed:
            raise RuntimeError("Adapter closed")
        return self.account

    async def get_chats(self) -> list[ChatInfo]:
        await self.authenticate()
        return list(self.chats.values())

    async def get_history(self, chat_id: str) -> list[Message]:
        await self.authenticate()
        if chat_id not in self.chats:
            raise ValueError("Unknown chat")
        return [message for message in self.messages if message.chat_id == chat_id]

    async def send_text(self, chat_id: str, text: str) -> Message:
        await self.get_history(chat_id)
        if not text.strip():
            raise ValueError("Empty text")
        message = Message(
            str(len(self.messages) + 1), self.account.id, chat_id, self.account.id, text
        )
        self.messages.append(message)
        return message

    async def close(self) -> None:
        self.closed = True
