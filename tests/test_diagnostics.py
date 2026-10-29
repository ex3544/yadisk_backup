"""Диагностический отчёт: секреты не утекают, ZIP собирается, отправка безопасна."""

import json
import sqlite3
import zipfile

import pytest

import diagnostics
import paths


TOKEN = "y0_" + "A" * 40
COOKIE_VALUE = "COOKIE-SECRET-VALUE-123"


@pytest.fixture
def temp_paths(tmp_path, monkeypatch):
    data = tmp_path / "data"
    logs = data / "logs"
    reports = data / "reports"
    for d in (data, logs, reports):
        d.mkdir(parents=True, exist_ok=True)
    mapping = {
        "DATA_DIR": data,
        "LOG_DIR": logs,
        "REPORT_DIR": reports,
        "ENV_FILE": data / ".env",
        "COOKIES_FILE": data / "cookies.json",
        "DB_FILE": data / "state.db",
        "UNLIM_DB_FILE": data / "unlim_state.db",
        "REPORT_KEY_FILE": data / ".report_key.txt",
        "BUNDLED_REPORT_KEY_FILE": data / ".bundled_report_key.txt",
        "SETUP_DONE_FILE": data / ".setup_done",
        "INSTALL_ID_FILE": data / ".install_id",
        "BACKUP_LOG": logs / "backup.log",
        "UNLIM_LOG": logs / "unlim_backup.log",
        "UNLIM_WORKER_LOG": logs / "unlim_worker.log",
        "UNLIM_COLLECT_LOG": logs / "unlim.log",
        "UNLIM_DIRECT_LOG": logs / "unlim_direct.log",
        "ACTION_LOG": logs / "webui_actions.log",
        "MIGRATIONS_LOG": logs / "migrations.log",
        "DISK_STATUS_FILE": logs / "disk_download_status.json",
        "UNLIM_STATUS_FILE": logs / "download_status.json",
        "COOKIES_FETCH_STATUS": logs / "cookies_fetch_status.json",
        "HTTP_ERRORS_FILE": logs / "http_errors.json",
        "CRASH_LOG": logs / "crash.log",
        "MONITOR_STATE_FILE": data / ".monitor_state.json",
        "LAUNCHER_LOG": logs / "launcher.log",
        "LAUNCHER_OUTPUT_LOG": logs / "launcher_output.log",
        "DOWNLOAD_REPORT": logs / "download_report.md",
    }
    for name, value in mapping.items():
        monkeypatch.setattr(paths, name, value)

    monkeypatch.delenv("YA_DISK_TOKEN", raising=False)
    monkeypatch.delenv("REPORT_KEY", raising=False)
    monkeypatch.delenv("REPORT_URL", raising=False)
    monkeypatch.delenv("YADISK_DATA_DIR", raising=False)

    (paths.ENV_FILE).write_text(
        f"YA_DISK_TOKEN={TOKEN}\n"
        f"DISK_DIR={tmp_path / 'disk'}\n"
        f"UNLIM_DIR={tmp_path / 'unlim'}\n",
        encoding="utf-8",
    )
    (paths.COOKIES_FILE).write_text(
        json.dumps([{"name": "Session_id", "value": COOKIE_VALUE}]),
        encoding="utf-8",
    )

    conn = sqlite3.connect(paths.DB_FILE)
    conn.execute("CREATE TABLE files (status TEXT, size INTEGER, downloaded_at TEXT)")
    conn.executemany(
        "INSERT INTO files VALUES (?, ?, NULL)",
        [("downloaded", 100), ("failed", 50)],
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(paths.UNLIM_DB_FILE)
    conn.execute("CREATE TABLE unlim_files (status TEXT)")
    conn.execute("INSERT INTO unlim_files VALUES ('pending')")
    conn.commit()
    conn.close()

    paths.BACKUP_LOG.write_text(
        "2026-10-26 10:00:00 | INFO | ok\n"
        f"2026-10-26 10:00:01 | ERROR | failed token={TOKEN}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_report_key_user_file_wins_over_bundled(temp_paths):
    paths.BUNDLED_REPORT_KEY_FILE.write_text("bundled-key", encoding="utf-8")
    assert diagnostics._read_report_key() == "bundled-key"

    paths.REPORT_KEY_FILE.write_text("user-key", encoding="utf-8")
    assert diagnostics._read_report_key() == "user-key"


def test_report_has_no_secrets(temp_paths):
    report = diagnostics.build_report()
    blob = json.dumps(report, ensure_ascii=False)

    assert TOKEN not in blob
    assert COOKIE_VALUE not in blob
    assert report["config"]["token_configured"] is True
    assert report["config"]["token_masked"].startswith("y0_")
    assert report["config"]["cookies"]["count"] == 1
    assert report["databases"]["state.db"]["total"] == 2
    assert report["databases"]["state.db"]["status"]["failed"] == 1
    assert any("ERROR" in e["line"] for e in report["logs"]["errors"])
    assert "***" in report["logs"]["errors"][0]["line"] or \
        TOKEN not in report["logs"]["errors"][0]["line"]


def test_zip_contains_only_safe_files(temp_paths):
    report = diagnostics.build_report()
    target = diagnostics.write_report(report)

    assert target.exists()
    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
        payload = "".join(zf.read(n).decode("utf-8") for n in names)
    # С 35.6.0 в архив кладутся хвосты логов: report.json/… плюс logs/<файл>.log.
    assert {"report.json", "report.txt", "README.txt"} <= names
    extra = names - {"report.json", "report.txt", "README.txt"}
    assert all(n.startswith("logs/") and n.endswith(".log") for n in extra)
    assert TOKEN not in payload
    assert COOKIE_VALUE not in payload


def test_summary_is_human_readable(temp_paths):
    report = diagnostics.build_report()
    summary = diagnostics.render_summary(report)
    assert "ОТЧЁТ" in summary
    assert "state.db" in summary
    assert TOKEN not in summary
    assert COOKIE_VALUE not in summary


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, "
        "status TEXT, error TEXT, attempts INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO files (remote_path, size, status, error) VALUES (?, ?, 'failed', ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_failure_groups_merge_by_host_and_signature(temp_paths):
    db = temp_paths / "failures.db"
    # Три ошибки одного вида (разные номера запросов) и одна другого хоста.
    _make_db(db, [
        ("disk:/a/1.jpg", 1024, "HTTPSConnectionPool(host='downloader.disk.yandex.ru', "
                                "port=443): Timeout, request id 8f3a1b2c4d5e6f70"),
        ("disk:/a/2.jpg", 1024, "HTTPSConnectionPool(host='downloader.disk.yandex.ru', "
                                "port=443): Timeout, request id 4a4a4a4a4b4b4b4b"),
        ("disk:/a/3.jpg", 1024, "HTTPSConnectionPool(host='downloader.disk.yandex.ru', "
                                "port=443): Timeout, request id 9999999999999999"),
        ("disk:/a/4.jpg", 2048, "Response 502 from uploader.disk.yandex.net"),
    ])
    groups = diagnostics.failure_groups(db, "files", "remote_path")
    assert groups["total"] == 4
    top = groups["groups"][0]
    assert top["count"] == 3
    assert top["host"] == "downloader.disk.yandex.ru"
    assert len(top["examples"]) == 3
    assert top["examples"][0].startswith("disk:/a/")
    assert groups["groups_total"] == 2


def test_report_delta_compares_with_previous_report(temp_paths):
    previous = {
        "report_id": "old",
        "statuses": {"disk_download": {"downloaded": 10, "failed": 4}},
        "databases": {"state.db": {"status": {"pending": 7}}},
    }
    old_zip = paths.REPORT_DIR / "diagnostics_2026-01-01_000000.zip"
    with zipfile.ZipFile(old_zip, "w") as zf:
        zf.writestr("report.json", json.dumps(previous))

    report = {
        "statuses": {"disk_download": {"downloaded": 15, "failed": 1}},
        "databases": {"state.db": {"status": {"pending": 2}}},
    }
    delta = diagnostics.report_delta(report)
    assert delta["available"] is True
    assert delta["previous_report_id"] == "old"
    assert delta["changes"]["disk.downloaded"] == {"was": 10, "now": 15, "delta": 5}
    assert delta["changes"]["disk.failed"] == {"was": 4, "now": 1, "delta": -3}
    assert delta["changes"]["disk_db.pending"] == {"was": 7, "now": 2, "delta": -5}


def test_path_access_checks_existence_and_writability(temp_paths):
    folder = temp_paths / "out"
    result = diagnostics.path_access(str(folder))
    assert result["exists"] is False

    folder.mkdir()
    result = diagnostics.path_access(str(folder))
    assert result["exists"] is True
    assert result["is_dir"] is True
    assert result["writable"] is True


def test_crash_hook_writes_redacted_log(temp_paths):
    import sys

    try:
        raise RuntimeError(f"boom {TOKEN}")
    except RuntimeError:
        diagnostics._handle_exception(*sys.exc_info(), "test")

    text = paths.CRASH_LOG.read_text(encoding="utf-8")
    assert "RuntimeError" in text
    assert "boom" in text
    assert TOKEN not in text
    assert "test" in text


def test_install_crash_hook_is_idempotent(monkeypatch):
    import sys

    saved = sys.excepthook
    try:
        diagnostics.install_crash_hook()
        ours = sys.excepthook
        diagnostics.install_crash_hook()
        assert sys.excepthook is ours
        assert ours is diagnostics._sys_excepthook
    finally:
        sys.excepthook = saved


def test_send_without_key_keeps_file(temp_paths):
    report = diagnostics.build_report()
    path = diagnostics.write_report(report)
    result = diagnostics.send_report(path, report)
    assert result["ok"] is False
    assert "Ключ" in result["error"]
    assert result["saved"] == str(path)


def test_send_with_key_calls_worker(temp_paths, monkeypatch):
    diagnostics.save_report_key("test-report-key")
    report = diagnostics.build_report()
    path = diagnostics.write_report(report)
    calls = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "message_id": 77, "file_name": path.name}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["headers"] = kwargs.get("headers", {})
        calls["data"] = kwargs.get("data", {})
        calls["files"] = kwargs.get("files", {})
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    result = diagnostics.send_report(path, report)

    assert result["ok"] is True
    assert result["message_id"] == 77
    assert calls["url"].endswith("/report")
    assert calls["headers"]["X-Report-Key"] == "test-report-key"
    assert calls["data"]["app_version"] == diagnostics.version.APP_VERSION
    assert calls["data"]["report_id"] == report["report_id"]


def test_report_key_save_and_clear(temp_paths):
    assert diagnostics.report_key_configured() is False
    diagnostics.save_report_key("abc")
    assert diagnostics.report_key_configured() is True
    assert paths.REPORT_KEY_FILE.read_text(encoding="utf-8") == "abc"
    diagnostics.save_report_key("")
    assert diagnostics.report_key_configured() is False
    assert not paths.REPORT_KEY_FILE.exists()
