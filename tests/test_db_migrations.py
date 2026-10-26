"""Тесты версионирования схемы и резервных копий перед миграцией."""

import sqlite3

import pytest

import db_migrations


def _make_old_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER)")
    con.execute("INSERT INTO files VALUES ('/a.jpg', 10)")
    con.commit()
    return con


def test_migration_applies_and_keeps_data(tmp_path):
    path = tmp_path / "state.db"
    con = _make_old_db(path)

    result = db_migrations.migrate(
        con,
        "state.db",
        [(1, "CREATE TABLE extra (id INTEGER);")],
    )

    assert result["applied"] == [1]
    assert result["to_version"] == 1
    assert db_migrations.get_user_version(con) == 1
    assert con.execute("SELECT size FROM files").fetchone()[0] == 10
    assert result["backup"] is not None
    assert (tmp_path / "state.db").exists()
    con.close()


def test_backup_created_before_migration(tmp_path):
    path = tmp_path / "state.db"
    con = _make_old_db(path)

    db_migrations.migrate(con, "state.db", [(1, "CREATE TABLE extra (id INTEGER);")])
    con.close()

    backups = list(tmp_path.glob("state.db.bak_v0_*"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("SELECT size FROM files").fetchone()[0] == 10
        assert db_migrations.get_user_version(backup) == 0
    finally:
        backup.close()


def test_second_run_is_noop(tmp_path):
    path = tmp_path / "state.db"
    con = _make_old_db(path)
    migrations = [(1, "CREATE TABLE extra (id INTEGER);")]

    db_migrations.migrate(con, "state.db", migrations)
    result = db_migrations.migrate(con, "state.db", migrations)
    con.close()

    assert result["applied"] == []
    assert len(list(tmp_path.glob("state.db.bak_*"))) == 1


def test_fresh_database_gets_no_backup(tmp_path):
    con = sqlite3.connect(tmp_path / "fresh.db")
    result = db_migrations.migrate(
        con, "fresh.db", [(1, "CREATE TABLE extra (id INTEGER);")]
    )
    con.close()

    assert result["backup"] is None
    assert not list(tmp_path.glob("*.bak_*"))


def test_future_database_is_rejected(tmp_path):
    con = sqlite3.connect(tmp_path / "future.db")
    con.execute("CREATE TABLE extra (id INTEGER)")
    db_migrations.set_user_version(con, 5)
    con.commit()

    with pytest.raises(RuntimeError, match="база новее"):
        db_migrations.migrate(con, "future.db", [(1, "SELECT 1;")])
    con.close()


def test_gap_in_versions_is_rejected(tmp_path):
    con = sqlite3.connect(tmp_path / "gap.db")
    with pytest.raises(ValueError, match="подряд"):
        db_migrations.migrate(con, "gap.db", [(1, "SELECT 1;"), (3, "SELECT 1;")])
    con.close()


def test_failed_migration_rolls_back(tmp_path):
    path = tmp_path / "state.db"
    con = _make_old_db(path)

    def broken(conn):
        conn.execute("CREATE TABLE extra (id INTEGER)")
        raise RuntimeError("сбой посреди миграции")

    with pytest.raises(RuntimeError, match="сбой"):
        db_migrations.migrate(con, "state.db", [(1, broken)])

    assert db_migrations.get_user_version(con) == 0
    assert con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='extra'"
    ).fetchone() is None
    con.close()
