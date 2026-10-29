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


def _migration_2(conn: sqlite3.Connection) -> None:
    """Повторно применяет схему: лечит базы, у которых таблицы пропали.

    В 35.5.7 кнопка «Пересоздать базу» удаляла таблицы `unlim_files` и
    `unlim_clusters`, но оставляла `user_version = 1`. Миграция 1 при этом уже
    считалась применённой, `CREATE TABLE IF NOT EXISTS` не выполнялся, и любой
    следующий запуск падал с `no such table: unlim_clusters`. Схема идемпотентна,
    так что достаточно применить её ещё раз.
    """
    db_migrations.execute_script(conn, DB_SCHEMA)


def _migration_3(conn: sqlite3.Connection) -> None:
    """Сервер хранения и превью отложенного фото.

    `storage_host` — какой сервер Яндекса не отдал файл, `preview_path` —
    уменьшенная копия, которая лежит в папке, пока оригинал ждёт.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(unlim_files)")}
    if "storage_host" not in cols:
        conn.execute("ALTER TABLE unlim_files ADD COLUMN storage_host TEXT")
    if "preview_path" not in cols:
        conn.execute("ALTER TABLE unlim_files ADD COLUMN preview_path TEXT")


def _migration_4(conn: sqlite3.Connection) -> None:
    """Пауза файла после ответа 5xx (`retry_after`, unix-время)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(unlim_files)")}
    if "retry_after" not in cols:
        conn.execute("ALTER TABLE unlim_files ADD COLUMN retry_after INTEGER")


def _migration_5(conn: sqlite3.Connection) -> None:
    """Сколько раз файл получал 5xx — по нему он объявляется сгоревшим."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(unlim_files)")}
    if "server_fails" not in cols:
        conn.execute(
            "ALTER TABLE unlim_files ADD COLUMN server_fails INTEGER NOT NULL DEFAULT 0"
        )


# История схемы. Добавлять только в конец, не переименовывать версии.
MIGRATIONS: list[db_migrations.Migration] = [
    (1, _migration_1),
    (2, _migration_2),
    (3, _migration_3),
    (4, _migration_4),
    (5, _migration_5),
]


def migrate(conn: sqlite3.Connection) -> dict:
    return db_migrations.migrate(conn, "unlim_state.db", MIGRATIONS)
