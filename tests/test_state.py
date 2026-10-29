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
