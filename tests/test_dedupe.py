import sqlite3
import dedupe


def create_test_db(tmp_path):
    db_path = str(tmp_path / "state.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE files (
            remote_path TEXT PRIMARY KEY,
            size INTEGER,
            md5 TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            local_path TEXT,
            error TEXT,
            downloaded_at TIMESTAMP
        );
        """
    )
    conn.executemany(
        "INSERT INTO files (remote_path, size, md5, status) VALUES (?, ?, ?, ?)",
        [
            ("disk:/doc.pdf", 100, "hash1", "pending"),
            ("disk:/photo1.jpg", 200, "hash2", "pending"),
            ("disk:/photo2.jpg", 200, "hash2", "pending"),  # дубликат
            ("disk:/code.py", 300, "hash3", "pending"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def test_dedupe_preserves_non_media_by_default(tmp_path):
    db_path = create_test_db(tmp_path)
    dedupe.main(["--db", db_path])

    conn = sqlite3.connect(db_path)
    rows = dict(conn.execute("SELECT remote_path, status FROM files").fetchall())
    conn.close()

    # Не-медиа файлы должны остаться в БД со статусом pending
    assert "disk:/doc.pdf" in rows
    assert rows["disk:/doc.pdf"] == "pending"
    assert "disk:/code.py" in rows
    assert rows["disk:/code.py"] == "pending"

    # Дубликат фото должен быть помечен как skipped
    assert rows["disk:/photo1.jpg"] == "pending"
    assert rows["disk:/photo2.jpg"] == "skipped"


def test_dedupe_deletes_non_media_with_flag(tmp_path):
    db_path = create_test_db(tmp_path)
    dedupe.main(["--db", db_path, "--media-only"])

    conn = sqlite3.connect(db_path)
    rows = dict(conn.execute("SELECT remote_path, status FROM files").fetchall())
    conn.close()

    # С флагом --media-only не-медиа файлы удаляются из pending
    assert "disk:/doc.pdf" not in rows
    assert "disk:/code.py" not in rows
    assert rows["disk:/photo1.jpg"] == "pending"
    assert rows["disk:/photo2.jpg"] == "skipped"
