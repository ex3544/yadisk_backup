"""Выбор «что качать»: список папок и файлов, статусы базы не меняются."""

import json

import pytest

import paths
import selection
from state import State


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", root)
    return root


@pytest.fixture
def disk_state(tmp_path):
    st = State(str(tmp_path / "state.db"))
    yield st
    st.close()


# ── нормализация и файл выбора ──────────────────────────────────────────────


def test_normalize_remote_adds_disk_prefix():
    assert selection.normalize_remote("Фото/2024") == "disk:/Фото/2024"
    assert selection.normalize_remote("/Фото/2024/") == "disk:/Фото/2024"
    assert selection.normalize_remote("disk:/a/b") == "disk:/a/b"
    assert selection.normalize_remote("") == ""


def test_normalize_remote_rejects_parent_dirs():
    with pytest.raises(ValueError):
        selection.normalize_remote("/Фото/../../Секреты")


def test_save_and_load_roundtrip(data_dir):
    saved = selection.save_disk(
        ["/Фото/2024", "Фото/2024", "disk:/Документы"],
        ["/a.jpg", {"path": "/b.jpg", "size": 15}, "disk:/a.jpg"],
    )
    assert saved["disk"]["folders"] == ["disk:/Фото/2024", "disk:/Документы"]
    assert saved["disk"]["files"] == [
        {"path": "disk:/a.jpg", "size": None},
        {"path": "disk:/b.jpg", "size": 15},
    ]
    loaded = selection.disk_selection()
    assert loaded["folders"] == saved["disk"]["folders"]
    assert loaded["files"] == saved["disk"]["files"]
    assert selection.summary()["files"] == 2
    assert selection.summary()["bytes"] == 15


def test_broken_selection_file_is_empty(data_dir):
    (data_dir / "selection.json").write_text("{не json", encoding="utf-8")
    assert selection.disk_selection() == {"folders": [], "files": []}
    assert selection.is_empty()


def test_clear_removes_everything(data_dir):
    selection.save_disk(["/Фото"], ["/a.jpg"])
    selection.clear_disk()
    assert selection.is_empty()


# ── задания по выбору ───────────────────────────────────────────────────────


def test_downloaded_files_are_not_selected_again(data_dir, disk_state):
    disk_state.mark_pending("disk:/a.jpg", 10, None)
    disk_state.mark_pending("disk:/b.jpg", 20, None)
    disk_state.mark_downloaded("disk:/b.jpg", r"C:\out\b.jpg")
    selection.save_disk([], ["/a.jpg", "/b.jpg"])

    built = selection.build_disk_jobs(disk_state, client=None, do_scan=False)
    assert [j.remote_path for j in built["jobs"]] == ["disk:/a.jpg"]
    assert built["already_done"] == 1
    # Скачанный так и остался скачанным.
    assert disk_state.get_statuses(["disk:/b.jpg"])["disk:/b.jpg"] == "downloaded"


def test_unselected_files_stay_in_queue(data_dir, disk_state):
    disk_state.mark_pending("disk:/a.jpg", 10, None)
    disk_state.mark_pending("disk:/c.jpg", 30, None)
    selection.save_disk([], ["/a.jpg"])

    built = selection.build_disk_jobs(disk_state, client=None, do_scan=False)
    assert [j.remote_path for j in built["jobs"]] == ["disk:/a.jpg"]
    # Невыбранное осталось ровно как было — в очереди.
    assert disk_state.get_statuses(["disk:/c.jpg"])["disk:/c.jpg"] == "pending"
    assert disk_state.get_stats()["pending"] == 2


def test_folder_selection_takes_files_under_it(data_dir, disk_state):
    disk_state.mark_pending("disk:/Фото/2024/a.jpg", 1, None)
    disk_state.mark_pending("disk:/Фото/2024/b.jpg", 2, None)
    disk_state.mark_pending("disk:/Фото/2025/c.jpg", 3, None)
    disk_state.mark_pending("disk:/Фото/2024/x.jpg", 4, None)
    disk_state.mark_downloaded("disk:/Фото/2024/x.jpg", "x")
    selection.save_disk(["/Фото/2024"], [])

    built = selection.build_disk_jobs(disk_state, client=None, do_scan=False)
    assert [j.remote_path for j in built["jobs"]] == [
        "disk:/Фото/2024/a.jpg", "disk:/Фото/2024/b.jpg"]
    statuses = disk_state.get_statuses(
        ["disk:/Фото/2025/c.jpg", "disk:/Фото/2024/x.jpg"])
    assert statuses["disk:/Фото/2025/c.jpg"] == "pending"
    assert statuses["disk:/Фото/2024/x.jpg"] == "downloaded"


def test_new_file_is_registered_but_not_others(data_dir, disk_state):
    selection.save_disk([], [{"path": "/Новое/new.jpg", "size": 99}])
    built = selection.build_disk_jobs(disk_state, client=None, do_scan=False)
    assert [j.remote_path for j in built["jobs"]] == ["disk:/Новое/new.jpg"]
    assert disk_state.get_statuses(["disk:/Новое/new.jpg"])["disk:/Новое/new.jpg"] == "pending"
    assert disk_state.get_stats()["total"] == 1


def test_empty_selection_gives_no_jobs(data_dir, disk_state):
    built = selection.build_disk_jobs(disk_state, client=None, do_scan=False)
    assert built["jobs"] == []
    assert disk_state.get_stats()["total"] == 0


def test_scan_failure_does_not_break_the_selection(data_dir, disk_state, monkeypatch):
    import downloader

    selection.save_disk(["/Пропала"], [])

    def broken_scan(*a, **kw):
        raise RuntimeError("нет сети")

    monkeypatch.setattr(downloader, "scan_tree", broken_scan)
    built = selection.build_disk_jobs(disk_state, client=object(), do_scan=True)
    assert built["jobs"] == []
    assert built["scanned_folders"] == 0


def test_selection_is_stored_per_profile(data_dir):
    selection.save_disk(["/Фото"], [])
    data = json.loads((data_dir / "selection.json").read_text(encoding="utf-8"))
    assert data["version"] == selection.VERSION
    assert data["disk"]["folders"] == ["disk:/Фото"]
