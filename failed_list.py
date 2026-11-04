"""Полный список файлов, которые ещё не выгружены.

Нужен, чтобы человек увидел конкретные имена, путь в облаке и дату (для
Безлимита) и мог найти файл вручную на сайте Яндекса. Модуль только читает
базы: ничего не меняет и не запускает задачи.

В список попадает всё, что ещё не скачано, — не только ошибки:
  * pending     — стоит в очереди;
  * in_progress — качается прямо сейчас;
  * failed      — ошибка загрузки файла Диска;
  * skipped     — неудачная попытка Безлимита;
  * lost        — файл ждёт серверы Яндекса, попытка будет позже;
  * denied      — сервер ответил 403, доступа нет;
  * deleted     — файла больше нет в облаке (404/410);
  * excluded    — исключён из выгрузки вручную.

Отчёты: TXT, CSV для Excel (ссылки через =HYPERLINK), XLSX с настоящими
гиперссылками и HTML для браузера.
"""

from __future__ import annotations

import csv
import html
import io
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Скачанное — единственный статус, который в отчёт не попадает.
SKIP_STATUSES = ("downloaded",)

STATUS_LABELS = {
    "pending": "в очереди",
    "in_progress": "качается сейчас",
    "failed": "ошибка загрузки",
    "skipped": "не скачался",
    "lost": "ждёт серверы Яндекса",
    "denied": "нет доступа (403)",
    "deleted": "нет в облаке",
    "excluded": "исключён из выгрузки",
}
_STATUS_ORDER = {name: i for i, name in enumerate(
    ("pending", "in_progress", "failed", "skipped", "lost", "denied",
     "deleted", "excluded"))}

_DATE_RE = re.compile(
    r"(20\d{2})-(\d{2})-(\d{2})[ _T](\d{2})[-:](\d{2})[-:](\d{2})")
_DATE_ONLY_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_MS_RE = re.compile(r"\d{13}")
_SEC_RE = re.compile(r"\d{10}")


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


def _rows(db_path, table: str, columns: str):
    con = _connect_readonly(db_path)
    if con is None:
        return []
    marks = ",".join("?" for _ in SKIP_STATUSES)
    try:
        return con.execute(
            f"SELECT {columns} FROM {table} "
            f"WHERE COALESCE(status, '') NOT IN ({marks})",
            SKIP_STATUSES,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _item(**kwargs) -> dict:
    item = {
        "name": "",
        "date": "",
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


def collect(disk_db, unlim_db, *_) -> dict:
    """Собирает все невыгруженные файлы обеих линий. Без ограничения."""
    disk_rows = _rows(
        disk_db, "files",
        "remote_path, size, status, error, COALESCE(attempts, 0) AS attempts",
    )
    unlim_rows = _rows(
        unlim_db, "unlim_files",
        "file_id, cluster_id, name, size, status, error, "
        "COALESCE(attempts, 0) AS attempts",
    )

    disk_items, unlim_items = [], []
    disk_counts, unlim_counts = {}, {}
    for row in disk_rows:
        status = row["status"] or ""
        disk_counts[status] = disk_counts.get(status, 0) + 1
        remote = row["remote_path"] or ""
        disk_items.append(_item(
            name=disk_name(remote),
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
        unlim_items.append(_item(
            name=name,
            date=unlim_date(name, row["cluster_id"] or ""),
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
        return {"total": len(items), "counts": counts, "items": items}

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
        "СПИСОК ФАЙЛОВ, КОТОРЫЕ ЕЩЁ НЕ ВЫГРУЖЕНЫ",
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
    "Линия", "Статус", "Имя", "Дата", "Путь в облаке",
    "Размер, байт", "Попыток", "Ошибка", "Ссылка",
)


def _csv_link(url: str) -> str:
    """Формула Excel: ячейка выглядит как «открыть» и ведёт по ссылке."""
    if not url:
        return ""
    return f'=HYPERLINK("{url}";"открыть")'


def render_csv(data: dict) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL,
                        lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for line_name, key in (("Обычный Диск", "disk"), ("Безлимит", "unlim")):
        for item in data[key]["items"]:
            writer.writerow((
                line_name, item["status_label"], item["name"], item["date"],
                item["cloud_path"], item["size"] or "", item["attempts"],
                item["error"], _csv_link(item["url"]),
            ))
    # BOM нужен Excel, иначе русские буквы превращаются в кракозябры.
    return "﻿" + buf.getvalue()


def render_xlsx(data: dict) -> bytes:
    """Книга Excel с настоящими гиперссылками (клик открывает браузер)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Что не скачалось"
    ws.append(list(CSV_HEADER))
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for line_name, key in (("Обычный Диск", "disk"), ("Безлимит", "unlim")):
        for item in data[key]["items"]:
            ws.append([
                line_name, item["status_label"], item["name"], item["date"],
                item["cloud_path"], item["size"] or "", item["attempts"],
                item["error"], "открыть" if item["url"] else "",
            ])
            if item["url"]:
                cell = ws.cell(row=ws.max_row, column=len(CSV_HEADER))
                cell.hyperlink = item["url"]
                cell.style = "Hyperlink"

    widths = (14, 22, 40, 20, 46, 14, 10, 40, 10)
    for idx, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_html(data: dict) -> str:
    """HTML-отчёт: ссылки кликабельны в любом браузере, без Excel."""
    e = html.escape

    def rows(section, with_date, with_url):
        out = []
        for item in section["items"]:
            link = ""
            if with_url and item["url"]:
                link = f'<a href="{e(item["url"])}" target="_blank" rel="noreferrer">открыть</a>'
            out.append(
                "<tr>"
                f"<td>{e(item['status_label'])}</td>"
                f"<td>{e(item['name'])}</td>"
                f"<td>{e(item['date']) if with_date else ''}</td>"
                f"<td>{e(item['cloud_path'])}</td>"
                f"<td class='num'>{e(item['size_text'])}</td>"
                f"<td class='num'>{item['attempts']}</td>"
                f"<td>{e(item['error'])}</td>"
                f"<td>{link}</td>"
                "</tr>"
            )
        return "".join(out)

    disk_rows = rows(data["disk"], False, True)
    unlim_rows = rows(data["unlim"], True, False)
    head = ("<tr><th>Статус</th><th>Имя</th><th>Дата</th><th>Путь в облаке</th>"
            "<th>Размер</th><th>Попыток</th><th>Ошибка</th><th>Ссылка</th></tr>")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Что не скачалось</title>
<style>
 body{{font:14px/1.45 Segoe UI,Arial,sans-serif;margin:24px;color:#1b1b1f}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:24px 0 8px}}
 p.meta{{color:#666;margin:0 0 16px}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}
 th{{background:#f4f4f6;position:sticky;top:0}}
 td.num{{text-align:right;white-space:nowrap}}
 a{{color:#0a84ff}}
</style></head><body>
<h1>Файлы, которые ещё не выгружены</h1>
<p class="meta">Сформирован: {e(data.get('generated_at', ''))} ·
Обычный Диск: {data['disk']['total']} · Безлимит: {data['unlim']['total']}</p>
<h2>Обычный Диск — {data['disk']['total']}</h2>
<table>{head}{disk_rows}</table>
<h2>Безлимит — {data['unlim']['total']}</h2>
<table>{head}{unlim_rows}</table>
</body></html>
"""


FORMATS = ("txt", "csv", "xlsx", "html")


def export_filename(fmt: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fmt = fmt if fmt in FORMATS else "txt"
    return f"failed-{stamp}.{fmt}"


def render(data: dict, fmt: str) -> bytes | str:
    if fmt == "csv":
        return render_csv(data)
    if fmt == "xlsx":
        return render_xlsx(data)
    if fmt == "html":
        return render_html(data)
    return render_txt(data)
