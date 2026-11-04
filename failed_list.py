"""Список файлов, которые не удалось скачать.

Нужен, чтобы человек мог увидеть конкретные имена, путь на диске и дату
(для Безлимита) и попробовать найти файл вручную на сайте Яндекса. Модуль
только читает базы: ничего не меняет и не запускает задачи.

Статусы намеренно не сводятся к одному «failed»:
  * failed   — обычная ошибка загрузки файла Диска;
  * skipped  — неудачная попытка Безлимита;
  * lost     — файл ждёт серверы Яндекса, попытка будет позже;
  * denied   — сервер ответил 403, доступа нет;
  * deleted  — файла больше нет в облаке (404/410).
"""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

DISK_STATUSES = ("failed", "lost", "denied", "deleted")
UNLIM_STATUSES = ("skipped", "failed", "lost", "denied", "deleted")

STATUS_LABELS = {
    "failed": "ошибка загрузки",
    "skipped": "не скачался",
    "lost": "ждёт серверы Яндекса",
    "denied": "нет доступа (403)",
    "deleted": "нет в облаке",
}
_STATUS_ORDER = {name: i for i, name in enumerate(
    ("failed", "skipped", "lost", "denied", "deleted"))}

_DATE_RE = re.compile(
    r"(20\d{2})-(\d{2})-(\d{2})[ _T](\d{2})[-:](\d{2})[-:](\d{2})")
_DATE_ONLY_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_MS_RE = re.compile(r"\d{13}")
_SEC_RE = re.compile(r"\d{10}")
_EXT_RE = re.compile(r"\.[A-Za-zА-Яа-я0-9]{1,8}$")


def _human_size(size) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}".replace(".", ",")


def disk_relative(remote_path: str) -> str:
    """Путь внутри облака без префикса disk:/ и ведущего слэша."""
    path = (remote_path or "").strip()
    if path.startswith("disk:/"):
        path = path[6:]
    elif path.startswith("disk:"):
        path = path[5:]
    return path.lstrip("/")


def disk_local_path(remote_path: str, disk_dir: str) -> str:
    rel = disk_relative(remote_path)
    if not rel:
        return str(disk_dir)
    return os.path.join(str(disk_dir), *rel.split("/"))


def disk_name(remote_path: str) -> str:
    rel = disk_relative(remote_path)
    return rel.rsplit("/", 1)[-1] if rel else (remote_path or "")


def disk_web_url(remote_path: str) -> str:
    """Ссылка на файл в веб-клиенте Яндекс.Диска.

    Для Безлимита такой ссылки нет: это отдельный раздел «Фото».
    """
    rel = disk_relative(remote_path)
    base = "https://disk.yandex.ru/client/disk"
    if not rel:
        return base
    return base + "/" + quote(rel, safe="/")


def unlim_date(name: str, cluster_id: str = "") -> str:
    """Дата съёмки/загрузки Безлимита: из имени файла, иначе из cluster_id."""
    text = name or ""
    m = _DATE_RE.search(text)
    if m:
        y, mo, d, h, mi, s = m.groups()
        return f"{y}-{mo}-{d} {h}:{mi}:{s}"
    m = _DATE_ONLY_RE.search(text)
    if m:
        return "-".join(m.groups())
    digits = re.sub(r"\D", "", cluster_id or "").lstrip("0")
    for pattern, div in ((_MS_RE, 1000.0), (_SEC_RE, 1.0)):
        m = pattern.search(digits)
        if not m:
            continue
        try:
            moment = datetime.fromtimestamp(int(m.group(0)) / div)
        except (OverflowError, OSError, ValueError):
            continue
        if 2000 <= moment.year <= 2100:
            return moment.strftime("%Y-%m-%d %H:%M:%S")
    return ""


def _connect_readonly(db_path) -> sqlite3.Connection | None:
    path = Path(str(db_path))
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _rows(db_path, table: str, columns: str, statuses: tuple[str, ...]):
    con = _connect_readonly(db_path)
    if con is None:
        return []
    marks = ",".join("?" for _ in statuses)
    try:
        return con.execute(
            f"SELECT {columns} FROM {table} WHERE status IN ({marks})",
            statuses,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _item(**kwargs) -> dict:
    item = {
        "name": "",
        "date": "",
        "local_path": "",
        "cloud_path": "",
        "size": 0,
        "size_text": "—",
        "status": "",
        "status_label": "",
        "error": "",
        "attempts": 0,
        "url": "",
    }
    item.update(kwargs)
    item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    item["size_text"] = _human_size(item.get("size"))
    item["error"] = (item.get("error") or "")[:300]
    return item


def collect(disk_db, unlim_db, disk_dir, unlim_dir, limit: int | None = None) -> dict:
    """Собирает не скачанные файлы обеих линий. limit — на каждую линию."""
    disk_rows = _rows(
        disk_db, "files",
        "remote_path, size, status, local_path, error, "
        "COALESCE(attempts, 0) AS attempts",
        DISK_STATUSES,
    )
    unlim_rows = _rows(
        unlim_db, "unlim_files",
        "file_id, cluster_id, name, size, status, local_path, error, "
        "COALESCE(attempts, 0) AS attempts",
        UNLIM_STATUSES,
    )

    disk_items, unlim_items = [], []
    disk_counts, unlim_counts = {}, {}
    for row in disk_rows:
        status = row["status"] or ""
        disk_counts[status] = disk_counts.get(status, 0) + 1
        remote = row["remote_path"] or ""
        local = row["local_path"] or disk_local_path(remote, disk_dir)
        disk_items.append(_item(
            name=disk_name(remote),
            local_path=local,
            cloud_path=remote,
            size=row["size"] or 0,
            status=status,
            error=row["error"] or "",
            attempts=row["attempts"] or 0,
            url=disk_web_url(remote),
        ))

    for row in unlim_rows:
        status = row["status"] or ""
        unlim_counts[status] = unlim_counts.get(status, 0) + 1
        name = row["name"] or row["file_id"] or ""
        local = row["local_path"] or os.path.join(str(unlim_dir), name)
        unlim_items.append(_item(
            name=name,
            date=unlim_date(name, row["cluster_id"] or ""),
            local_path=local,
            cloud_path=name,
            size=row["size"] or 0,
            status=status,
            error=row["error"] or "",
            attempts=row["attempts"] or 0,
        ))

    def order(item):
        return (_STATUS_ORDER.get(item["status"], 99),
                item["date"] or item["name"].lower())

    disk_items.sort(key=order)
    unlim_items.sort(key=order)

    def section(items, counts):
        return {
            "total": len(items),
            "counts": counts,
            "items": items[:limit] if limit else items,
        }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "disk": section(disk_items, disk_counts),
        "unlim": section(unlim_items, unlim_counts),
    }


def _counts_text(section: dict) -> str:
    if not section["counts"]:
        return "нет"
    parts = [f"{STATUS_LABELS.get(k, k)}: {v}"
             for k, v in sorted(section["counts"].items(),
                                key=lambda kv: _STATUS_ORDER.get(kv[0], 99))]
    return f"{section['total']} ({', '.join(parts)})"


def render_txt(data: dict) -> str:
    lines = [
        "СПИСОК ФАЙЛОВ, КОТОРЫЕ НЕ УДАЛОСЬ СКАЧАТЬ",
        f"Сформирован: {data.get('generated_at', '')}",
        f"Обычный Диск: {_counts_text(data['disk'])}",
        f"Безлимит: {_counts_text(data['unlim'])}",
        "",
    ]

    def block(title, section, with_date, with_url):
        lines.append("=" * 78)
        lines.append(title)
        lines.append("=" * 78)
        if not section["items"]:
            lines.append("Нет файлов.")
            lines.append("")
            return
        for n, item in enumerate(section["items"], 1):
            head = f"{n}. [{item['status_label']}]"
            if with_date and item["date"]:
                head += f" {item['date']}"
            head += f" {item['name']}"
            lines.append(head)
            lines.append(f"   куда: {item['local_path']}")
            if item["cloud_path"]:
                lines.append(f"   в облаке: {item['cloud_path']}")
            lines.append(f"   размер: {item['size_text']} · попыток: {item['attempts']}")
            if item["error"]:
                lines.append(f"   ошибка: {item['error']}")
            if with_url and item["url"]:
                lines.append(f"   ссылка: {item['url']}")
            lines.append("")

    block("ОБЫЧНЫЙ ДИСК", data["disk"], False, True)
    block("БЕЗЛИМИТ", data["unlim"], True, False)
    return "\n".join(lines).rstrip() + "\n"


CSV_HEADER = (
    "Линия", "Статус", "Имя", "Дата", "Куда сохранить", "Путь в облаке",
    "Размер, байт", "Попыток", "Ошибка", "Ссылка",
)


def render_csv(data: dict) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL,
                        lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for line_name, key in (("Обычный Диск", "disk"), ("Безлимит", "unlim")):
        for item in data[key]["items"]:
            writer.writerow((
                line_name, item["status_label"], item["name"], item["date"],
                item["local_path"], item["cloud_path"], item["size"] or "",
                item["attempts"], item["error"], item["url"],
            ))
    # BOM нужен Excel, иначе русские буквы превращаются в кракозябры.
    return "﻿" + buf.getvalue()


def export_filename(fmt: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"failed-{stamp}.{'csv' if fmt == 'csv' else 'txt'}"
