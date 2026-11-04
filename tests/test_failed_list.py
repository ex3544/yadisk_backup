"""Полный список невыгруженных файлов: пути, дата, ссылки и форматы."""

from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

import failed_list


def _make_dbs(tmp_path: Path) -> tuple[Path, Path]:
    disk_db = tmp_path / "state.db"
    unlim_db = tmp_path / "unlim_state.db"

    con = sqlite3.connect(disk_db)
    con.execute("""CREATE TABLE files (
        remote_path TEXT PRIMARY KEY, size INTEGER, status TEXT,
        error TEXT, attempts INTEGER)""")
    con.executemany(
        "INSERT INTO files (remote_path, size, status, error, attempts) "
        "VALUES (?,?,?,?,?)",
        [
            ("disk:/Фото/снимок 1.jpg", 2048, "failed", "HTTP 500", 3),
            ("disk:/a.jpg", 10, "deleted", "PathNotFound", 3),
            ("disk:/lost.bin", 100, "lost", "сервер не отвечает", 0),
            ("disk:/ok.jpg", 5, "downloaded", "", 1),
            ("disk:/wait.jpg", 7, "pending", "", 0),
            ("disk:/skip.txt", 8, "excluded", "", 0),
            ("disk:/now.jpg", 9, "in_progress", "", 1),
        ],
    )
    con.commit()
    con.close()

    con = sqlite3.connect(unlim_db)
    con.execute("""CREATE TABLE unlim_files (
        file_id TEXT PRIMARY KEY, cluster_id TEXT, name TEXT, size INTEGER,
        status TEXT, error TEXT, attempts INTEGER)""")
    con.executemany(
        "INSERT INTO unlim_files (file_id, cluster_id, name, size, status, "
        "error, attempts) VALUES (?,?,?,?,?,?,?)",
        [
            ("fid1", "0000001791626926000_abc", "2026-10-10 13-09-22.JPG",
             1024, "skipped", "mpfs HTTP 500", 4),
            ("fid2", "0000001600000000000_xyz", "no-date.mp4",
             4096, "denied", "HTTP 403", 3),
            ("fid3", "0000001791626926000_q", "done.jpg",
             1, "downloaded", "", 1),
            ("fid4", "0000001791626926000_p", "wait.jpg",
             2, "pending", "", 0),
        ],
    )
    con.commit()
    con.close()
    return disk_db, unlim_db


def test_collect_keeps_everything_not_downloaded(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(disk_db, unlim_db)

    assert data["disk"]["total"] == 6
    assert data["disk"]["counts"] == {
        "failed": 1, "deleted": 1, "lost": 1,
        "pending": 1, "excluded": 1, "in_progress": 1,
    }
    assert data["unlim"]["total"] == 3
    assert data["unlim"]["counts"] == {"skipped": 1, "denied": 1, "pending": 1}

    by_name = {it["name"]: it for it in data["disk"]["items"]}
    assert "ok.jpg" not in by_name  # скачанное в список не попадает
    assert by_name["снимок 1.jpg"]["status_label"] == "ошибка загрузки"
    assert by_name["wait.jpg"]["status_label"] == "в очереди"
    assert by_name["now.jpg"]["status_label"] == "качается сейчас"
    assert by_name["снимок 1.jpg"]["url"].startswith(
        "https://disk.yandex.ru/client/disk/")
    assert " " not in by_name["снимок 1.jpg"]["url"]
    # Пути на диске в отчёт больше не идут.
    assert "local_path" not in by_name["снимок 1.jpg"]


def test_unlim_date_from_name_and_cluster_id(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(disk_db, unlim_db)
    by_name = {it["name"]: it for it in data["unlim"]["items"]}

    assert by_name["2026-10-10 13-09-22.JPG"]["date"] == "2026-10-10 13:09:22"
    assert by_name["no-date.mp4"]["date"].startswith("2020-")
    assert by_name["2026-10-10 13-09-22.JPG"]["url"] == ""


def test_missing_databases_return_empty_sections(tmp_path):
    data = failed_list.collect(tmp_path / "none.db", tmp_path / "none2.db")
    assert data["disk"]["total"] == 0
    assert data["unlim"]["total"] == 0
    assert data["disk"]["items"] == []


def test_txt_has_no_save_path_but_has_cloud_path_and_link(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(disk_db, unlim_db)
    text = failed_list.render_txt(data)

    assert "ОБЫЧНЫЙ ДИСК" in text and "БЕЗЛИМИТ" in text
    assert "куда:" not in text
    assert "в облаке: disk:/Фото/снимок 1.jpg" in text
    assert "2026-10-10 13:09:22" in text
    assert "https://disk.yandex.ru/client/disk/" in text


def test_csv_is_excel_friendly_and_has_hyperlink_formula(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(disk_db, unlim_db)
    text = failed_list.render_csv(data)

    assert text.startswith("﻿")
    assert "Линия;Статус;Имя;Дата;Путь в облаке;Размер, байт;Попыток;Ошибка;Ссылка" in text
    assert "Куда сохранить" not in text
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    disk_rows = [r for r in rows[1:] if r[0] == "Обычный Диск"]
    first = next(r for r in disk_rows if r[2] == "снимок 1.jpg")
    assert first[-1].startswith('=HYPERLINK("https://disk.yandex.ru/')
    assert first[-1].endswith(';"открыть")')


def test_xlsx_has_real_hyperlinks(tmp_path):
    from openpyxl import load_workbook

    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(disk_db, unlim_db)
    wb = load_workbook(io.BytesIO(failed_list.render_xlsx(data)))
    ws = wb.active

    assert ws["A1"].value == "Линия"
    headers = [c.value for c in ws[1]]
    assert "Куда сохранить" not in headers
    link_col = headers.index("Ссылка") + 1
    linked = [row for row in ws.iter_rows(min_row=2) if row[link_col - 1].hyperlink]
    assert linked
    assert linked[0][link_col - 1].hyperlink.target.startswith(
        "https://disk.yandex.ru/client/disk/")


def test_html_report_has_clickable_links(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(disk_db, unlim_db)
    page = failed_list.render_html(data)

    assert page.startswith("<!doctype html>")
    assert '<a href="https://disk.yandex.ru/client/disk/' in page
    assert "target=\"_blank\"" in page
    assert "Куда сохранить" not in page


def test_export_filenames_for_all_formats():
    names = {fmt: failed_list.export_filename(fmt) for fmt in failed_list.FORMATS}
    assert names["txt"].endswith(".txt")
    assert names["csv"].endswith(".csv")
    assert names["xlsx"].endswith(".xlsx")
    assert names["html"].endswith(".html")
    assert names["txt"].startswith("failed-")
