"""Проверки безопасного дополнения базы Безлимита без потери статусов."""

from __future__ import annotations

import pytest

import unlim_backup as ub


def _make_db(tmp_path):
    db = tmp_path / "unlim_state.db"
    ub.init_db(db)
    con = ub.get_db(db)
    with con:
        con.execute(
            """INSERT INTO unlim_files
               (file_id, cluster_id, name, size, status, local_path)
               VALUES ('f1', 'c1', 'a.jpg', 10, 'downloaded', 'C:/Unlim/a.jpg')"""
        )
        con.execute(
            "INSERT INTO unlim_clusters (cluster_id, size, fetched) VALUES ('c1', 2, 1)"
        )
        con.execute(
            "INSERT INTO unlim_clusters (cluster_id, size, fetched) VALUES ('cold', 1, 1)"
        )
    con.close()
    return db


def test_incremental_scan_preserves_statuses_and_adds_new_files(monkeypatch, tmp_path):
    db = _make_db(tmp_path)

    def fake_api(s, sk, uid, method, params=None):
        if method == "intapi/photo-init-snapshot":
            return {"photoslice_id": "ps1"}
        if method == "intapi/photo-get-snapshot":
            return {"items": [{"id": "c1", "size": 2}]}
        if method == "intapi/photo-get-clusters-with-resources":
            assert set(params["clusters"]) == {"c1"}
            return {
                "clusters": {
                    "fetched": [
                        {
                            "id": "c1",
                            "items": [
                                {"id": "f1", "name": "a.jpg", "size": 10},
                                {"id": "f2", "name": "b.jpg", "size": 20},
                            ],
                        }
                    ]
                }
            }
        raise AssertionError(f"неожиданный метод: {method}")

    monkeypatch.setattr(ub, "api_call", fake_api)

    ub.populate_db_from_cloud(db, None, "sk", "uid", incremental=True)

    con = ub.get_db(db)
    files = {row["file_id"]: row for row in con.execute("SELECT * FROM unlim_files")}
    clusters = {
        row["cluster_id"]: row for row in con.execute("SELECT * FROM unlim_clusters")
    }
    con.close()

    # Старый файл не перезаписан и не вернулся в pending.
    assert files["f1"]["status"] == "downloaded"
    assert files["f1"]["local_path"] == "C:/Unlim/a.jpg"
    # Новый файл добавлен в очередь.
    assert files["f2"]["status"] == "pending"
    # Старый кластер, которого больше нет в слепке, не удалён.
    assert "cold" in clusters
    assert clusters["c1"]["fetched"] == 1


def test_scan_stops_when_server_never_returns_a_cluster(monkeypatch, tmp_path):
    """Кластер, который сервер не отдаёт, не должен крутить цикл бесконечно.

    Регрессия 29.10.2026: у тестера 16 таких кластеров дали 1 052 403 итерации
    и «Безлимит: подготовка…» на 5,5 часов.
    """
    db = _make_db(tmp_path)
    con = ub.get_db(db)
    with con:
        con.execute(
            "INSERT INTO unlim_clusters (cluster_id, size, fetched) VALUES ('dead', 1, 0)"
        )
    con.close()

    calls = []

    def fake_api(s, sk, uid, method, params=None):
        if method == "intapi/photo-init-snapshot":
            return {"photoslice_id": "ps1"}
        if method == "intapi/photo-get-snapshot":
            return {"items": [{"id": "dead", "size": 1}]}
        if method == "intapi/photo-get-clusters-with-resources":
            calls.append(set(params["clusters"]))
            return {"clusters": {"fetched": []}}
        raise AssertionError(f"неожиданный метод: {method}")

    monkeypatch.setattr(ub, "api_call", fake_api)

    ub.populate_db_from_cloud(db, None, "sk", "uid", incremental=True)

    con = ub.get_db(db)
    fetched = con.execute(
        "SELECT fetched FROM unlim_clusters WHERE cluster_id='dead'"
    ).fetchone()[0]
    con.close()

    assert fetched == 1, "недоступный кластер должен помечаться обработанным"
    assert 1 <= len(calls) <= 5, f"цикл должен прекратиться, а не крутиться: {len(calls)}"


def test_scan_fills_missing_size_for_known_file(monkeypatch, tmp_path):
    """Размер, которого не было в старой базе, дозаполняется при сканировании."""
    db = _make_db(tmp_path)
    con = ub.get_db(db)
    with con:
        con.execute(
            """INSERT INTO unlim_files (file_id, cluster_id, name, size, status)
               VALUES ('f9', 'c1', 'old.jpg', 0, 'skipped')"""
        )
    con.close()

    def fake_api(s, sk, uid, method, params=None):
        if method == "intapi/photo-init-snapshot":
            return {"photoslice_id": "ps1"}
        if method == "intapi/photo-get-snapshot":
            return {"items": [{"id": "c1", "size": 2}]}
        if method == "intapi/photo-get-clusters-with-resources":
            return {"clusters": {"fetched": [{
                "id": "c1",
                "items": [{"id": "f9", "name": "old.jpg", "size": 1234}],
            }]}}
        raise AssertionError(f"неожиданный метод: {method}")

    monkeypatch.setattr(ub, "api_call", fake_api)

    ub.populate_db_from_cloud(db, None, "sk", "uid", incremental=True)

    con = ub.get_db(db)
    row = con.execute(
        "SELECT size, status FROM unlim_files WHERE file_id='f9'"
    ).fetchone()
    con.close()

    assert row["size"] == 1234
    assert row["status"] == "skipped", "статус не должен меняться"


def test_default_scan_does_not_touch_existing_db(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    monkeypatch.setattr(
        ub, "api_call", lambda *a, **k: pytest.fail("сеть не должна вызываться")
    )

    ub.populate_db_from_cloud(db, None, "sk", "uid")

    con = ub.get_db(db)
    count = con.execute("SELECT count(*) FROM unlim_files").fetchone()[0]
    status = con.execute("SELECT status FROM unlim_files WHERE file_id='f1'").fetchone()[0]
    con.close()

    assert count == 1
    assert status == "downloaded"
