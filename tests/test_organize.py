import organize


def test_get_date_from_regex(tmp_path):
    f = tmp_path / "IMG_20230514_120000.jpg"
    f.write_bytes(b"dummy")
    assert organize.get_date(str(f)) == (2023, 5)


def test_get_date_from_exif(tmp_path):
    # Файл без даты в имени, но с заголовком EXIF
    f = tmp_path / "vacation_photo.jpg"
    exif_payload = b"\xff\xd8\xff\xe1\x00\x30Exif\x00\x00II*\x00\x08\x00\x00\x00" + b"some_bytes 2019:08:25 15:30:00\x00"
    f.write_bytes(exif_payload)
    assert organize.get_date(str(f)) == (2019, 8)


def test_get_date_from_mp4_atom(tmp_path):
    import struct
    f = tmp_path / "clip.mp4"
    # Создаем фиктивный атом mvhd с датой создания 2022-07-10 UTC
    # 2022-07-10 00:00:00 UTC = Unix timestamp 1657411200
    # MP4 epoch (seconds since 1904): 1657411200 + 2082844800 = 3740256000
    mp4_seconds = 1657411200 + 2082844800
    payload = b"\x00\x00\x00\x20mvhd" + b"\x00\x00\x00\x00" + struct.pack(">I", mp4_seconds) + b"\x00" * 32
    f.write_bytes(payload)
    assert organize.get_date(str(f)) == (2022, 7)


def test_report_only_shows_where_files_will_go(tmp_path):
    """Отчёт должен показывать не только итоги, но и «куда что ляжет»."""
    root = tmp_path / "backup"
    root.mkdir()
    src = root / "IMG_20230514_120000.jpg"
    src.write_bytes(b"photo")
    report = tmp_path / "report.txt"
    plan = tmp_path / "plan.csv"

    code = organize.main([
        "--input", str(root), "--action", "report-only", "--quick",
        "--report-file", str(report), "--plan-file", str(plan),
    ])

    assert code == 0
    text = report.read_text(encoding="utf-8")
    assert "КУДА ЛЯГУТ ФАЙЛЫ" in text
    assert "Фото" in text and "2023-05" in text
    assert str(src) in text and "IMG_20230514_120000.jpg" in text
    assert "Файлы НЕ перемещались" in text
    assert src.exists()
    assert plan.exists()
    assert "IMG_20230514_120000.jpg" in plan.read_text(encoding="utf-8-sig")


def test_move_updates_database_paths(tmp_path):
    import sqlite3

    root = tmp_path / "backup"
    root.mkdir()
    src = root / "IMG_20230514_120000.jpg"
    src.write_bytes(b"photo")
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, local_path TEXT)"
    )
    con.execute("INSERT INTO files VALUES (?, ?)", ("disk:/photo.jpg", str(src)))
    con.commit()
    con.close()

    code = organize.main([
        "--input", str(root), "--action", "move", "--quick",
        "--db", str(db), "--table", "files",
    ])

    assert code == 0
    moved = root / "Фото" / "2023" / "2023-05" / "IMG_20230514_120000.jpg"
    assert moved.exists()
    assert not src.exists()
    con = sqlite3.connect(db)
    assert con.execute("SELECT local_path FROM files").fetchone()[0] == str(moved)
    con.close()
