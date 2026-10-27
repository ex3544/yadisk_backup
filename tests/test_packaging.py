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
    assert "Полный цикл: проверка облака на новые файлы" in html

    # Повтор сбойных больше не отдельная галочка — он часть полного цикла.
    assert "Сначала новые, затем сбойные" not in html
    assert "по сбойным (ошибки Диска и пропущенные Безлимита)" in html

    # Автосортировка доступна в обоих режимах и честно предупреждает о времени.
    assert 'id="all-sort-after"' in html
    assert "document.getElementById('all-sort-after').checked" in html
    assert html.count("Может занять много времени. Можно не включать") == 2

    # Повторное сканирование Безлимита должно дополнять базу, а не пересоздавать.
    assert "Дополнить её новыми файлами из облака" in html
    assert "Статусы уже скачанных и пропущенных файлов сохранятся" in html
    assert "база дополнится — статусы уже скачанных сохранятся" in html
    assert "JSON.stringify({ mode: mode, force: false })" in html

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


def test_unlim_rebuild_lives_in_diagnostics_with_warning_and_backup():
    """Полное пересоздание базы — только в «Диагностике», с предупреждением."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    # Кнопка не на главной: ищем её внутри панели диагностики.
    diag_start = html.index('<div class="panel" data-panel="diagnostics">')
    diag_end = html.index("<!-- =============", diag_start) if "<!-- =============" in html[diag_start:] else len(html)
    diag_html = html[diag_start:diag_end]
    assert 'data-panel="launch"' not in diag_html
    assert "Пересоздание базы Безлимита" in diag_html
    assert 'id="rebuild-btn"' in diag_html

    # Последствия описаны прямо в окне.
    assert "файлы на диске" in diag_html and "не удаляются" in diag_html
    assert "будут <strong>скачаны заново</strong>" in diag_html
    assert "копия текущей базы сохраняется автоматически" in diag_html

    # Подтверждение словом и защита от случайного нажатия.
    assert "ПЕРЕСОЗДАТЬ" in diag_html
    assert "oninput=\"updateRebuildBtn()\"" in diag_html
    assert "inp.value.trim().toUpperCase() !== 'ПЕРЕСОЗДАТЬ'" in html
    assert "/api/unlim/rebuild" in html
    assert "updateRebuildBtn();" in html

    # Счётчики в предупреждении берутся из живого статуса.
    assert "function renderRebuildCounts(s)" in html
    assert "renderRebuildCounts(s);" in html


def test_backups_block_creates_and_restores_with_warnings():
    """Копии баз: создание по отдельности и вместе, восстановление с подтверждением."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    diag_start = html.index('<div class="panel" data-panel="diagnostics">')
    diag_html = html[diag_start:]
    assert "Резервные копии баз" in diag_html
    assert "Сами файлы не копируются" in diag_html

    for scope in ("disk", "unlim", "all"):
        assert f"createBackup('{scope}')" in diag_html
    assert 'id="backups-list"' in diag_html
    assert 'id="backup-restore-panel"' in diag_html

    assert "Введите ВОССТАНОВИТЬ" in diag_html
    assert "Файлы на дисках не удаляются" in diag_html
    assert "программа сама сохранит её копию" in diag_html
    assert "разложенные сортировкой — скачаются заново" in diag_html

    assert "inp.value.trim().toUpperCase() !== 'ВОССТАНОВИТЬ'" in html
    assert "/api/backups/restore" in html
    assert "fetch('/api/backups')" in html
    assert "function loadBackups()" in html
    assert "if(t.dataset.tab === 'diagnostics') loadBackups();" in html


def test_download_all_is_a_full_cycle_in_ui():
    """«Скачать всё» = проверка облака + проходы по сбойным, без старой галочки."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "all-retry-skipped" not in html
    assert "renderAutoRuns" not in html
    assert 'id="all-runs"' in html
    assert "Проходов по сбойным:" in html
    assert 'id="cycle-modal"' in html
    assert "function renderFullCycle(fc)" in html
    assert "renderFullCycle(s.full_cycle);" in html
    assert "JSON.stringify({ workers: workers, passes: passes, sort_after: sort_after })" in html


def test_exit_code_one_is_not_treated_as_user_stop():
    """main.py возвращает 1 при сбойных файлах — это не «остановлено»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    marker = html.index("function isStopped(code){")
    body = html[marker : html.index("}", marker) + 1]
    import re

    codes = [int(n) for n in re.findall(r"code === (-?\d+)", body)]
    assert codes == [-15, -2, 143, 130, 15]
