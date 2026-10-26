"""Проверки упаковки: динамические импорты должны попадать в frozen-сборку."""

from __future__ import annotations

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_spec_keeps_yadisk_session_modules():
    """yadisk импортирует сессию динамически, PyInstaller её не видит сам."""
    spec = (PROJECT_DIR / "yadisk_backup.spec").read_text(encoding="utf-8")
    assert "'yadisk.sessions'" in spec
    assert "'yadisk.sessions.requests_session'" in spec


def test_installer_license_is_bilingual_and_russian_first():
    """В установщике русский текст соглашения должен идти перед английским."""
    iss = (PROJECT_DIR / "installer" / "yadisk_backup.iss").read_text(encoding="utf-8")
    assert "LicenseFile=LICENSE_BILINGUAL.txt" in iss

    license_text = (PROJECT_DIR / "installer" / "LICENSE_BILINGUAL.txt").read_text(
        encoding="utf-8-sig"
    )
    assert license_text.index("ЛИЦЕНЗИЯ MIT") < license_text.index("MIT License")


def test_russian_license_file_exists():
    assert (PROJECT_DIR / "LICENSE.ru.txt").is_file()
