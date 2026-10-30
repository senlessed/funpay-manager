import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from funpay_manager.app.services import ApplicationServices
from funpay_manager.funpay.adapter import AccountInfo, ChatInfo
from funpay_manager.funpay.readonly import (
    FunPayConnectionError,
    FunPayReadOnlyAdapter,
    parse_chats,
    parse_identity,
)
from funpay_manager.storage.database import LocalStorage


def identity_html(user_id=42):
    data = json.dumps({"userId": user_id, "csrf-token": "test-csrf"})
    return f"<body data-app-data='{data}'><div class='user-link-name'>Продавец</div></body>"


def test_identity_and_captcha():
    account, csrf = parse_identity(identity_html())
    assert account == AccountInfo("42", "Продавец")
    assert csrf == "test-csrf"
    for html in ("<body>CAPTCHA</body>", identity_html(0), identity_html("42")):
        with pytest.raises(FunPayConnectionError):
            parse_identity(html)


def test_chats_parser_does_not_use_username_as_key():
    html = "".join(
        f'<a class="contact-item unread" data-id="{chat_id}">'
        '<div class="media-user-name">same name</div>'
        '<div class="contact-item-message">&lt;unsafe&gt;</div></a>'
        for chat_id in (1, 2)
    )
    payload = {"objects": [{"type": "chat_bookmarks", "data": {"html": html}}]}
    chats = parse_chats(payload)
    assert [chat.id for chat in chats] == ["1", "2"]
    assert chats[0].last_message == "<unsafe>"
    assert chats[0].unread
    with pytest.raises(FunPayConnectionError):
        parse_chats({"objects": []})


async def test_adapter_uses_only_verified_read_contract():
    adapter = FunPayReadOnlyAdapter("test-key")
    adapter._request = AsyncMock(
        side_effect=[
            identity_html(),
            json.dumps(
                {
                    "objects": [{"type": "chat_bookmarks", "data": {"html": ""}}],
                }
            ),
        ]
    )
    assert await adapter.get_chats() == []
    first, second = adapter._request.call_args_list
    assert first.args == ("GET", "/")
    assert second.args == ("POST", "/runner/")
    assert json.loads(second.kwargs["data"]["objects"])[0]["type"] == "chat_bookmarks"
    assert not hasattr(adapter, "send_text")
    assert not hasattr(adapter, "raise_lots")
    await adapter.close()


async def test_services_cache_close_and_cancel(tmp_path):
    storage = LocalStorage(tmp_path / "db")
    await storage.migrate()
    # Vault is sync; use a small in-memory implementation for to_thread calls.
    secrets = {}

    class Vault:
        def set(self, name, value):
            secrets[name] = value

        def get(self, name):
            return secrets.get(name)

    adapters = []

    def factory(key):
        adapter = AsyncMock()
        adapter.authenticate.return_value = AccountInfo("42", "seller")
        adapter.get_chats.return_value = [ChatInfo("1", "buyer")]
        adapters.append(adapter)
        return adapter

    services = ApplicationServices(storage, lambda *a: None, lambda **kw: Vault(), factory)
    await services.connect_funpay("test-key")
    assert secrets == {"funpay_session": "test-key"}
    assert (await asyncio.gather(services.get_chats(), services.get_chats()))[0][0].id == "1"
    assert len(adapters) == 2
    for adapter in adapters:
        adapter.close.assert_awaited_once()
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(RuntimeError):
        await services.connect_funpay("must-not-store", cancelled)
    assert secrets["funpay_session"] == "test-key"
    adapters[-1].close.assert_awaited_once()


async def test_services_refuses_session_of_another_account(tmp_path):
    storage = LocalStorage(tmp_path / "db")
    await storage.migrate()
    await storage.set_setting("funpay_account", {"id": "42", "username": "expected"})

    class Vault:
        def get(self, name):
            return "test-only-key"

    adapter = AsyncMock()
    adapter.authenticate.return_value = AccountInfo("99", "other")
    services = ApplicationServices(
        storage, lambda *a: None, lambda **kw: Vault(), lambda _: adapter
    )
    with pytest.raises(RuntimeError):
        await services.get_chats()
    adapter.get_chats.assert_not_awaited()
    adapter.close.assert_awaited_once()
