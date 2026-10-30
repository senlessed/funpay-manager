MIGRATION_4 = """
ALTER TABLE outgoing_messages ADD COLUMN source_thread_id INTEGER;
ALTER TABLE outgoing_messages ADD COLUMN source_binding_key TEXT;
ALTER TABLE outgoing_messages ADD COLUMN source_owner_id INTEGER;
CREATE TABLE telegram_topic_mappings (
    chat_id INTEGER PRIMARY KEY REFERENCES chats(id),
    binding_key TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    thread_id INTEGER,
    status TEXT NOT NULL CHECK(status IN ('pending','creating','ready','invalid','unknown')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(group_id,thread_id)
);
CREATE TABLE telegram_message_mappings (
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    remote_message_id TEXT NOT NULL,
    binding_key TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY(chat_id,remote_message_id,binding_key),
    UNIQUE(group_id,telegram_message_id)
);
CREATE TABLE telegram_topic_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_key TEXT NOT NULL UNIQUE,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    remote_message_id TEXT NOT NULL,
    binding_key TEXT NOT NULL,
    direction TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','sending','sent','failed','unknown')),
    next_attempt REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(chat_id,remote_message_id,binding_key)
);
CREATE INDEX topic_deliveries_ready ON telegram_topic_deliveries(status,next_attempt,id);
"""

TOPIC_COLUMNS = {
    "telegram_topic_mappings": {
        "chat_id",
        "binding_key",
        "group_id",
        "thread_id",
        "status",
        "created_at",
        "updated_at",
    },
    "telegram_message_mappings": {
        "chat_id",
        "remote_message_id",
        "binding_key",
        "group_id",
        "thread_id",
        "telegram_message_id",
        "source",
    },
    "telegram_topic_deliveries": {
        "id",
        "delivery_key",
        "chat_id",
        "remote_message_id",
        "binding_key",
        "direction",
        "text",
        "status",
        "next_attempt",
        "last_error",
        "created_at",
    },
}
