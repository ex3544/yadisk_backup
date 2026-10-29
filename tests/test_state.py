"""Тесты SQLite-трекера прогресса."""

import pytest

from state import SCHEMA, State


@pytest.fixture()
def st(tmp_path):
    s = State(str(tmp_path / "state.db"))
    yield s
    s.close()


def test_mark_pending_and_stats(st):
    st.mark_pending("disk:/a.jpg", 100, "md5a")
    st.mark_pending("disk:/b.jpg", 200, None)
    stats = st.get_stats()
    assert stats["pending"] == 2
    assert stats["total"] == 2
    assert stats["bytes_total"] == 300
    assert stats["bytes_downloaded"] == 0


def test_downloaded_file_is_not_reset_by_rescan(st):
    """Повторный scan не должен возвращать скачанный файл в очередь."""
    st.mark_pending("disk:/a.jpg", 100, "md5a")
    st.mark_downloaded("disk:/a.jpg", "C:/out/a.jpg")
    st.mark_pending("disk:/a.jpg", 100, "md5a")
    assert st.get_stats()["pending"] == 0
    assert st.get_stats()["downloaded"] == 1


def test_md5_is_kept_when_api_returns_none(st):
    st.mark_pending("disk:/a.jpg", 100, "md5a")
    st.mark_pending("disk:/a.jpg", 100, None)
    row = next(iter(st.get_pending()))
    assert row["md5"] == "md5a"


def test_failed_then_retry(st):
    st.mark_pending("disk:/a.jpg", 100, None)
    st.mark_failed("disk:/a.jpg", "network died")
    assert st.get_stats()["failed"] == 1
    assert st.reset_failed_to_pending() == 1
    assert st.get_stats()["pending"] == 1
    assert next(iter(st.get_failed()), None) is None


def test_downloaded_and_skipped_count_as_done(st):
    st.mark_pending("disk:/a.jpg", 100, None)
    st.mark_pending("disk:/b.jpg", 50, None)
    st.mark_downloaded("disk:/a.jpg", "C:/out/a.jpg")
    st.mark_skipped("disk:/b.jpg", "C:/out/b.jpg")
    stats = st.get_stats()
    assert stats["bytes_downloaded"] == 150
    assert len(list(st.get_all_downloaded())) == 2


def test_error_is_truncated(st):
    st.mark_pending("disk:/a.jpg", 1, None)
    st.mark_failed("disk:/a.jpg", "x" * 5000)
    row = next(iter(st.get_failed()))
    assert len(row["error"]) == 2000


def test_downloaded_at_is_written(st):
    st.mark_pending("disk:/a.jpg", 1, None)
    st.mark_downloaded("disk:/a.jpg", "C:/out/a.jpg")
    row = next(iter(st.get_all_downloaded()))
    assert row["local_path"] == "C:/out/a.jpg"


def test_existing_remote_paths(st):
    st.mark_pending("disk:/a.jpg", 1, None)
    st.mark_pending("disk:/b.jpg", 1, None)
    assert st.get_existing_remote_paths() == {"disk:/a.jpg", "disk:/b.jpg"}


def test_attempts_grow_on_every_failure(st):
    st.mark_pending("disk:/a.jpg", 1, None)
    st.mark_failed("disk:/a.jpg", "timeout")
    st.mark_failed("disk:/a.jpg", "timeout")
    row = next(iter(st.get_failed()))
    assert row["attempts"] == 2


def test_old_database_gets_attempts_column(tmp_path):
    """База версии 1 (без attempts) должна обновиться, а не упасть."""
    import sqlite3

    import db_migrations

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    db_migrations.migrate(conn, "state.db", [(1, SCHEMA)])
    conn.execute(
        "INSERT INTO files (remote_path, size, status) VALUES ('disk:/a.jpg', 5, 'failed')"
    )
    conn.commit()
    conn.close()

    s = State(str(path))
    try:
        s.mark_failed("disk:/a.jpg", "again")
        row = next(iter(s.get_failed()))
        assert row["attempts"] == 1
    finally:
        s.close()


def test_reset_recreates_schema_and_resets_version(st):
    st.mark_pending("disk:/a.jpg", 1, "md5")
    st.reset()
    assert st.get_stats()["total"] == 0
    st.mark_pending("disk:/b.jpg", 2, None)
    assert st.get_stats()["pending"] == 1


# ── отложенные файлы (ждут Яндекса) ─────────────────────────────────────────


def test_deferred_file_stays_pending_and_counts_as_waiting(st):
    st.mark_pending("disk:/a.jpg", 100, None)
    st.mark_deferred("disk:/a.jpg", "storage_down", "storage.yandex.net")
    stats = st.get_stats()
    assert stats["pending"] == 1
    assert stats["failed"] == 0
    assert stats["waiting"] == 1
    row = next(iter(st.get_pending()))
    assert row["error"] == "storage_down | storage.yandex.net"
    assert row["storage_host"] == "storage.yandex.net"


def test_deferred_marker_without_host():
    from state import deferred_marker

    assert deferred_marker("timeout") == "storage_down"
    assert deferred_marker("server_50x", "h") == "storage_down | h"


def test_get_pending_puts_deferred_first(st):
    st.mark_pending("disk:/plain.jpg", 1, None)
    st.mark_pending("disk:/wait.jpg", 1, None)
    st.mark_deferred("disk:/wait.jpg", "storage_down", "h")
    first = next(iter(st.get_pending(deferred_first=True)))
    assert first["remote_path"] == "disk:/wait.jpg"


def test_mark_downloaded_clears_deferred_marker_and_preview(st):
    st.mark_pending("disk:/a.jpg", 10, None)
    st.mark_deferred("disk:/a.jpg", "storage_down", "h")
    st.set_preview_path("disk:/a.jpg", "C:/out/_превью/a.jpg")
    st.mark_downloaded("disk:/a.jpg", "C:/out/a.jpg")
    stats = st.get_stats()
    assert stats["downloaded"] == 1
    assert stats["waiting"] == 0
    assert list(st.get_preview_paths()) == []


def test_clear_deferred_marks_returns_to_plain_queue(st):
    st.mark_pending("disk:/a.jpg", 10, None)
    st.mark_deferred("disk:/a.jpg", "storage_down", "h")
    assert st.clear_deferred_marks() == 1
    assert st.get_stats()["waiting"] == 0


def test_waiting_is_zero_on_empty_db(st):
    assert st.get_stats()["waiting"] == 0


# ── 35.6.7: пауза файла после 5xx и «сгоревшие» файлы ───────────────────────


def test_server_5xx_pauses_only_that_file(st):
    """Ответ 500 ставит на паузу один файл, остальные остаются в очереди."""
    st.mark_pending("disk:/bad.jpg", 10, None)
    st.mark_pending("disk:/good.jpg", 10, None)
    status = st.mark_deferred("disk:/bad.jpg", "server_50x", "downloader.disk.yandex.ru")
    assert status == "pending"
    assert [r["remote_path"] for r in st.get_pending()] == ["disk:/good.jpg"]
    # Ждать его всё равно считаем: файл в очереди, просто с паузой.
    assert st.get_stats()["waiting"] == 1


def test_file_becomes_lost_after_three_5xx(st):
    """Три прохода с 500 подряд — файл сгоревший, он выходит из очереди."""
    st.mark_pending("disk:/bad.jpg", 10, None)
    for _ in range(2):
        assert st.mark_deferred(
            "disk:/bad.jpg", "server_50x", "h", cooldown=0
        ) == "pending"
    assert st.mark_deferred("disk:/bad.jpg", "server_50x", "h", cooldown=0) == "lost"
    stats = st.get_stats()
    assert stats["lost"] == 1
    assert stats["pending"] == 0
    assert stats["waiting"] == 0
    assert list(st.get_pending()) == []
    row = st._conn().execute(
        "SELECT error FROM files WHERE remote_path='disk:/bad.jpg'"
    ).fetchone()
    assert row["error"].startswith("yandex_lost")


def test_retry_lost_returns_files_to_queue(st):
    st.mark_pending("disk:/bad.jpg", 10, None)
    for _ in range(3):
        st.mark_deferred("disk:/bad.jpg", "server_50x", "h", cooldown=0)
    assert st.get_stats()["lost"] == 1
    assert st.reset_lost() == 1
    assert st.get_stats()["lost"] == 0
    assert [r["remote_path"] for r in st.get_pending()] == ["disk:/bad.jpg"]
    # Счётчик обнулён: у файла снова три попытки.
    assert st.mark_deferred("disk:/bad.jpg", "server_50x", "h", cooldown=0) == "pending"


def test_timeout_does_not_count_as_server_failure(st):
    """Таймаут — молчание сервера, а не ответ 500: файл просто ждёт."""
    st.mark_pending("disk:/a.jpg", 10, None)
    for _ in range(5):
        assert st.mark_deferred("disk:/a.jpg", "timeout", "h", cooldown=0) == "pending"
    assert st.get_stats()["lost"] == 0
    assert [r["remote_path"] for r in st.get_pending()] == ["disk:/a.jpg"]


def test_success_clears_cooldown_and_counter(st):
    st.mark_pending("disk:/a.jpg", 10, None)
    st.mark_deferred("disk:/a.jpg", "server_50x", "h")
    assert list(st.get_pending()) == []
    st.mark_downloaded("disk:/a.jpg", "C:/out/a.jpg")
    stats = st.get_stats()
    assert stats["downloaded"] == 1
    assert stats["waiting"] == 0
