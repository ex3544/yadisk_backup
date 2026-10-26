"""Проверки сборочных артефактов установщика (без запуска Inno Setup)."""

from __future__ import annotations

from pathlib import Path

import pytest

import build_installer


def test_report_key_can_be_disabled(monkeypatch):
    monkeypatch.setenv("REPORT_KEY", "secret-key")
    assert build_installer.read_report_key(False) == ""


def test_report_key_from_env(monkeypatch):
    monkeypatch.setenv("REPORT_KEY", "abc-123_XYZ")
    assert build_installer.read_report_key(True) == "abc-123_XYZ"


def test_invalid_report_key_rejected(monkeypatch):
    monkeypatch.setenv("REPORT_KEY", "bad key with spaces")
    with pytest.raises(SystemExit):
        build_installer.read_report_key(True)


def test_existing_webview2_bootstrapper_is_used(monkeypatch, tmp_path):
    bootstrapper = tmp_path / "MicrosoftEdgeWebView2Setup.exe"
    bootstrapper.write_bytes(b"MZ-fake")
    monkeypatch.setattr(build_installer, "WEBVIEW2_BOOTSTRAPPER", bootstrapper)
    assert build_installer.ensure_webview2_bootstrapper() == bootstrapper


def test_iss_script_contract():
    text = (Path(build_installer.PROJECT_DIR) / "installer" / "yadisk_backup.iss").read_text(
        encoding="utf-8"
    )
    assert "PrivilegesRequired=admin" in text
    assert "DefaultDirName={autopf}\\YandexDiskBackup" in text
    assert "YandexDiskBackup.exe" in text
    assert "dist\\YandexDiskBackup" in text
    assert "ReportKey" in text
    assert "AppVersion" in text
    # WebView2 должен ставиться сам на новой машине.
    assert "MicrosoftEdgeWebView2Setup.exe" in text
    assert "/silent /install" in text
    # Данные пользователя не должны удаляться вместе с программой.
    assert "localappdata}\\YandexDiskBackup" in text
