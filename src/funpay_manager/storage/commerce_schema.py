MIGRATION_5 = """
ALTER TABLE chats ADD COLUMN buyer_id TEXT;
CREATE TABLE orders_cache (
 account_id TEXT NOT NULL, order_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
 status TEXT NOT NULL, description TEXT NOT NULL, amount TEXT, currency TEXT,
 position INTEGER NOT NULL, observed_at REAL NOT NULL,
 PRIMARY KEY(account_id,order_id)
);
CREATE INDEX orders_buyer ON orders_cache(account_id,buyer_id);
CREATE TABLE lot_schedules (
 account_id TEXT NOT NULL, game_id INTEGER NOT NULL, name TEXT NOT NULL, nodes TEXT NOT NULL,
 available INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 0,
 interval_seconds INTEGER NOT NULL DEFAULT 3600, next_attempt REAL NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'idle', last_success REAL, last_error TEXT,
 PRIMARY KEY(account_id,game_id)
);
CREATE TABLE raise_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, game_id INTEGER NOT NULL,
 started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL
);
"""
COMMERCE_COLUMNS = {
    "orders_cache": {
        "account_id",
        "order_id",
        "buyer_id",
        "status",
        "description",
        "amount",
        "currency",
        "position",
        "observed_at",
    },
    "lot_schedules": {
        "account_id",
        "game_id",
        "name",
        "nodes",
        "available",
        "enabled",
        "interval_seconds",
        "next_attempt",
        "status",
        "last_success",
        "last_error",
    },
    "raise_attempts": {"id", "account_id", "game_id", "started_at", "finished_at", "status"},
}
