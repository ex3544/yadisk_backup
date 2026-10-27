"""Проверки проверки обновлений (GitHub Releases) без реальной сети."""

from __future__ import annotations

import io
import json
import urllib.error


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
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _release("v99.0.0"))
    result = updater.check_for_updates()
    assert result["ok"] is True
    assert result["latest"] == "99.0.0"
    assert result["update_available"] is True
    assert result["url"].endswith("/releases/tag/v99.0.0")


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


# ── загрузка установщика ────────────────────────────────────────────────────

ASSET_URL = (
    f"https://github.com/{version.APP_REPO}/releases/download/v99.0.0/"
    "YandexDiskBackup-99.0.0-setup.exe"
)


def _asset_release(name: str = "YandexDiskBackup-99.0.0-setup.exe",
                   url: str = ASSET_URL, extra: bool = True) -> FakeResponse:
    assets = [{
        "name": name,
        "browser_download_url": url,
        "size": 11,
        "state": "uploaded",
    }]
    if extra:
        assets.append({
            "name": name + ".sha256",
            "browser_download_url": url + ".sha256",
            "size": 80,
            "state": "uploaded",
        })
    payload = {"tag_name": "v99.0.0", "html_url": f"{version.APP_REPO_URL}/releases/tag/v99.0.0",
               "assets": assets}
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def test_check_exposes_installer_and_sha(monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _asset_release())
    result = updater.check_for_updates()
    assert result["installer"]["name"] == "YandexDiskBackup-99.0.0-setup.exe"
    assert result["installer"]["sha256_url"].endswith(".sha256")
    assert result["portable"] is None


def test_pick_assets_ignores_other_versions_and_foreign_hosts():
    data = {"assets": [
        {"name": "YandexDiskBackup-90.0.0-setup.exe",
         "browser_download_url": ASSET_URL},
        {"name": "YandexDiskBackup-99.0.0-setup.exe",
         "browser_download_url": "https://evil.example/setup.exe"},
        {"name": "YandexDiskBackup-99.0.0-portable.zip",
         "browser_download_url": ASSET_URL.replace("-setup.exe", "-portable.zip")},
    ]}
    setup, portable = updater.pick_assets(data, (99, 0, 0))
    assert setup is None
    assert portable is not None
    assert portable["sha256_url"] == ""


def test_asset_url_allowed_only_our_releases():
    assert updater.asset_url_allowed(ASSET_URL)
    assert not updater.asset_url_allowed("https://github.com/other/repo/releases/download/x")
    assert not updater.asset_url_allowed("file:///C:/Windows/setup.exe")


class FakeAssetResponse(io.BytesIO):
    headers = {"Content-Length": "11"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_download_asset_verifies_sha256(monkeypatch, tmp_path):
    payload = b"hello world"
    import hashlib

    monkeypatch.setattr(
        updater.urllib.request, "urlopen",
        lambda *a, **k: FakeAssetResponse(payload),
    )
    seen = []
    dest = tmp_path / "setup.exe"

    actual = updater.download_asset(
        ASSET_URL, dest, hashlib.sha256(payload).hexdigest().upper(),
        progress=lambda done, total: seen.append((done, total)),
    )

    assert dest.read_bytes() == payload
    assert actual == hashlib.sha256(payload).hexdigest()
    assert seen == [(len(payload), len(payload))]
    assert list(tmp_path.glob("*.part")) == []


def test_download_asset_rejects_wrong_sha(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *a, **k: FakeAssetResponse(b"hello world")
    )
    dest = tmp_path / "setup.exe"

    with pytest.raises(ValueError, match="контрольная сумма"):
        updater.download_asset(ASSET_URL, dest, "0" * 64)

    assert not dest.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_download_asset_rejects_foreign_url(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="недопустимый адрес"):
        updater.download_asset("https://evil.example/setup.exe", tmp_path / "x.exe")
