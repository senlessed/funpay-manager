import json
from unittest.mock import AsyncMock

import pytest

from funpay_manager.funpay.adapter import AccountInfo, Message
from funpay_manager.funpay.history import parse_messages
from funpay_manager.funpay.messaging import (
    FunPayMessagingAdapter,
    NotSentError,
    UnknownDeliveryError,
)
from funpay_manager.funpay.readonly import FunPayConnectionError, FunPayReadOnlyAdapter


def item(remote_id=2, author=42, html="<div class='message-text'>reply</div>"):
    return {"id": remote_id, "author": author, "html": html}


def response(messages=None, chat_id=1):
    return json.dumps(
        {
            "response": {"success": True},
            "objects": [
                {
                    "type": "chat_node",
                    "id": chat_id,
                    "data": {"messages": messages if messages is not None else [item()]},
                }
            ],
        }
    )


def adapter():
    result = FunPayMessagingAdapter("test-only-key")
    result.account = AccountInfo("42", "seller")
    result._csrf = "test-only-csrf"
    result.get_history = AsyncMock(return_value=[Message("1", "42", "1", "99", "hi")])
    result._request = AsyncMock(return_value=response())
    return result


async def test_send_contract_and_exact_confirmation():
    result = adapter()
    message = await result.send_text("1", "reply")
    assert message == Message("2", "42", "1", "42", "reply")
    result._request.assert_awaited_once()
    args = result._request.await_args
    assert args.args == ("POST", "/runner/")
    request = json.loads(args.kwargs["data"]["request"])
    assert request == {
        "action": "chat_message",
        "data": {"node": 1, "last_message": -1, "content": "reply"},
    }


@pytest.mark.parametrize(
    "answer",
    [
        response([item(remote_id=1)]),
        response(chat_id=999),
        response([item(author=99)]),
        response([item(), item(remote_id=3)]),
        "{}",
        "not json",
    ],
)
async def test_ambiguous_ack_is_unknown_and_never_retried(answer):
    result = adapter()
    result._request.return_value = answer
    with pytest.raises(UnknownDeliveryError):
        await result.send_text("1", "reply")
    result._request.assert_awaited_once()


async def test_network_failure_after_write_boundary_is_unknown():
    result = adapter()
    result._request.side_effect = TimeoutError
    with pytest.raises(UnknownDeliveryError):
        await result.send_text("1", "reply")
    result._request.assert_awaited_once()


async def test_preflight_failure_and_explicit_rejection_are_not_sent():
    result = adapter()
    result.get_history.side_effect = TimeoutError
    with pytest.raises(NotSentError):
        await result.send_text("1", "reply")
    result._request.assert_not_awaited()
    result = adapter()
    result._request.return_value = '{"response":{"error":"test-only rejection"}}'
    with pytest.raises(NotSentError):
        await result.send_text("1", "reply")


async def test_readonly_history_contract():
    result = FunPayReadOnlyAdapter("test-only-key")
    result.account = AccountInfo("42", "seller")
    result._request = AsyncMock(
        return_value=json.dumps({"chat": {"node": {"id": 1}, "messages": [item(author=99)]}})
    )
    messages = await result.get_history("1")
    assert messages[0].author_id == "99"
    assert result._request.await_args.args[0] == "GET"
    with pytest.raises(FunPayConnectionError):
        await result.get_history("../../invalid")


def test_history_preserves_multiline_and_marks_media():
    rows = parse_messages(
        [
            item(html="<div class='message-text'>a<br>b &lt;unsafe&gt;</div>"),
            item(3, 0, "<div class='alert'>system</div>"),
            item(4, 99, "<a class='chat-img-link'>image</a>"),
        ],
        "42",
        "1",
    )
    assert rows[0].text == "a\nb <unsafe>"
    assert rows[1].author_id == "0"
    assert rows[2].kind == "image" and rows[2].text == ""
