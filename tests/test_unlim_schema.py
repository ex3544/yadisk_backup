"""Пересоздание базы Безлимита не должно оставлять её без таблиц.

Ошибка из отчёта первого пользователя 35.5.7: «Пересоздать базу» удаляла
таблицы `unlim_files` и `unlim_clusters`, но оставляла `user_version = 1`.
Миграция считалась применённой, схема не создавалась заново, и любой следующий
запуск падал с `sqlite3.OperationalError: no such table: unlim_clusters`.
"""

from __future__ import annotations

import sqlite3

import db_migrations
import unlim_backup as ub
import unlim_schema


def _tables(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _make_db(tmp_path):
    db = tmp_path / "unlim_state.db"
    ub.init_db(db)
    con = ub.get_db(db)
    with con:
        con.execute(
            "INSERT INTO unlim_files (file_id, name, size, status) "
            "VALUES ('old', 'old.jpg', 1, 'downloaded')"
        )
    con.close()
    return db


def test_migration_repairs_database_without_tables(tmp_path):
    """База, пострадавшая от 35.5.7: версия 1 есть, таблиц нет."""
    db = tmp_path / "unlim_state.db"
    con = sqlite3.connect(str(db))
    db_migrations.set_user_version(con, 1)
    assert _tables(con) == set()

    result = unlim_schema.migrate(con)

    assert {"unlim_files", "unlim_clusters"} <= _tables(con)
    assert result["from_version"] == 1
    assert result["to_version"] == len(unlim_schema.MIGRATIONS)
    con.close()


def test_force_recreate_restores_schema(monkeypatch, tmp_path):
    """После «пересоздать с нуля» база снова рабочая, а не пустая версия 1."""
    db = _make_db(tmp_path)

    def fake_api(s, sk, uid, method, params=None):
        if method == "intapi/photo-init-snapshot":
            return {"photoslice_id": "ps1"}
        if method == "intapi/photo-get-snapshot":
            return {"items": [{"id": "c1", "size": 1}]}
        if method == "intapi/photo-get-clusters-with-resources":
            return {"clusters": {"fetched": [
                {"id": "c1", "items": [{"id": "new", "name": "n.jpg", "size": 1}]}
            ]}}
        raise AssertionError(f"неожиданный метод {method}")

    monkeypatch.setattr(ub, "api_call", fake_api)

    ub.populate_db_from_cloud(db, s=None, sk="sk", uid="1", force=True)

    con = ub.get_db(db)
    assert {"unlim_files", "unlim_clusters"} <= _tables(con)
    assert db_migrations.get_user_version(con) == len(unlim_schema.MIGRATIONS)
    # Старая запись удалена, новая собрана из облака.
    assert con.execute("SELECT file_id FROM unlim_files").fetchall()[0][0] == "new"
    con.close()


def test_force_reset_zeroes_schema_version():
    """Сброс версии обязателен: иначе миграции не выполнятся повторно."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (id INTEGER)")
    db_migrations.set_user_version(con, 2)
    db_migrations.set_user_version(con, 0)
    assert db_migrations.get_user_version(con) == 0
    con.close()
