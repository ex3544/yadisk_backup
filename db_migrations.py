"""Версионирование схемы SQLite через `PRAGMA user_version`.

Каждая база (`state.db`, `unlim_state.db`) сама объявляет список миграций:
    MIGRATIONS = [(1, "CREATE TABLE ..."), (2, callable), ...]

Перед первой миграцией существующей базы автоматически делается резервная
копия рядом с файлом БД: `<имя>.bak_v<старая версия>_<дата>`. Свежая пустая
база копируется не будет — копировать нечего.

Миграции обязаны быть идемпотентными: если процесс упал после части SQL, но
до записи `user_version`, при следующем запуске они выполнятся повторно.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)

Migration = tuple[int, Union[str, Callable[[sqlite3.Connection], None]]]


def get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    if version < 0:
        raise ValueError("user_version не может быть отрицательным")
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _database_path(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list"):
        # (seq, name, file); у ':memory:' путь пустой
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def _has_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def iter_statements(sql: str) -> Iterable[str]:
    """Разбирает простой SQL-скрипт на отдельные операторы.

    Нужен, чтобы выполнять миграции внутри одной транзакции: `executescript()`
    неявно коммитит и ломает атомарность. Триггеры с `BEGIN ... END` здесь не
    поддерживаются — для схемы проекта они не нужны.
    """
    buf: list[str] = []
    for line in sql.splitlines(keepends=True):
        buf.append(line)
        candidate = "".join(buf)
        if sqlite3.complete_statement(candidate):
            stmt = candidate.strip()
            if stmt:
                yield stmt
            buf.clear()
    tail = "".join(buf).strip()
    if tail:
        yield tail


def execute_script(conn: sqlite3.Connection, sql: str) -> None:
    for statement in iter_statements(sql):
        conn.execute(statement)


def backup_database(conn: sqlite3.Connection, reason: str) -> Path | None:
    """Делает консистентную копию БД через sqlite3 backup API.

    Возвращает путь к копии или None, если база в памяти / ещё пустая.
    """
    db_path = _database_path(conn)
    if db_path is None or not db_path.exists() or not _has_tables(conn):
        return None

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak_{reason}_{stamp}")
    n = 1
    while backup_path.exists():
        backup_path = db_path.with_name(f"{db_path.name}.bak_{reason}_{stamp}_{n}")
        n += 1

    conn.commit()
    dst = sqlite3.connect(str(backup_path))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    log.info("Резервная копия БД: %s", backup_path)
    return backup_path


def migrate(
    conn: sqlite3.Connection,
    name: str,
    migrations: Iterable[Migration],
    *,
    make_backup: bool = True,
) -> dict:
    """Применяет недостающие миграции и возвращает сводку.

    `name` используется только в сообщениях. Повторный вызов безопасен.
    """
    ordered = sorted(migrations, key=lambda m: m[0])
    versions = [v for v, _ in ordered]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(
            f"{name}: версии миграций должны идти подряд с 1, получено {versions}"
        )

    start_version = current = get_user_version(conn)
    target = versions[-1] if versions else 0
    if current > target:
        raise RuntimeError(
            f"{name}: база новее программы (user_version={current}, максимум={target}). "
            "Обновите приложение."
        )

    applied: list[int] = []
    backup_path: Path | None = None
    db_path = _database_path(conn)
    if (
        current < target
        and make_backup
        and db_path is not None
        and db_path.exists()
        and _has_tables(conn)
    ):
        backup_path = backup_database(conn, reason=f"v{current}")
        if backup_path is None:
            raise RuntimeError(
                f"{name}: не удалось создать резервную копию перед миграцией — "
                "миграция отменена"
            )

    for version, migration in ordered:
        if version <= current:
            continue
        log.info("%s: миграция схемы %s → %s", name, version - 1, version)

        # BEGIN IMMEDIATE сериализует миграцию между процессами. Внутри
        # транзакции ещё раз проверяем версию: пока мы делали backup, другой
        # процесс мог уже применить эту же миграцию.
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = get_user_version(conn)
            if version <= current:
                conn.execute("ROLLBACK")
                continue
            if callable(migration):
                migration(conn)
            else:
                execute_script(conn, migration)
            set_user_version(conn, version)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        current = version
        applied.append(version)

    return {
        "name": name,
        "from_version": start_version,
        "to_version": current,
        "applied": applied,
        "backup": str(backup_path) if backup_path else None,
    }
