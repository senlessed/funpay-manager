import json
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .readonly import FunPayConnectionError


@dataclass(frozen=True)
class Order:
    id: str
    buyer_id: str
    status: str
    description: str
    amount: str | None
    currency: str | None


@dataclass(frozen=True)
class RaiseCategory:
    game_id: int
    name: str
    nodes: tuple[int, ...]


def parse_orders(html):
    page = BeautifulSoup(html, "html.parser")
    if not page.select_one(".user-link-name"):
        raise FunPayConnectionError("Orders page is not authenticated")
    result = []
    for row in page.select("a.tc-item")[:300]:
        number, buyer = row.select_one(".tc-order"), row.select_one(".media-user-name [data-href]")
        if number is None or buyer is None:
            raise FunPayConnectionError("Order markup changed")
        order_id = number.get_text(strip=True).removeprefix("#")
        path = urlparse(buyer.get("data-href", ""))
        matched = re.fullmatch(r"/users/([0-9]+)/?", path.path)
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", order_id) or not matched:
            raise FunPayConnectionError("Invalid order identity")
        if path.netloc not in ("", "funpay.com"):
            raise FunPayConnectionError("Invalid buyer link")
        label = row.select_one(".tc-status")
        status = {
            "оплачен": "paid",
            "paid": "paid",
            "закрыт": "closed",
            "closed": "closed",
            "возврат": "refunded",
            "refunded": "refunded",
        }.get(label.get_text(strip=True).casefold() if label else "", "unknown")
        desc = row.select_one(".order-desc")
        price = row.select_one(".tc-price")
        money = re.fullmatch(
            r"\s*([0-9\s\u00a0]+(?:[.,][0-9]{1,2})?)\s*(₽|RUB|руб\.?|\$|USD|€|EUR)\s*",
            price.get_text(" ", strip=True) if price else "",
            re.I,
        )
        amount, currency = None, None
        if money:
            amount = str(Decimal(re.sub(r"\s", "", money[1]).replace(",", ".")))
            currency = {"₽": "RUB", "руб": "RUB", "руб.": "RUB", "$": "USD", "€": "EUR"}.get(
                money[2].lower(), money[2].upper()
            )
        result.append(
            Order(
                order_id,
                matched[1],
                status,
                desc.get_text(" ", strip=True)[:1000] if desc else "",
                amount,
                currency,
            )
        )
    cursor = page.select_one("input[name=continue]")
    return result, bool(cursor and cursor.get("value"))


def parse_raise_categories(home, profile):
    catalog = {}
    for game in BeautifulSoup(home, "html.parser").select(".promo-game-item"):
        title = game.select_one(".game-title[data-id]")
        if not title or not str(title["data-id"]).isdigit():
            continue
        for link in game.select("a[href]"):
            match = re.fullmatch(r"/lots/([0-9]+)/?", urlparse(link["href"]).path)
            if match:
                catalog[int(match[1])] = (int(title["data-id"]), title.get_text(" ", strip=True))
    page = BeautifulSoup(profile, "html.parser")
    if not page.select_one(".user-link-name"):
        raise FunPayConnectionError("Profile is not authenticated")
    groups = {}
    for section in page.select(".offer-list-title-container"):
        link = section.select_one("h3 a[href]")
        if not link or not section.parent.select_one("a.tc-item"):
            continue
        match = re.fullmatch(r"/lots/([0-9]+)/?", urlparse(link["href"]).path)
        if not match:
            continue  # Currency/chips are not supported by lots/raise.
        node = int(match[1])
        if node not in catalog:
            raise FunPayConnectionError("Cannot map lot category to game")
        game, name = catalog[node]
        groups.setdefault(game, (name, set()))[1].add(node)
    return [
        RaiseCategory(game, name, tuple(sorted(nodes))) for game, (name, nodes) in groups.items()
    ]


@dataclass(frozen=True)
class RaiseResult:
    status: str
    wait_seconds: int | None = None


def parse_raise_result(text):
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return RaiseResult("unknown")
    if not isinstance(data, dict):
        return RaiseResult("unknown")
    if data.get("error") is False or type(data.get("error")) is int and data["error"] == 0:
        return RaiseResult("raised")
    if data.get("error"):
        msg = data.get("msg", "")
        if isinstance(msg, str) and re.search(r"подождите|wait", msg, re.I):
            parts = re.findall(
                r"(\d+)\s*(час\w*|мин\w*|сек\w*|hours?|minutes?|seconds?)", msg, re.I
            )
            seconds = sum(
                int(n)
                * (
                    3600
                    if u.lower().startswith(("час", "hour"))
                    else 60
                    if u.lower().startswith(("мин", "minute"))
                    else 1
                )
                for n, u in parts
            )
            if 0 < seconds <= 7 * 86400:
                return RaiseResult("cooldown", seconds)
        return RaiseResult("rejected")
    return RaiseResult("unknown")
