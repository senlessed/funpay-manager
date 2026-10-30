class GroupPermissionError(RuntimeError):
    """Group is not a private forum administered by the owner and bot."""


async def validate_group(bot, group_id: int, binding: dict):
    group = await bot.get_chat(group_id, request_timeout=15)
    if (
        group.id != group_id
        or group.type != "supergroup"
        or not group.is_forum
        or getattr(group, "username", None)
        or getattr(group, "active_usernames", None)
        or getattr(group, "linked_chat_id", None)
        or getattr(group, "location", None)
    ):
        raise GroupPermissionError("Нужна закрытая супергруппа с темами, без публичных ссылок")
    owner = await bot.get_chat_member(group_id, binding["owner_id"], request_timeout=15)
    member = await bot.get_chat_member(group_id, binding["bot_id"], request_timeout=15)
    if (
        owner.status not in ("creator", "administrator")
        or member.status != "administrator"
        or not getattr(member, "can_manage_topics", False)
    ):
        raise GroupPermissionError("Владелец и бот должны иметь необходимые права администратора")
    return group
