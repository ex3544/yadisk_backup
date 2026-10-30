"""SQLite-трекер состояния скачивания.

Каждый файл с Яндекс.Диска регистрируется в таблице `files` с одним из статусов:
pending / downloaded / failed / skipped. WAL-режим позволяет писать из нескольких
потоков без блокировок.
"""

from __future__ import annotations

import sqlite3
import threading
import time
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
    # Пауза конкретного файла (unix-время): сервер ответил 500 именно по нему.
    # Пауза вместо «мёртвого хоста» — соседние файлы продолжают качаться.
    (5, "ALTER TABLE files ADD COLUMN retry_after INTEGER"),
    # Сколько раз файл получил 5xx. После SERVER_FAIL_LIMIT проходов он
    # признаётся сгоревшим и выходит из очереди, не тормозя остальные.
    (6, "ALTER TABLE files ADD COLUMN server_fails INTEGER NOT NULL DEFAULT 0"),
]

# Префикс маркера в поле error: файл не сбойный, а ждёт Яндекса.
DEFERRED_PREFIX = "storage_down"

# Файл, который Яндекс не отдаёт уже несколько проходов подряд. Это не «ждёт»:
# ждать дальше бессмысленно, но и пугать словом «сбой» не за что.
LOST_PREFIX = "yandex_lost"

# Пауза файла после ответа 5xx: столько секунд его не трогаем.
SERVER_COOLDOWN_SEC = 600

# Сколько разных проходов с 5xx терпим, прежде чем считать файл сгоревшим.
SERVER_FAIL_LIMIT = 3

# Пауза сгоревшего файла: сразу после приговора его не трогаем полчаса.
# Без паузы новый запуск начинался бы с сотен мёртвых запросов — у тестера
# сгорело около 995 файлов, и каждый запуск тратил на них десятки минут.
LOST_RETRY_SEC = 30 * 60

# Сколько сгоревших файлов возвращаем в очередь за один запуск. Остальные
# остаются сгоревшими до следующего запуска — иначе после долгого простоя
# один прогон превращается в сплошные повторы.
LOST_RETURN_LIMIT = 200


def is_deferred_error(error: object) -> bool:
    return isinstance(error, str) and error.startswith(DEFERRED_PREFIX)


def deferred_marker(kind: str, host: Optional[str] = None) -> str:
    """Текст маркера: «storage_down | storage.yandex.net»."""
    return f"{DEFERRED_PREFIX} | {host}" if host else DEFERRED_PREFIX


def is_lost_error(error: object) -> bool:
    """Файл сгорел у Яндекса: ждать его больше не имеет смысла."""
    return isinstance(error, str) and error.startswith(LOST_PREFIX)


def lost_marker(host: Optional[str] = None) -> str:
    return f"{LOST_PREFIX} | {host}" if host else LOST_PREFIX


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
                       retry_after = NULL,
                       server_fails = 0,
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

    def mark_deferred(
        self,
        remote_path: str,
        kind: str,
        host: Optional[str] = None,
        cooldown: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Optional[str]:
        """Файл не скачался по вине Яндекса — остаётся в очереди, не сбойный.

        Попытки (attempts) не тратятся: сервер хранения молчит не по вине файла.
        Превью, если было, остаётся лежать до появления оригинала.

        Ответ 5xx — особый случай: сервер жив, но именно этот файл не отдаёт.
        Такой файл получает паузу `cooldown` секунд (остальные качаются дальше),
        а после `limit` проходов подряд объявляется сгоревшим (`lost`) и выходит
        из очереди. Возвращает новый статус файла.
        """
        cooldown = SERVER_COOLDOWN_SEC if cooldown is None else int(cooldown)
        limit = SERVER_FAIL_LIMIT if limit is None else max(1, int(limit))
        if kind != "server_50x":
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
            return "pending"
        until = int(time.time()) + cooldown
        # Сгоревший файл получает свою, более долгую паузу: автоматический
        # возврат в очередь (return_lost) берёт только просроченные.
        lost_until = int(time.time()) + LOST_RETRY_SEC
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE files
                   SET server_fails = COALESCE(server_fails, 0) + 1,
                       status = CASE WHEN COALESCE(server_fails, 0) + 1 >= ?
                                     THEN 'lost' ELSE 'pending' END,
                       retry_after = CASE WHEN COALESCE(server_fails, 0) + 1 >= ?
                                          THEN ? ELSE ? END,
                       error = CASE WHEN COALESCE(server_fails, 0) + 1 >= ?
                                    THEN ? ELSE ? END,
                       storage_host = COALESCE(?, storage_host)
                 WHERE remote_path = ?
                """,
                (
                    limit, limit, lost_until, until, limit,
                    lost_marker(host), deferred_marker(kind, host),
                    host, remote_path,
                ),
            )
            row = conn.execute(
                "SELECT status FROM files WHERE remote_path = ?", (remote_path,)
            ).fetchone()
        return str(row["status"]) if row is not None else "pending"

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

    def reset_lost(self) -> int:
        """Возвращает «сгоревшие» файлы в очередь — по явному клику пользователя.

        Счётчик 5xx обнуляется: у файла снова три попытки. Без обнуления он
        вышел бы из очереди после первого же ответа 500.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE files SET status='pending', error=NULL, "
                "server_fails=0, retry_after=NULL WHERE status='lost'"
            )
            return cur.rowcount

    def return_lost(self, limit: int = LOST_RETURN_LIMIT) -> int:
        """Возвращает в очередь сгоревшие файлы, чья пауза уже прошла.

        Это не кнопка «Попробовать», а то, что делает каждый запуск: файл,
        прождавший полчаса, снова получает шанс. Счётчик 5xx НЕ обнуляем —
        сгоревший файл при первом же новом ответе 500 снова уйдёт в lost,
        а не потратит ещё три попытки. Успешная попытка всё равно скачает его.
        """
        now = int(time.time())
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE files
                   SET status='pending', error=NULL, retry_after=NULL
                 WHERE remote_path IN (
                       SELECT remote_path FROM files
                        WHERE status='lost'
                          AND (retry_after IS NULL OR retry_after <= ?)
                        ORDER BY COALESCE(retry_after, 0), rowid
                        LIMIT ?)
                """,
                (now, max(1, int(limit))),
            )
            return cur.rowcount

    def get_lost_count(self) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM files WHERE status='lost'"
        ).fetchone()
        return int(row["n"] or 0)

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
        # Файл на паузе после 5xx не берём: сервер уже ответил по нему, и
        # следующий проход подхватит его, когда пауза истечёт.
        return self._conn().execute(
            "SELECT remote_path, size, md5, error, storage_host FROM files "
            f"WHERE status='pending' "
            f"  AND (retry_after IS NULL OR retry_after <= ?) "
            f"ORDER BY {prefix}{order}",
            (int(time.time()),),
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
            "lost": 0,
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

    def get_statuses(self, paths: Iterable[str]) -> dict[str, str]:
        """Статусы для набора путей — одним запросом пачками по 500."""
        items = [str(p) for p in paths or []]
        out: dict[str, str] = {}
        for start in range(0, len(items), 500):
            chunk = items[start:start + 500]
            if not chunk:
                continue
            marks = ",".join("?" * len(chunk))
            rows = self._conn().execute(
                f"SELECT remote_path, status FROM files WHERE remote_path IN ({marks})",
                chunk,
            )
            for row in rows:
                out[row["remote_path"]] = row["status"]
        return out

    def get_files_under(
        self,
        prefixes: Iterable[str],
        statuses: tuple[str, ...] = ("pending", "failed", "lost"),
    ) -> list[sqlite3.Row]:
        """Файлы под указанными папками с интересующими статусами.

        Нужно выбору «скачать эту папку»: в базу попадают и вложенные файлы,
        а префикс пути позволяет взять их без обхода облака.
        """
        folders = [str(p).rstrip("/") for p in prefixes or [] if str(p).strip()]
        if not folders:
            return []
        allowed = tuple(statuses) or ("pending",)
        marks = ",".join("?" * len(allowed))
        out: list[sqlite3.Row] = []
        seen: set[str] = set()
        for folder in folders:
            escaped = (
                folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            rows = self._conn().execute(
                "SELECT remote_path, size, md5, status FROM files "
                f"WHERE status IN ({marks}) "
                "  AND (remote_path = ? OR remote_path LIKE ? ESCAPE '\\')",
                (*allowed, folder, escaped + "/%"),
            )
            for row in rows:
                if row["remote_path"] not in seen:
                    seen.add(row["remote_path"])
                    out.append(row)
        return out

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
