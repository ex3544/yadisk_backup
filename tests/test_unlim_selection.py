"""«Качать только выбранные дни» во всех загрузчиках Безлимита.

Фильтр сужает выборку и не трогает статусы остальной очереди.
"""

import datetime as dt
import sqlite3

import pytest

import paths
import selection
import unlim_backup
import unlim_dates
import unlim_direct
import unlim_schema
import unlim_worker


def _cid(year, month, day, hour=10):
    start = dt.datetime(year, month, day, hour)
    end = start + dt.timedelta(hours=1)
    return (
        f"{int(start.timestamp() * 1000):019d}_"
        f"{int(end.timestamp() * 1000):019d}"
    )


MAY1 = _cid(2024, 5, 1)
MAY2 = _cid(2024, 5, 2)
JUNE = _cid(2024, 6, 1)


def _make_db(path, statuses=("pending", "pending", "pending")):
    con = sqlite3.connect(path)
    unlim_schema.migrate(con)
    con.executemany(
        "INSERT INTO unlim_files (file_id, cluster_id, name, size, status) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("1", MAY1, "a.jpg", 10, statuses[0]),
            ("2", MAY2, "b.jpg", 20, statuses[1]),
            ("3", JUNE, "c.jpg", 30, statuses[2]),
        ],
    )
    con.commit()
    con.close()


def _statuses(path):
    con = sqlite3.connect(path)
    try:
        return dict(con.execute("SELECT file_id, status FROM unlim_files"))
    finally:
        con.close()


RANGES = [["2024-05-01", "2024-05-01"]]


# ── unlim_backup ────────────────────────────────────────────────────────────


def test_backup_take_batch_takes_only_selected_days(tmp_path):
    db = tmp_path / "unlim_state.db"
    _make_db(db)
    batch = unlim_backup.take_batch(db, 10, cluster_ranges=RANGES)
    assert [it["file_id"] for it in batch] == ["1"]
    statuses = _statuses(db)
    assert statuses["1"] == "in_progress"
    # Невыбранное осталось в очереди как было.
    assert statuses["2"] == "pending" and statuses["3"] == "pending"


def test_backup_take_batch_without_selection_takes_everything(tmp_path):
    db = tmp_path / "unlim_state.db"
    _make_db(db)
    batch = unlim_backup.take_batch(db, 10)
    assert {it["file_id"] for it in batch} == {"1", "2", "3"}


def test_backup_reset_skipped_only_inside_selected_days(tmp_path):
    db = tmp_path / "unlim_state.db"
    _make_db(db, statuses=("skipped", "skipped", "skipped"))
    assert unlim_backup.reset_skipped_files(db, cluster_ranges=RANGES) == 1
    statuses = _statuses(db)
    assert statuses["1"] == "pending"
    assert statuses["2"] == "skipped" and statuses["3"] == "skipped"


def test_backup_reset_skipped_without_selection_resets_all(tmp_path):
    db = tmp_path / "unlim_state.db"
    _make_db(db, statuses=("skipped", "skipped", "skipped"))
    assert unlim_backup.reset_skipped_files(db) == 3


# ── unlim_worker ────────────────────────────────────────────────────────────


def test_worker_take_batch_takes_only_selected_days(tmp_path, monkeypatch):
    db = str(tmp_path / "unlim_state.db")
    _make_db(db)
    monkeypatch.setattr(unlim_worker, "DB_FILE", db)
    batch = unlim_worker.take_batch(1, 10, RANGES)
    assert [it["file_id"] for it in batch] == ["1"]
    statuses = _statuses(db)
    assert statuses["2"] == "pending" and statuses["3"] == "pending"


# ── unlim_direct ────────────────────────────────────────────────────────────


def test_direct_take_batch_takes_only_selected_days(tmp_path, monkeypatch):
    db = str(tmp_path / "unlim_state.db")
    _make_db(db)
    monkeypatch.setattr(unlim_direct, "DB_FILE", db)
    batch = unlim_direct.take_batch(10, cluster_ranges=RANGES)
    assert [it["file_id"] for it in batch] == ["1"]
    statuses = _statuses(db)
    assert statuses["2"] == "pending" and statuses["3"] == "pending"


# ── файл выбора ─────────────────────────────────────────────────────────────


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", root)
    return root


def test_days_are_stored_as_ranges_and_do_not_touch_disk_choice(data_dir):
    selection.save_disk(["/Фото/2024"], ["/a.jpg"])
    selection.save_unlim_dates(["2024-05-01", "2024-05-02", "2024-05-05"])
    assert selection.unlim_ranges() == [
        ["2024-05-01", "2024-05-02"], ["2024-05-05", "2024-05-05"]]
    assert selection.unlim_days() == ["2024-05-01", "2024-05-02", "2024-05-05"]
    assert selection.disk_selection()["folders"] == ["disk:/Фото/2024"]

    # Сохранение Диска и его очистка дни Безлимита не стирают.
    selection.save_disk([], [])
    assert selection.unlim_ranges()
    selection.clear_disk()
    assert selection.unlim_ranges()
    assert selection.is_empty()

    selection.clear_unlim()
    assert selection.is_unlim_empty()
    assert selection.unlim_days() == []


def test_loader_reads_saved_days(data_dir):
    selection.save_unlim_dates(["2024-05-02"])
    ranges = selection.unlim_ranges()
    clause, params = unlim_dates.range_clause(ranges)
    assert "cluster_id" in clause
    assert params[0].startswith(f"{unlim_dates.date_to_ms(dt.date(2024, 5, 2)):019d}")
