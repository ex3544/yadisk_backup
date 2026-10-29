"""SQLite-трекер состояния скачивания.

Каждый файл с Яндекс.Диска регистрируется в таблице `files` с одним из статусов:
pending / downloaded / failed / skipped. WAL-режим позволяет писать из нескольких
потоков без блокировок.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

import db_migrations


def _utc_now() -> str:
    """Текущее время UTC в том же формате, что писали раньше (utcnow() устарел)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    remote_path TEXT PRIMARY KEY,
    size INTEGER,
    md5 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    local_path TEXT,
    error TEXT,
    downloaded_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);
"""

# История схемы. Добавлять только в конец, не переименовывать версии.
MIGRATIONS: list[db_migrations.Migration] = [
    (1, SCHEMA),
    # Счётчик попыток: по нему видно, файл упал один раз или бьётся неделю.
    # У Безлимита такая колонка была с самого начала, у Диска её не было.
    (2, "ALTER TABLE files ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
    # Сервер хранения, который отдал (или не отдал) файл. Нужен, чтобы в
    # диагностике было видно, на каком именно сервере Яндекса застряло.
    (3, "ALTER TABLE files ADD COLUMN storage_host TEXT"),
    # Превью отложенного фото: пока оригинал не скачался, в папке лежит
    # уменьшенная копия, чтобы не потерять представление о содержимом.
    (4, "ALTER TABLE files ADD COLUMN preview_path TEXT"),
]

# Префикс маркера в поле error: файл не сбойный, а ждёт Яндекса.
DEFERRED_PREFIX = "storage_down"


def is_deferred_error(error: object) -> bool:
    return isinstance(error, str) and error.startswith(DEFERRED_PREFIX)


def deferred_marker(kind: str, host: Optional[str] = None) -> str:
    """Текст маркера: «storage_down | storage.yandex.net»."""
    return f"{DEFERRED_PREFIX} | {host}" if host else DEFERRED_PREFIX


class State:
    """Тонкая обёртка над SQLite. Создаёт по соединению на поток (через threading.local).

    SQLite в WAL умеет конкурентные читатели + один писатель. Чтобы не ловить
    `database is locked`, у каждого потока своё соединение.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._tls = threading.local()
        self._lock = threading.Lock()  # для редких операций над schema
        self._all_conns: list[sqlite3.Connection] = []
        self._all_conns_lock = threading.Lock()
        self._init_schema()

    # ── низкоуровневое ────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30,
                isolation_level=None,  # autocommit; явные транзакции через BEGIN
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.row_factory = sqlite3.Row
            self._tls.conn = conn
            with self._all_conns_lock:
                self._all_conns.append(conn)
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            db_migrations.migrate(self._conn(), "state.db", MIGRATIONS)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ── публичное API ─────────────────────────────────────────────────

    def mark_pending(
        self,
        remote_path: str,
        size: Optional[int],
        md5: Optional[str],
    ) -> None:
        """Регистрирует файл как ожидающий скачивания. Не перетирает уже завершённые."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO files (remote_path, size, md5, status)
                VALUES (?, ?, ?, 'pending')
                ON CONFLICT(remote_path) DO UPDATE SET
                    size = excluded.size,
                    md5 = COALESCE(excluded.md5, files.md5)
                WHERE files.status NOT IN ('downloaded', 'skipped')
                """,
                (remote_path, size, md5),
            )

    def mark_downloaded(
        self, remote_path: str, local_path: str, storage_host: Optional[str] = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE files
                   SET status = 'downloaded',
                       local_path = ?,
                       error = NULL,
                       preview_path = NULL,
                       storage_host = COALESCE(?, storage_host),
                       downloaded_at = ?
                 WHERE remote_path = ?
                """,
                (local_path, storage_host, _utc_now(), remote_path),
            )

    def mark_skipped(
        self, remote_path: str, local_path: str, storage_host: Optional[str] = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE files
                   SET status = 'skipped',
                       local_path = ?,
                       error = NULL,
                       preview_path = NULL,
                       storage_host = COALESCE(?, storage_host),
                       downloaded_at = ?
                 WHERE remote_path = ?
                """,
                (local_path, storage_host, _utc_now(), remote_path),
            )

    def mark_deferred(self, remote_path: str, kind: str, host: Optional[str] = None) -> None:
        """Файл не скачался по вине Яндекса — остаётся в очереди, не сбойный.

        Попытки (attempts) не тратятся: сервер хранения молчит не по вине файла.
        Превью, если было, остаётся лежать до появления оригинала.
        """
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE files
                   SET status = 'pending',
                       error = ?,
                       storage_host = COALESCE(?, storage_host)
                 WHERE remote_path = ?
                """,
                (deferred_marker(kind, host), host, remote_path),
            )

    def get_waiting_stats(self) -> int:
        """Сколько файлов сейчас ждут Яндекса (отложенные, не сбойные)."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM files WHERE status='pending' "
            "AND error LIKE ?",
            (DEFERRED_PREFIX + "%",),
        ).fetchone()
        return int(row["n"] or 0)

    def get_deferred(self) -> Iterable[sqlite3.Row]:
        """Отложенные файлы — их пробуем первыми, до остальной очереди."""
        return self._conn().execute(
            "SELECT remote_path, size, md5, error, storage_host FROM files "
            "WHERE status='pending' AND error LIKE ? ORDER BY rowid ASC",
            (DEFERRED_PREFIX + "%",),
        )

    def clear_deferred_marks(self) -> int:
        """Снимает маркеры «ждёт Яндекса», возвращая файлы в обычную очередь."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE files SET error = NULL WHERE status='pending' AND error LIKE ?",
                (DEFERRED_PREFIX + "%",),
            )
            return cur.rowcount

    def set_preview_path(self, remote_path: str, preview_path: Optional[str]) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE files SET preview_path = ? WHERE remote_path = ?",
                (preview_path, remote_path),
            )

    def get_preview_paths(self) -> Iterable[sqlite3.Row]:
        """Все записи с превью — для выметания остатков при старте."""
        return self._conn().execute(
            "SELECT remote_path, local_path, preview_path, status FROM files "
            "WHERE preview_path IS NOT NULL"
        )

    def mark_failed(self, remote_path: str, error: str) -> None:
        """Помечает файл сбойным и увеличивает счётчик попыток."""
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE files
                   SET status = 'failed',
                       error = ?,
                       attempts = COALESCE(attempts, 0) + 1
                 WHERE remote_path = ?
                """,
                (error[:2000], remote_path),
            )

    def reset_failed_to_pending(self) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE files SET status='pending', error=NULL WHERE status='failed'"
            )
            return cur.rowcount

    def get_pending(
        self, order_by: str = "rowid ASC", deferred_first: bool = False
    ) -> Iterable[sqlite3.Row]:
        """Возвращает курсор с pending-файлами. Читается лениво, без загрузки всего в RAM.

        `deferred_first=True` ставит вперёд файлы, которые в прошлый раз ждали
        Яндекса: их немного, и их стоит попробовать, как только сервер ожил.
        """
        valid_orders = {
            "rowid ASC": "rowid ASC",
            "rowid DESC": "rowid DESC",
            "size ASC": "size ASC",
            "size DESC": "size DESC",
        }
        order = valid_orders.get(order_by, "rowid ASC")
        prefix = ""
        if deferred_first:
            prefix = f"(error LIKE '{DEFERRED_PREFIX}%') DESC, "
        return self._conn().execute(
            "SELECT remote_path, size, md5, error, storage_host FROM files "
            f"WHERE status='pending' ORDER BY {prefix}{order}"
        )

    def get_failed(self) -> Iterable[sqlite3.Row]:
        return self._conn().execute(
            "SELECT remote_path, size, md5, error, "
            "COALESCE(attempts, 0) AS attempts FROM files WHERE status='failed'"
        )

    def get_all_downloaded(self) -> Iterable[sqlite3.Row]:
        return self._conn().execute(
            "SELECT remote_path, local_path, md5, size FROM files "
            "WHERE status IN ('downloaded', 'skipped') AND local_path IS NOT NULL"
        )

    def get_stats(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM files GROUP BY status"
        ).fetchall()
        stats: dict[str, int] = {
            "pending": 0,
            "downloaded": 0,
            "failed": 0,
            "skipped": 0,
            "waiting": 0,
            "total": 0,
            "bytes_total": 0,
            "bytes_downloaded": 0,
        }
        for r in rows:
            stats[r["status"]] = r["n"]
            stats["total"] += r["n"]
            stats["bytes_total"] += r["bytes"] or 0
            if r["status"] in ("downloaded", "skipped"):
                stats["bytes_downloaded"] += r["bytes"] or 0
        try:
            stats["waiting"] = self.get_waiting_stats()
        except sqlite3.Error:
            stats["waiting"] = 0
        return stats

    def reset(self) -> None:
        """Пересоздаёт схему с нуля (для `main.py scan --force`).

        Сбрасывать user_version обязательно: иначе миграция считается
        применённой, CREATE TABLE не выполняется, и следующая запись падает с
        «no such table: files». Именно так ломался Безлимит.
        """
        with self._lock:
            conn = self._conn()
            conn.execute("DROP TABLE IF EXISTS files")
            db_migrations.set_user_version(conn, 0)
            db_migrations.migrate(conn, "state.db", MIGRATIONS)

    def get_existing_remote_paths(self) -> set[str]:
        """Все уже зарегистрированные пути — для инкрементального scan."""
        return {
            row["remote_path"]
            for row in self._conn().execute("SELECT remote_path FROM files")
        }

    def close(self) -> None:
        """Закрывает все соединения, открытые в любых потоках."""
        with self._all_conns_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._tls.conn = None
