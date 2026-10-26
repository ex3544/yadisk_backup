"""Проверки упаковки: динамические импорты должны попадать в frozen-сборку."""

from __future__ import annotations

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_spec_keeps_yadisk_session_modules():
    """yadisk импортирует сессию динамически, PyInstaller её не видит сам."""
    spec = (PROJECT_DIR / "yadisk_backup.spec").read_text(encoding="utf-8")
    assert "'yadisk.sessions'" in spec
    assert "'yadisk.sessions.requests_session'" in spec
