"""Тесты воркера безлимита: распаковка ZIP и учёт того, что реально приехало."""

import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

import unlim_schema
import unlim_worker as uw


# ── защита от zip-slip ──────────────────────────────────────────────────────


def test_safe_extract_normal_name(tmp_path):
    base = str(tmp_path)
    dst = uw.safe_extract_dst(base, "2020/photo.jpg")
    assert dst == os.path.join(base, "2020", "photo.jpg")


def test_safe_extract_strips_parent_refs(tmp_path):
    base = str(tmp_path / "extract")
    os.makedirs(base)
    dst = uw.safe_extract_dst(base, "../../evil.txt")
    assert dst == os.path.join(base, "evil.txt")


def test_safe_extract_rejects_drive_letter(tmp_path):
    """'C:/evil.txt' — os.path.join просто выбросил бы базу и записал в корень диска."""
    base = str(tmp_path / "extract")
    os.makedirs(base)
    dst = uw.safe_extract_dst(base, "C:/evil.txt")
    assert dst == os.path.join(base, "evil.txt")


def test_safe_extract_rejects_absolute(tmp_path):
    base = str(tmp_path / "extract")
    os.makedirs(base)
    dst = uw.safe_extract_dst(base, "/etc/passwd")
    assert dst == os.path.join(base, "etc", "passwd")


def test_safe_extract_result_always_inside_base(tmp_path):
    base = str(tmp_path / "extract")
    os.makedirs(base)
    for name in ["../x", r"..\..\x", "C:/x", "//server/share/x", "./x", "a/../../x"]:
        dst = uw.safe_extract_dst(base, name)
        if dst is None:
            continue
        assert os.path.commonpath([os.path.realpath(base), os.path.realpath(dst)]) \
            == os.path.realpath(base), name


def test_safe_extract_empty_name(tmp_path):
    assert uw.safe_extract_dst(str(tmp_path), "../..") is None
    assert uw.safe_extract_dst(str(tmp_path), "") is None


# ── сопоставление «что просили» ↔ «что приехало» ────────────────────────────


def test_split_marks_only_extracted_files():
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "b.jpg", "size": 200},
        {"file_id": "3", "name": "c.jpg", "size": 300},
    ]
    done, missing = uw.split_by_extracted(batch, [("a.jpg", 100), ("c.jpg", 300)])
    assert done == ["1", "3"]
    assert missing == ["2"]


def test_split_all_missing_when_zip_empty():
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [])
    assert done == []
    assert missing == ["1"]


def test_split_respects_multiplicity():
    """Два одинаковых файла в пачке требуют двух записей в архиве."""
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "a.jpg", "size": 100},
    ]
    done, missing = uw.split_by_extracted(batch, [("a.jpg", 100)])
    assert done == ["1"]
    assert missing == ["2"]


def test_split_size_must_match():
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [("a.jpg", 999)])
    assert done == []
    assert missing == ["1"]


def test_split_ignores_zip_subfolders():
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [("2020/a.jpg", 100)])
    assert done == ["1"]
    assert missing == []


# ── БД: попытки и переход в failed ──────────────────────────────────────────


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "unlim_state.db")
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, cluster_id TEXT, name TEXT, size INTEGER,
            md5 TEXT, mime TEXT, status TEXT DEFAULT 'pending', local_path TEXT,
            error TEXT, worker_id INTEGER, assigned_at TIMESTAMP,
            downloaded_at TIMESTAMP
        );
        """
    )
    con.executemany(
        "INSERT INTO unlim_files (file_id, name, size) VALUES (?, ?, ?)",
        [("1", "a.jpg", 10), ("2", "b.jpg", 20)],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(uw, "DB_FILE", path)
    return path


def status_of(db_path, file_id):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT status, attempts FROM unlim_files WHERE file_id=?", (file_id,)
        ).fetchone()
    finally:
        con.close()


def test_migration_adds_attempts_column(db):
    uw.ensure_attempts_column()
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(unlim_files)")}
    version = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    assert "attempts" in cols
    assert version == max(v for v, _ in unlim_schema.MIGRATIONS)
    assert list(Path(db).parent.glob("unlim_state.db.bak_v0_*"))


def test_take_batch_returns_names_and_sizes(db):
    uw.ensure_attempts_column()
    batch = uw.take_batch(worker_id=1, size=10)
    assert {it["file_id"] for it in batch} == {"1", "2"}
    assert all("name" in it and "size" in it for it in batch)
    # повторный вызов ничего не отдаёт — файлы уже in_progress
    assert uw.take_batch(worker_id=2, size=10) == []


def test_mark_pending_counts_attempts_then_fails(db):
    uw.ensure_attempts_column()
    for expected_attempt in (1, 2):
        uw.mark_pending(["1"], "boom")
        status, attempts = status_of(db, "1")
        assert (status, attempts) == ("pending", expected_attempt)

    uw.mark_pending(["1"], "boom")
    status, attempts = status_of(db, "1")
    assert status == "failed"          # больше не крутится в бесконечном цикле
    assert attempts == uw.MAX_ATTEMPTS


def test_mark_done(db):
    uw.ensure_attempts_column()
    uw.mark_done(["1"])
    status, _ = status_of(db, "1")
    assert status == "downloaded"


# ── интеграция: злой архив не вылезает из папки ─────────────────────────────


def test_download_and_extract_contains_malicious_zip(tmp_path, monkeypatch):
    extract_dir = tmp_path / "extract"
    zip_dir = tmp_path / "zips"
    monkeypatch.setattr(uw, "EXTRACT_DIR", str(extract_dir))
    monkeypatch.setattr(uw, "ZIP_TMP_DIR", str(zip_dir))

    src_zip = tmp_path / "payload.zip"
    with zipfile.ZipFile(src_zip, "w") as zf:
        zf.writestr("ok.jpg", b"good")
        zf.writestr("../../escaped.txt", b"bad")
        zf.writestr("C:/escaped2.txt", b"bad")

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield self._data

    class FakeSession:
        def get(self, url, stream=False, timeout=None):
            return FakeResponse(src_zip.read_bytes())

    extracted, total = uw.download_and_extract(FakeSession(), "http://x", 1, 1)

    names = sorted(n for n, _ in extracted)
    assert names == ["escaped.txt", "escaped2.txt", "ok.jpg"]
    # всё легло внутрь extract_dir и никуда больше
    assert (extract_dir / "ok.jpg").exists()
    assert (extract_dir / "escaped.txt").exists()
    assert (extract_dir / "escaped2.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert total == 10  # 4 ("good") + 3 + 3


def test_split_falls_back_to_size_when_zip_renamed():
    """Яндекс может переименовать файл в архиве — по размеру всё равно засчитываем."""
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [("a (1).jpg", 100)])
    assert done == ["1"]
    assert missing == []


def test_split_fallback_does_not_invent_files():
    """Если в архиве нет файла подходящего размера — честно считаем непришедшим."""
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [("b.jpg", 7)])
    assert done == []
    assert missing == ["1"]


def test_split_fallback_respects_counts():
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "b.jpg", "size": 100},
    ]
    done, missing = uw.split_by_extracted(batch, [("renamed.jpg", 100)])
    assert done == ["1"]
    assert missing == ["2"]


def test_split_matches_by_md5_when_names_differ(tmp_path, monkeypatch):
    import hashlib
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir(parents=True)
    monkeypatch.setattr(uw, "EXTRACT_DIR", str(extract_dir))

    file1 = extract_dir / "renamed1.jpg"
    file1.write_bytes(b"0123456789")
    h1 = hashlib.md5(b"0123456789").hexdigest()

    file2 = extract_dir / "renamed2.jpg"
    file2.write_bytes(b"abcdefghij")
    h2 = hashlib.md5(b"abcdefghij").hexdigest()

    batch = [
        {"file_id": "target_id", "name": "original_target.jpg", "size": 10, "md5": h2},
        {"file_id": "other_id", "name": "original_other.jpg", "size": 10, "md5": h1},
    ]

    done, missing = uw.split_by_extracted(batch, [("renamed1.jpg", 10), ("renamed2.jpg", 10)])
    assert set(done) == {"target_id", "other_id"}
    assert missing == []


# ── статус сессии Безлимита для Web UI ──────────────────────────────────────


def test_status_file_has_started_at(tmp_path):
    """UI отличает статус этого процесса от файла прошлой сессии по started_at."""
    import json

    import unlim_backup

    status = tmp_path / "unlim_status.json"
    unlim_backup.update_status_file(status, active=True, success=7, skipped=2)

    data = json.loads(status.read_text(encoding="utf-8"))
    assert data["started_at"] == unlim_backup.SESSION_STARTED_AT
    assert data["active"] is True
    assert data["success"] == 7
    assert data["skipped"] == 2


def test_zero_status_is_written_before_auth(tmp_path, monkeypatch):
    """Нулевой статус пишется до авторизации: плашка не висит «подготовкой»."""
    import json

    import unlim_backup

    status = tmp_path / "unlim_status.json"
    monkeypatch.setattr(unlim_backup, "check_target_dir", lambda path: None)

    def stop_on_auth(path):
        raise SystemExit(1)

    monkeypatch.setattr(unlim_backup, "load_cookies", stop_on_auth)

    with pytest.raises(SystemExit):
        unlim_backup.run_backup(
            db_path=tmp_path / "unlim_state.db",
            cookies_path=tmp_path / "cookies.json",
            out_dir_path=tmp_path,
            status_file=status,
        )

    data = json.loads(status.read_text(encoding="utf-8"))
    assert data["active"] is True
    assert data["success"] == 0
    assert "started_at" in data


# ── отложенные файлы Безлимита (35.6.3) ─────────────────────────────────────


def test_mark_deferred_keeps_file_pending_and_refunds_attempt(db):
    """Сервер хранения молчит — это не вина файла: попытку не тратим."""
    import unlim_backup

    uw.ensure_attempts_column()
    # take_batch уже списал попытку — mark_deferred её возвращает.
    unlim_backup.take_batch(Path(db), 10)
    assert status_of(db, "1") == ("in_progress", 1)
    unlim_backup.mark_deferred(Path(db), "1", "storage.yandex.net")

    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT status, error, storage_host, attempts, assigned_at "
            "FROM unlim_files WHERE file_id='1'"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "pending"
    assert row[1] == "storage_down | storage.yandex.net"
    assert row[2] == "storage.yandex.net"
    assert row[3] == 0
    assert row[4] is None


def test_mark_deferred_without_host():
    import unlim_backup

    assert unlim_backup.deferred_error(None) == "storage_down"
    assert unlim_backup.deferred_error("h") == "storage_down | h"


def test_deferred_batch_is_not_counted_as_skipped(monkeypatch):
    """Итог прохода: отложенные отдельно от пропущенных."""
    import unlim_backup

    src = Path(unlim_backup.__file__).read_text(encoding="utf-8")
    assert 'msg.startswith(("storage_down", "storage_skip"))' in src
    assert "total_deferred_files" in src
    assert "ждут Яндекса" in src


# ── отложенные не крутят проход по кругу (35.6.5) ───────────────────────────


def test_take_batch_puts_deferred_files_last(db):
    """Отложенные уходят в конец очереди: иначе проход берёт только их."""
    import unlim_backup

    unlim_backup.init_db(Path(db))
    con = sqlite3.connect(db)
    con.execute("UPDATE unlim_files SET error='storage_down | s1' WHERE file_id='1'")
    con.execute("INSERT INTO unlim_files (file_id, name, size) VALUES ('3', 'c.jpg', 30)")
    con.commit()
    con.close()

    batch = unlim_backup.take_batch(Path(db), 10)
    assert [b["file_id"] for b in batch] == ["2", "3", "1"]


def test_run_backup_finishes_pass_when_everything_waits_for_yandex(db, tmp_path, monkeypatch, capsys):
    """Батч, целиком ушедший ждать Яндекса, завершает проход.

    Без стоп-условия `take_batch` возвращал те же файлы снова и снова, и в 35.6.4
    проход «1 из 10» крутился часами, ничего не скачивая.
    """
    import unlim_backup

    unlim_backup.init_db(Path(db))
    monkeypatch.setattr(unlim_backup, "check_target_dir", lambda path: None)
    monkeypatch.setattr(unlim_backup, "load_cookies", lambda path: [])
    monkeypatch.setattr(unlim_backup, "make_session", lambda cookies, **kw: object())
    monkeypatch.setattr(unlim_backup, "get_sk_uid", lambda s: ("sk12345678", "uid"))
    monkeypatch.setattr(unlim_backup, "recover_stuck", lambda p: None)

    calls = []

    def fake_download(db_path, cookies, sk, uid, item, *rest, **kw):
        if len(calls) > 20:
            raise AssertionError("проход крутится по кругу: батч из отложенных не завершает загрузку")
        calls.append(item["file_id"])
        unlim_backup.mark_deferred(Path(db), item["file_id"], "s1")
        return False, 0, "storage_down: сервер s1 не отвечает"

    monkeypatch.setattr(unlim_backup, "download_one_file", fake_download)

    unlim_backup.run_backup(
        db_path=Path(db),
        cookies_path=tmp_path / "cookies.json",
        out_dir_path=tmp_path,
        target_status="pending",
        workers=2,
        batch_size=10,
        make_previews=False,
    )

    out = capsys.readouterr().out
    assert "ждут Яндекса" in out
    # Один батч из двух файлов, и ни одного лишнего круга.
    assert sorted(calls) == ["1", "2"]
