"""Схема и миграции базы «Безлимита» (`unlim_state.db`).

Вынесено отдельно, потому что эту базу открывают несколько процессов:
unlim_backup.py, unlim_download.py, unlim_worker.py и веб-панель.
У всех должна быть одна и та же история миграций.
"""

from __future__ import annotations

import sqlite3

import db_migrations

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS unlim_files (
    file_id TEXT PRIMARY KEY,
    cluster_id TEXT,
    name TEXT,
    size INTEGER,
    md5 TEXT,
    mime TEXT,
    status TEXT DEFAULT 'pending',
    local_path TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    worker_id INTEGER,
    assigned_at TIMESTAMP,
    downloaded_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_unlim_status ON unlim_files(status);
CREATE TABLE IF NOT EXISTS unlim_clusters (
    cluster_id TEXT PRIMARY KEY,
    size INTEGER,
    fetched INTEGER DEFAULT 0
);
"""


def _migration_1(conn: sqlite3.Connection) -> None:
    """Базовая схема. Для баз, созданных до появления `attempts`, добавляет колонку."""
    db_migrations.execute_script(conn, DB_SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(unlim_files)")}
    if "attempts" not in cols:
        conn.execute("ALTER TABLE unlim_files ADD COLUMN attempts INTEGER DEFAULT 0")


# История схемы. Добавлять только в конец, не переименовывать версии.
MIGRATIONS: list[db_migrations.Migration] = [
    (1, _migration_1),
]


def migrate(conn: sqlite3.Connection) -> dict:
    return db_migrations.migrate(conn, "unlim_state.db", MIGRATIONS)
