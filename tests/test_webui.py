"""Тесты серверной логики дашборда (без запуска uvicorn)."""

import sqlite3

import pytest

import webui


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
