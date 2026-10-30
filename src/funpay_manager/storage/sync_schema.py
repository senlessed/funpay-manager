MIGRATION_3 = """
CREATE TABLE chat_sync_state (
    chat_id INTEGER PRIMARY KEY REFERENCES chats(id),
    watermark TEXT NOT NULL DEFAULT '0',
    initialized INTEGER NOT NULL DEFAULT 0,
    baseline_pending INTEGER NOT NULL,
    preview TEXT,
    unread INTEGER,
    last_checked REAL NOT NULL DEFAULT 0
);
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    bot_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    callback_data TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued','sending','sent','failed','unknown')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    telegram_message_id INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(event_key, bot_id, owner_id)
);
CREATE INDEX notifications_ready
ON notifications(account_id,bot_id,owner_id,status,next_attempt,id);
"""

SYNC_COLUMNS = {
    "chat_sync_state": {
        "chat_id",
        "watermark",
        "initialized",
        "baseline_pending",
        "preview",
        "unread",
        "last_checked",
    },
    "notifications": {
        "id",
        "event_key",
        "account_id",
        "bot_id",
        "owner_id",
        "text",
        "callback_data",
        "status",
        "attempts",
        "next_attempt",
        "telegram_message_id",
        "last_error",
        "created_at",
        "updated_at",
    },
}
