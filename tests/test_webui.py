"""Тесты серверной логики дашборда (без запуска uvicorn)."""

import os
import sqlite3
from pathlib import Path

import pytest

import webui

PROJECT_DIR = Path(__file__).resolve().parents[1]


# ── классификация ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("disk:/a/photo.JPG", "photos"),
        ("disk:/a/clip.mp4", "videos"),
        ("disk:/a/song.flac", "audio"),
        ("disk:/a/doc.pdf", "documents"),
        ("disk:/a/pack.7z", "archives"),
        ("disk:/a/setup.exe", "programs"),
        ("disk:/a/noext", "other"),
        ("disk:/a/weird.qqq", "other"),
    ],
)
def test_categorize(path, expected):
    assert webui.categorize(path) == expected


# ── маскировка токена ───────────────────────────────────────────────────────


def test_mask_token_hides_middle():
    token = "y0_AgAAAABsecretsecretsecret"
    masked = webui.mask_token(token)
    assert "secretsecret" not in masked
    assert masked.startswith("y0_AgA")


def test_mask_token_empty():
    assert webui.mask_token("") == ""
    assert webui.mask_token("short") == ""


def test_public_config_never_exposes_raw_token():
    cfg = {
        "token": "y0_AgAAAABsecretsecretsecret",
        "download_dir": r"C:\NeUnlim",
        "unlim_dir": r"C:\Unlim",
        "disk_dir": r"C:\NeUnlim",
        "max_workers": 4,
    }
    public = webui.public_config(cfg)
    assert "token" not in public
    assert public["token_masked"].startswith("y0_AgA")
    assert "secretsecret" not in public["token_masked"]


# ── защита organize от системных папок ──────────────────────────────────────


def test_organize_rejects_windows_dir():
    ok, msg = webui._is_safe_organize_path(r"C:\Windows")
    assert ok is False
    assert "Системная" in msg


def test_organize_rejects_program_files_subdir():
    ok, _ = webui._is_safe_organize_path(r"C:\Program Files\что-то")
    assert ok is False


def test_organize_rejects_missing_dir(tmp_path):
    ok, msg = webui._is_safe_organize_path(str(tmp_path / "нет-такой"))
    assert ok is False
    assert "не существует" in msg


def test_organize_allows_normal_dir(tmp_path):
    ok, msg = webui._is_safe_organize_path(str(tmp_path))
    assert ok is True, msg


# ── фильтр по категориям ────────────────────────────────────────────────────


@pytest.fixture()
def disk_db(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE files (
            remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,
            status TEXT NOT NULL DEFAULT 'pending', local_path TEXT,
            error TEXT, downloaded_at TIMESTAMP
        );
        """
    )
    con.executemany(
        "INSERT INTO files (remote_path, size, status) VALUES (?, ?, ?)",
        [
            ("disk:/a.jpg", 10, "pending"),
            ("disk:/b.mp4", 20, "pending"),
            ("disk:/c.pdf", 30, "pending"),
            ("disk:/d.jpg", 40, "downloaded"),
        ],
    )
    con.commit()
    con.close()
    return db


def statuses(db):
    con = sqlite3.connect(db)
    try:
        return dict(con.execute("SELECT remote_path, status FROM files"))
    finally:
        con.close()


def test_filter_keeps_only_selected_categories(disk_db):
    skipped, kept = webui.filter_to_categories(disk_db, "files", "remote_path", ["photos"])
    assert (skipped, kept) == (2, 1)
    st = statuses(disk_db)
    assert st["disk:/a.jpg"] == "pending"
    assert st["disk:/b.mp4"] == "skipped"
    assert st["disk:/c.pdf"] == "skipped"
    assert st["disk:/d.jpg"] == "downloaded"  # скачанное не трогаем


def test_filter_is_reversible(disk_db):
    webui.filter_to_categories(disk_db, "files", "remote_path", ["photos"])
    n = webui.unskip_category_marker(disk_db, "files")
    assert n == 2
    st = statuses(disk_db)
    assert st["disk:/b.mp4"] == "pending"
    assert st["disk:/c.pdf"] == "pending"


def test_unskip_does_not_touch_other_skips(disk_db):
    con = sqlite3.connect(disk_db)
    con.execute("UPDATE files SET status='skipped', error='duplicate (md5 match)' "
                "WHERE remote_path='disk:/c.pdf'")
    con.commit()
    con.close()

    webui.filter_to_categories(disk_db, "files", "remote_path", ["photos"])
    webui.unskip_category_marker(disk_db, "files")
    st = statuses(disk_db)
    assert st["disk:/c.pdf"] == "skipped"  # дубликат остался дубликатом


# ── статистика ──────────────────────────────────────────────────────────────


def test_db_stats_counts(disk_db):
    out = webui.db_stats(disk_db, "files", "remote_path")
    assert out["total"] == 4
    assert out["pending"] == 3
    assert out["downloaded"] == 1
    assert out["bytes_total"] == 100
    assert out["bytes_done"] == 40


def test_db_stats_missing_file(tmp_path):
    out = webui.db_stats(tmp_path / "нет.db", "files", "remote_path")
    assert out["total"] == 0
    assert out["recent"] == []


def test_preview_groups_by_category(disk_db):
    out = webui.preview_db(disk_db, "files", "remote_path")
    assert out["empty"] is False
    assert out["categories"]["photos"]["count"] == 2
    assert out["categories"]["videos"]["count"] == 1
    assert out["categories"]["documents"]["count"] == 1


def test_local_size_marks_partial_scan(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"12345")
    webui._local_size_cache.clear()
    out = webui.local_size(str(tmp_path))
    assert out["bytes"] == 5
    assert out["partial"] is False


# ── пересоздание базы Безлимита ─────────────────────────────────────────────


def _make_unlim_db(path):
    con = sqlite3.connect(path)
    with con:
        con.execute("CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY)")
        con.execute("INSERT INTO unlim_files VALUES ('f1')")
    con.close()


def test_rebuild_requires_confirm_word(monkeypatch, tmp_path):
    db = tmp_path / "unlim_state.db"
    _make_unlim_db(db)
    monkeypatch.setattr(webui, "UNLIM_DB_FILE", db)
    started = []
    monkeypatch.setattr(webui.tm, "start",
                        lambda *a, **k: started.append(a) or (True, "ok"))

    out = webui.unlim_rebuild(webui.RebuildPayload(confirm="да"))

    assert out["ok"] is False
    assert started == []
    assert db.exists()


def test_rebuild_saves_backup_and_forces_rescan(monkeypatch, tmp_path):
    db = tmp_path / "unlim_state.db"
    _make_unlim_db(db)
    monkeypatch.setattr(webui, "UNLIM_DB_FILE", db)
    started = []
    monkeypatch.setattr(webui.tm, "start",
                        lambda *a, **k: started.append(a) or (True, "ok"))

    out = webui.unlim_rebuild(webui.RebuildPayload(confirm=" пересоздать "))

    assert out["ok"] is True
    assert started == [("unlim", "unlim_backup.py", ["--scan-only", "--force"])]
    backups = list(tmp_path.glob("unlim_state.bak-*.db"))
    assert len(backups) == 1
    assert out["backup"] == str(backups[0])
    con = sqlite3.connect(backups[0])
    rows = con.execute("SELECT file_id FROM unlim_files").fetchall()
    con.close()
    assert rows == [("f1",)]


def test_rebuild_refuses_when_unlim_running(monkeypatch, tmp_path):
    db = tmp_path / "unlim_state.db"
    _make_unlim_db(db)
    monkeypatch.setattr(webui, "UNLIM_DB_FILE", db)
    started = []

    class Alive:
        def alive(self):
            return True

    monkeypatch.setitem(webui.tm._tasks, "unlim_w1", Alive())
    try:
        monkeypatch.setattr(webui.tm, "start",
                            lambda *a, **k: started.append(a) or (True, "ok"))
        out = webui.unlim_rebuild(webui.RebuildPayload(confirm="ПЕРЕСОЗДАТЬ"))
    finally:
        webui.tm._tasks.pop("unlim_w1", None)

    assert out["ok"] is False
    assert started == []
    assert list(tmp_path.glob("unlim_state.bak-*.db")) == []


# ── резервные копии баз ─────────────────────────────────────────────────────


def test_backup_restore_requires_confirm_word(monkeypatch):
    calls = []
    monkeypatch.setattr(webui.backups, "restore_backup",
                        lambda *a, **k: calls.append((a, k)) or {})

    out = webui.api_backups_restore(
        webui.BackupRestorePayload(id="disk-20261027-153000-manual.db", confirm="да")
    )

    assert out["ok"] is False
    assert calls == []


def test_backup_restore_refuses_while_tasks_run(monkeypatch):
    class Alive:
        def alive(self):
            return True

    calls = []
    monkeypatch.setattr(webui.backups, "restore_backup",
                        lambda *a, **k: calls.append((a, k)) or {})
    monkeypatch.setitem(webui.tm._tasks, "unlim_w1", Alive())
    try:
        out = webui.api_backups_restore(
            webui.BackupRestorePayload(
                id="disk-20261027-153000-manual.db", confirm="ВОССТАНОВИТЬ"
            )
        )
    finally:
        webui.tm._tasks.pop("unlim_w1", None)

    assert out["ok"] is False
    assert "остановите" in out["msg"]
    assert calls == []


def test_backup_create_returns_item(monkeypatch):
    fake = {"id": "disk-20261027-153000-manual.db", "scope": "disk"}
    monkeypatch.setattr(webui.backups, "create_backup",
                        lambda *a, **k: fake)

    out = webui.api_backups_create(webui.BackupCreatePayload(scope="disk"))

    assert out == {"ok": True, "item": fake}


# ── полный цикл «Скачать всё» ───────────────────────────────────────────────


def _make_db(path, rows):
    con = sqlite3.connect(path)
    try:
        with con:
            con.execute(
                "CREATE TABLE files "
                "(remote_path TEXT PRIMARY KEY, size INTEGER, status TEXT)"
            )
            con.executemany("INSERT INTO files VALUES (?, ?, ?)", rows)
    finally:
        con.close()


def test_full_cycle_snapshot_counts_downloaded_and_errors(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db, [
        ("a", 10, "downloaded"),
        ("b", 20, "downloaded"),
        ("c", 30, "failed"),
        ("d", 40, "pending"),
        ("e", 50, "skipped"),
    ])

    snap = webui.FullCycle._db_snapshot(db, "files", ("failed",))

    assert snap == {"downloaded": 2, "errors": 1, "bytes": 30}


def test_full_cycle_snapshot_missing_db_is_empty(tmp_path):
    snap = webui.FullCycle._db_snapshot(
        tmp_path / "nope.db", "unlim_files", ("skipped",)
    )

    assert snap == {"downloaded": 0, "errors": 0, "bytes": 0}


def test_full_cycle_refuses_while_task_slot_is_busy():
    class Alive:
        def alive(self):
            return True

    webui.tm._tasks["disk"] = Alive()
    try:
        ok, msg = webui.full_cycle.start(workers=4, passes=3)
    finally:
        webui.tm._tasks.pop("disk", None)

    assert ok is False
    assert "Уже идёт" in msg
    assert webui.full_cycle.is_running() is False


def test_full_cycle_clamps_passes_and_workers(monkeypatch):
    """Значения из формы не могут выйти за границы 1–5 проходов и 1–24 потоков."""
    captured = {}

    def fake_run(self):
        captured["passes"] = self.passes
        captured["workers"] = self.workers

    monkeypatch.setattr(webui.FullCycle, "_run", fake_run)
    saved = (webui.full_cycle.state, webui.full_cycle.summary,
             webui.full_cycle._thread)
    try:
        ok, _ = webui.full_cycle.start(workers=999, passes=99)
        thread = webui.full_cycle._thread
        if thread is not None:
            thread.join(timeout=5)
    finally:
        (webui.full_cycle.state, webui.full_cycle.summary,
         webui.full_cycle._thread) = saved

    assert ok is True
    assert captured == {"passes": 5, "workers": 24}


def test_download_all_passes_payload_to_full_cycle(monkeypatch):
    calls = []
    monkeypatch.setattr(
        webui.full_cycle, "start",
        lambda workers, passes, sort_after: calls.append(
            (workers, passes, sort_after)) or (True, "ok"),
    )

    out = webui.api_download_all(
        webui.DownloadAllPayload(workers=6, passes=2, sort_after=True)
    )

    assert out["ok"] is True
    assert calls == [(6, 2, True)]


def test_download_all_payload_defaults():
    payload = webui.DownloadAllPayload()

    assert payload.workers == 10
    assert payload.passes == 3
    assert payload.sort_after is False


def test_download_start_refuses_unavailable_folder(monkeypatch, tmp_path):
    """Отключённый диск должен давать отказ с текстом, а не падение процесса."""
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    monkeypatch.setattr(webui, "get_config",
                        lambda: {"disk_dir": str(blocker / "sub")})
    started = []
    monkeypatch.setattr(webui.tm, "start",
                        lambda *a, **k: started.append(a) or (True, "ok"))

    out = webui.api_download_start(
        webui.DownloadControlPayload(mode="disk")
    )

    assert out["ok"] is False
    assert "Папка выгрузки недоступна" in out["msg"]
    assert started == []


# ── обновление программы ────────────────────────────────────────────────────


def test_write_update_helper_waits_and_runs_silent_setup(tmp_path):
    import os

    payload = tmp_path / "YandexDiskBackup-99.0.0-setup.exe"
    payload.write_bytes(b"x")

    helper = webui.write_update_helper(payload, "setup", restart=True)
    text = helper.read_text(encoding="utf-8-sig")

    assert f"Wait-Process -Id {os.getpid()}" in text
    assert "/SILENT" in text
    assert "Start-Process -FilePath" in text
    assert "Remove-Item" in text
    # След в логе — по нему видно, что помощник вообще запустился.
    assert "помощник запущен" in text
    # Ошибка установщика не должна проходить молча.
    assert "MessageBox" in text


def test_write_update_helper_portable_expands_archive(tmp_path):
    payload = tmp_path / "YandexDiskBackup-99.0.0-portable.zip"
    payload.write_bytes(b"x")

    helper = webui.write_update_helper(payload, "portable", restart=False)
    text = helper.read_text(encoding="utf-8-sig")

    assert "Expand-Archive" in text
    assert "/SILENT" not in text


def test_update_install_refuses_when_not_frozen(monkeypatch):
    import os

    if os.name != "nt":
        pytest.skip("установка обновлений только для Windows")
    monkeypatch.setattr(webui.paths, "IS_FROZEN", False)
    out = webui.api_updates_install(webui.UpdateInstallPayload())
    assert out["ok"] is False
    assert "исходников" in out["msg"]


def test_update_install_requires_downloaded_file(monkeypatch, tmp_path):
    import os

    if os.name != "nt":
        pytest.skip("установка обновлений только для Windows")
    monkeypatch.setattr(webui.paths, "IS_FROZEN", True)
    monkeypatch.setattr(webui.update_manager, "state", "idle")
    out = webui.api_updates_install(webui.UpdateInstallPayload())
    assert out["ok"] is False
    assert "не загружено" in out["msg"]


def test_update_install_refuses_while_tasks_run(monkeypatch, tmp_path):
    import os

    if os.name != "nt":
        pytest.skip("установка обновлений только для Windows")

    class Alive:
        def alive(self):
            return True

    payload = tmp_path / "setup.exe"
    payload.write_bytes(b"x")
    monkeypatch.setattr(webui.paths, "IS_FROZEN", True)
    monkeypatch.setattr(webui.update_manager, "state", "ready")
    monkeypatch.setattr(webui.update_manager, "path", payload)
    monkeypatch.setitem(webui.tm._tasks, "disk", Alive())
    try:
        out = webui.api_updates_install(webui.UpdateInstallPayload())
    finally:
        webui.tm._tasks.pop("disk", None)

    assert out["ok"] is False
    assert "остановите" in out["msg"]


def test_update_manager_status_defaults():
    status = webui.update_manager.status()
    for key in ("state", "version", "percent", "can_install", "running"):
        assert key in status
    assert isinstance(status["percent"], float)


# ── установка без прав администратора ───────────────────────────────────────


def _fake_exe(monkeypatch, tmp_path, subdir="AppData/Programs/YandexDiskBackup"):
    exe_dir = tmp_path / subdir
    exe_dir.mkdir(parents=True, exist_ok=True)
    exe = exe_dir / "YandexDiskBackup.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(webui.paths, "IS_FROZEN", True)
    monkeypatch.setattr(webui.sys, "executable", str(exe))
    webui.reset_install_dir_cache()
    return exe_dir


@pytest.mark.skipif(os.name != "nt", reason="реестр Windows")
def test_per_user_install_updates_with_setup_not_zip(monkeypatch, tmp_path):
    exe_dir = _fake_exe(monkeypatch, tmp_path)
    monkeypatch.setattr(webui, "installed_app_dirs", lambda *_a, **_k: [exe_dir])
    # %LOCALAPPDATA%\Programs — не portable: обновляем установщиком.
    assert webui.UpdateManager.is_portable() is False


@pytest.mark.skipif(os.name != "nt", reason="реестр Windows")
def test_unpacked_zip_copy_is_portable(monkeypatch, tmp_path):
    _fake_exe(monkeypatch, tmp_path, "Downloads/YandexDiskBackup")
    monkeypatch.setattr(
        webui, "installed_app_dirs",
        lambda *_a, **_k: [tmp_path / "Program Files" / "YandexDiskBackup"],
    )
    assert webui.UpdateManager.is_portable() is True


@pytest.mark.skipif(os.name != "nt", reason="реестр Windows")
def test_legacy_install_is_reported_only_for_another_folder(monkeypatch, tmp_path):
    exe_dir = _fake_exe(monkeypatch, tmp_path)
    legacy = tmp_path / "Program Files" / "YandexDiskBackup"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(webui, "installed_app_dirs", lambda *_a, **_k: [legacy])
    monkeypatch.setattr(webui, "_in_program_files", lambda _p: True)
    assert webui.legacy_install_dir() == legacy.resolve()
    assert webui.legacy_install_status() == str(legacy.resolve())

    # Реестр указывает на нашу же папку — старой копии нет.
    monkeypatch.setattr(webui, "installed_app_dirs", lambda *_a, **_k: [exe_dir])
    assert webui.legacy_install_dir() is None
    assert webui.legacy_install_status() == ""


def test_source_run_is_never_portable(monkeypatch):
    monkeypatch.setattr(webui.paths, "IS_FROZEN", False)
    assert webui.UpdateManager.is_portable() is False
    assert webui.legacy_install_dir() is None


@pytest.mark.skipif(os.name != "nt", reason="реестр Windows")
def test_legacy_install_found_next_to_per_user_copy(monkeypatch, tmp_path):
    """Две записи в реестре: своя пер-пользовательская и старая system-wide."""
    exe_dir = _fake_exe(monkeypatch, tmp_path)
    legacy = tmp_path / "Program Files" / "YandexDiskBackup"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(webui, "installed_app_dirs",
                        lambda *_a, **_k: [exe_dir, legacy])
    monkeypatch.setattr(webui, "_in_program_files", lambda _p: True)
    assert webui.UpdateManager.is_portable() is False
    assert webui.legacy_install_dir() == legacy.resolve()


def test_update_helper_restarts_the_newly_installed_copy(tmp_path):
    """После setup.exe нельзя запускать старый путь из Program Files."""
    payload = tmp_path / "YandexDiskBackup-99.0.0-setup.exe"
    payload.write_bytes(b"x")

    text = webui.write_update_helper(payload, "setup", restart=True).read_text(
        encoding="utf-8-sig"
    )
    assert webui.APP_UNINSTALL_KEY in text
    assert "InstallLocation" in text
    assert "Start-Process -FilePath $target" in text


# ── сессионные счётчики (35.5.7) ────────────────────────────────────────────


def test_session_counters_are_cleared_before_a_new_run(monkeypatch, tmp_path):
    """Чужие числа прошлой сессии не должны висеть на плашках нового запуска."""
    unlim = tmp_path / "download_status.json"
    disk = tmp_path / "disk_download_status.json"
    unlim.write_text('{"success": 136}', encoding="utf-8")
    disk.write_text('{"downloaded": 7}', encoding="utf-8")
    monkeypatch.setattr(webui.paths, "UNLIM_STATUS_FILE", unlim)
    monkeypatch.setattr(webui.paths, "DISK_STATUS_FILE", disk)

    webui.reset_session_counters("unlim")
    assert not unlim.exists()
    assert disk.exists()          # чужой вид не тронут

    webui.reset_session_counters("disk")
    assert not disk.exists()

    # Повторный вызов не падает, когда файла уже нет.
    webui.reset_session_counters("disk")


def test_status_writers_report_process_start():
    """По started_at интерфейс отличает текущую сессию от прошлой."""
    downloader_src = (PROJECT_DIR / "downloader.py").read_text(encoding="utf-8")
    assert "SESSION_STARTED_AT = time.time()" in downloader_src
    assert '"started_at": SESSION_STARTED_AT' in downloader_src

    direct_src = (PROJECT_DIR / "unlim_direct.py").read_text(encoding="utf-8")
    assert "SESSION_STARTED_AT = time.time()" in direct_src
    assert '"started_at": SESSION_STARTED_AT' in direct_src


def test_task_is_dead_once_stdout_ends():
    """EOF в stdout — процесс дописал; полный цикл не должен ждать его вечно."""
    import queue

    class Lines:
        """Строки «как из процесса»: блокируются, пока в очередь ничего не положат."""

        def __init__(self):
            self.q: queue.Queue = queue.Queue()

        def __iter__(self):
            return self

        def __next__(self):
            item = self.q.get(timeout=5)
            if item is None:
                raise StopIteration
            return item

    class FakeProc:
        def __init__(self):
            self.code = None
            self.stdout = Lines()

        def poll(self):
            return self.code

        def wait(self, timeout=None):
            return 0

    proc = FakeProc()
    task = webui.Task("disk", proc)
    assert task.alive()

    # poll() продолжает отдавать None, но поток чтения уже завершился — задача мертва.
    proc.stdout.q.put(None)
    task._t.join(timeout=5)
    assert task.finished
    assert not task.alive()

# ── 35.5.9: итоги сессии для плашек и таймера ───────────────────────────────


def _build_session(fc=None, tasks=None, unlim_st=None, disk_st=None,
                   disk_db=None, unlim_db=None, retry=None):
    return webui.build_session(
        fc or {}, tasks or {}, unlim_st or {}, disk_st or {},
        disk_db or {}, unlim_db or {}, retry or {},
    )


def _lane(over=None):
    lane = {"state": "ожидание", "pass": 0, "passes": 3,
            "downloaded": 0, "bytes": 0, "errors": None, "error": None}
    lane.update(over or {})
    return lane


def test_session_task_counts_downloaded_and_db_failures():
    """Скачано — из файла статуса, сбойные — из базы (каждый файл один раз)."""
    sess = _build_session(
        tasks={"disk": {"running": True, "started_at": 100.0, "exit_code": None}},
        disk_st={"started_at": 100.0, "updated_at": 200.0,
                 "downloaded": 58, "failed": 3},
        disk_db={"pending": 10, "failed": 451},
    )

    disk = sess["disk"]
    assert disk["success"] == 58
    assert disk["failed"] == 451
    assert disk["remaining"] == 10       # обычный запуск не повторяет сбойные
    assert disk["running"] is True
    assert disk["preparing"] is False
    assert sess["unlim"] is None


def test_session_task_without_fresh_status_is_preparing():
    """Числа прошлой сессии не выдаются за текущие."""
    sess = _build_session(
        tasks={"unlim": {"running": True, "started_at": 100.0, "exit_code": None}},
        unlim_st={"started_at": 10.0, "updated_at": 20.0, "success": 99},
        unlim_db={"pending": 5, "skipped": 0},
    )

    assert sess["unlim"]["preparing"] is True
    assert sess["unlim"]["success"] == 0


def test_session_cycle_adds_live_pass_to_accumulated_total():
    """Плашка растёт за все проходы: итог линии + живой текущий проход."""
    sess = _build_session(
        fc={"started_at": 100.0, "finished_at": None, "running": True,
            "lanes": {"disk": _lane({"state": "скачивание, проход 2",
                                     "downloaded": 50, "pass": 2}),
                      "unlim": _lane({"state": "сбор списка"})}},
        tasks={"disk": {"running": True, "started_at": 300.0, "exit_code": None}},
        disk_st={"started_at": 300.0, "updated_at": 400.0, "downloaded": 5},
        disk_db={"pending": 7, "failed": 451},
        unlim_db={"pending": 3, "skipped": 0},
    )

    assert sess["disk"]["success"] == 55
    assert sess["disk"]["pass"] == 2
    assert sess["disk"]["remaining"] == 7 + 451   # цикл повторяет сбойные
    assert sess["unlim"]["preparing"] is True


def test_session_cycle_totals_stay_after_finish_and_stop():
    """После завершения/остановки цикл остаётся владельцем сессии."""
    sess = _build_session(
        fc={"started_at": 100.0, "finished_at": 500.0, "running": False,
            "lanes": {"disk": _lane({"state": "готово", "downloaded": 63,
                                     "pass": 2}),
                      "unlim": _lane({"state": "остановлено",
                                      "downloaded": 19, "pass": 1})}},
        tasks={"disk": {"running": False, "started_at": 300.0, "exit_code": 0}},
        disk_db={"pending": 0, "failed": 451},
        unlim_db={"pending": 0, "skipped": 922},
    )

    assert sess["disk"]["success"] == 63
    assert sess["unlim"]["success"] == 19
    assert sess["unlim"]["running"] is False
    assert sess["unlim"]["failed"] == 922


def test_session_task_started_after_cycle_takes_over():
    sess = _build_session(
        fc={"started_at": 100.0, "finished_at": 500.0, "running": False,
            "lanes": {"disk": _lane({"state": "готово", "downloaded": 63,
                                     "pass": 2})}},
        tasks={"disk": {"running": True, "started_at": 600.0, "exit_code": None}},
        disk_st={"started_at": 600.0, "updated_at": 700.0, "downloaded": 4},
        disk_db={"pending": 2, "failed": 3},
    )

    assert sess["disk"]["success"] == 4
    assert sess["disk"]["started_at"] == 600.0
    assert sess["disk"]["passes"] == 0


def test_session_retry_mode_counts_failed_as_remaining():
    sess = _build_session(
        tasks={"unlim": {"running": True, "started_at": 100.0, "exit_code": None}},
        unlim_st={"started_at": 100.0, "updated_at": 200.0, "success": 1},
        unlim_db={"pending": 4, "skipped": 6},
        retry={"unlim": True},
    )

    assert sess["unlim"]["remaining"] == 10
