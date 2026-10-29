"""Проверка скачанных файлов (C1): сверка с базой и запуск из окна."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import paths
import verify
import webui


# ── check_one ───────────────────────────────────────────────────────────────


def _write(tmp_path: Path, name: str, data: bytes = b"hello") -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_check_one_ok_when_size_matches(tmp_path):
    p = _write(tmp_path, "a.txt", b"12345")
    status, detail = verify.check_one(str(p), 5, None)
    assert status == "ok"
    assert detail == ""


def test_check_one_missing_when_file_absent(tmp_path):
    status, detail = verify.check_one(str(tmp_path / "нет.txt"), 5, None)
    assert status == "missing"
    assert "нет" in detail


def test_check_one_reports_size_mismatch(tmp_path):
    p = _write(tmp_path, "b.txt", b"123")
    status, detail = verify.check_one(str(p), 99, None)
    assert status == "size"
    assert "99" in detail


def test_check_one_deep_md5_match_and_mismatch(tmp_path):
    p = _write(tmp_path, "c.txt", b"payload")
    md5 = hashlib.md5(b"payload").hexdigest()
    assert verify.check_one(str(p), 7, md5, deep=True)[0] == "ok"
    status, detail = verify.check_one(str(p), 7, "0" * 32, deep=True)
    assert status == "md5"
    assert "0" * 32 in detail


def test_check_one_skips_md5_without_deep(tmp_path):
    p = _write(tmp_path, "d.txt", b"payload")
    assert verify.check_one(str(p), 7, "0" * 32)[0] == "ok"


def test_check_one_read_error_is_not_md5(tmp_path, monkeypatch):
    """Если файл не читается — это «error», а не «md5»: виноват не файл."""

    def boom(path):
        raise OSError("диск отключился")

    monkeypatch.setattr(verify, "compute_md5", boom)
    p = _write(tmp_path, "e.txt", b"x")
    status, detail = verify.check_one(str(p), 1, "0" * 32, deep=True)
    assert status == "error"
    assert "отключился" in detail


# ── run по временным базам ──────────────────────────────────────────────────


@pytest.fixture()
def temp_paths(tmp_path, monkeypatch):
    """Уводим обе базы и статус проверки во временную папку."""
    disk_db = tmp_path / "state.db"
    unlim_db = tmp_path / "unlim_state.db"
    monkeypatch.setattr(paths, "DB_FILE", disk_db)
    monkeypatch.setattr(paths, "UNLIM_DB_FILE", unlim_db)
    monkeypatch.setattr(paths, "VERIFY_STATUS_FILE", tmp_path / "verify_status.json")
    return disk_db, unlim_db, tmp_path


def _make_disk_db(db: Path, rows) -> None:
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE files (
            remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,
            status TEXT NOT NULL DEFAULT 'pending', local_path TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO files (remote_path, size, md5, status, local_path) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def _make_unlim_db(db: Path, rows) -> None:
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT, name TEXT, size INTEGER, md5 TEXT,
            status TEXT NOT NULL DEFAULT 'pending', local_path TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO unlim_files (file_id, name, size, md5, status, local_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def test_run_disk_counts_and_status_file(temp_paths):
    disk_db, _, root = temp_paths
    good = _write(root, "files/good.txt", b"12345")
    bad_size = _write(root, "files/bad.txt", b"1")
    rows = [
        ("disk:/good.txt", 5, None, "downloaded", str(good)),
        ("disk:/bad.txt", 5, None, "downloaded", str(bad_size)),
        ("disk:/gone.txt", 5, None, "downloaded", str(root / "files/gone.txt")),
        # не скачан — проверке не подлежит
        ("disk:/pending.txt", 5, None, "pending", None),
    ]
    _make_disk_db(disk_db, rows)

    data = verify.run("disk")

    assert data["total"] == 3
    assert data["checked"] == 3
    assert (data["ok"], data["size"], data["missing"]) == (1, 1, 1)
    assert data["active"] is False
    assert data["bytes_checked"] == 5
    assert {p["status"] for p in data["problems"]} == {"size", "missing"}
    # статус дочитывается панелью из файла
    assert verify.read_status()["checked"] == 3


def test_run_does_not_touch_database(temp_paths):
    disk_db, _, root = temp_paths
    good = _write(root, "files/good.txt", b"12345")
    _make_disk_db(
        disk_db,
        [("disk:/good.txt", 5, None, "downloaded", str(good))],
    )
    verify.run("disk")
    con = sqlite3.connect(disk_db)
    try:
        assert con.execute("SELECT status FROM files").fetchone()[0] == "downloaded"
        assert con.execute("SELECT local_path FROM files").fetchone()[0] == str(good)
    finally:
        con.close()


def test_run_unlim_scope(temp_paths):
    _, unlim_db, root = temp_paths
    good = _write(root, "unlim/good.bin", b"abc")
    _make_unlim_db(
        unlim_db,
        [
            ("id-1", "good.bin", 3, None, "downloaded", str(good)),
            ("id-2", "missing.bin", 3, None, "downloaded", str(root / "unlim/missing.bin")),
        ],
    )
    data = verify.run("unlim")
    assert (data["total"], data["ok"], data["missing"]) == (2, 1, 1)
    assert data["scope"] == "unlim"


def test_run_both_sums_bases(temp_paths):
    disk_db, unlim_db, root = temp_paths
    good = _write(root, "f.txt", b"12345")
    _make_disk_db(disk_db, [("disk:/f.txt", 5, None, "downloaded", str(good))])
    _make_unlim_db(unlim_db, [("id", "f.txt", 5, None, "downloaded", str(good))])
    data = verify.run("both")
    assert data["total"] == 2
    assert data["ok"] == 2


def test_run_honours_limit(temp_paths):
    disk_db, _, root = temp_paths
    rows = []
    for i in range(5):
        p = _write(root, f"files/{i}.txt", b"1")
        rows.append((f"disk:/{i}.txt", 1, None, "downloaded", str(p)))
    _make_disk_db(disk_db, rows)
    data = verify.run("disk", limit=2)
    assert data["total"] == 2
    assert data["checked"] == 2


def test_run_without_databases_is_empty(temp_paths):
    data = verify.run("both")
    assert data["total"] == 0
    assert data["checked"] == 0


def test_run_rejects_unknown_scope():
    with pytest.raises(ValueError):
        verify.run("cloud")


def test_run_reports_progress(temp_paths):
    disk_db, _, root = temp_paths
    p = _write(root, "files/x.txt", b"1")
    _make_disk_db(disk_db, [("disk:/x.txt", 1, None, "downloaded", str(p))])
    seen = []
    verify.run("disk", on_progress=seen.append)
    assert len(seen) == 1
    assert seen[0].checked == 1


def test_read_status_empty_when_no_file(temp_paths):
    assert verify.read_status() == {}


def test_render_report_mentions_problems_and_safety(temp_paths):
    disk_db, _, root = temp_paths
    rows = [
        ("disk:/ok.txt", 1, None, "downloaded", str(_write(root, "ok.txt", b"1"))),
        ("disk:/gone.txt", 1, None, "downloaded", str(root / "gone.txt")),
    ]
    _make_disk_db(disk_db, rows)
    report = verify.render_report(verify.run("disk"))
    assert "# Проверка файлов" in report
    assert "Нет на диске: 1" in report
    assert "disk:/gone.txt" in report
    assert "статусы в базе не менялись" in report


# ── окно: статус и роуты ────────────────────────────────────────────────────


def test_verify_status_marks_running_and_report(temp_paths, monkeypatch):
    _, _, root = temp_paths
    report = root / "verify_report.md"
    monkeypatch.setattr(paths, "VERIFY_REPORT_FILE", report)

    out = webui._verify_status({"checked": 3}, {"verify": {"running": True}})
    assert out["running"] is True
    assert out["checked"] == 3
    assert out["report_available"] is False

    report.write_text("x", encoding="utf-8")
    assert webui._verify_status({}, {})["report_available"] is True
    assert webui._verify_status({}, {})["running"] is False


def test_api_verify_start_rejects_bad_scope(monkeypatch):
    called = []
    monkeypatch.setattr(webui.tm, "start", lambda *a, **k: called.append(a) or (True, "ok"))
    res = webui.api_verify_start(webui.VerifyPayload(scope="cloud"))
    assert res["ok"] is False
    assert called == []


def test_api_verify_start_passes_scope_and_deep(monkeypatch):
    called = []

    def fake_start(slot, script, args):
        called.append((slot, script, args))
        return True, "Проверка запущена"

    monkeypatch.setattr(webui.tm, "start", fake_start)
    res = webui.api_verify_start(webui.VerifyPayload(scope="both", deep=True))
    assert res["ok"] is True
    assert called == [("verify", "verify_local.py", ["--scope", "both", "--deep"])]


def test_api_verify_stop_stops_slot(monkeypatch):
    monkeypatch.setattr(webui.tm, "stop", lambda slot: (True, f"остановлено {slot}"))
    res = webui.api_verify_stop()
    assert res["ok"] is True


def test_api_verify_report_reads_file(temp_paths, monkeypatch):
    _, _, root = temp_paths
    report = root / "verify_report.md"
    monkeypatch.setattr(paths, "VERIFY_REPORT_FILE", report)
    assert webui.api_verify_report()["report"] == ""
    report.write_text("# Проверка файлов", encoding="utf-8")
    assert "Проверка файлов" in webui.api_verify_report()["report"]


# ── упаковка и окно ─────────────────────────────────────────────────────────


def _project() -> Path:
    return Path(__file__).resolve().parents[1]


def test_verify_worker_is_packaged():
    spec = (_project() / "yadisk_backup.spec").read_text(encoding="utf-8")
    assert "'verify'" in spec
    assert "'verify_local'" in spec
    assert webui.WORKER_SCRIPTS["verify_local.py"] == "verify_local"


def test_verify_card_in_ui():
    html = (_project() / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert 'id="verify-card"' in html
    assert 'id="verify-start-btn"' in html
    assert 'id="verify-stop-btn"' in html
    assert "function renderVerify(" in html
    assert "function startVerify(" in html
    # проверка не прячет кнопки выгрузки
    assert "k !== 'verify'" in html
