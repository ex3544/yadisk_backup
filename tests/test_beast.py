"""Режим «Зверь», heartbeat и плашки, переживающие перезапуск (35.8.4)."""

import sqlite3

import pytest

import paths
import webui


# ── статус и итог сессии ────────────────────────────────────────────────────


def test_stale_status_stops_showing_phantom_speed():
    """Статус «active» без задачи гасится, но не сразу: процесс мог писать."""
    st = {"active": True, "updated_at": 1000.0, "speed_mb": 5, "speed_mbit": 40}

    assert webui._mark_stale_inactive(st, lane_running=True, now=9999.0) is st
    assert webui._mark_stale_inactive(st, lane_running=False, now=1030.0) is st
    out = webui._mark_stale_inactive(st, lane_running=False, now=1200.0)
    assert out is not st
    assert out["active"] is False
    assert out["speed_mb"] == 0 and out["speed_mbit"] == 0
    assert st["active"] is True  # исходный словарь не портим
    assert webui._mark_stale_inactive({}, lane_running=False, now=1.0) == {}


def test_last_session_survives_restart(monkeypatch, tmp_path):
    """Итог прошлой сессии остаётся на плашке после перезапуска приложения."""
    monkeypatch.setattr(paths, "LAST_SESSION_FILE", tmp_path / "last_session.json")
    live = {
        "disk": {"started_at": 100.0, "success": 12, "pass": 2, "passes": 3,
                 "mode": "all", "total": 50, "running": True},
        "unlim": {"started_at": 100.0, "success": 0, "pass": 0, "passes": 0,
                  "mode": "all", "total": 0, "preparing": True},
    }
    webui.remember_sessions(live)
    saved = webui._load_last_sessions()
    assert saved["disk"]["success"] == 12
    assert saved["disk"]["passes"] == 3
    # «Подготовка» — это ещё не итог: ноль не затирает прошлый результат.
    assert saved["unlim"]["success"] == 0

    db_disk = {"failed": 4, "waiting": 2, "lost": 1, "pending": 3,
               "in_progress": 1, "bytes_by_status": {"pending": 700,
                                                     "in_progress": 100}}
    db_unlim = {"skipped": 8, "pending": 0, "in_progress": 0}
    out = webui.restore_sessions({"disk": None, "unlim": None}, db_disk, db_unlim)
    assert out["disk"]["restored"] is True
    assert out["disk"]["running"] is False
    assert out["disk"]["success"] == 12
    assert out["disk"]["failed"] == 4
    assert out["disk"]["waiting"] == 2
    assert out["disk"]["remaining"] == 4
    assert out["disk"]["remaining_bytes"] == 800
    assert out["unlim"]["failed"] == 8

    # Живая сессия важнее сохранённой — её не подменяем.
    live_session = {"disk": {"success": 1}, "unlim": None}
    out = webui.restore_sessions(live_session, db_disk, db_unlim)
    assert out["disk"] == {"success": 1}


def test_remember_sessions_keeps_restored_record(monkeypatch, tmp_path):
    """Восстановленная запись не перезаписывается сама собой."""
    monkeypatch.setattr(paths, "LAST_SESSION_FILE", tmp_path / "last_session.json")
    rec = {"started_at": 1.0, "success": 5, "pass": 1, "passes": 1,
           "mode": "all", "total": 5}
    webui._save_last_sessions({"disk": rec})
    webui.remember_sessions({"disk": {"restored": True, "success": 0,
                                      "started_at": 1.0}})
    assert webui._load_last_sessions()["disk"] == rec


# ── «Зверь»: база ──────────────────────────────────────────────────────────


def _beast_dbs(tmp_path, monkeypatch):
    """Две пустые базы с нужными колонками и пути на временную папку."""
    disk = tmp_path / "state.db"
    unlim = tmp_path / "unlim.db"
    con = sqlite3.connect(disk)
    con.execute(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,"
        " status TEXT, local_path TEXT, error TEXT, downloaded_at TIMESTAMP,"
        " attempts INTEGER DEFAULT 0, retry_after INTEGER,"
        " server_fails INTEGER DEFAULT 0)"
    )
    con.commit()
    con.close()
    con = sqlite3.connect(unlim)
    con.execute(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT,"
        " size INTEGER, status TEXT, error TEXT, attempts INTEGER DEFAULT 0,"
        " retry_after INTEGER, server_fails INTEGER DEFAULT 0,"
        " assigned_at TIMESTAMP)"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(webui, "DB_FILE", disk)
    monkeypatch.setattr(webui, "UNLIM_DB_FILE", unlim)
    monkeypatch.setattr(paths, "DISK_HEARTBEAT_FILE", tmp_path / "disk_hb")
    monkeypatch.setattr(paths, "UNLIM_HEARTBEAT_FILE", tmp_path / "unlim_hb")
    return disk, unlim


def test_beast_reset_returns_lost_and_clears_counters(monkeypatch, tmp_path):
    disk, unlim = _beast_dbs(tmp_path, monkeypatch)
    con = sqlite3.connect(disk)
    con.executemany(
        "INSERT INTO files (remote_path, size, status, error, server_fails,"
        " retry_after) VALUES (?, 10, ?, ?, ?, ?)",
        [
            ("a", "lost", "yandex_lost", 3, 999),
            ("b", "in_progress", None, 0, None),
            ("c", "pending", "storage_down", 0, 12345),
            ("d", "downloaded", None, 0, None),
        ],
    )
    con.commit()
    con.close()
    con = sqlite3.connect(unlim)
    con.executemany(
        "INSERT INTO unlim_files (file_id, status, error, attempts,"
        " server_fails, retry_after, assigned_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("u1", "lost", "yandex_lost", 4, 3, 999, 1),
            ("u2", "skipped", "404", 3, 0, None, 1),
            ("u3", "in_progress", None, 1, 0, None, 5),
        ],
    )
    con.commit()
    con.close()

    b = webui.Beast()
    # Диск: lost + in_progress; Безлимит: lost + skipped + in_progress.
    assert b._reset_lost() == 5

    con = sqlite3.connect(disk)
    rows = dict(con.execute("SELECT remote_path, status FROM files"))
    fails = dict(con.execute("SELECT remote_path, server_fails FROM files"))
    pauses = dict(con.execute("SELECT remote_path, retry_after FROM files"))
    con.close()
    assert rows == {"a": "pending", "b": "pending", "c": "pending",
                    "d": "downloaded"}
    assert fails["a"] == 0
    assert pauses["c"] is None

    con = sqlite3.connect(unlim)
    rows = dict(con.execute("SELECT file_id, status FROM unlim_files"))
    fails = dict(con.execute("SELECT file_id, server_fails FROM unlim_files"))
    attempts = dict(con.execute("SELECT file_id, attempts FROM unlim_files"))
    con.close()
    assert rows == {"u1": "pending", "u2": "pending", "u3": "pending"}
    assert fails["u1"] == 0
    # У Безлимита попытки не обнуляются: по ним видно вечно недоступные файлы.
    assert attempts["u2"] == 3


def test_beast_marks_unavailable_only_after_three_attempts(monkeypatch, tmp_path):
    """404/410 — навсегда deleted; 403 — только denied, не «удалён навсегда»."""
    disk, unlim = _beast_dbs(tmp_path, monkeypatch)
    con = sqlite3.connect(disk)
    con.executemany(
        "INSERT INTO files (remote_path, size, status, error, attempts)"
        " VALUES (?, 1, 'failed', ?, ?)",
        [("gone", "HTTP 404: файла на сервере нет", 3),
         ("gone410", "HTTP 410 Gone", 3),
         ("soon", "HTTP 404: файла на сервере нет", 1),
         ("denied", "HTTP 403 от сервера хранения", 4)],
    )
    con.commit()
    con.close()
    con = sqlite3.connect(unlim)
    con.executemany(
        "INSERT INTO unlim_files (file_id, status, error, attempts)"
        " VALUES (?, 'skipped', ?, 3)",
        [("u", "HTTP 404"), ("u403", "CDN HTTP 403")],
    )
    con.commit()
    con.close()

    b = webui.Beast()
    b._mark_unavailable()
    con = sqlite3.connect(disk)
    rows = dict(con.execute("SELECT remote_path, status FROM files"))
    con.close()
    assert rows == {"gone": "deleted", "gone410": "deleted", "soon": "failed",
                    "denied": "denied"}
    con = sqlite3.connect(unlim)
    rows = dict(con.execute("SELECT file_id, status FROM unlim_files"))
    con.close()
    assert rows == {"u": "deleted", "u403": "denied"}

    # denied не держат «Зверя»: в остатке только файл с одной попыткой.
    assert b._remaining("disk") == 1
    assert b._remaining("unlim") == 0


def test_manual_reset_returns_denied_and_lost_but_not_deleted(monkeypatch, tmp_path):
    """Кнопка «Отложенные сервером» возвращает lost и denied, 404 не оживляет."""
    disk, unlim = _beast_dbs(tmp_path, monkeypatch)
    con = sqlite3.connect(disk)
    con.executemany(
        "INSERT INTO files (remote_path, size, status, error, attempts,"
        " server_fails) VALUES (?, 1, ?, ?, ?, ?)",
        [("a", "lost", "HTTP 500", 3, 3),
         ("b", "denied", "HTTP 403", 3, 0),
         ("c", "deleted", "HTTP 404", 3, 0)],
    )
    con.commit()
    con.close()
    con = sqlite3.connect(unlim)
    con.execute(
        "INSERT INTO unlim_files (file_id, status, error, attempts)"
        " VALUES ('u', 'denied', 'CDN HTTP 403', 3)")
    con.commit()
    con.close()

    monkeypatch.setattr(webui, "_any_task_running", lambda *_: False)
    monkeypatch.setattr(webui, "_auto_backup",
                        lambda *a, **k: (True, {"id": "b1"}, ""))
    out = webui.api_reset(webui.ResetPayload(what="lost"))

    assert out["ok"] is True
    assert out["count"] == 3
    assert "Отложенных" in out["msg"]
    con = sqlite3.connect(disk)
    rows = dict(con.execute("SELECT remote_path, status FROM files"))
    fails = dict(con.execute("SELECT remote_path, server_fails FROM files"))
    attempts = dict(con.execute("SELECT remote_path, attempts FROM files"))
    con.close()
    assert rows == {"a": "pending", "b": "pending", "c": "deleted"}
    assert fails["a"] == 0 and attempts["b"] == 0
    con = sqlite3.connect(unlim)
    assert con.execute(
        "SELECT status FROM unlim_files WHERE file_id='u'").fetchone()[0] == "pending"
    con.close()


def test_beast_counts_remaining_and_progress(monkeypatch, tmp_path):
    disk, unlim = _beast_dbs(tmp_path, monkeypatch)
    con = sqlite3.connect(disk)
    con.executemany(
        "INSERT INTO files (remote_path, size, status) VALUES (?, ?, ?)",
        [("a", 10, "pending"), ("b", 20, "downloaded"),
         ("c", 30, "lost"), ("d", 40, "failed")],
    )
    con.commit()
    con.close()
    con = sqlite3.connect(unlim)
    con.executemany(
        "INSERT INTO unlim_files (file_id, size, status) VALUES (?, ?, ?)",
        [("u1", 5, "pending"), ("u2", 6, "skipped"),
         ("u3", 7, "downloaded")],
    )
    con.commit()
    con.close()

    b = webui.Beast()
    assert b._remaining("disk") == 3
    assert b._remaining("unlim") == 2
    assert b._progress("disk") == (1, 20)
    assert b._progress("unlim") == (1, 7)


# ── «Зверь»: сценарии ──────────────────────────────────────────────────────


def test_beast_accumulates_gained_per_lane_across_rounds(monkeypatch):
    """Итог «Зверя» по линиям не сбрасывается при смене тактики."""
    b = webui.Beast()
    monkeypatch.setattr(b, "_reset_lost", lambda: 0)
    monkeypatch.setattr(b, "_refresh_remaining", lambda: None)
    monkeypatch.setattr(b, "_remaining", lambda kind: 1)
    monkeypatch.setattr(b, "_remaining_total", lambda: 2)
    monkeypatch.setattr(b, "_start_lane", lambda kind, strategy: (True, ""))
    monkeypatch.setattr(b, "_fatal_reason", lambda: "")
    progress = {"disk": (10, 0), "unlim": (5, 0)}
    monkeypatch.setattr(b, "_progress", lambda kind: progress[kind])

    def fake_watch(kinds, strategy):
        progress["disk"] = (13, 0)
        progress["unlim"] = (6, 0)
        return True

    monkeypatch.setattr(b, "_watch", fake_watch)
    gained, _, _ = b._run_round({"key": "reset"})
    assert gained == 4
    assert b.gained_by_kind == {"disk": 3, "unlim": 1}

    # Следующий круг: файлы уже скачаны в прошлом — счётчик продолжает расти.
    def fake_watch2(kinds, strategy):
        progress["disk"] = (15, 0)
        progress["unlim"] = (9, 0)
        return True

    monkeypatch.setattr(b, "_watch", fake_watch2)
    gained, _, _ = b._run_round({"key": "few"})
    assert gained == 5
    assert b.gained_by_kind == {"disk": 5, "unlim": 4}


def test_beast_rotates_strategy_when_nothing_downloads(monkeypatch):
    """Нет новых файлов — тактика меняется; файлы пошли — остаётся на ней."""
    b = webui.Beast()
    monkeypatch.setattr(webui, "_auto_backup",
                        lambda *a, **k: (True, {"id": "x"}, ""))
    monkeypatch.setattr(b, "_mark_unavailable", lambda: None)
    monkeypatch.setattr(b, "_refresh_remaining", lambda: None)
    monkeypatch.setattr(b, "_pause", lambda *_: None)
    remaining = {"n": 2}
    monkeypatch.setattr(b, "_remaining_total", lambda: remaining["n"])
    seen = []

    def fake_round(strategy):
        seen.append(strategy["key"])
        if len(seen) == 2:
            remaining["n"] = 0
            return 2, False, ""
        return 0, False, ""

    monkeypatch.setattr(b, "_run_round", fake_round)
    b._run()

    assert seen == ["reset", "few"]
    assert b.state == "done"
    assert b.gained == 2
    assert b.finished_at is not None


def test_beast_finishes_immediately_when_nothing_left(monkeypatch):
    b = webui.Beast()
    monkeypatch.setattr(webui, "_auto_backup", lambda *a, **k: (True, None, ""))
    monkeypatch.setattr(b, "_mark_unavailable", lambda: None)
    monkeypatch.setattr(b, "_refresh_remaining", lambda: None)
    monkeypatch.setattr(b, "_remaining_total", lambda: 0)
    monkeypatch.setattr(b, "_run_round", lambda s: pytest.fail("круг не нужен"))
    b._run()
    assert b.state == "done"
    assert "всё скачано" in b.message


def test_beast_stops_on_fatal_error(monkeypatch):
    b = webui.Beast()
    monkeypatch.setattr(webui, "_auto_backup", lambda *a, **k: (True, None, ""))
    monkeypatch.setattr(b, "_mark_unavailable", lambda: None)
    monkeypatch.setattr(b, "_refresh_remaining", lambda: None)
    monkeypatch.setattr(b, "_remaining_total", lambda: 1)
    monkeypatch.setattr(b, "_run_round",
                        lambda s: (0, False, "Сессия Яндекса истекла"))
    b._run()
    assert b.state == "failed"
    assert "Сессия" in b.message


def test_beast_stop_request_ends_the_loop(monkeypatch):
    """Нажатие «зверь» во время работы останавливает круги и печатает итог."""
    b = webui.Beast()
    monkeypatch.setattr(webui, "_auto_backup", lambda *a, **k: (True, None, ""))
    monkeypatch.setattr(b, "_mark_unavailable", lambda: None)
    monkeypatch.setattr(b, "_refresh_remaining", lambda: None)
    monkeypatch.setattr(b, "_remaining_total", lambda: 5)

    def fake_round(strategy):
        b._stop = True
        return 0, False, ""

    monkeypatch.setattr(b, "_run_round", fake_round)
    b._run()
    assert b.state == "stopped"
    assert b.finished_at is not None


def test_beast_start_is_blocked_by_another_task(monkeypatch):
    b = webui.Beast()

    class Alive:
        def alive(self):
            return True

    monkeypatch.setitem(webui.tm._tasks, "verify", Alive())
    try:
        ok, msg = b.start()
    finally:
        webui.tm._tasks.pop("verify", None)
    assert ok is False
    assert "verify" in msg


def test_beast_stop_when_idle_is_reported():
    b = webui.Beast()
    ok, msg = b.stop()
    assert ok is False and "не работает" in msg


def test_beast_status_is_json_ready():
    b = webui.Beast()
    st = b.status()
    for key in ("state", "running", "round", "strategy", "started_at",
                "finished_at", "elapsed", "gained", "gained_by_kind",
                "message", "remaining", "log"):
        assert key in st
    assert st["running"] is False
    assert st["state"] == "idle"


def test_beast_heartbeat_files_are_per_lane(monkeypatch, tmp_path):
    disk_hb = tmp_path / "disk_hb"
    unlim_hb = tmp_path / "unlim_hb"
    monkeypatch.setattr(paths, "DISK_HEARTBEAT_FILE", disk_hb)
    monkeypatch.setattr(paths, "UNLIM_HEARTBEAT_FILE", unlim_hb)
    disk_hb.write_text("", encoding="utf-8")

    assert webui.Beast._heartbeat_mtime("disk") > 0
    assert webui.Beast._heartbeat_mtime("unlim") == 0
    webui.Beast._clear_heartbeat("disk")
    assert webui.Beast._heartbeat_mtime("disk") == 0


def test_beast_watch_stops_lanes_after_stall(monkeypatch):
    """90 секунд без файлов и байт — линии останавливаются, тактика меняется."""
    b = webui.Beast()
    monkeypatch.setattr(webui.Beast, "TICK", 0.01)
    monkeypatch.setattr(webui.Beast, "STALL_SEC", 0.0)
    monkeypatch.setattr(webui.Beast, "GRACE_SEC", 0.0)
    monkeypatch.setattr(b, "_progress", lambda kind: (0, 0))
    monkeypatch.setattr(b, "_heartbeat_mtime", lambda kind: 0.0)
    state = {"alive": True}
    monkeypatch.setattr(b, "_slot_running", lambda slot: state["alive"])
    stopped = []

    def fake_stop(slot):
        stopped.append(slot)
        state["alive"] = False

    monkeypatch.setattr(webui.tm, "stop", fake_stop)
    assert b._watch(["disk"], {"scan": False}) is False
    assert stopped == ["disk"]
    assert any("нет прогресса" in line for line in b.log)


def test_touch_heartbeat_marks_both_lanes(monkeypatch, tmp_path):
    """Воркеры отмечают «данные идут» на каждом чанке, не дожидаясь статуса."""
    import downloader
    import unlim_backup

    disk_hb = tmp_path / "disk_hb"
    unlim_hb = tmp_path / "unlim_hb"
    monkeypatch.setattr(paths, "DISK_HEARTBEAT_FILE", disk_hb)
    monkeypatch.setattr(paths, "UNLIM_HEARTBEAT_FILE", unlim_hb)

    downloader._touch_heartbeat()
    assert disk_hb.exists()
    unlim_backup._touch_heartbeat()
    assert unlim_hb.exists()
