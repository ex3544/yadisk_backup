"""Исключения из выгрузки: нормализация, файл, статусы базы."""

import json
import sqlite3

import pytest

import exclusions
import paths


@pytest.fixture
def data(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setattr(paths, "DB_FILE", db)
    monkeypatch.setattr(exclusions, "FILE", tmp_path / "exclusions.json")
    return tmp_path


def _make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, "
        "md5 TEXT, status TEXT, local_path TEXT, error TEXT)"
    )
    con.executemany(
        "INSERT INTO files (remote_path, size, md5, status, local_path, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


# ── нормализация ────────────────────────────────────────────────────────────


def test_normalize_items_fixes_slashes_and_drops_root():
    items = exclusions.normalize_items([
        "disk:/Photos/", "Photos", "", "disk:/", "/", "../secret",
        "disk:/Photos", "disk:/Videos\\2024",
    ])
    assert items == ["disk:/Photos", "disk:/Videos/2024"]


def test_is_excluded_covers_folder_and_exact_file():
    items = ["disk:/Photos"]
    assert exclusions.is_excluded("disk:/Photos", items)
    assert exclusions.is_excluded("disk:/Photos/2024/a.jpg", items)
    assert not exclusions.is_excluded("disk:/Photoshop/a.jpg", items)
    assert exclusions.is_excluded("disk:/a/b.txt", ["disk:/a/b.txt"])
    assert not exclusions.is_excluded("disk:/a/b.tx", ["disk:/a/b.txt"])


def test_is_excluded_ignores_invalid_paths():
    assert not exclusions.is_excluded("", ["disk:/Photos"])
    assert not exclusions.is_excluded("disk:/", ["disk:/Photos"])
    assert not exclusions.is_excluded(None, ["disk:/Photos"])


# ── файл исключений ─────────────────────────────────────────────────────────


def test_save_and_load_roundtrip(data):
    saved = exclusions.save(["disk:/Photos", "Photos\\2024", "disk:/Photos"])
    assert saved == ["disk:/Photos", "disk:/Photos/2024"]
    assert exclusions.load() == saved
    payload = json.loads(exclusions.FILE.read_text(encoding="utf-8"))
    assert payload == {"items": saved}


def test_load_tolerates_broken_file(data):
    exclusions.FILE.write_text("{не json", encoding="utf-8")
    assert exclusions.load() == []
    exclusions.FILE.write_text(json.dumps({"items": "нет"}), encoding="utf-8")
    assert exclusions.load() == []


# ── статусы базы ────────────────────────────────────────────────────────────


def test_sync_db_excludes_and_returns_rows(data):
    _make_db(data / "state.db", [
        ("disk:/Photos/a.jpg", 1, None, "pending", "C:/x/a.jpg", None),
        ("disk:/Photos/b.jpg", 1, None, "failed", "C:/x/b.jpg", "timeout"),
        ("disk:/Photos/c.jpg", 1, None, "downloaded", "C:/x/c.jpg", None),
        ("disk:/Videos/d.mp4", 1, None, "pending", "C:/x/d.mp4", None),
    ])

    changed = exclusions.sync_db(["disk:/Photos"])
    assert changed == {"excluded": 2, "returned": 0}

    con = sqlite3.connect(data / "state.db")
    rows = dict(con.execute("SELECT remote_path, status FROM files"))
    errors = dict(con.execute("SELECT remote_path, error FROM files"))
    con.close()
    assert rows["disk:/Photos/a.jpg"] == "excluded"
    assert rows["disk:/Photos/b.jpg"] == "excluded"
    # Скачанное исключение не переписывает.
    assert rows["disk:/Photos/c.jpg"] == "downloaded"
    assert rows["disk:/Videos/d.mp4"] == "pending"
    assert errors["disk:/Photos/b.jpg"] is None

    # Сняли папку — временно исключённые вернулись в очередь.
    returned = exclusions.sync_db([])
    assert returned == {"excluded": 0, "returned": 2}
    con = sqlite3.connect(data / "state.db")
    rows = dict(con.execute("SELECT remote_path, status FROM files"))
    con.close()
    assert rows["disk:/Photos/a.jpg"] == "pending"
    assert rows["disk:/Photos/b.jpg"] == "pending"
    assert rows["disk:/Photos/c.jpg"] == "downloaded"


def test_sync_db_without_database_is_noop(data):
    assert exclusions.sync_db(["disk:/Photos"]) == {"excluded": 0, "returned": 0}
