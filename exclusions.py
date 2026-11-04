"""Исключения из выгрузки обычного Диска.

Человек отмечает в дереве облака папки и файлы, которые не нужно скачивать.
Список хранится рядом с базами (`exclusions.json`) и применяется:

  * при обходе облака — папка не обходится, файл не ставится в очередь;
  * к уже известным строкам базы — они получают статус `excluded`;
  * при снятии исключения — строки возвращаются в `pending`.

Скачанные файлы не удаляются: исключение запрещает только новую загрузку.
Для Безлимита исключений нет — там выбор идёт календарём дат.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from typing import Iterable, Optional

import paths

FILE = paths.DATA_DIR / "exclusions.json"

# Статусы, которые исключение не имеет права переписать: файл уже на диске
# или удалён из облака. Исключение касается только будущих загрузок.
KEEP_STATUSES = ("downloaded", "skipped", "deleted")


def _norm(path: object) -> str:
    """Приводит путь облака к виду `disk:/папка/файл`.

    Пустая строка и корень Диска запрещены: исключить весь диск одним
    нажатием — это не то, чего ждёт человек.
    """
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("disk:"):
        raw = raw[len("disk:"):]
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "disk:/" + "/".join(parts)


def normalize_items(items: Iterable[str]) -> list[str]:
    """Нормализованный список без пустых путей и дублей, в исходном порядке."""
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        path = _norm(item)
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def load() -> list[str]:
    """Сохранённый список исключений. Битый файл не роняет программу."""
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return normalize_items(items)


def save(items: Iterable[str]) -> list[str]:
    """Атомарно записывает список и возвращает то, что реально сохранено."""
    clean = normalize_items(items)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"items": clean}, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix="exclusions-", suffix=".json",
                               dir=str(FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, FILE)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return clean


def is_excluded(remote_path: object, items: Optional[Iterable[str]] = None) -> bool:
    """Путь исключён целиком или лежит внутри исключённой папки."""
    path = _norm(remote_path)
    if not path:
        return False
    if items is None:
        items = load()
    for item in items:
        item = _norm(item)
        if not item:
            continue
        if path == item or path.startswith(item + "/"):
            return True
    return False


def sync_db(items: Optional[Iterable[str]] = None) -> dict:
    """Приводит статусы строк Диска в соответствие со списком исключений.

    Возвращает `{"excluded": сколько убрано из очереди,
    "returned": сколько вернулось в очередь}`. Скачанные не трогаются.
    """
    clean = normalize_items(items) if items is not None else load()
    db_file = paths.DB_FILE
    result = {"excluded": 0, "returned": 0}
    if not db_file.exists():
        return result
    con = sqlite3.connect(str(db_file), timeout=30)
    try:
        rows = list(con.execute("SELECT remote_path, status FROM files"))
        to_exclude: list[tuple[str]] = []
        to_return: list[tuple[str]] = []
        for remote_path, status in rows:
            status = str(status or "")
            covered = is_excluded(remote_path, clean)
            if covered and status not in KEEP_STATUSES and status != "excluded":
                to_exclude.append((str(remote_path),))
            elif not covered and status == "excluded":
                to_return.append((str(remote_path),))
        with con:
            if to_exclude:
                con.executemany(
                    "UPDATE files SET status = 'excluded', error = NULL "
                    "WHERE remote_path = ?",
                    to_exclude,
                )
            if to_return:
                con.executemany(
                    "UPDATE files SET status = 'pending', error = NULL "
                    "WHERE remote_path = ? AND status = 'excluded'",
                    to_return,
                )
        result["excluded"] = len(to_exclude)
        result["returned"] = len(to_return)
        return result
    finally:
        con.close()
