"""Перепривязка баз: новые папки, сортировка, отключённые диски."""

import sqlite3

import pytest

import paths
import relocate


def _make_db(path, rows, table="files"):
    con = sqlite3.connect(path)
    if table == "files":
        con.execute(
            "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, "
            "md5 TEXT, status TEXT, local_path TEXT, error TEXT, "
            "downloaded_at TEXT, attempts INTEGER DEFAULT 0, "
            "retry_after INTEGER, server_fails INTEGER DEFAULT 0)"
        )
        con.executemany(
            "INSERT INTO files (remote_path, size, md5, status, local_path) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    else:
        con.execute(
            "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT, "
            "size INTEGER, md5 TEXT, status TEXT, local_path TEXT, error TEXT, "
            "downloaded_at TEXT, attempts INTEGER DEFAULT 0, "
            "worker_id INTEGER, assigned_at TEXT)"
        )
        con.executemany(
            "INSERT INTO unlim_files (file_id, name, size, md5, status, local_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
    con.commit()
    con.close()


@pytest.fixture
def data(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    udb = tmp_path / "unlim_state.db"
    monkeypatch.setattr(paths, "DB_FILE", db)
    monkeypatch.setattr(paths, "UNLIM_DB_FILE", udb)
    return tmp_path


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_relocate_by_exact_path(data):
    old = data / "old"
    new = data / "new"
    src = old / "Photos" / "a.jpg"
    dst = _write(new / "Photos" / "a.jpg", b"aaa")
    _make_db(data / "state.db", [
        ("disk:/Photos/a.jpg", 3, None, "downloaded", str(src)),
    ])

    plan = relocate.plan_relocate("disk", str(new), str(old))
    assert plan.old_root == str(old)
    assert plan.by_path == 1
    assert plan.updates == [("disk:/Photos/a.jpg", str(dst))]
    # Предпросмотр ничего не меняет.
    con = sqlite3.connect(data / "state.db")
    assert con.execute("SELECT local_path FROM files").fetchone()[0] == str(src)
    con.close()

    assert relocate.apply_relocate(plan) == 1
    con = sqlite3.connect(data / "state.db")
    assert con.execute("SELECT local_path FROM files").fetchone()[0] == str(dst)
    con.close()


def test_relocate_finds_file_in_sorted_subfolder(data):
    old = data / "old"
    new = data / "new"
    src = old / "IMG_1.jpg"
    dst = _write(new / "Фото" / "2024" / "2024-05" / "IMG_1.jpg", b"12345")
    _make_db(data / "state.db", [
        ("disk:/IMG_1.jpg", 5, None, "downloaded", str(src)),
    ])

    plan = relocate.plan_relocate("disk", str(new), str(old))
    assert plan.by_name == 1
    assert plan.updates == [("disk:/IMG_1.jpg", str(dst))]


def test_relocate_understands_duplicate_suffix(data):
    old = data / "old"
    new = data / "new"
    src = old / "photo.jpg"
    dst = _write(new / "Фото" / "photo_1.jpg", b"12345")
    _make_db(data / "state.db", [
        ("disk:/photo.jpg", 5, None, "downloaded", str(src)),
    ])

    plan = relocate.plan_relocate("disk", str(new), str(old))
    assert plan.by_name == 1
    assert plan.updates == [("disk:/photo.jpg", str(dst))]


def test_relocate_uses_md5_when_names_collide(data):
    old = data / "old"
    new = data / "new"
    src = old / "same.jpg"
    good = _write(new / "A" / "same.jpg", b"one")
    _write(new / "B" / "same.jpg", b"two")
    import hashlib
    md5 = hashlib.md5(b"one").hexdigest()
    _make_db(data / "state.db", [
        ("disk:/same.jpg", 3, md5, "downloaded", str(src)),
    ])

    plan = relocate.plan_relocate("disk", str(new), str(old))
    assert plan.by_md5 == 1
    assert plan.updates == [("disk:/same.jpg", str(good))]


def test_relocate_reports_offline_drive(data, monkeypatch):
    old = data / "old"
    new = data / "new"
    src = old / "a.jpg"
    new.mkdir()
    _make_db(data / "state.db", [
        ("disk:/a.jpg", 3, None, "downloaded", str(src)),
    ])
    monkeypatch.setattr(relocate, "volume_ready", lambda _p: False)

    plan = relocate.plan_relocate("disk", str(new), str(old))
    assert plan.offline == 1
    assert plan.missing == 0
    assert plan.offline_items[0]["path"] == "disk:/a.jpg"


def test_relocate_reports_missing_file(data):
    old = data / "old"
    new = data / "new"
    new.mkdir()
    # Старая папка на месте (диск подключён), а файла в ней нет — это и есть
    # «не найдено». На Linux иначе volume_ready счёл бы диск отключённым.
    old.mkdir()
    _make_db(data / "state.db", [
        ("disk:/gone.jpg", 3, None, "downloaded", str(old / "gone.jpg")),
    ])

    plan = relocate.plan_relocate("disk", str(new), str(old))
    assert plan.missing == 1
    assert plan.conflicts == 0


def test_relocate_unlim_by_name(data):
    old = data / "old"
    new = data / "new"
    src = old / "IMG_9.jpg"
    dst = _write(new / "2024" / "2024-06" / "IMG_9.jpg", b"12345")
    _make_db(data / "unlim_state.db", [
        ("file-1", "IMG_9.jpg", 5, None, "downloaded", str(src)),
    ], table="unlim_files")

    plan = relocate.plan_relocate("unlim", str(new), str(old))
    assert plan.by_name == 1
    assert plan.updates == [("file-1", str(dst))]


def test_relocate_finds_pathless_unlim_row_by_name(data):
    """Строки из старого «воркера» без local_path тоже находятся по имени и размеру."""
    new = data / "new"
    dst = _write(new / "2024" / "VID_1.mp4", b"12345")
    _make_db(data / "unlim_state.db", [
        ("file-1", "VID_1.mp4", 5, None, "downloaded", None),
    ], table="unlim_files")

    plan = relocate.plan_relocate("unlim", str(new))
    assert plan.by_name == 1
    assert plan.updates == [("file-1", str(dst))]
    assert plan.missing == 0


def test_relocate_finds_pathless_disk_row_by_name(data):
    new = data / "new"
    dst = _write(new / "Photos" / "a.jpg", b"aaa")
    _make_db(data / "state.db", [
        ("disk:/Photos/a.jpg", 3, None, "downloaded", ""),
    ])

    plan = relocate.plan_relocate("disk", str(new))
    assert plan.by_name == 1
    assert plan.updates == [("disk:/Photos/a.jpg", str(dst))]


def test_pathless_row_not_found_is_missing_not_offline(data):
    """Без пути нечего проверять на диске — это «не найдено», а не «диск отключён»."""
    new = data / "new"
    new.mkdir()
    _make_db(data / "unlim_state.db", [
        ("file-1", "gone.mp4", 5, None, "downloaded", None),
    ], table="unlim_files")

    plan = relocate.plan_relocate("unlim", str(new))
    assert plan.missing == 1
    assert plan.offline == 0
    assert plan.missing_keys == ["file-1"]


def test_pathless_row_survives_old_folder_filter(data):
    """Фильтр «Старая папка» не прячет строки без пути."""
    old = data / "old"
    new = data / "new"
    dst = _write(new / "x.mp4", b"12")
    _make_db(data / "unlim_state.db", [
        ("file-1", "x.mp4", 2, None, "downloaded", None),
        ("file-2", "y.mp4", 2, None, "downloaded", str(old / "y.mp4")),
    ], table="unlim_files")

    plan = relocate.plan_relocate("unlim", str(new), only_under=str(old))
    assert plan.by_name == 1
    assert plan.updates == [("file-1", str(dst))]


def test_relocate_matches_sanitized_file_name(data):
    """На диске имя очищено ('>' → '_'), в базе — яндексовое. Найти надо."""
    new = data / "new"
    dst = _write(new / "004. Весна G_A_H .jpg", b"12345")
    _make_db(data / "unlim_state.db", [
        ("file-1", "004. Весна G>A>H .jpg", 5, None, "downloaded", None),
    ], table="unlim_files")

    plan = relocate.plan_relocate("unlim", str(new))
    assert plan.by_name == 1
    assert plan.updates == [("file-1", str(dst))]


def test_relocate_refuses_missing_new_folder(data):
    with pytest.raises(relocate.RelocateError):
        relocate.plan_relocate("disk", str(data / "nope"))


def test_relocate_does_not_touch_already_correct_files(data):
    new = data / "new"
    dst = _write(new / "a.jpg", b"1")
    _make_db(data / "state.db", [
        ("disk:/a.jpg", 1, None, "downloaded", str(dst)),
    ])
    plan = relocate.plan_relocate("disk", str(new), str(new))
    assert plan.already == 1
    assert plan.updates == []


def test_requeue_missing_returns_only_listed_files(data):
    _make_db(data / "state.db", [
        ("disk:/a.jpg", 1, None, "downloaded", "C:/old/a.jpg"),
        ("disk:/b.jpg", 1, None, "downloaded", "C:/old/b.jpg"),
    ])
    updated = relocate.requeue_missing("disk", ["disk:/a.jpg"])
    assert updated == 1
    con = sqlite3.connect(data / "state.db")
    rows = dict(con.execute(
        "SELECT remote_path, status || '|' || COALESCE(local_path, '') FROM files"
    ))
    con.close()
    assert rows["disk:/a.jpg"] == "pending|"
    assert rows["disk:/b.jpg"] == "downloaded|C:/old/b.jpg"


def test_restore_cloud_plan_uses_remote_paths(data):
    root = data / "disk"
    moved = _write(root / "Фото" / "a.jpg", b"123")
    target = root / "Photos" / "a.jpg"
    _make_db(data / "state.db", [
        ("disk:/Photos/a.jpg", 3, None, "downloaded", str(moved)),
    ])

    plan = relocate.plan_restore_cloud("disk", str(root))
    assert plan.moves == 1
    assert plan.updates == [("disk:/Photos/a.jpg", str(moved), str(target))]

    done, errors = relocate.apply_restore_cloud(plan)
    assert done == 1 and errors == []
    assert target.read_bytes() == b"123"
    assert not moved.exists()
    con = sqlite3.connect(data / "state.db")
    assert con.execute("SELECT local_path FROM files").fetchone()[0] == str(target)
    con.close()


def test_restore_cloud_counts_already_correct(data):
    root = data / "disk"
    target = _write(root / "Photos" / "a.jpg", b"123")
    _make_db(data / "state.db", [
        ("disk:/Photos/a.jpg", 3, None, "downloaded", str(target)),
    ])
    plan = relocate.plan_restore_cloud("disk", str(root))
    assert plan.already == 1
    assert plan.updates == []


def test_restore_cloud_only_for_disk(data):
    with pytest.raises(relocate.RelocateError):
        relocate.plan_restore_cloud("unlim", str(data))


def test_relocate_only_under_old_folder(data):
    r"""35.9.0: «Старая папка» перепривязывает только записи из неё.

    Так переезд C:\Unlim -> D:\Unlim не трогает уже правильные D:\Unlim
    и старые Linux-пути.
    """
    old_c = data / "oldC"
    old_d = data / "oldD"
    new = data / "new"
    # Старые файлы уже удалены/перенесены — на старом месте их нет.
    _write(new / "a.jpg", b"a")
    _write(new / "b.jpg", b"b")
    _make_db(data / "state.db", [
        ("disk:/a.jpg", 1, None, "downloaded", str(old_c / "a.jpg")),
        ("disk:/b.jpg", 1, None, "downloaded", str(old_d / "b.jpg")),
    ])

    plan = relocate.plan_relocate("disk", str(new), str(old_c), str(old_c))
    assert plan.total == 1
    assert plan.old_root == str(old_c)
    assert plan.updates == [("disk:/a.jpg", str(new / "a.jpg"))]

    # Без фильтра обе записи находятся в новой папке.
    everything = relocate.plan_relocate("disk", str(new), str(old_c))
    assert everything.total == 2
    assert {key for key, _ in everything.updates} == {"disk:/a.jpg", "disk:/b.jpg"}
