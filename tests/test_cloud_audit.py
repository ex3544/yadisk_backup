"""Сверка с облаком (35.7.6): сравнение, отчёты и безопасные действия."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

import cloud_audit
import paths


DISK_SCHEMA = """
CREATE TABLE files (
    remote_path TEXT PRIMARY KEY,
    size INTEGER,
    md5 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    local_path TEXT,
    error TEXT,
    downloaded_at TIMESTAMP,
    attempts INTEGER NOT NULL DEFAULT 0,
    storage_host TEXT,
    preview_path TEXT,
    retry_after INTEGER,
    server_fails INTEGER NOT NULL DEFAULT 0
);
"""

UNLIM_SCHEMA = """
CREATE TABLE unlim_files (
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
    downloaded_at TIMESTAMP,
    storage_host TEXT,
    preview_path TEXT,
    retry_after INTEGER,
    server_fails INTEGER DEFAULT 0
);
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    mapping = {
        "DB_FILE": data / "state.db",
        "UNLIM_DB_FILE": data / "unlim_state.db",
        "CLOUD_AUDIT_STATUS_FILE": data / "logs" / "cloud_audit_status.json",
        "CLOUD_AUDIT_REPORT_FILE": data / "logs" / "cloud_audit_report.txt",
        "CLOUD_AUDIT_CSV_FILE": data / "logs" / "cloud_audit.csv",
        "CLOUD_AUDIT_PLAN_FILE": data / "logs" / "cloud_audit_plan.json",
        "CLOUD_AUDIT_ACTIONS_FILE": data / "logs" / "cloud_audit_actions.json",
    }
    for name, value in mapping.items():
        monkeypatch.setattr(paths, name, value)
    (data / "logs").mkdir()
    return data


def make_disk_db(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute(DISK_SCHEMA)
    con.executemany(
        "INSERT INTO files (remote_path, size, md5, status, local_path) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def make_unlim_db(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute(UNLIM_SCHEMA)
    con.executemany(
        "INSERT INTO unlim_files (file_id, name, size, md5, status, local_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def row(path: Path, table: str, key_col: str, key: str) -> dict:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    out = con.execute(
        f"SELECT * FROM {table} WHERE {key_col} = ?", (key,)
    ).fetchone()
    con.close()
    return dict(out) if out else {}


# ── сравнение ───────────────────────────────────────────────────────────────


def test_compare_classifies_same_new_changed_and_gone():
    cloud = {
        "disk:/same.jpg": {"name": "same.jpg", "size": 10, "md5": "aa"},
        "disk:/new.jpg": {"name": "new.jpg", "size": 20, "md5": "bb"},
        "disk:/changed.jpg": {"name": "changed.jpg", "size": 30, "md5": "cc"},
        "disk:/returned.jpg": {"name": "returned.jpg", "size": 5, "md5": "dd"},
    }
    db = {
        "disk:/same.jpg": {"name": "same.jpg", "size": 10, "md5": "aa",
                           "status": "downloaded", "local": "C:/x/same.jpg"},
        "disk:/changed.jpg": {"name": "changed.jpg", "size": 11, "md5": "cc",
                              "status": "downloaded", "local": "C:/x/ch.jpg"},
        "disk:/gone.jpg": {"name": "gone.jpg", "size": 40, "md5": "ee",
                           "status": "downloaded", "local": "C:/x/gone.jpg"},
        "disk:/returned.jpg": {"name": "returned.jpg", "size": 5, "md5": "dd",
                               "status": "deleted", "local": "C:/x/ret.jpg"},
    }
    items = cloud_audit.compare("disk", cloud, db)
    kinds = {item["key"]: item["kind"] for item in items}
    assert kinds["disk:/same.jpg"] == "same"
    assert kinds["disk:/new.jpg"] == "new"
    assert kinds["disk:/changed.jpg"] == "changed"
    assert kinds["disk:/gone.jpg"] == "gone"
    # Файл, помеченный удалённым, но снова появившийся в облаке, — «новое».
    assert kinds["disk:/returned.jpg"] == "new"
    assert cloud_audit._counts(items) == {
        "same": 1, "new": 2, "changed": 1, "gone": 1,
    }


def test_compare_detects_md5_change_with_same_size():
    cloud = {"disk:/a.jpg": {"name": "a.jpg", "size": 10, "md5": "new"}}
    db = {"disk:/a.jpg": {"name": "a.jpg", "size": 10, "md5": "old",
                          "status": "downloaded", "local": None}}
    item = cloud_audit.compare("disk", cloud, db)[0]
    assert item["kind"] == "changed"
    assert "MD5" in item["detail"]


# ── полный проход ───────────────────────────────────────────────────────────


def test_run_writes_status_report_csv_and_plan(env, monkeypatch):
    make_disk_db(paths.DB_FILE, [
        ("disk:/same.jpg", 10, "aa", "downloaded", "C:/x/same.jpg"),
        ("disk:/gone.jpg", 40, "ee", "downloaded", "C:/x/gone.jpg"),
    ])
    make_unlim_db(paths.UNLIM_DB_FILE, [
        ("fid-1", "IMG_1.jpg", 7, "11", "downloaded", "C:/u/IMG_1.jpg"),
    ])
    monkeypatch.setattr(cloud_audit, "scan_disk_cloud", lambda *a, **k: {
        "disk:/same.jpg": {"name": "same.jpg", "size": 10, "md5": "aa"},
        "disk:/new.jpg": {"name": "new.jpg", "size": 20, "md5": "bb"},
        "disk:/changed.jpg": {"name": "changed.jpg", "size": 30, "md5": "cc"},
    })
    monkeypatch.setattr(cloud_audit, "scan_unlim_cloud", lambda *a, **k: {
        "fid-1": {"name": "IMG_1.jpg", "size": 7, "md5": "11"},
        "fid-2": {"name": "IMG_2.jpg", "size": 9, "md5": "22"},
    })

    data = cloud_audit.run("both")
    assert data["same"] == 2
    assert data["new"] == 3
    assert data["changed"] == 0
    assert data["gone"] == 1
    assert data["per_base"]["disk"] == {"cloud": 3, "db": 2}
    assert data["per_base"]["unlim"] == {"cloud": 2, "db": 1}

    status = cloud_audit.read_status()
    assert status["active"] is False
    assert status["stage"] == "done"
    assert status["new"] == 3
    assert len(status["new_items"]) == 3

    report = paths.CLOUD_AUDIT_REPORT_FILE.read_text(encoding="utf-8")
    assert "СВЕРКА С ОБЛАКОМ" in report
    assert "Обе базы" in report
    assert "➕ Новое в облаке: 3" in report
    assert "➖ Пропало из облака: 1" in report
    assert "действий не было" in report

    with open(paths.CLOUD_AUDIT_CSV_FILE, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    assert rows[0][0] == "база"
    assert len(rows) == 7  # заголовок + 6 файлов

    plan = json.loads(paths.CLOUD_AUDIT_PLAN_FILE.read_text(encoding="utf-8"))
    assert len(plan["items"]) == 6


def test_run_marks_failure_with_readable_reason(env, monkeypatch):
    def boom(*_a, **_k):
        raise cloud_audit.AuditError("Яндекс не принял токен")

    monkeypatch.setattr(cloud_audit, "scan_disk_cloud", boom)
    with pytest.raises(cloud_audit.AuditError):
        cloud_audit.run("disk")
    status = cloud_audit.read_status()
    assert status["active"] is False
    assert status["stage"] == "error"
    assert "Яндекс не принял токен" in status["error"]


# ── действия ────────────────────────────────────────────────────────────────


def _seed_audit(env, monkeypatch, *, disk_rows, disk_cloud):
    make_disk_db(paths.DB_FILE, disk_rows)
    monkeypatch.setattr(cloud_audit, "scan_disk_cloud", lambda *a, **k: disk_cloud)
    monkeypatch.setattr(cloud_audit, "scan_unlim_cloud", lambda *a, **k: {})
    return cloud_audit.run("disk")


def test_add_new_enqueues_new_files(env, monkeypatch):
    data = _seed_audit(env, monkeypatch, disk_rows=[], disk_cloud={
        "disk:/new.jpg": {"name": "new.jpg", "size": 20, "md5": "bb"},
    })
    assert data["new"] == 1

    result = cloud_audit.add_new()
    assert result["added"] == 1
    found = row(paths.DB_FILE, "files", "remote_path", "disk:/new.jpg")
    assert found["status"] == "pending"
    assert found["size"] == 20
    assert found["md5"] == "bb"


def test_add_new_renames_old_copy_and_requeues_changed(env, monkeypatch, tmp_path):
    old = tmp_path / "Отчёт.docx"
    old.write_bytes(b"old version")
    _seed_audit(env, monkeypatch, disk_rows=[
        ("disk:/Отчёт.docx", 11, "aa", "downloaded", str(old)),
    ], disk_cloud={
        "disk:/Отчёт.docx": {"name": "Отчёт.docx", "size": 99, "md5": "cc"},
    })

    result = cloud_audit.add_new()
    assert result["requeued"] == 1
    assert result["renamed"] == 1
    old_copy = tmp_path / "Отчёт_старая.docx"
    assert old_copy.exists()
    assert not old.exists()
    assert old_copy.read_bytes() == b"old version"
    found = row(paths.DB_FILE, "files", "remote_path", "disk:/Отчёт.docx")
    assert found["status"] == "pending"
    assert found["local_path"] is None
    assert found["attempts"] == 0

    report = paths.CLOUD_AUDIT_REPORT_FILE.read_text(encoding="utf-8")
    assert "Добавить новые в очередь" in report
    assert "старых копий переименовано 1" in report


def test_old_copy_path_adds_number_when_name_is_taken(tmp_path):
    src = tmp_path / "фото.jpg"
    src.write_bytes(b"1")
    (tmp_path / "фото_старая.jpg").write_bytes(b"2")
    assert cloud_audit.old_copy_path(str(src)) == str(tmp_path / "фото_старая_2.jpg")


def test_add_new_skips_changed_file_on_disconnected_drive(env, monkeypatch, tmp_path):
    old = tmp_path / "a.jpg"
    old.write_bytes(b"x")
    _seed_audit(env, monkeypatch, disk_rows=[
        ("disk:/a.jpg", 1, "aa", "downloaded", str(old)),
    ], disk_cloud={
        "disk:/a.jpg": {"name": "a.jpg", "size": 2, "md5": "bb"},
    })
    monkeypatch.setattr(cloud_audit, "volume_ready", lambda _p: False)

    result = cloud_audit.add_new()
    assert result["skipped"] == 1
    assert result["requeued"] == 0
    assert old.exists()
    found = row(paths.DB_FILE, "files", "remote_path", "disk:/a.jpg")
    assert found["status"] == "downloaded"


def test_mark_gone_marks_deleted_but_keeps_local_file(env, monkeypatch, tmp_path):
    local = tmp_path / "gone.jpg"
    local.write_bytes(b"keep me")
    _seed_audit(env, monkeypatch, disk_rows=[
        ("disk:/gone.jpg", 7, "aa", "downloaded", str(local)),
    ], disk_cloud={})

    data = cloud_audit.read_status()
    assert data["gone"] == 1

    result = cloud_audit.mark_gone()
    assert result["marked"] == 1
    found = row(paths.DB_FILE, "files", "remote_path", "disk:/gone.jpg")
    assert found["status"] == "deleted"
    assert found["error"] == "удалён из облака"
    assert found["local_path"] == str(local)
    assert local.read_bytes() == b"keep me"

    report = paths.CLOUD_AUDIT_REPORT_FILE.read_text(encoding="utf-8")
    assert "Пометить пропавшие" in report

    # Повторное нажатие ничего не добавляет: файл уже помечен.
    assert cloud_audit.mark_gone()["marked"] == 0


def test_actions_require_a_finished_audit(env):
    with pytest.raises(cloud_audit.AuditError):
        cloud_audit.add_new()
    with pytest.raises(cloud_audit.AuditError):
        cloud_audit.mark_gone()


def test_actions_refuse_while_audit_is_running(env, monkeypatch):
    cloud_audit._write_json(paths.CLOUD_AUDIT_STATUS_FILE, {
        "scope": "disk", "active": True, "started_at": 1,
    })
    with pytest.raises(cloud_audit.AuditError):
        cloud_audit.add_new()
