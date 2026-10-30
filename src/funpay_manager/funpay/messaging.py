import asyncio
import json

from .history import parse_messages
from .readonly import FunPayReadOnlyAdapter


class NotSentError(RuntimeError):
    """Failure proven to occur before the write, or an explicit server rejection."""


class UnknownDeliveryError(RuntimeError):
    """The write might have been accepted; never retry automatically."""


class FunPayMessagingAdapter(FunPayReadOnlyAdapter):
    async def send_text(self, chat_id: str, text: str):
        if not text.strip() or len(text) > 4000:
            raise NotSentError("Недопустимый текст")
        try:
            before = {message.id for message in await self.get_history(chat_id)}
        except Exception as error:
            raise NotSentError("Не удалось проверить историю перед отправкой") from error
        async with self._lock:
            data = {
                "objects": json.dumps(
                    [
                        {
                            "type": "chat_node",
                            "id": int(chat_id),
                            "tag": "00000000",
                            "data": {"node": int(chat_id), "last_message": -1, "content": ""},
                        }
                    ]
                ),
                "request": json.dumps(
                    {
                        "action": "chat_message",
                        "data": {"node": int(chat_id), "last_message": -1, "content": text},
                    }
                ),
                "csrf_token": self._csrf,
            }
            # Every failure beyond this boundary is unknown unless the server
            # explicitly rejects the operation. No transport retry is performed.
            try:
                response = json.loads(await self._request("POST", "/runner/", data=data))
                ack = response.get("response")
                if isinstance(ack, dict) and ack.get("error") is not None:
                    raise NotSentError("FunPay отклонил сообщение")
                if not ack:
                    raise UnknownDeliveryError("Нет подтверждения")
                candidates = []
                for obj in response["objects"]:
                    if obj.get("type") != "chat_node" or str(obj.get("id")) != chat_id:
                        continue
                    parsed = await asyncio.to_thread(
                        parse_messages, obj["data"]["messages"], self.account.id, chat_id
                    )
                    candidates.extend(
                        message
                        for message in parsed
                        if message.id not in before
                        and message.author_id == self.account.id
                        and message.kind == "text"
                        and message.text == text
                    )
                if len(candidates) != 1:
                    raise UnknownDeliveryError("Нет однозначного нового remote ID")
                return candidates[0]
            except (NotSentError, UnknownDeliveryError):
                raise
            except Exception as error:
                raise UnknownDeliveryError("Результат отправки неизвестен") from error
