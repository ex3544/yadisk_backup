"""Даты Безлимита: календарь дней и фильтр «качать только выбранные дни».

Зачем
-----
У Безлимита нет папок — есть плоская лента фотографий, нарезанная кластерами
(кусок ленты за один съёмочный день). Сергей просил выбирать для скачивания не
всю ленту, а нужные дни — как проводник папок у Диска.

Как устроен кластер
-------------------
`cluster_id` — строка `"<начало>_<конец>"`, где оба числа — unix-время в
миллисекундах, дополненное нулями до 19 знаков. Поэтому строковое сравнение
cluster_id совпадает с хронологическим: фильтр по дате — это обычное сравнение
диапазона и обычный индекс, без разбора строк в SQL.

Кластер может пересекать полночь (например, съёмка с 23:50 до 00:20). Днём
кластера считается день его **начала** — так один кластер не попадает в оба дня
и не удваивает счётчики.

Главное правило то же, что и у выбора папок Диска: фильтр только сужает
выборку, статусы в базе не меняются, невыбранное остаётся в очереди.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from utils import sqlite_ro_uri

DAY_MS = 24 * 60 * 60 * 1000
# Больше 55 лет дней в выборе — это уже не выбор, а вся база.
MAX_DAYS = 20000

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


# ── кластеры и даты ─────────────────────────────────────────────────────────


def parse_cluster_id(cluster_id) -> Optional[tuple[int, int]]:
    """`'0000001105196016000_0000001105198719000'` → (1105196016000, 1105198719000)."""
    text = str(cluster_id or "")
    if len(text) != 39 or text[19] != "_":
        return None
    start, end = text[:19], text[20:]
    if not (start.isdigit() and end.isdigit()):
        return None
    return int(start), int(end)


def cluster_start_ms(cluster_id) -> Optional[int]:
    parsed = parse_cluster_id(cluster_id)
    return parsed[0] if parsed else None


def ms_to_date(ms: int) -> _dt.date:
    """Миллисекунды → локальная дата (как её видит пользователь в календаре)."""
    return _dt.datetime.fromtimestamp(ms / 1000).date()


def date_to_ms(day: _dt.date) -> int:
    """Начало локальных суток в миллисекундах."""
    return int(_dt.datetime.combine(day, _dt.time.min).timestamp() * 1000)


def _norm_date(text) -> Optional[_dt.date]:
    value = str(text or "").strip()
    if not _DATE_RE.match(value):
        return None
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        return None


def merge_dates(dates: Iterable) -> list[list[str]]:
    """Список дней → смежные диапазоны `[[первый, последний], ...]`.

    Хранить диапазоны, а не тысячи отдельных дней, нужно ради SQL: один
    диапазон — одно сравнение, и индекс по cluster_id работает.
    """
    days = sorted({d for d in (_norm_date(x) for x in dates or []) if d is not None})
    out: list[list[str]] = []
    for day in days[:MAX_DAYS]:
        if out and day == _dt.date.fromisoformat(out[-1][1]) + _dt.timedelta(days=1):
            out[-1][1] = day.isoformat()
        else:
            out.append([day.isoformat(), day.isoformat()])
    return out


def expand_ranges(ranges: Iterable) -> list[str]:
    """Диапазоны → список отдельных дней (для окна)."""
    days: list[str] = []
    for item in ranges or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        first, last = _norm_date(item[0]), _norm_date(item[1])
        if not first or not last or last < first:
            continue
        while first <= last and len(days) < MAX_DAYS:
            days.append(first.isoformat())
            first += _dt.timedelta(days=1)
    return days


def clean_ranges(ranges: Iterable) -> list[list[str]]:
    """Проверяет и нормализует диапазоны: только реальные даты, без наложения."""
    days: list[str] = []
    for item in ranges or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        first, last = _norm_date(item[0]), _norm_date(item[1])
        if not first or not last or last < first:
            continue
        while first <= last and len(days) < MAX_DAYS:
            days.append(first.isoformat())
            first += _dt.timedelta(days=1)
    return merge_dates(days)


def range_clause(ranges: Iterable) -> tuple[str, list]:
    """SQL-условие по cluster_id и его параметры.

    Пустой выбор даёт `1=0`: случайно «пустой фильтр» не должен означать «всё».
    """
    parts: list[str] = []
    params: list[str] = []
    for item in clean_ranges(ranges):
        first = _dt.date.fromisoformat(item[0])
        after_last = _dt.date.fromisoformat(item[1]) + _dt.timedelta(days=1)
        parts.append("(cluster_id >= ? AND cluster_id < ?)")
        params.append(f"{date_to_ms(first):019d}_")
        params.append(f"{date_to_ms(after_last):019d}_")
    if not parts:
        return "1 = 0", []
    return "(" + " OR ".join(parts) + ")", params


# ── чтение базы (только чтение: календарь не мешает выгрузке) ───────────────


def _readonly(db_path: Path) -> Optional[sqlite3.Connection]:
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(sqlite_ro_uri(path), uri=True, timeout=15)
        con.row_factory = sqlite3.Row
        # Проверяем, что база вообще наша: без таблицы считать нечего.
        con.execute("SELECT 1 FROM unlim_files LIMIT 1").fetchone()
        return con
    except sqlite3.Error:
        return None


def _status_rows(con: sqlite3.Connection, ranges: Optional[list[list[str]]]) -> list[sqlite3.Row]:
    where = "cluster_id IS NOT NULL AND length(cluster_id) = 39"
    params: list = []
    if ranges is not None:
        clause, params = range_clause(ranges)
        where += f" AND {clause}"
    return list(con.execute(
        f"""
        SELECT substr(cluster_id, 1, 19) AS ms,
               COALESCE(status, 'pending') AS status,
               COUNT(*) AS files,
               COALESCE(SUM(size), 0) AS bytes
          FROM unlim_files
         WHERE {where}
         GROUP BY ms, status
        """,
        params,
    ))


def _empty_day(day: str) -> dict:
    return {
        "date": day,
        "files": 0,
        "bytes": 0,
        "downloaded": 0,
        "left": 0,
        "clusters": 0,
    }


def calendar(db_path: Path, month: str = "") -> dict:
    """Дни месяца с количеством файлов и статусами.

    День кластера — день его начала (локальное время). Дни без файлов в ответ
    не попадают: окно рисует пустые клетки само.
    """
    month = normalize_month(month)
    con = _readonly(db_path)
    if con is None:
        return {"month": month, "days": [], "totals": _empty_day("")}
    try:
        first = _dt.date.fromisoformat(month + "-01")
        after_last = (first.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
    except ValueError:
        return {"month": month, "days": [], "totals": _empty_day("")}
    ranges = [[first.isoformat(), (after_last - _dt.timedelta(days=1)).isoformat()]]
    try:
        rows = _status_rows(con, ranges)
    except sqlite3.Error:
        return {"month": month, "days": [], "totals": _empty_day("")}
    finally:
        con.close()

    by_day: dict[str, dict] = {}
    clusters: dict[str, set[int]] = {}
    for row in rows:
        ms = int(row["ms"])
        day = ms_to_date(ms).isoformat()
        if not day.startswith(month):
            continue
        item = by_day.setdefault(day, _empty_day(day))
        files = int(row["files"] or 0)
        item["files"] += files
        item["bytes"] += int(row["bytes"] or 0)
        if row["status"] == "downloaded":
            item["downloaded"] += files
        clusters.setdefault(day, set()).add(ms)
    for day, item in by_day.items():
        item["left"] = item["files"] - item["downloaded"]
        item["clusters"] = len(clusters[day])

    days = [by_day[d] for d in sorted(by_day)]
    totals = _empty_day("")
    for item in days:
        for key in ("files", "bytes", "downloaded", "left", "clusters"):
            totals[key] += item[key]
    return {"month": month, "days": days, "totals": totals}


def selection_stats(db_path: Path, ranges: Iterable) -> dict:
    """Сколько файлов попадает в выбранные дни и сколько из них уже скачано."""
    ranges = clean_ranges(ranges)
    stats = {
        "days": len(expand_ranges(ranges)),
        "ranges": ranges,
        "files": 0,
        "bytes": 0,
        "downloaded": 0,
        "left": 0,
        "clusters": 0,
    }
    if not ranges:
        return stats
    con = _readonly(db_path)
    if con is None:
        return stats
    try:
        rows = _status_rows(con, ranges)
    except sqlite3.Error:
        return stats
    finally:
        con.close()
    seen_clusters: set[int] = set()
    for row in rows:
        files = int(row["files"] or 0)
        stats["files"] += files
        stats["bytes"] += int(row["bytes"] or 0)
        if row["status"] == "downloaded":
            stats["downloaded"] += files
        seen_clusters.add(int(row["ms"]))
    stats["clusters"] = len(seen_clusters)
    stats["left"] = stats["files"] - stats["downloaded"]
    return stats


def normalize_month(month: str) -> str:
    """`2024-5` или мусор → текущий (или нормальный) месяц `ГГГГ-ММ`."""
    value = str(month or "").strip()
    if _MONTH_RE.match(value):
        try:
            _dt.date.fromisoformat(value + "-01")
            return value
        except ValueError:
            pass
    return _dt.date.today().strftime("%Y-%m")


def shift_month(month: str, delta: int) -> str:
    first = _dt.date.fromisoformat(normalize_month(month) + "-01")
    total = first.year * 12 + (first.month - 1) + delta
    return _dt.date(total // 12, total % 12 + 1, 1).strftime("%Y-%m")
