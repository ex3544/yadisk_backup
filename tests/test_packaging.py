"""Проверки упаковки: динамические импорты должны попадать в frozen-сборку."""

from __future__ import annotations

from pathlib import Path

from version import APP_VERSION

PROJECT_DIR = Path(__file__).resolve().parents[1]
REPORT_KEY_HINT = "ключ выдаёт ex3544; без него отчёт остаётся на диске"


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


def test_changelog_has_current_version():
    changelog = (PROJECT_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{APP_VERSION}]" in changelog


def test_release_workflow_uses_changelog_body():
    workflow = (PROJECT_DIR / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "tools/changelog_for_tag.py" in workflow
    assert "body_path: release_notes.md" in workflow
    assert "generate_release_notes: true" not in workflow


def test_report_key_hint_is_visible_in_both_places():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert html.count(REPORT_KEY_HINT) >= 2


def test_organize_stop_button_disabled_until_sorting_runs():
    """Кнопка «Остановить» не должна выглядеть активной без запущенной задачи."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    marker = html.index('id="organize-stop-btn"')
    tag_start = html.rindex("<button", 0, marker)
    button_tag = html[tag_start : html.index(">", tag_start) + 1]
    assert "disabled" in button_tag

    assert "function updateOrganizeControls(task)" in html
    assert "btn.disabled = !(task && task.running)" in html
    assert "updateOrganizeControls(tasks.organize);" in html
    assert ".big-btn.danger:disabled" in html


def test_launch_modes_are_visually_separate_with_button_hints():
    """«Скачать всё» и пошаговый режим — разные блоки, у кнопок есть подсказки."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'class="card launch-card auto"' in html
    assert 'class="card launch-card manual"' in html
    assert "Быстрый запуск: скачать всё" in html
    assert "Пошаговый режим" in html
    assert "1 клик" in html
    assert "вручную" in html
    assert "Настройки пошагового режима ниже не используются." in html

    # Галочка больше не притворяется общей: объясняем, что повторяются и failed, и skipped.
    assert "Сначала новые, затем сбойные" in html
    assert "ошибки Диска (failed) и пропущенные Безлимита (skipped)" in html

    for wrapper in (
        "ctrl-all-wrap",
        "ctrl-scan-wrap",
        "ctrl-start-wrap",
        "ctrl-stop-wrap",
    ):
        assert f'id="{wrapper}"' in html

    for hint in (
        "Диск + Безлимит сразу, без ручной настройки.",
        "Обновляет список файлов в облаке. Ничего не скачивает.",
        "Скачивает только выбранный раздел по настройкам выше.",
        "Прерывает скачивание. Прогресс и очередь сохраняются.",
    ):
        assert hint in html

    # Подсказки прячутся вместе с кнопками, а не остаются висеть.
    assert "startWrap.style.display = 'none'" in html
    assert "stopWrap.style.display = 'flex'" in html


def test_readme_offers_ready_build_before_python_source():
    readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
    assert "Python устанавливать не нужно" in readme
    assert readme.index("Вариант A — готовый установщик") < readme.index(
        "Вариант C — из исходников"
    )
