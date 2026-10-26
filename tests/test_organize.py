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
