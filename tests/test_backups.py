"""Резервные копии баз: создание, список, восстановление."""

from __future__ import annotations

import sqlite3
import zipfile

import pytest

import backups


def _make_disk_db(path, rows=(("f1", "downloaded"), ("f2", "pending"))):
    con = sqlite3.connect(path)
    with con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS files (
                   remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,
                   status TEXT NOT NULL DEFAULT 'pending', local_path TEXT,
                   error TEXT, downloaded_at TIMESTAMP)"""
        )
        for name, status in rows:
            con.execute(
                "INSERT OR REPLACE INTO files (remote_path, size, status, local_path)"
                " VALUES (?, 10, ?, ?)",
                (name, status, f"C:/Disk/{name}"),
            )
        con.execute("PRAGMA user_version = 1")
    con.close()


def _make_unlim_db(path, rows=(("u1", "downloaded"), ("u2", "skipped"))):
    con = sqlite3.connect(path)
    with con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS unlim_files (
                   file_id TEXT PRIMARY KEY, cluster_id TEXT, name TEXT, size INTEGER,
                   md5 TEXT, mime TEXT, status TEXT DEFAULT 'pending',
                   local_path TEXT, error TEXT, attempts INTEGER DEFAULT 0,
                   worker_id INTEGER, assigned_at TIMESTAMP,
                   downloaded_at TIMESTAMP)"""
        )
        for fid, status in rows:
            con.execute(
                "INSERT OR REPLACE INTO unlim_files (file_id, name, size, status, local_path)"
                " VALUES (?, ?, 5, ?, ?)",
                (fid, f"{fid}.jpg", status, f"C:/Unlim/{fid}.jpg"),
            )
        con.execute("PRAGMA user_version = 1")
    con.close()


def _status(path, table, key):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            f"SELECT status FROM {table} WHERE {key}='f1' OR {key}='u1'"
        ).fetchall()
    finally:
        con.close()


def test_create_single_backup_and_list(tmp_path):
    db = tmp_path / "state.db"
    _make_disk_db(db)

    item = backups.create_backup(
        tmp_path, "disk", {"disk": db}, "35.5.0"
    )

    assert item["scope"] == "disk"
    assert item["databases"] == ["disk"]
    assert item["counts"]["downloaded"] == 1
    assert item["counts"]["pending"] == 1
    assert item["app_version"] == "35.5.0"
    assert (tmp_path / "backups" / item["id"]).is_file()

    listed = backups.list_backups(tmp_path)
    assert [x["id"] for x in listed] == [item["id"]]
    assert listed[0]["counts"]["total"] == 2


def test_combined_backup_contains_both_databases(tmp_path):
    disk = tmp_path / "state.db"
    unlim = tmp_path / "unlim_state.db"
    _make_disk_db(disk)
    _make_unlim_db(unlim)

    item = backups.create_backup(
        tmp_path, "all", {"disk": disk, "unlim": unlim}, "35.5.0"
    )

    assert item["scope"] == "all"
    assert item["databases"] == ["disk", "unlim"]
    assert item["counts"]["total"] == 4

    path = tmp_path / "backups" / item["id"]
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    assert {"disk.db", "unlim.db", "manifest.json"} <= names


def test_restore_returns_statuses_and_saves_pre_restore_copy(tmp_path):
    disk = tmp_path / "state.db"
    _make_disk_db(disk)
    item = backups.create_backup(tmp_path, "disk", {"disk": disk}, "35.5.0")

    # Пользователь пересоздал базу: статусы сброшены.
    con = sqlite3.connect(disk)
    with con:
        con.execute("UPDATE files SET status='pending', local_path=NULL")
    con.close()

    result = backups.restore_backup(
        tmp_path, item["id"], ["disk"], {"disk": disk}, "35.5.0"
    )

    assert result["restored"] == ["disk"]
    assert result["counts"]["disk"]["downloaded"] == 1
    rows = _status(disk, "files", "remote_path")
    assert ("downloaded",) in rows

    # Текущее (испорченное) состояние тоже сохранено — откат обратим.
    pre = tmp_path / "backups" / result["pre_backups"][0]
    assert pre.is_file()
    con = sqlite3.connect(pre)
    try:
        assert con.execute(
            "SELECT status FROM files WHERE remote_path='f1'"
        ).fetchone()[0] == "pending"
    finally:
        con.close()


def test_restore_combined_can_pick_one_database(tmp_path):
    disk = tmp_path / "state.db"
    unlim = tmp_path / "unlim_state.db"
    _make_disk_db(disk)
    _make_unlim_db(unlim)
    item = backups.create_backup(
        tmp_path, "all", {"disk": disk, "unlim": unlim}, "35.5.0"
    )

    con = sqlite3.connect(disk)
    with con:
        con.execute("DELETE FROM files")
    con.close()
    con = sqlite3.connect(unlim)
    with con:
        con.execute("DELETE FROM unlim_files")
    con.close()

    backups.restore_backup(
        tmp_path, item["id"], ["disk"], {"disk": disk, "unlim": unlim}, "35.5.0"
    )

    con = sqlite3.connect(disk)
    assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
    con.close()
    con = sqlite3.connect(unlim)
    assert con.execute("SELECT COUNT(*) FROM unlim_files").fetchone()[0] == 0
    con.close()


def test_restore_refuses_newer_schema_and_keeps_target(tmp_path):
    disk = tmp_path / "state.db"
    _make_disk_db(disk)
    item = backups.create_backup(tmp_path, "disk", {"disk": disk}, "35.5.0")

    # Имитируем копию из будущей версии программы.
    backup_path = tmp_path / "backups" / item["id"]
    con = sqlite3.connect(backup_path)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()

    con = sqlite3.connect(disk)
    with con:
        con.execute("DELETE FROM files")
    con.close()

    with pytest.raises(backups.BackupError):
        backups.restore_backup(
            tmp_path, item["id"], ["disk"], {"disk": disk}, "35.5.0"
        )

    con = sqlite3.connect(disk)
    assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    con.close()


def test_restore_refuses_corrupted_backup(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    bad = folder / "disk-20261027-153000-manual.db"
    bad.write_bytes(b"not a sqlite database")
    disk = tmp_path / "state.db"
    _make_disk_db(disk)

    with pytest.raises(backups.BackupError):
        backups.restore_backup(
            tmp_path, bad.name, ["disk"], {"disk": disk}, "35.5.0"
        )


def test_restore_rejects_path_traversal(tmp_path):
    disk = tmp_path / "state.db"
    _make_disk_db(disk)
    with pytest.raises(backups.BackupError):
        backups.restore_backup(
            tmp_path, "../../state.db", ["disk"], {"disk": disk}, "35.5.0"
        )


def test_auto_backups_are_pruned_but_manual_kept(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    for i in range(7):
        (folder / f"disk-2026102{i}-120000-before-restore.db").write_bytes(b"x")
    (folder / "disk-20261020-120000-manual.db").write_bytes(b"x")

    backups._prune_auto(tmp_path, keep=5)

    left = sorted(p.name for p in folder.iterdir())
    assert len([n for n in left if "before-restore" in n]) == 5
    assert "disk-20261020-120000-manual.db" in left


def test_legacy_migration_backup_is_listed(tmp_path):
    legacy = tmp_path / "state.db.bak_v0_20261027_031703"
    _make_disk_db(legacy)

    items = backups.list_backups(tmp_path)

    assert len(items) == 1
    assert items[0]["legacy"] is True
    assert items[0]["reason"] == "migration"
    assert items[0]["counts"]["total"] == 2


def test_delete_removes_only_known_backup(tmp_path):
    disk = tmp_path / "state.db"
    _make_disk_db(disk)
    item = backups.create_backup(tmp_path, "disk", {"disk": disk}, "35.5.0")

    backups.delete_backup(tmp_path, item["id"])

    assert not (tmp_path / "backups" / item["id"]).exists()
    with pytest.raises(backups.BackupError):
        backups.delete_backup(tmp_path, "нет-такой-копии.db")
