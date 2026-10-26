"""Первичная настройка: маркер, папки и ключ отчётов сохраняются локально."""

import os

import pytest

import paths
import webui
from webui import ReportKeyPayload, SetupPayload


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(webui, "ENV_FILE", data / ".env")
    monkeypatch.setattr(webui, "ACTION_LOG", data / "actions.log")
    monkeypatch.setattr(paths, "COOKIES_FILE", data / "cookies.json")
    monkeypatch.setattr(paths, "SETUP_DONE_FILE", data / ".setup_done")
    monkeypatch.setattr(paths, "REPORT_KEY_FILE", data / ".report_key.txt")
    monkeypatch.setattr(paths, "BUNDLED_REPORT_KEY_FILE", data / ".bundled_report_key.txt")

    for key in ("YA_DISK_TOKEN", "REPORT_KEY", "DISK_DIR",
                "DOWNLOAD_DIR", "UNLIM_DIR"):
        monkeypatch.delenv(key, raising=False)
    try:
        yield data
    finally:
        for key in ("DISK_DIR", "DOWNLOAD_DIR", "UNLIM_DIR"):
            os.environ.pop(key, None)


def test_new_install_needs_setup(setup_env):
    status = webui.api_setup_status()
    assert status["needs_setup"] is True
    assert status["has_token"] is False
    assert status["has_cookies"] is False
    assert status["setup_done"] is False


def test_setup_saves_dirs_and_marks_done(setup_env, tmp_path):
    disk = tmp_path / "disk"
    unlim = tmp_path / "unlim"
    result = webui.api_setup(SetupPayload(
        disk_dir=str(disk), unlim_dir=str(unlim), report_key="secret-key"
    ))

    assert result["ok"] is True
    assert disk.is_dir()
    assert unlim.is_dir()
    assert paths.SETUP_DONE_FILE.exists()
    env_text = webui.ENV_FILE.read_text(encoding="utf-8")
    assert f"DISK_DIR={disk}" in env_text
    assert f"UNLIM_DIR={unlim}" in env_text
    assert paths.REPORT_KEY_FILE.read_text(encoding="utf-8") == "secret-key"
    assert result["status"]["needs_setup"] is False


def test_skip_marks_done(setup_env):
    result = webui.api_setup_skip()
    assert result["ok"] is True
    assert paths.SETUP_DONE_FILE.exists()
    assert result["status"]["needs_setup"] is False
    assert webui.api_setup_status()["needs_setup"] is False


def test_report_key_can_be_saved_and_cleared(setup_env):
    result = webui.api_diagnostics_key(ReportKeyPayload(key="abc"))
    assert result == {"ok": True, "configured": True}
    assert webui.api_setup_status()["report_key_configured"] is True

    result = webui.api_diagnostics_key(ReportKeyPayload(key=""))
    assert result == {"ok": True, "configured": False}
    assert not paths.REPORT_KEY_FILE.exists()
