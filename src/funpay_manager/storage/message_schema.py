MIGRATION_2 = """
CREATE TABLE accounts (id TEXT PRIMARY KEY, username TEXT NOT NULL);
CREATE TABLE chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    remote_id TEXT NOT NULL,
    username TEXT NOT NULL,
    route_key TEXT NOT NULL UNIQUE,
    UNIQUE(account_id, remote_id),
    UNIQUE(id,account_id)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    remote_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing','system','unknown')),
    observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(chat_id, remote_id)
);
CREATE INDEX messages_history ON messages(chat_id, id);
CREATE TABLE outgoing_messages (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    source TEXT NOT NULL,
    bot_id INTEGER NOT NULL,
    telegram_chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    source_update_id INTEGER,
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','sending','sent','failed','unknown')),
    attempts INTEGER NOT NULL DEFAULT 0,
    remote_message_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(bot_id, telegram_chat_id, telegram_message_id),
    UNIQUE(bot_id, source_update_id),
    FOREIGN KEY(chat_id,account_id) REFERENCES chats(id,account_id)
);
CREATE INDEX outgoing_ready ON outgoing_messages(account_id,status,sequence);
CREATE INDEX outgoing_chat ON outgoing_messages(chat_id,sequence,status);
CREATE TABLE reply_contexts (
    bot_id INTEGER NOT NULL,
    telegram_chat_id INTEGER NOT NULL,
    prompt_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    expires_at REAL NOT NULL,
    PRIMARY KEY(bot_id,telegram_chat_id,prompt_id)
);
"""

MESSAGE_COLUMNS = {
    "accounts": {"id", "username"},
    "chats": {"id", "account_id", "remote_id", "username", "route_key"},
    "messages": {
        "id",
        "chat_id",
        "remote_id",
        "author_id",
        "text",
        "kind",
        "direction",
        "observed_at",
    },
    "outgoing_messages": {
        "sequence",
        "operation_id",
        "account_id",
        "chat_id",
        "source",
        "bot_id",
        "telegram_chat_id",
        "telegram_message_id",
        "source_update_id",
        "text",
        "status",
        "attempts",
        "remote_message_id",
        "last_error",
        "created_at",
        "updated_at",
    },
    "reply_contexts": {"bot_id", "telegram_chat_id", "prompt_id", "chat_id", "expires_at"},
}
