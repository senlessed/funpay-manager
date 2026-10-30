import pytest

from funpay_manager.funpay.mock import MockFunPayAdapter


async def test_mock_chat_isolation_and_confirmation():
    adapter = MockFunPayAdapter()
    account = await adapter.authenticate()
    sent = await adapter.send_text("mock-chat", "Тест")
    assert sent.author_id == account.id
    assert await adapter.get_history("mock-chat") == [sent]
    with pytest.raises(ValueError):
        await adapter.send_text("foreign-chat", "Не отправлять")
    assert len(adapter.messages) == 1
    await adapter.close()
    with pytest.raises(RuntimeError):
        await adapter.get_chats()
