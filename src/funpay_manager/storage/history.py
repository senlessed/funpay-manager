async def store_message(db, local_id, message, direction):
    async with db.execute(
        "SELECT kind,author_id FROM messages WHERE chat_id=? AND remote_id=?",
        (local_id, message.id),
    ) as cursor:
        old = await cursor.fetchone()
    repaired = bool(
        old
        and old[0] == "unsupported"
        and message.kind != "unsupported"
        and old[1] == message.author_id
    )
    if repaired:
        await db.execute(
            "UPDATE messages SET text=?,kind=? WHERE chat_id=? AND remote_id=?",
            (message.text, message.kind, local_id, message.id),
        )
    else:
        await db.execute(
            "INSERT INTO messages(chat_id,remote_id,author_id,text,kind,direction) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id,remote_id) DO NOTHING",
            (local_id, message.id, message.author_id, message.text, message.kind, direction),
        )
    return repaired
