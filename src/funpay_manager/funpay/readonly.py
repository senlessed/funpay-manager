"""Read-only FunPay contract. No send/raise/payment operations are exposed."""

import asyncio
import json
import secrets

import aiohttp
from bs4 import BeautifulSoup

from funpay_manager.utils.diagnostics import DiagnosticError, FailureCode

from .adapter import AccountInfo, ChatInfo


class FunPayConnectionError(DiagnosticError):
    """Safe diagnostic; never retain raw responses/cookies in the exception."""

    default_code = FailureCode.FP_CONNECTION


class FunPayAuthError(FunPayConnectionError):
    """Session expired or site protection requires a manual check."""

    default_code = FailureCode.FP_AUTH


def normalize_golden_key(value: str) -> str:
    """Accept a single copied value/assignment, never a whole Cookie header."""
    key = value.strip()
    if key.startswith("golden_key="):
        key = key[len("golden_key=") :].strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1]
    if not key or any(ord(c) < 33 or ord(c) > 126 or c in '";,\\' for c in key):
        raise FunPayConnectionError("Некорректный сессионный ключ", code=FailureCode.FP_KEY)
    return key


def parse_identity(html: str) -> tuple[AccountInfo, str]:
    page = BeautifulSoup(html, "html.parser")
    name, body = page.select_one(".user-link-name"), page.body
    if name is None or body is None:
        raise FunPayAuthError("Авторизация не подтверждена; возможна CAPTCHA")
    try:
        data = json.loads(body.get("data-app-data", ""))
        user_id, csrf = data["userId"], data["csrf-token"]
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise ValueError
        if not isinstance(csrf, str) or not csrf or not name.get_text(strip=True):
            raise ValueError
        return AccountInfo(str(user_id), name.get_text(strip=True)), csrf
    except (ValueError, KeyError, TypeError) as error:
        raise FunPayConnectionError(
            "Изменился формат страницы FunPay", code=FailureCode.FP_FORMAT
        ) from error


def parse_chats(payload: dict) -> list[ChatInfo]:
    try:
        objects = payload["objects"]
        bookmarks = next(item for item in objects if item.get("type") == "chat_bookmarks")
        html = bookmarks["data"]["html"]
        if not isinstance(html, str):
            raise ValueError
        page = BeautifulSoup(html, "html.parser")
        result = []
        for node in page.select("a.contact-item")[:50]:
            chat_id = str(node["data-id"])
            name, message = (
                node.select_one(".media-user-name"),
                node.select_one(".contact-item-message"),
            )
            if (
                not chat_id.isascii()
                or not chat_id.isdecimal()
                or len(chat_id) > 32
                or name is None
                or message is None
            ):
                raise ValueError
            result.append(
                ChatInfo(
                    chat_id,
                    name.get_text(strip=True),
                    message.get_text(),
                    "unread" in node.get("class", []),
                )
            )
        return result
    except (KeyError, TypeError, ValueError, StopIteration, AttributeError) as error:
        raise FunPayConnectionError("Не удалось прочитать список чатов") from error


class FunPayReadOnlyAdapter:
    def __init__(self, golden_key: str):
        self._key = normalize_golden_key(golden_key)
        self._session: aiohttp.ClientSession | None = None
        self.account: AccountInfo | None = None
        self._csrf: str | None = None
        self._lock = asyncio.Lock()
        self.request_gate = None

    async def _request(self, method: str, path: str, data=None) -> str:
        if self.request_gate is not None:
            async with self.request_gate.slot():
                return await self._perform_request(method, path, data)
        return await self._perform_request(method, path, data)

    async def _perform_request(self, method: str, path: str, data=None) -> str:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                cookies={"golden_key": self._key},
                headers={
                    "User-Agent": "Mozilla/5.0 FunPayTelegramManager/0.1",
                },
            )
        try:
            async with self._session.request(
                method,
                "https://funpay.com" + path,
                data=data,
                allow_redirects=False,
                # FunPay returns only a page fragment for AJAX requests. The
                # homepage must include its layout/body data for authentication.
                headers=(
                    {"X-Requested-With": "XMLHttpRequest"}
                    if path.startswith(("/runner/", "/chat/history"))
                    else {"Accept": "text/html"}
                ),
            ) as response:
                if response.status in (401, 403):
                    raise FunPayAuthError(
                        "Требуется проверить авторизацию на FunPay",
                        code=(
                            FailureCode.FP_HTTP_401
                            if response.status == 401
                            else FailureCode.FP_HTTP_403
                        ),
                    )
                if response.status != 200:
                    raise FunPayConnectionError(
                        f"FunPay ответил HTTP {response.status}",
                        code=FailureCode.FP_HTTP_429
                        if response.status == 429
                        else FailureCode.FP_HTTP,
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 4_000_000:
                        raise FunPayConnectionError("Слишком большой ответ FunPay")
                return body.decode("utf-8")
        except (aiohttp.ClientSSLError, aiohttp.ServerFingerprintMismatch) as error:
            raise FunPayConnectionError("Ошибка TLS FunPay", code=FailureCode.FP_TLS) from error
        except TimeoutError as error:
            raise FunPayConnectionError("Таймаут FunPay", code=FailureCode.FP_TIMEOUT) from error
        except aiohttp.ClientError as error:
            raise FunPayConnectionError(
                "Ошибка сети FunPay", code=FailureCode.FP_NETWORK
            ) from error
        except UnicodeError as error:
            raise FunPayConnectionError(
                "Кодировка ответа FunPay", code=FailureCode.FP_FORMAT
            ) from error

    async def authenticate(self) -> AccountInfo:
        async with self._lock:
            html = await self._request("GET", "/")
            self.account, self._csrf = await asyncio.to_thread(parse_identity, html)
            return self.account

    async def get_chats(self) -> list[ChatInfo]:
        if self.account is None:
            await self.authenticate()
        async with self._lock:
            # runner request describes a read-only chat_bookmarks object, not an action.
            objects = [
                {
                    "type": "chat_bookmarks",
                    "id": int(self.account.id),
                    "tag": secrets.token_hex(5),
                    "data": False,
                }
            ]
            text = await self._request(
                "POST",
                "/runner/",
                data={
                    "objects": json.dumps(objects),
                    "request": "False",
                    "csrf_token": self._csrf,
                },
            )
            try:
                payload = json.loads(text)
            except ValueError as error:
                raise FunPayConnectionError("FunPay вернул неожиданный ответ") from error
            return await asyncio.to_thread(parse_chats, payload)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
        self._key = ""

    async def get_history(self, chat_id: str):
        from .history import parse_messages

        if not chat_id.isascii() or not chat_id.isdecimal() or len(chat_id) > 32:
            raise FunPayConnectionError("Некорректный ID чата")
        if self.account is None:
            await self.authenticate()
        async with self._lock:
            text = await self._request(
                "GET", f"/chat/history?node={chat_id}&last_message=99999999999999999999999"
            )
            try:
                payload = json.loads(text)
                node = payload["chat"]
                if "id" in node.get("node", {}) and str(node["node"]["id"]) != chat_id:
                    raise ValueError
                items = node["messages"]
            except (ValueError, KeyError, TypeError) as error:
                raise FunPayConnectionError("История недоступна") from error
            return await asyncio.to_thread(parse_messages, items, self.account.id, chat_id)
