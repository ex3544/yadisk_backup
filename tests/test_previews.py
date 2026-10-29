"""Тесты превью отложенных фото."""

import os

import pytest

from previews import (
    PREVIEW_DIR_NAME,
    is_photo,
    preview_dir,
    preview_path_for,
    remove_preview,
    save_from_link,
    sweep,
    unlim_preview_url,
)
from state import State


class _Resp:
    def __init__(self, status=200, payload=None, body=b""):
        self.status_code = status
        self._payload = payload
        self._body = body
        self.headers = {"Content-Type": "image/jpeg"}

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
def st(tmp_path):
    s = State(str(tmp_path / "state.db"))
    yield s
    s.close()


def test_is_photo_by_extension():
    assert is_photo("disk:/Фото/a.JPG")
    assert is_photo("x.heic")
    assert not is_photo("disk:/видео/a.mp4")
    assert not is_photo("")


def test_preview_path_mirrors_structure(tmp_path):
    p = preview_path_for(str(tmp_path), "disk:/Фото/2019/a.jpg")
    assert PREVIEW_DIR_NAME in p
    assert p.endswith("a.jpg")
    assert os.sep + "Фото" in p
    assert p.startswith(str(tmp_path))


def test_preview_dir_inside_download_dir(tmp_path):
    assert preview_dir(str(tmp_path)) == os.path.join(str(tmp_path), PREVIEW_DIR_NAME)


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
    assert sweep(st, str(tmp_path)) == 1
    assert not preview.exists()
    assert list(st.get_preview_paths()) == []


def test_sweep_forgets_missing_preview(st, tmp_path):
    st.mark_pending("disk:/a.jpg", 1, None)
    st.set_preview_path("disk:/a.jpg", str(tmp_path / "_превью" / "gone.jpg"))
    sweep(st, str(tmp_path))
    assert list(st.get_preview_paths()) == []


def test_sweep_keeps_preview_of_pending_file(st, tmp_path):
    preview = tmp_path / "_превью" / "a.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"x")
    st.mark_pending("disk:/a.jpg", 1, None)
    st.set_preview_path("disk:/a.jpg", str(preview))
    assert sweep(st, str(tmp_path)) == 0
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
