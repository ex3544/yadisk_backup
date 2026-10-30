"""Снимки баз: точка отсчёта и откат к ней."""

import sqlite3
import time

import pytest

import dbsnapshots
import paths


def _make_db(path, rows):
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE files (remote_path TEXT PRIMARY KEY, status TEXT)"
        )
        con.executemany(
            "INSERT INTO files (remote_path, status) VALUES (?, ?)", rows
        )
        con.commit()
    finally:
        con.close()


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return dict(con.execute("SELECT remote_path, status FROM files"))
    finally:
        con.close()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    disk = root / "state.db"
    unlim = root / "unlim_state.db"
    _make_db(disk, [("disk:/a.jpg", "pending")])
    _make_db(unlim, [("disk:/b.jpg", "downloaded")])
    monkeypatch.setattr(paths, "DATA_DIR", root)
    monkeypatch.setattr(paths, "DB_FILE", disk)
    monkeypatch.setattr(paths, "UNLIM_DB_FILE", unlim)
    return root


def test_snapshot_copies_both_databases(data_dir):
    made = dbsnapshots.make_snapshot()
    assert {item["kind"] for item in made} == {"state", "unlim"}
    files = sorted(p.name for p in (data_dir / "snapshots").glob("*.db"))
    assert len(files) == 2
    assert files[0].startswith("state-")
    assert files[1].startswith("unlim-")


def test_list_is_newest_first(data_dir):
    dbsnapshots.make_snapshot()
    time.sleep(0.01)
    dbsnapshots.make_snapshot()
    items = dbsnapshots.list_snapshots()
    assert len(items) == 4
    assert items[0]["mtime"] >= items[-1]["mtime"]


def test_restore_brings_back_the_snapshot_state(data_dir):
    dbsnapshots.make_snapshot()
    # После снимка в базе появился новый файл — откат должен его убрать.
    con = sqlite3.connect(paths.DB_FILE)
    con.execute("INSERT INTO files (remote_path, status) VALUES ('disk:/new.jpg', 'pending')")
    con.commit()
    con.close()
    snapshot = next(i for i in dbsnapshots.list_snapshots() if i["kind"] == "state")

    target = dbsnapshots.restore(snapshot["name"])
    assert target == paths.DB_FILE
    rows = _rows(paths.DB_FILE)
    assert "disk:/new.jpg" not in rows
    assert rows["disk:/a.jpg"] == "pending"


def test_restore_refuses_a_foreign_name(data_dir):
    with pytest.raises(ValueError):
        dbsnapshots.restore("../state.db")
    with pytest.raises(ValueError):
        dbsnapshots.restore("нет-такого.db")


def test_delete_removes_the_snapshot(data_dir):
    dbsnapshots.make_snapshot()
    snapshot = dbsnapshots.list_snapshots()[0]
    dbsnapshots.delete(snapshot["name"])
    names = [i["name"] for i in dbsnapshots.list_snapshots()]
    assert snapshot["name"] not in names


def test_keeps_only_five_newest(data_dir):
    for _ in range(7):
        dbsnapshots.make_snapshot()
        time.sleep(0.01)
    state_items = [i for i in dbsnapshots.list_snapshots() if i["kind"] == "state"]
    assert len(state_items) == dbsnapshots.KEEP
