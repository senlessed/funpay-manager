MIGRATION_6 = """
CREATE TABLE reply_templates (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL
);
CREATE INDEX templates_account ON reply_templates(account_id,title);
CREATE TABLE template_previews (
 preview_key TEXT PRIMARY KEY, template_id TEXT NOT NULL, revision INTEGER NOT NULL,
 account_id TEXT NOT NULL, chat_id INTEGER NOT NULL REFERENCES chats(id), route_key TEXT NOT NULL,
 bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, telegram_message_id INTEGER,
 text TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','ready','queued','cancelled')),
 operation_id TEXT
);
CREATE INDEX template_preview_expiry ON template_previews(status,expires_at);
"""
TEMPLATE_COLUMNS = {
    "reply_templates": {"id", "account_id", "title", "body", "revision", "updated_at"},
    "template_previews": {
        "preview_key",
        "template_id",
        "revision",
        "account_id",
        "chat_id",
        "route_key",
        "bot_id",
        "owner_id",
        "telegram_message_id",
        "text",
        "created_at",
        "expires_at",
        "status",
        "operation_id",
    },
}
