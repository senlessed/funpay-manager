from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

REASONS = {
    "permissions": "Привязка группы или права не прошли проверку.",
    "id": "Telegram вернул другую группу. Повторите привязку в нужной группе.",
    "type": "Telegram не считает этот чат супергруппой. Нужна группа с темами, не канал.",
    "forum": "По ответу Telegram темы выключены. Включите «Темы» в настройках группы.",
    "public": "Telegram сообщает о публичном username. Уберите публичные имена группы.",
    "linked": "Telegram сообщает о связанном канале. Нужна отдельная группа без связи с каналом.",
    "location": "Telegram сообщает о геолокации группы. Нужна группа без геолокации.",
    "owner_admin": "Привязанный владелец бота не является администратором этой группы.",
    "bot_admin": "Telegram не считает бота администратором этой группы.",
    "manage_topics": "Бот — администратор, но Telegram не подтвердил право «Управление темами».",
}


class GroupPermissionError(RuntimeError):
    """Known permission failure with fixed, non-secret diagnostic details."""

    def __init__(self, message="", *, code="permissions", checks=()):
        super().__init__(message)
        self.code = code if code in REASONS else "permissions"
        self.checks = checks

    @property
    def diagnostic(self):
        details = "\n".join(self.checks)
        return f"{REASONS[self.code]}\n{details}\nКод: tg_group_{self.code}."


class GroupLookupError(RuntimeError):
    """An API failure is not evidence that the group's settings are wrong."""

    def __init__(self, stage, kind):
        self.stage, self.kind = stage, kind
        super().__init__("Telegram group check failed")

    @property
    def diagnostic(self):
        stage = {
            "chat": "getChat — сведения о группе",
            "owner": "getChatMember — права владельца",
            "bot": "getChatMember — права бота",
        }[self.stage]
        reason = {
            "forbidden": "Telegram запретил запрос. Проверьте, что бот добавлен в группу.",
            "bad_request": "Telegram отклонил запрос проверки. Настройки группы не подтверждены.",
            "unauthorized": "Telegram не принял токен бота. Переподключите Telegram в Windows.",
            "network": "Не удалось получить ответ Telegram. Проверьте сеть и повторите позже.",
            "unexpected": (
                "Не удалось разобрать результат проверки Telegram. Нужна диагностика приложения."
            ),
        }[self.kind]
        return f"{reason}\nЭтап: {stage}.\nКод: tg_group_{self.stage}_{self.kind}."


async def lookup(operation, stage):
    try:
        return await operation
    except TelegramRetryAfter:
        raise
    except Exception as error:
        kind = "unexpected"
        for category, name in (
            (TelegramForbiddenError, "forbidden"),
            (TelegramBadRequest, "bad_request"),
            (TelegramUnauthorizedError, "unauthorized"),
            ((TelegramNetworkError, TimeoutError), "network"),
        ):
            if isinstance(error, category):
                kind = name
                break
        raise GroupLookupError(stage, kind) from error


def role_name(status):
    return {
        "creator": "создатель",
        "administrator": "администратор",
        "member": "участник",
        "restricted": "ограничен",
        "left": "не состоит в группе",
        "kicked": "исключён",
    }.get(status, "неизвестна")


async def validate_group(bot, group_id: int, binding: dict):
    group = await lookup(bot.get_chat(group_id, request_timeout=15), "chat")
    public = bool(getattr(group, "username", None) or getattr(group, "active_usernames", None))
    linked = bool(getattr(group, "linked_chat_id", None))
    location = bool(getattr(group, "location", None))
    forum = bool(getattr(group, "is_forum", False))
    chat_type = {"supergroup": "супергруппа", "group": "обычная группа", "channel": "канал"}.get(
        group.type, "другой"
    )
    checks = (
        f"Тип чата: {chat_type}; темы: {'включены' if forum else 'выключены'}.",
        f"Публичное имя: {'есть' if public else 'нет'}; "
        f"связанный канал: {'есть' if linked else 'нет'}; "
        f"геолокация: {'есть' if location else 'нет'}.",
    )
    for failed, code in (
        (group.id != group_id, "id"),
        (group.type != "supergroup", "type"),
        (not forum, "forum"),
        (public, "public"),
        (linked, "linked"),
        (location, "location"),
    ):
        if failed:
            raise GroupPermissionError(REASONS[code], code=code, checks=checks)
    owner = await lookup(
        bot.get_chat_member(group_id, binding["owner_id"], request_timeout=15), "owner"
    )
    member = await lookup(
        bot.get_chat_member(group_id, binding["bot_id"], request_timeout=15), "bot"
    )
    manage = bool(getattr(member, "can_manage_topics", False))
    checks += (
        f"Владелец: {role_name(owner.status)}; бот: {role_name(member.status)}; "
        f"управление темами: {'разрешено' if manage else 'не подтверждено'}.",
    )
    for failed, code in (
        (owner.status not in ("creator", "administrator"), "owner_admin"),
        (member.status != "administrator", "bot_admin"),
        (not manage, "manage_topics"),
    ):
        if failed:
            raise GroupPermissionError(REASONS[code], code=code, checks=checks)
    return group


def pairing_failure(error):
    if isinstance(error, (GroupPermissionError, GroupLookupError)):
        return error.diagnostic
    if isinstance(error, TelegramRetryAfter):
        return "Telegram ограничил частоту запросов. Повторите позже.\nКод: tg_group_rate_limit."
    return "Внутренняя ошибка проверки группы.\nКод: tg_group_unexpected."
