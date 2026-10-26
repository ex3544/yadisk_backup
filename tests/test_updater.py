"""Проверки проверки обновлений (GitHub Releases) без реальной сети."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import updater
import version


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _release(tag: str, url: str | None = None) -> FakeResponse:
    payload = {
        "tag_name": tag,
        "name": f"Release {tag}",
        "html_url": url or f"https://github.com/{version.APP_REPO}/releases/tag/{tag}",
        "published_at": "2026-10-26T10:00:00Z",
        "body": "Что нового",
    }
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def test_parse_version_accepts_v_prefix_and_numeric():
    assert updater.parse_version("v35.4.4") == (35, 4, 4)
    assert updater.parse_version("35.4") == (35, 4)
    assert updater.parse_version(" 35.4.10 ") == (35, 4, 10)
    for bad in ("", "release-35", "35.4.4-beta", "v", None):
        assert updater.parse_version(bad) is None


def test_version_comparison_is_numeric():
    assert updater.is_newer((35, 4, 10), (35, 4, 9))
    assert not updater.is_newer((35, 4, 4), (35, 4, 4))
    assert not updater.is_newer((35, 4), (35, 4, 1))
    assert updater.is_newer((36,), (35, 9, 9))


def test_check_reports_new_version(monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _release("v35.4.5"))
    result = updater.check_for_updates()
    assert result["ok"] is True
    assert result["latest"] == "35.4.5"
    assert result["update_available"] is True
    assert result["url"].endswith("/releases/tag/v35.4.5")


def test_check_same_version_is_up_to_date(monkeypatch):
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *a, **k: _release(f"v{version.APP_VERSION}")
    )
    result = updater.check_for_updates()
    assert result["ok"] is True
    assert result["update_available"] is False


def test_check_handles_404(monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.HTTPError(updater.API_URL, 404, "Not Found", None, None)

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    result = updater.check_for_updates()
    assert result["ok"] is False
    assert "опубликованы" in result["error"]


def test_check_handles_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    result = updater.check_for_updates()
    assert result["ok"] is False
    assert "GitHub" in result["error"]


def test_check_rejects_foreign_release_url(monkeypatch):
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda *a, **k: _release("v99.0.0", "https://evil.example/release"),
    )
    result = updater.check_for_updates()
    assert result["ok"] is True
    assert result["url"] == f"{version.APP_REPO_URL}/releases"


def test_release_url_allowed_only_our_repo():
    assert updater.release_url_allowed(f"https://github.com/{version.APP_REPO}/releases/tag/v1")
    assert not updater.release_url_allowed("https://github.com/other/repo/releases")
    assert not updater.release_url_allowed("javascript:alert(1)")


def test_huge_response_rejected(monkeypatch):
    big = FakeResponse(b"x" * (updater.MAX_RESPONSE_BYTES + 1))
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: big)
    result = updater.check_for_updates()
    assert result["ok"] is False


def test_parse_error_is_reported(monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: FakeResponse(b"not json"))
    result = updater.check_for_updates()
    assert result["ok"] is False
