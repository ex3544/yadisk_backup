"""Скачивание файла Безлимита: докачка по Range, сохранение `.part`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests

import unlim_backup
import unlim_schema
from fetch import DeadHosts, part_path_for

LINK = "https://cdn.example/f"


class _JsonResp:
    def __init__(self, status: int = 200, data: dict | None = None) -> None:
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class _StreamResp:
    def __init__(self, status: int = 200, body: bytes = b"", headers=None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class _Session:
    """mpfs/url отвечает ссылкой, CDN — заданным ответом или исключением."""

    def __init__(self, cdn):
        self.cdn = cdn
        self.gets: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        return _JsonResp(200, {"file": LINK})

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if isinstance(self.cdn, Exception):
            raise self.cdn
        return self.cdn


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "unlim_state.db"
    con = sqlite3.connect(path)
    con.executescript(unlim_schema.DB_SCHEMA)
    unlim_schema.migrate(con)
    con.execute("INSERT INTO unlim_files (file_id, name, size) VALUES ('1', 'a.mp4', 9)")
    con.commit()
    con.close()
    monkeypatch.setattr(unlim_backup, "DEAD_HOSTS", DeadHosts())
    monkeypatch.setattr(unlim_backup, "_touch_heartbeat", lambda: None)
    return path


def _run(db_path, out_dir, session, monkeypatch, name="a.mp4", size=9):
    monkeypatch.setattr(
        unlim_backup, "get_thread_session", lambda *a, **k: session
    )
    item = {"file_id": "1", "name": name, "size": size}
    return unlim_backup.download_one_file(
        db_path, {}, "sk", "uid", item, out_dir, make_previews=False
    )


def _status(db_path):
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT status, error FROM unlim_files WHERE file_id='1'"
        ).fetchone()
        return row
    finally:
        con.close()


def _make_part(out_dir, name="a.mp4", data=b"12345"):
    part = Path(part_path_for(str(out_dir / name)))
    part.write_bytes(data)
    return part


def test_range_resume_appends_and_marks_done(db, tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    part = _make_part(out_dir)
    sess = _Session(_StreamResp(206, body=b"6789"))

    ok, written, msg = _run(db, out_dir, sess, monkeypatch)

    assert (ok, written, msg) == (True, 9, "OK")
    assert sess.gets[0][1]["headers"]["Range"] == "bytes=5-"
    assert (out_dir / "a.mp4").read_bytes() == b"123456789"
    assert not part.exists()
    status, error = _status(db)
    assert status == "downloaded"
    assert error is None


def test_plain_200_restarts_the_file(db, tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _make_part(out_dir)
    sess = _Session(_StreamResp(200, body=b"abcdefghi"))

    ok, written, _ = _run(db, out_dir, sess, monkeypatch)

    assert ok
    assert written == 9
    assert (out_dir / "a.mp4").read_bytes() == b"abcdefghi"


def test_timeout_keeps_part_and_defers_the_file(db, tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    part = _make_part(out_dir)
    sess = _Session(requests.Timeout("read timed out"))

    ok, _, msg = _run(db, out_dir, sess, monkeypatch)

    assert not ok
    assert msg.startswith(unlim_backup.DEFERRED_PREFIX)
    assert "нет данных" in msg
    assert part.exists()
    assert part.read_bytes() == b"12345"
    status, error = _status(db)
    assert status == "pending"
    assert error.startswith(unlim_backup.DEFERRED_PREFIX)


def test_http_404_drops_part_and_marks_skipped(db, tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    part = _make_part(out_dir)
    sess = _Session(_StreamResp(404))

    ok, _, msg = _run(db, out_dir, sess, monkeypatch)

    assert not ok
    assert msg == "CDN HTTP 404"
    assert not part.exists()
    status, error = _status(db)
    assert status == "skipped"
    assert error == "CDN HTTP 404"


def test_old_style_part_file_is_adopted_for_resume(db, tmp_path, monkeypatch):
    """Прошлые версии оставляли `<имя>.part` — не теряем скачанные байты."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    legacy = out_dir / "a.mp4.part"
    legacy.write_bytes(b"12345")
    sess = _Session(_StreamResp(206, body=b"6789"))

    ok, written, _ = _run(db, out_dir, sess, monkeypatch)

    assert ok
    assert written == 9
    assert sess.gets[0][1]["headers"]["Range"] == "bytes=5-"
    assert (out_dir / "a.mp4").read_bytes() == b"123456789"
    assert not legacy.exists()
