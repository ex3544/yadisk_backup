"""Полуавтоматическое получение OAuth-токена: разбор страницы и сохранение."""

import json

import fetch_token


TOKEN = "y0_" + "B" * 40


def test_token_regex_accepts_yandex_token():
    assert fetch_token._found_in_text(f"ваш токен: {TOKEN}") == TOKEN
    assert fetch_token._found_in_text("обычный текст без токена") == ""


class _Field:
    def __init__(self, value):
        self.value = value

    def input_value(self):
        return self.value


class _Page:
    def __init__(self, fields=(), url="https://yandex.ru/dev/disk/poligon/",
                 content="", storage=""):
        self.fields = list(fields)
        self.url = url
        self.content_text = content
        self.storage = storage

    def is_closed(self):
        return False

    def query_selector_all(self, selector):
        return self.fields

    def content(self):
        return self.content_text

    def evaluate(self, script):
        return self.storage


def test_scan_page_finds_token_in_input():
    page = _Page(fields=[_Field(""), _Field(TOKEN)])
    assert fetch_token._scan_page(page) == TOKEN


def test_scan_page_finds_token_in_url_and_storage():
    page = _Page(url=f"https://yandex.ru/dev/disk/poligon/#access_token={TOKEN}")
    assert fetch_token._scan_page(page) == TOKEN

    page = _Page(storage=json.dumps({"token": TOKEN}))
    assert fetch_token._scan_page(page) == TOKEN


def test_save_token_keeps_other_env_values(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DISK_DIR=C:/disk\nYA_DISK_TOKEN=old\n", encoding="utf-8")
    monkeypatch.setattr(fetch_token, "ENV_FILE", env)

    fetch_token.save_token(TOKEN)

    text = env.read_text(encoding="utf-8")
    assert f"YA_DISK_TOKEN={TOKEN}" in text
    assert "DISK_DIR=C:/disk" in text
    assert "YA_DISK_TOKEN=old" not in text


def test_check_token_reports_login(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"user": {"login": "sergey"}}

    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse())
    assert fetch_token.check_token(TOKEN) == {
        "ok": True, "msg": "OK", "login": "sergey"}


def test_check_token_rejects_bad_token(monkeypatch):
    class FakeResponse:
        status_code = 401

        def json(self):
            return {}

    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResponse())
    out = fetch_token.check_token(TOKEN)
    assert out["ok"] is False
    assert "401" in out["msg"]
