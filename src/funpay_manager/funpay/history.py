from bs4 import BeautifulSoup

from .adapter import Message
from .readonly import FunPayConnectionError


def parse_messages(items, account_id: str, chat_id: str) -> list[Message]:
    if not isinstance(items, list) or len(items) > 1000:
        raise FunPayConnectionError("Неожиданный формат истории")
    result = []
    try:
        for item in items:
            remote_id, author = str(item["id"]), str(item["author"])
            if not remote_id.isascii() or not remote_id.isdecimal() or len(remote_id) > 32:
                raise ValueError
            if not author.isascii() or not author.isdecimal() or len(author) > 32:
                raise ValueError
            page = BeautifulSoup(item["html"], "html.parser")
            text = page.select_one(".message-text" if author != "0" else ".alert")
            if text:
                for line_break in text.select("br"):
                    line_break.replace_with("\n")
            image = page.select_one("a.chat-img-link")
            kind = "image" if image else "text" if text else "unsupported"
            content = text.get_text() if kind == "text" else ""
            result.append(
                Message(str(int(remote_id)), account_id, chat_id, str(int(author)), content, kind)
            )
        return sorted(result, key=lambda message: int(message.id))
    except (KeyError, TypeError, ValueError) as error:
        raise FunPayConnectionError("Не удалось разобрать историю") from error
