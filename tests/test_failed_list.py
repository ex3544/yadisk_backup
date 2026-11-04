"""Список «что не скачалось»: путь, дата Безлимита, ссылка и экспорт."""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from pathlib import Path

import failed_list


def _make_dbs(tmp_path: Path) -> tuple[Path, Path]:
    disk_db = tmp_path / "state.db"
    unlim_db = tmp_path / "unlim_state.db"

    con = sqlite3.connect(disk_db)
    con.execute("""CREATE TABLE files (
        remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,
        status TEXT, local_path TEXT, error TEXT, downloaded_at TIMESTAMP,
        attempts INTEGER DEFAULT 0, server_fails INTEGER DEFAULT 0,
        retry_after INTEGER, storage_host TEXT, preview_path TEXT)""")
    con.executemany(
        "INSERT INTO files (remote_path, size, status, error, attempts) "
        "VALUES (?,?,?,?,?)",
        [
            ("disk:/Фото/снимок 1.jpg", 2048, "failed", "HTTP 500", 3),
            ("disk:/a.jpg", 10, "deleted", "PathNotFound", 3),
            ("disk:/lost.bin", 100, "lost", "сервер не отвечает", 0),
            ("disk:/ok.jpg", 5, "downloaded", "", 1),
        ],
    )
    con.commit()
    con.close()

    con = sqlite3.connect(unlim_db)
    con.execute("""CREATE TABLE unlim_files (
        file_id TEXT PRIMARY KEY, cluster_id TEXT, name TEXT, size INTEGER,
        md5 TEXT, mime TEXT, status TEXT, local_path TEXT, error TEXT,
        attempts INTEGER DEFAULT 0, worker_id INTEGER, assigned_at TIMESTAMP,
        downloaded_at TIMESTAMP, server_fails INTEGER DEFAULT 0,
        retry_after INTEGER, storage_host TEXT, preview_path TEXT)""")
    con.executemany(
        "INSERT INTO unlim_files (file_id, cluster_id, name, size, status, "
        "error, attempts) VALUES (?,?,?,?,?,?,?)",
        [
            ("fid1", "0000001791626926000_abc", "2026-10-10 13-09-22.JPG",
             1024, "skipped", "mpfs/url HTTP 500", 4),
            ("fid2", "0000001600000000000_xyz", "no-date.mp4",
             4096, "denied", "HTTP 403", 3),
            ("fid3", "0000001791626926000_q", "done.jpg",
             1, "downloaded", "", 1),
        ],
    )
    con.commit()
    con.close()
    return disk_db, unlim_db


def test_collect_keeps_failed_and_waiting_but_not_downloaded(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(
        disk_db, unlim_db, r"D:\Backup\Disk", r"D:\Backup\Unlim")

    assert data["disk"]["total"] == 3
    assert data["disk"]["counts"] == {"failed": 1, "lost": 1, "deleted": 1}
    assert data["unlim"]["total"] == 2
    assert data["unlim"]["counts"] == {"skipped": 1, "denied": 1}

    first = data["disk"]["items"][0]
    assert first["name"] == "снимок 1.jpg"
    assert first["local_path"] == os.path.join(r"D:\Backup\Disk", "Фото",
                                               "снимок 1.jpg")
    assert first["url"].startswith("https://disk.yandex.ru/client/disk/")
    assert " " not in first["url"]  # пробел закодирован
    assert first["status_label"] == "ошибка загрузки"


def test_unlim_date_from_name_and_cluster_id(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(
        disk_db, unlim_db, "D:/Backup/Disk", "D:/Backup/Unlim")
    by_name = {it["name"]: it for it in data["unlim"]["items"]}

    assert by_name["2026-10-10 13-09-22.JPG"]["date"] == "2026-10-10 13:09:22"
    assert by_name["no-date.mp4"]["date"].startswith("2020-")
    assert by_name["2026-10-10 13-09-22.JPG"]["local_path"] == os.path.join(
        "D:/Backup/Unlim", "2026-10-10 13-09-22.JPG")


def test_limit_limits_items_but_not_total(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(
        disk_db, unlim_db, "D:/Disk", "D:/Unlim", limit=1)
    assert data["disk"]["total"] == 3
    assert len(data["disk"]["items"]) == 1


def test_missing_databases_return_empty_sections(tmp_path):
    data = failed_list.collect(
        tmp_path / "none.db", tmp_path / "none2.db", "D:/Disk", "D:/Unlim")
    assert data["disk"]["total"] == 0
    assert data["unlim"]["total"] == 0
    assert data["disk"]["items"] == []


def test_txt_contains_paths_dates_and_links(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(
        disk_db, unlim_db, r"D:\Backup\Disk", r"D:\Backup\Unlim")
    text = failed_list.render_txt(data)

    assert "ОБЫЧНЫЙ ДИСК" in text
    assert "БЕЗЛИМИТ" in text
    assert "куда: " in text
    assert "2026-10-10 13:09:22" in text
    assert "https://disk.yandex.ru/client/disk/" in text
    assert "ошибка загрузки" in text


def test_csv_is_excel_friendly(tmp_path):
    disk_db, unlim_db = _make_dbs(tmp_path)
    data = failed_list.collect(
        disk_db, unlim_db, r"D:\Backup\Disk", r"D:\Backup\Unlim")
    text = failed_list.render_csv(data)

    assert text.startswith("\ufeff")
    assert "Линия;Статус;Имя" in text
    assert "Обычный Диск;ошибка загрузки;снимок 1.jpg" in text
    assert "Безлимит;не скачался;2026-10-10 13-09-22.JPG" in text

    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
    assert rows[0][0] == "Линия"
    assert len(rows) == 6  # шапка + 3 Диска + 2 Безлимита
