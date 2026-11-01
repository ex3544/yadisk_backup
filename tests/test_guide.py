"""Инструкция «Быстрый старт и все функции» и её первый показ."""

from __future__ import annotations

import paths
import guide

PROJECT_DIR = paths.BUNDLE_DIR


def test_instruction_file_is_in_place_and_covers_the_main_sections():
    assert (PROJECT_DIR / "INSTRUCTION.md").exists()
    text = guide.read_text()
    assert "Yad.backup — инструкция" in text
    assert "## Быстрый старт" in text
    for section in ("Главная", "Настройки", "Организация", "Диагностика"):
        assert section in text
    # Самые важные функции, которые люди ищут в справке.
    for feature in ("Сверка с облаком", "Файлы переехали?", "Проверка файлов",
                    "Cookies браузера", "Даты Безлимита"):
        assert feature in text


def test_status_reports_text_then_disappears_after_mark(monkeypatch, tmp_path):
    text_file = tmp_path / "INSTRUCTION.md"
    text_file.write_text("# Инструкция\n\n## Быстрый старт\n", encoding="utf-8")
    monkeypatch.setattr(guide, "GUIDE_FILE", text_file)
    monkeypatch.setattr(paths, "GUIDE_SEEN_FILE", tmp_path / ".guide_seen")
    monkeypatch.setattr(paths, "SETUP_DONE_FILE", tmp_path / ".setup_done")

    before = guide.status()
    assert before["ok"] is True
    assert before["seen"] is False
    assert before["setup_done"] is False
    assert "Быстрый старт" in before["text"]

    # Мастер первичной настройки ещё не закончен — окно не показываем.
    paths.SETUP_DONE_FILE.write_text("done", encoding="utf-8")
    assert guide.status()["setup_done"] is True

    guide.mark_seen()
    assert guide.seen() is True
    assert guide.status()["seen"] is True
    assert paths.GUIDE_SEEN_FILE.exists()


def test_missing_instruction_file_gives_a_human_message(monkeypatch, tmp_path):
    monkeypatch.setattr(guide, "GUIDE_FILE", tmp_path / "missing.md")
    monkeypatch.setattr(paths, "GUIDE_SEEN_FILE", tmp_path / ".guide_seen")
    monkeypatch.setattr(paths, "SETUP_DONE_FILE", tmp_path / ".setup_done")

    assert guide.read_text() == guide.MISSING_TEXT
    assert guide.status()["ok"] is False


def test_mark_seen_does_not_fail_when_folder_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        paths, "GUIDE_SEEN_FILE", tmp_path / "no" / "such" / ".guide_seen"
    )
    # Каталог создаётся по требованию; если прав нет — молча пропускаем.
    guide.mark_seen()
    assert paths.GUIDE_SEEN_FILE.exists()
