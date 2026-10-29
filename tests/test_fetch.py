"""Тесты собственного транспорта: редиректы, мёртвые серверы, проверки файла."""

import hashlib
import os

import pytest

from fetch import (
    DeadHosts,
    FetchFailure,
    fetch_to_file,
    is_md5,
    part_path_for,
    scrub_urls,
)


class _Resp:
    """Минимальный дублёр requests.Response для stream=True."""

    def __init__(self, status=200, body=b"", location=None, ctype=None):
        self.status_code = status
        self._body = body
        self.headers = {}
        if location:
            self.headers["Location"] = location
        if ctype:
            self.headers["Content-Type"] = ctype
        self.closed = False

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class _Session:
    """Отдаёт заранее заданные ответы по порядку и запоминает запросы."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self._responses:
            raise AssertionError(f"лишний запрос на {url}")
        return self._responses.pop(0)

    def close(self):
        pass


# ── мелочи ──────────────────────────────────────────────────────────────────


def test_is_md5_accepts_only_32_hex():
    assert is_md5("0" * 32)
    assert is_md5("ABCDEF" * 5 + "AB")
    assert not is_md5("0" * 31)
    assert not is_md5("z" * 32)
    assert not is_md5(None)


def test_scrub_urls_hides_signed_links():
    text = "ошибка https://storage.yandex.net/x?signature=secret при загрузке"
    assert "signature" not in scrub_urls(text)
    assert "<ссылка скрыта>" in scrub_urls(text)


def test_part_path_is_hidden_and_unique(tmp_path):
    dst = str(tmp_path / "a.jpg")
    p1 = part_path_for(dst)
    p2 = part_path_for(dst)
    assert p1 != p2
    assert os.path.dirname(p1) == str(tmp_path)
    assert os.path.basename(p1).startswith(".yadisk-")
    assert os.path.basename(p1).endswith(".part")


def test_failure_is_deferred_only_for_transport_kinds():
    assert FetchFailure("timeout", "x").is_deferred
    assert FetchFailure("storage_skip", "x").is_deferred
    assert not FetchFailure("not_found", "x").is_deferred
    assert not FetchFailure("bad_file", "x").is_deferred


def test_failure_str_never_contains_url():
    e = FetchFailure("timeout", "не успел https://h/p?sign=1")
    assert "sign" not in str(e)


# ── редиректы ───────────────────────────────────────────────────────────────


def test_redirect_chain_reaches_storage_host(tmp_path):
    dst = str(tmp_path / "f.bin")
    body = b"hello world"
    sess = _Session(
        _Resp(302, location="https://storage.yandex.net/f?sig=1"),
        _Resp(200, body=body),
    )
    res = fetch_to_file("https://cloud-api.yandex.net/v1/disk/x", dst, session=sess)
    assert res.bytes_written == len(body)
    assert res.host == "storage.yandex.net"
    assert open(dst, "rb").read() == body
    # Первый запрос — без авторедиректа, токен на хранилище не уходит.
    assert sess.calls[0][1]["allow_redirects"] is False
    assert sess.calls[1][1]["allow_redirects"] is False


def test_protocol_relative_location_is_upgraded(tmp_path):
    dst = str(tmp_path / "f.bin")
    sess = _Session(
        _Resp(302, location="//storage.yandex.net/f"),
        _Resp(200, body=b"x"),
    )
    res = fetch_to_file("https://cloud-api.yandex.net/x", dst, session=sess)
    assert sess.calls[1][0].startswith("https://storage.yandex.net/")
    assert res.host == "storage.yandex.net"


def test_too_many_redirects_is_bad_redirect(tmp_path):
    dst = str(tmp_path / "f.bin")
    sess = _Session(*[_Resp(302, location="/again") for _ in range(5)])
    with pytest.raises(FetchFailure) as ei:
        fetch_to_file("https://a.example/x", dst, session=sess)
    assert ei.value.kind == "bad_redirect"
    assert not os.path.exists(dst)


# ── мёртвые серверы ─────────────────────────────────────────────────────────


def test_dead_host_skips_without_request(tmp_path):
    dst = str(tmp_path / "f.bin")
    dead = DeadHosts()
    dead.mark("storage.yandex.net")
    sess = _Session()  # ни одного запроса быть не должно
    with pytest.raises(FetchFailure) as ei:
        fetch_to_file(
            "https://storage.yandex.net/f", dst, dead_hosts=dead, session=sess
        )
    assert ei.value.kind == "storage_skip"
    assert ei.value.is_deferred
    assert sess.calls == []


def test_dead_host_expires_by_ttl():
    now = [1000.0]
    dead = DeadHosts(ttl=60.0, clock=lambda: now[0])
    dead.mark("h")
    assert dead.is_dead("h")
    now[0] += 61
    assert not dead.is_dead("h")
    assert dead.snapshot() == []


def test_snapshot_lists_only_live_hosts():
    now = [0.0]
    dead = DeadHosts(ttl=10.0, clock=lambda: now[0])
    dead.mark("a")
    now[0] = 5
    dead.mark("b")
    now[0] = 12
    assert dead.snapshot() == ["b"]


# ── проверки файла ──────────────────────────────────────────────────────────


def test_size_mismatch_is_not_a_kept_file(tmp_path):
    dst = str(tmp_path / "f.bin")
    sess = _Session(_Resp(200, body=b"12345"))
    with pytest.raises(FetchFailure) as ei:
        fetch_to_file("https://h/f", dst, expected_size=10, session=sess)
    assert ei.value.kind == "bad_file"
    assert not os.path.exists(dst)
    # Временный файл тоже не должен остаться.
    assert [n for n in os.listdir(tmp_path)] == []


def test_md5_matches_and_is_returned(tmp_path):
    dst = str(tmp_path / "f.bin")
    body = b"payload"
    md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
    sess = _Session(_Resp(200, body=body))
    res = fetch_to_file("https://h/f", dst, expected_md5=md5, session=sess)
    assert res.md5 == md5
    assert open(dst, "rb").read() == body


def test_md5_mismatch_leaves_no_file(tmp_path):
    dst = str(tmp_path / "f.bin")
    sess = _Session(_Resp(200, body=b"payload"))
    with pytest.raises(FetchFailure) as ei:
        fetch_to_file("https://h/f", dst, expected_md5="0" * 32, session=sess)
    assert ei.value.kind == "bad_file"
    assert not os.path.exists(dst)


def test_existing_file_survives_failed_attempt(tmp_path):
    """Проверка до os.replace: старый файл не должен исчезнуть при сбое."""
    dst = str(tmp_path / "f.bin")
    with open(dst, "wb") as f:
        f.write(b"old")
    sess = _Session(_Resp(200, body=b"new"))
    with pytest.raises(FetchFailure):
        fetch_to_file("https://h/f", dst, expected_md5="0" * 32, session=sess)
    assert open(dst, "rb").read() == b"old"


def test_reject_pages_blocks_html(tmp_path):
    dst = str(tmp_path / "f.jpg")
    sess = _Session(_Resp(200, body=b"<html/>", ctype="text/html; charset=utf-8"))
    with pytest.raises(FetchFailure) as ei:
        fetch_to_file("https://h/f.jpg", dst, reject_pages=True, session=sess)
    assert ei.value.kind == "bad_file"
    assert not os.path.exists(dst)


def test_html_allowed_when_pages_not_rejected(tmp_path):
    """У обычного Диска есть настоящие .html — их не отбрасываем."""
    dst = str(tmp_path / "p.html")
    sess = _Session(_Resp(200, body=b"<html/>", ctype="text/html"))
    res = fetch_to_file("https://h/p.html", dst, session=sess)
    assert res.bytes_written == 7


# ── HTTP-коды ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,kind,deferred",
    [
        (401, "refused", False),
        (403, "refused", False),
        (404, "not_found", False),
        (429, "rate_limit", True),
        (500, "server_50x", True),
        (503, "server_50x", True),
    ],
)
def test_status_codes_are_typed(tmp_path, status, kind, deferred):
    dst = str(tmp_path / "f.bin")
    sess = _Session(_Resp(status))
    with pytest.raises(FetchFailure) as ei:
        fetch_to_file("https://h/f", dst, session=sess)
    assert ei.value.kind == kind
    assert ei.value.status == status
    assert ei.value.is_deferred is deferred


def test_deferred_failure_carries_host(tmp_path):
    dst = str(tmp_path / "f.bin")
    sess = _Session(_Resp(503))
    with pytest.raises(FetchFailure) as ei:
        fetch_to_file("https://storage.yandex.net/f", dst, session=sess)
    assert ei.value.host == "storage.yandex.net"
