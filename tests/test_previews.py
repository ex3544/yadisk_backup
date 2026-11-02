"""Тесты превью отложенных фото."""

import os

import pytest

import paths
import unlim_backup as ub
from previews import (
    PREVIEW_DIR_NAME,
    is_photo,
    make_disk_preview,
    preview_dir,
    preview_path_for,
    remove_preview,
    save_from_link,
    sweep,
    unlim_preview_url,
)
from state import State


class _Resp:
    def __init__(self, status=200, payload=None, body=b"", headers=None):
        self.status_code = status
        self._payload = payload
        self._body = body
        self.headers = headers or {"Content-Type": "image/jpeg"}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, chunk_size=1024):
        yield self._body

    def close(self):
        pass


class _Session:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)

    def close(self):
        pass


@pytest.fixture()
def st(tmp_path, monkeypatch):
    # sweep ищет брошенные .part в папке превью — уводим её во временный каталог,
    # чтобы тесты не ходили в настоящие данные программы.
    monkeypatch.setenv("PREVIEWS_DIR", str(tmp_path / "_превью"))
    s = State(str(tmp_path / "state.db"))
    yield s
    s.close()


def test_is_photo_by_extension():
    assert is_photo("disk:/Фото/a.JPG")
    assert is_photo("x.heic")
    assert not is_photo("disk:/видео/a.mp4")
    assert not is_photo("")


def test_preview_paths_are_flat_by_kind(tmp_path, monkeypatch):
    """35.7.9: превью Диска и Безлимита — в своих плоских папках disk/unlim."""
    monkeypatch.setenv("PREVIEWS_DIR", str(tmp_path))
    p = preview_path_for("disk", "disk:/Фото/2019/a.jpg")
    assert os.path.dirname(p) == os.path.join(str(tmp_path), "disk")
    assert "Фото" not in p              # не повторяем структуру облака
    assert os.path.basename(p).startswith("a__")
    assert p.endswith(".jpg")

    u = preview_path_for("unlim", "/photounlim/2026-09-15 16-29-11/b.jpg")
    assert os.path.dirname(u) == os.path.join(str(tmp_path), "unlim")
    assert os.path.basename(u).startswith("b__")


def test_preview_names_do_not_collide_for_same_file_name(tmp_path, monkeypatch):
    monkeypatch.setenv("PREVIEWS_DIR", str(tmp_path))
    a = preview_path_for("disk", "disk:/Фото/2019/IMG_0001.JPG")
    b = preview_path_for("disk", "disk:/Разное/IMG_0001.JPG")
    assert a != b
    assert preview_path_for("disk", "disk:/Фото/2019/IMG_0001.JPG") == a


def test_preview_dir_defaults_to_program_data(monkeypatch):
    monkeypatch.delenv("PREVIEWS_DIR", raising=False)
    default = preview_dir()
    assert default == os.path.join(str(paths.DATA_DIR), PREVIEW_DIR_NAME)
    # по умолчанию превью лежат рядом с базами, а не в папке выгрузки
    assert "previews" in default


def test_preview_dir_follows_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("PREVIEWS_DIR", str(tmp_path))
    assert preview_dir() == str(tmp_path)


def test_remove_preview_is_quiet_on_missing(tmp_path):
    assert remove_preview(None) is False
    assert remove_preview(str(tmp_path / "nope.jpg")) is False
    f = tmp_path / "p.jpg"
    f.write_bytes(b"x")
    assert remove_preview(str(f)) is True
    assert not f.exists()


def test_save_from_link_writes_file(tmp_path):
    dst = str(tmp_path / "_превью" / "a.jpg")
    sess = _Session(_Resp(200, body=b"img"))
    save_from_link("https://storage.yandex.net/p.jpg", dst, session=sess)
    assert open(dst, "rb").read() == b"img"


def test_save_from_link_rejects_html_stub(tmp_path):
    from fetch import FetchFailure

    sess = _Session(_Resp(200, body=b"<html/>"))
    sess._responses[0].headers["Content-Type"] = "text/html"
    with pytest.raises(FetchFailure):
        save_from_link("https://h/p.jpg", str(tmp_path / "a.jpg"), session=sess)


def test_save_from_link_sends_auth_only_to_preview_link(tmp_path):
    """Токен уходит в первый запрос, но не на сервер хранения после редиректа."""
    sess = _Session(
        _Resp(302, headers={"Location": "https://mds.storage.yandex.net/x"}),
        _Resp(200, body=b"img"),
    )
    dst = str(tmp_path / "prev" / "a.jpg")
    save_from_link(
        "https://downloader.disk.yandex.ru/preview/x",
        dst,
        session=sess,
        headers={"Authorization": "OAuth secret"},
    )
    assert sess.calls[0][1]["headers"]["Authorization"] == "OAuth secret"
    assert "Authorization" not in sess.calls[1][1]["headers"]
    assert open(dst, "rb").read() == b"img"


class _Meta:
    preview = "https://downloader.disk.yandex.ru/preview/x"


class _Client:
    token = "y0_test"

    def get_meta(self, path, preview_size=None):
        return _Meta()


def test_make_disk_preview_uses_oauth_and_disk_folder(tmp_path, monkeypatch):
    """35.7.9: без токена Яндекс отвечает 403 — превью качается авторизованным."""
    monkeypatch.setenv("PREVIEWS_DIR", str(tmp_path))
    sess = _Session(_Resp(200, body=b"img"))
    dst = make_disk_preview(_Client(), "disk:/Фото/a.jpg", session=sess)
    assert dst == preview_path_for("disk", "disk:/Фото/a.jpg")
    assert open(dst, "rb").read() == b"img"
    assert sess.calls[0][1]["headers"]["Authorization"] == "OAuth y0_test"
    assert os.path.dirname(dst) == os.path.join(str(tmp_path), "disk")


def test_existing_disk_preview_is_not_downloaded_again(tmp_path, monkeypatch):
    """Повторный проход не дёргает API и не качает уже сохранённое превью."""
    monkeypatch.setenv("PREVIEWS_DIR", str(tmp_path))
    dst = preview_path_for("disk", "disk:/Фото/a.jpg")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(b"old")

    class _NoCalls(_Client):
        def get_meta(self, path, preview_size=None):
            raise AssertionError("get_meta не должен вызываться")

    sess = _Session()
    assert make_disk_preview(_NoCalls(), "disk:/Фото/a.jpg", session=sess) == dst
    assert sess.calls == []


def test_existing_unlim_preview_is_not_downloaded_again(tmp_path, monkeypatch):
    monkeypatch.setenv("PREVIEWS_DIR", str(tmp_path))
    db_path = tmp_path / "unlim_state.db"
    ub.init_db(db_path)
    con = ub.get_db(db_path)
    with con:
        con.execute("INSERT INTO unlim_files (file_id, name, size) VALUES ('fid','a.jpg',1)")
    con.close()
    dst = preview_path_for("unlim", "fid")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(b"old")

    class _NoPost(_Session):
        def post(self, url, **kwargs):
            raise AssertionError("сеть не нужна: превью уже есть")

    assert ub.save_preview_for_deferred(
        db_path, _NoPost(), "sk", "1", "fid", "a.jpg", tmp_path
    ) == dst

    con = ub.get_db(db_path)
    row = con.execute(
        "SELECT preview_path FROM unlim_files WHERE file_id='fid'"
    ).fetchone()
    con.close()
    assert row["preview_path"] == dst


def test_old_preview_is_removed_when_disk_original_arrives(st, tmp_path):
    preview = tmp_path / "disk" / "a__1.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"x")
    st.mark_pending("disk:/a.jpg", 1, None)
    st.set_preview_path("disk:/a.jpg", str(preview))

    old = st.mark_downloaded("disk:/a.jpg", str(tmp_path / "a.jpg"))

    assert old == str(preview)
    assert list(st.get_preview_paths()) == []


def test_old_preview_is_removed_when_unlim_original_arrives(tmp_path, monkeypatch):
    db_path = tmp_path / "unlim_state.db"
    ub.init_db(db_path)
    con = ub.get_db(db_path)
    preview = tmp_path / "unlim" / "a__1.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"x")
    with con:
        con.execute(
            "INSERT INTO unlim_files (file_id, name, size, preview_path) "
            "VALUES ('fid', 'a.jpg', 1, ?)",
            (str(preview),),
        )
    con.close()

    ub.mark_done(db_path, "fid", str(tmp_path / "a.jpg"), 1)

    assert not preview.exists()
    con = ub.get_db(db_path)
    row = con.execute(
        "SELECT preview_path FROM unlim_files WHERE file_id='fid'"
    ).fetchone()
    con.close()
    assert row["preview_path"] is None


def test_sweep_removes_preview_of_downloaded_file(st, tmp_path):
    preview = tmp_path / "_превью" / "a.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"x")
    st.mark_pending("disk:/a.jpg", 1, None)
    st.mark_deferred("disk:/a.jpg", "storage_down", "h")
    st.set_preview_path("disk:/a.jpg", str(preview))
    st.mark_downloaded("disk:/a.jpg", str(tmp_path / "a.jpg"))

    # mark_downloaded сам чистит preview_path, восстановим для проверки sweep.
    st.set_preview_path("disk:/a.jpg", str(preview))
    assert sweep(st) == 1
    assert not preview.exists()
    assert list(st.get_preview_paths()) == []


def test_sweep_forgets_missing_preview(st, tmp_path):
    st.mark_pending("disk:/a.jpg", 1, None)
    st.set_preview_path("disk:/a.jpg", str(tmp_path / "_превью" / "gone.jpg"))
    sweep(st)
    assert list(st.get_preview_paths()) == []


def test_sweep_keeps_preview_of_pending_file(st, tmp_path):
    preview = tmp_path / "_превью" / "a.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"x")
    st.mark_pending("disk:/a.jpg", 1, None)
    st.set_preview_path("disk:/a.jpg", str(preview))
    assert sweep(st) == 0
    assert preview.exists()


# ── ссылка на превью Безлимита ──────────────────────────────────────────────


def test_unlim_preview_prefers_xxxl():
    payload = {"meta": {"sizes": [
        {"name": "XL", "url": "//img/xl"},
        {"name": "XXXL", "url": "//img/xxxl"},
    ]}}
    sess = _Session(_Resp(200, payload))
    url = unlim_preview_url(sess, "sk", "42", "file-id", "https://base")
    assert url == "https://img/xxxl"


def test_unlim_preview_falls_back_to_xxl():
    payload = {"meta": {"sizes": [{"name": "XXL", "url": "https://img/xxl"}]}}
    sess = _Session(_Resp(200, payload))
    assert unlim_preview_url(sess, "sk", "42", "f", "https://base") == "https://img/xxl"


def test_unlim_preview_returns_none_on_error():
    sess = _Session(_Resp(500, {"meta": {}}))
    assert unlim_preview_url(sess, "sk", "42", "f", "https://base") is None


def test_unlim_preview_returns_none_without_sizes():
    sess = _Session(_Resp(200, {"meta": {}}))
    assert unlim_preview_url(sess, "sk", "42", "f", "https://base") is None


def test_unlim_preview_request_shape():
    payload = {"meta": {"sizes": []}}
    sess = _Session(_Resp(200, payload))
    unlim_preview_url(sess, "sk", "42", "file-id", "https://base")
    url, kwargs = sess.calls[0]
    assert url == "https://base/models-v2"
    assert kwargs["params"] == {"m": "mpfs/info"}
    assert kwargs["json"]["requestParams"] == {"path": "file-id", "meta": "sizes"}
