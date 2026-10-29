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
    assert "дополнится — статусы уже скачанных сохранятся" in html
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
        "Обходит облако и добавляет новые файлы. Ничего не скачивает.",
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


def test_db_cards_live_in_diagnostics_with_warning_and_backup():
    """Пересоздание, перепривязка и повторная загрузка — в «Диагностике»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    # Кнопка не на главной: ищем её внутри панели диагностики.
    diag_start = html.index('<div class="panel" data-panel="diagnostics">')
    diag_end = html.index("<!-- =============", diag_start) if "<!-- =============" in html[diag_start:] else len(html)
    diag_html = html[diag_start:diag_end]
    assert 'data-panel="launch"' not in diag_html
    assert "Базы данных" in diag_html
    assert 'id="rebuild-btn"' in diag_html
    # База выбирается одна: блок общий для Диска и Безлимита.
    assert 'id="db-scope"' in diag_html
    assert 'value="disk"' in diag_html and 'value="unlim"' in diag_html

    # Последствия описаны прямо в окне.
    assert "файлы на диске" in diag_html and "не удаляются" in diag_html
    assert "будут <strong>скачаны заново</strong>" in diag_html
    assert "копия текущей базы сохраняется рядом с ней" in diag_html

    # Подтверждение словом и защита от случайного нажатия.
    assert "ПЕРЕСОЗДАТЬ" in diag_html
    assert "oninput=\"updateRebuildBtn()\"" in diag_html
    assert "inp.value.trim().toUpperCase() !== 'ПЕРЕСОЗДАТЬ'" in html
    assert "/api/db/rebuild" in html
    assert "updateRebuildBtn();" in html

    # Переезд на другой диск: перепривязка без скачивания и повторная загрузка.
    assert 'id="db-new-dir"' in diag_html
    assert "onclick=\"relocateDb()\"" in diag_html
    assert "onclick=\"redownloadDb()\"" in diag_html
    assert "/api/db/relocate" in html
    assert "/api/db/redownload" in html

    # Счётчики в предупреждении берутся из живого статуса.
    assert "function renderRebuildCounts(s)" in html
    assert "renderRebuildCounts(s);" in html


def test_backups_live_in_organize_and_restore_keeps_its_guard():
    """Копии баз переехали в «Организацию», восстановление осталось с вводом слова."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    organize = _panel_html(html, "organize")
    diagnostics = _panel_html(html, "diagnostics")
    assert "Резервные копии баз" in organize
    assert "Резервные копии баз" not in diagnostics
    assert "Сами файлы не копируются" in organize

    for scope in ("disk", "unlim", "all"):
        assert f"createBackup('{scope}')" in organize
    assert 'id="backups-list"' in organize
    assert 'id="backup-restore-panel"' in organize

    assert "Введите ВОССТАНОВИТЬ" in organize
    assert "Файлы на дисках не удаляются" in organize
    assert "программа сама сохранит её копию" in organize
    assert "разложенные сортировкой — скачаются заново" in organize

    assert "inp.value.trim().toUpperCase() !== 'ВОССТАНОВИТЬ'" in html
    assert "/api/backups/restore" in html
    assert "fetch('/api/backups')" in html
    assert "function loadBackups()" in html
    # Список грузится при входе на вкладку «Организация», а не «Диагностика».
    assert "if(name === 'organize'){ toggleBackupsList(false); loadBackups(); }" in html
    assert "if(t.dataset.tab === 'diagnostics') loadBackups();" not in html


def test_backups_list_is_collapsed_by_default():
    """Список копий свёрнут всегда и раскрывается только по нажатию."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    organize = _panel_html(html, "organize")

    assert 'id="backups-toggle"' in organize
    assert 'id="backups-wrap" style="display:none"' in organize
    assert 'id="backups-count"' in organize
    assert "let _backupsOpen = false;" in html
    assert "function toggleBackupsList(force)" in html
    assert "wrap.style.display = _backupsOpen ? 'block' : 'none';" in html
    # Вход на вкладку снова сворачивает список.
    assert "toggleBackupsList(false); loadBackups();" in html
    # Созданную копию показываем сразу.
    assert "toggleBackupsList(true);" in html


def test_backups_and_reset_buttons_explain_themselves_without_confirm():
    """У шести кнопок — подсказки вместо окна подтверждения."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    for button_id, needle in (
        ("backup-create-disk", "createBackup('disk')"),
        ("backup-create-unlim", "createBackup('unlim')"),
        ("backup-create-all", "createBackup('all')"),
    ):
        marker = html.index(f'id="{button_id}"')
        tag_start = html.rindex("<button", 0, marker)
        tag = html[tag_start : html.index(">", tag_start) + 1]
        assert "tipnode" in tag, button_id
        assert "data-tip=" in tag, button_id
        assert needle in tag, button_id

    # У трёх кнопок сброса тоже есть подсказки.
    for what in ("disk_failed", "unlim_skipped", "unlim_stuck"):
        marker = html.index(f"resetStatus('{what}')")
        tag_start = html.rindex("<button", 0, marker)
        tag = html[tag_start : html.index(">", tag_start) + 1]
        assert "tipnode" in tag and "data-tip=" in tag, what

    # Ни создание копии, ни сброс больше не спрашивают подтверждения.
    for func in ("async function createBackup(scope){", "async function resetStatus(what){"):
        body = html[html.index(func) : html.index("\n}", html.index(func))]
        assert "askConfirm" not in body, func


def test_watchdog_is_a_compact_strip_between_the_folders_and_token():
    """Сторож — одна строка между «Папка для Безлимита» и «OAuth-токен»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    setup = _panel_html(html, "setup")

    unlim_pos = setup.index("Папка для Безлимита")
    watchdog_pos = setup.index('id="watchdog-card"')
    token_pos = setup.index("OAuth-токен")
    assert unlim_pos < watchdog_pos < token_pos

    # Компактная строка, а не карточка с абзацем описания.
    assert 'class="watchdog-line"' in setup
    assert "Проверяет диск раз в 30 секунд и останавливает выгрузку" not in setup
    assert "проверяет свободное место раз в 30 секунд" in setup  # это уже подсказка
    assert "#watchdog-card{padding:10px 14px}" in html
    # Две независимые колонки, чтобы сторож не уезжал вниз за высокий блок кук.
    assert 'class="setup-col"' in setup
    assert ".setup-col{display:flex;flex-direction:column;gap:12px}" in html
    assert 'data-panel="setup"] .card:nth-child(' not in html


def test_download_all_is_a_full_cycle_in_ui():
    """«Скачать всё» = проверка облака + проходы по сбойным, без старой галочки."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "all-retry-skipped" not in html
    assert "renderAutoRuns" not in html
    assert 'id="all-runs"' in html
    assert "Проходов по сбойным:" in html
    # Ровно заданное число проходов: верхняя граница 10, без досрочного выхода.
    assert 'id="all-runs" value="3" min="1" max="10"' in html
    assert ", 10));" in html
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


def test_console_scroll_sticks_only_when_user_is_at_bottom():
    """Журнал не должен прыгать вниз, пока пользователь читает выше."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'id="pipeline-console" tabindex="0"' in html
    assert "const CONSOLE_STICK_PX = 24;" in html
    assert "_consoleStick[id] = consoleNearBottom(el);" in html

    marker = html.index("function setConsole(id, text){")
    body = html[marker : html.index("\n}\n", marker)]
    assert "const stick = _consoleStick[id] !== false;" in body
    assert "if(stick)" in body


def test_backup_buttons_are_equal_and_lock_while_copying():
    """Все три кнопки копий одинаковые, на время создания блокируются."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    marker = html.index('id="backup-create-all"')
    tag_start = html.rindex("<button", 0, marker)
    tag = html[tag_start : html.index(">", tag_start) + 1]
    assert "primary" not in tag

    assert "backup-create-disk','backup-create-unlim','backup-create-all'" in html
    assert "buttons.forEach(b => { b.disabled = false; });" in html


def test_confirmation_uses_themed_dialog_instead_of_native_confirm():
    """Все подтверждения — через своё окно в цветах приложения."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'id="confirm-modal"' in html
    assert "function askConfirm(opts)" in html
    assert "function askConfirmClose(result)" in html
    # В 35.5.7 выпилены девять мёртвых функций (два их подтверждения ушли
    # вместе с ними), поэтому порог снижен: 12 живых подтверждений.
    assert html.count("await askConfirm(") >= 12
    # Системный confirm() остался только как страховка.
    assert "window.confirm(" in html
    assert "!confirm(" not in html

    # Компактное окно чуть ниже центра, в цветах приложения.
    assert ".confirm-box{" in html
    assert "max-width:430px" in html
    assert "margin-top:14vh" in html
    # Enter подтверждает, Esc и клик мимо — отмена.
    assert "e.key === 'Escape'" in html
    assert "e.key === 'Enter'" in html
    assert "askConfirmClose(false)" in html


def test_header_pills_have_hover_hints():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert ".pill[data-tip]:hover::after{" in html
    assert "content:attr(data-tip)" in html
    for pill in ("pill-token", "pill-cookies", "pill-speed",
                 "pill-session", "pill-disk-session", "pill-watchdog"):
        marker = f'id="{pill}"'
        tag_start = html.rindex("<span", 0, html.index(marker))
        tag = html[tag_start : html.index(">", tag_start) + 1]
        assert "data-tip=" in tag, pill


def _panel_html(html: str, name: str) -> str:
    start = html.index(f'data-panel="{name}">')
    nxt = html.find("<!-- =============", start)
    return html[start : nxt if nxt != -1 else len(html)]


def test_watchdog_lives_in_settings_with_status_pill():
    """Сторож перенесён в «Настройки», в шапке осталась его пилюля."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    home = _panel_html(html, "home")
    setup = _panel_html(html, "setup")
    assert 'id="watchdog-card"' in setup
    assert 'id="watchdog-threshold"' in setup
    assert 'id="watchdog-btn"' in setup
    assert 'id="watchdog-threshold"' not in home

    # Пилюля показывает состояние, но не управляет сторожем.
    assert "pill-watchdog-text" in html
    assert "pillText.textContent = `🛡 ${w.threshold_gb} ГБ`" in html
    assert "pillText.textContent = '🛡 выкл'" in html


def test_queue_reset_lives_in_diagnostics_and_fits_its_buttons():
    """Сброс очереди — рядом с пересозданием базы, подписи не вылезают вправо."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    home = _panel_html(html, "home")
    diag = _panel_html(html, "diagnostics")
    assert 'id="reset-queue-card"' in diag
    assert 'onclick="resetStatus(' in diag
    assert 'id="reset-queue-card"' not in home

    # Кнопки: подпись и счётчик в две строки, без nowrap и жёсткой ширины.
    assert ".reset-btn{" in html
    assert "flex-direction:column" in html
    assert "white-space:normal" in html
    assert "min-width:200px" not in html
    for counter in ("reset-disk-failed", "reset-unlim-skipped", "reset-unlim-stuck"):
        assert f'id="{counter}"' in diag


def test_workers_hint_reacts_to_the_chosen_value():
    """Подсказка потоков показывает выбранное и рекомендованное число."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    # С 35.5.7 подсказка держится в одну строку, а ОЗУ ушло в data-tip.
    assert "Потоки: — · совет —" in html
    assert "hint.textContent = `Потоки: ${Number.isFinite(val) ? val : '—'} · совет ${rec}`" in html
    assert "hint.dataset.tip = `Выбрано" in html
    assert "renderRamHint(window._lastStatus.ram);" in html
    assert "hint.classList.toggle('warn'" in html
    # Поле честно подписано: только Безлимит.
    assert "Потоков (Безлимит):" in html


def test_cloud_sync_button_is_called_update_file_list():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "Сформировать базу данных" not in html
    assert "Обновить список файлов" in html
    assert "Обходит облако и добавляет новые файлы. Ничего не скачивает." in html


def test_update_card_can_install_from_the_app():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    for marker in ('id="update-install-btn"', 'id="update-apply-btn"',
                   'id="update-progress-bar"', 'id="update-cancel-btn"'):
        assert marker in html
    assert "renderUpdate(s.update);" in html
    assert "Установка из этой сборки недоступна" in html


def test_installer_runs_without_admin_rights():
    """Установка идёт в папку пользователя, без UAC и без 64-битного режима."""
    iss = (PROJECT_DIR / "installer" / "yadisk_backup.iss").read_text(encoding="utf-8")

    assert "PrivilegesRequired=lowest" in iss
    assert "PrivilegesRequired=admin" not in iss
    assert r"{localappdata}\Programs\YandexDiskBackup" in iss
    assert "ArchitecturesInstallIn64BitMode" not in iss
    assert "LegacyInstallDir" in iss


def test_ui_warns_about_legacy_admin_install():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'id="legacy-install-banner"' in html
    assert "s.legacy_install" in html
    assert "запросить подтверждение администратора" not in html


def test_update_helper_is_not_started_detached():
    """DETACHED_PROCESS заставлял powershell.exe выйти, не выполнив скрипт.

    Симптом: нажали «Перезапустить и установить», программа закрылась,
    а версия осталась прежней и install_update.log не появлялся.
    """
    text = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    start = text.index("def api_updates_install")
    body = text[start : text.index('@app.post("/api/reports/open")', start)]

    assert 'getattr(subprocess, "CREATE_NO_WINDOW", 0)' in body
    assert 'getattr(subprocess, "DETACHED_PROCESS", 0)' not in body


def test_release_workflow_publishes_checksums():
    workflow = (PROJECT_DIR / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "Get-FileHash -Algorithm SHA256" in workflow
    assert "installer_output/YandexDiskBackup-*-setup.exe.sha256" in workflow
    assert "dist/YandexDiskBackup-*-portable.zip.sha256" in workflow


def test_window_frame_and_scrollbars_match_the_dark_theme():
    """Чёрный интерфейс не должен обрамляться белой системной рамкой."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert "color-scheme:dark;" in html
    assert "::-webkit-scrollbar{" in html
    assert "::-webkit-scrollbar-thumb{" in html

    launcher = (PROJECT_DIR / "launcher.py").read_text(encoding="utf-8")
    assert "_DWMWA_USE_IMMERSIVE_DARK_MODE = 20" in launcher
    assert "_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19" in launcher
    assert "DwmSetWindowAttribute" in launcher
    assert "_CAPTION_COLOR = 0x000A0A0D" in launcher
    # Только before_show: после показа окна Windows 10 вызов игнорирует.
    assert (
        "self.window.events.before_show += lambda: _darken_window_frame(self.window)"
        in launcher
    )


def test_step_mode_fields_have_themed_hints():
    """У всех четырёх полей — своя плашка вместо системной белой подсказки."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    for field in ("ctrl-workers", "ctrl-batch-size", "ctrl-batches", "ctrl-timeout"):
        marker = html.index(f'id="{field}"')
        tag_start = html.rindex("<input", 0, marker)
        tag = html[tag_start : html.index(">", tag_start) + 1]
        assert "title=" not in tag, field

        label_start = html.rindex("<label", 0, tag_start)
        label = html[label_start : html.index("</label>", label_start)]
        assert "tip-i" in label, field
        assert "data-tip=" in label, field

    # Системные подсказки из интерфейса убраны полностью.
    assert 'title="' not in html
    assert ".tipnode[data-tip]:hover::after{" in html
    assert ".tip-i{" in html


def test_app_is_named_yad_backup():
    """Сменилась только вывеска: установка и папка данных остались прежними."""
    from version import APP_NAME

    assert APP_NAME == "Yad.backup"

    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert "<title>Yad.backup" in html
    assert '<div class="brand-name">Yad.backup</div>' in html

    iss = (PROJECT_DIR / "installer" / "yadisk_backup.iss").read_text(encoding="utf-8")
    assert '#define AppName "Yad.backup"' in iss
    # Эти три вещи менять нельзя: иначе это будет другая программа для Windows
    # и обновление поверх установленной копии не сработает.
    assert '#define AppExeName "YandexDiskBackup.exe"' in iss
    assert r"DefaultDirName={localappdata}\Programs\YandexDiskBackup" in iss
    assert "8E4A3F42-6C1D-4A7E-9F2B-3544A1B2C3D4" in iss
    # Старые ярлыки от прежнего названия убираются.
    assert r'"{userprograms}\Yandex.Disk Backup.lnk"' in iss
    assert r'"{userdesktop}\Yandex.Disk Backup.lnk"' in iss

    import paths

    assert paths.APP_NAME == "YandexDiskBackup"


# ── 35.5.7: счётчики, подсказки, мёртвый код ────────────────────────────────


def test_session_pills_do_not_show_previous_session_numbers():
    """На старте плашки показывают «подготовка», а не итоги прошлой сессии.

    С 35.5.9 свежесть файла статуса проверяет сервер (build_session), а UI
    рисует уже готовый блок session.
    """
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    webui_src = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")

    assert "def build_session(" in webui_src
    assert "_status_fresh(" in webui_src
    assert '"preparing"' in webui_src
    assert "function renderSessionPill(" in html
    assert "подготовка…" in html
    # Дублирующие бейджи в панели управления удалены вместе с их отрисовкой.
    assert "session-count-badge" not in html
    assert "disk-session-badge" not in html
    assert "live-speed-badge" not in html


def test_service_pills_look_different_from_download_totals():
    """Токен, куки и сторож отделены от зелёных итоговых плашек."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'class="pill service clickable" id="pill-token"' in html
    assert 'class="pill service clickable" id="pill-cookies"' in html
    assert 'class="pills-sep"' in html
    assert ".pill.service{" in html
    assert ".pill.info{" in html
    # Сторож включён — тот же овал без заливки и рамки: состояние показывают
    # только точка и цвет текста, поэтому плашка не выглядит выше соседних.
    assert "pill.className = 'pill service on';" in html
    assert ".pill.service.on{background:transparent;border-color:transparent" in html
    assert "pill.className = 'pill info';" not in html


def test_watchdog_threshold_applies_without_restart():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    webui_src = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")

    assert 'onchange="applyWatchdogThreshold()"' in html
    assert "async function applyWatchdogThreshold()" in html
    # Сервер меняет порог у работающего сторожа, а не отказывает.
    assert "Порог сторожа изменён" in webui_src


def test_token_show_button_toggles_label_and_needs_input():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'id="btn-token-view"' in html
    assert "function syncTokenViewButton()" in html
    assert "b.textContent = show ? 'Скрыть' : 'Показать';" in html
    assert "b.disabled = !has;" in html
    assert "syncTokenViewButton();" in html
    # Кнопка по умолчанию неактивна: показывать нечего.
    assert 'id="btn-token-view" onclick="toggleTokenView()" disabled' in html


def test_reset_buttons_are_disabled_when_there_is_nothing_to_reset():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "setEnabled('disk_failed', diskFailed > 0 && !diskBusy);" in html
    assert "setEnabled('unlim_skipped', unlimSkipped > 0 && !unlimBusy);" in html
    assert "setEnabled('unlim_stuck', unlimStuck > 0 && !unlimBusy);" in html
    assert ".reset-btn:disabled{" in html


def test_partial_failure_is_orange_and_user_stop_is_neutral():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert ".badge.warn{" in html
    assert ".badge.stop{" in html
    assert "ps.className = 'badge warn'" in html
    assert "ps.className = 'badge stop'" in html
    # Серой плашки «с ошибками» больше нет.
    assert "ps.className = partial ? 'badge' : 'badge ok'" not in html


def test_dead_javascript_is_removed():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    for name in ("bigStart", "bigStop", "clearDb", "clearLog", "downloadSelected",
                 "updateDownloadBanner", "updateScanStatus", "updateUnlimBanner",
                 "workersWord"):
        assert name not in html, name


def test_ui_speaks_to_the_user_formally():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    for informal in ("Введи токен", "Укажи папку", "Залогинься", "Скопируй строку",
                     "Установи <a", "Кликни на иконку"):
        assert informal not in html, informal
    assert "Нет связи с программой: " in html


def test_home_layout_has_no_empty_half():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "align-content:start;" in html
    assert ".panel[data-panel=\"home\"] > .card:not(#pipeline-card){grid-column:1 / -1}" in html


def test_main_sets_utf8_stdout_before_scanning():
    """Без этого «→» в консоли cp1251 роняет сканирование в установленной сборке."""
    src = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")

    assert 'sys.stdout, sys.stderr' in src
    assert "reconfigure(encoding=\"utf-8\", errors=\"replace\")" in src


# ── 35.5.9: кликабельные плашки, таймер, кнопка обновления ──────────────────


def test_header_pills_navigate_to_their_settings():
    """Плашки в шапке — не только индикаторы: клик ведёт к настройке."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "function showPanel(name)" in html
    assert "function revealTarget(panel, selector)" in html
    assert "function gotoSetting(panel, selector)" in html
    assert "const PILL_TARGETS = {" in html
    for pill in ("active-tasks", "pill-token", "pill-cookies", "pill-watchdog",
                 "pill-speed", "pill-session", "pill-disk-session"):
        assert f"'{pill}':" in html, pill
    # С клавиатуры тоже работает, иначе кликабельность — только для мыши.
    assert html.count('role="button" tabindex="0"') >= 7
    assert "e.key === 'Enter' || e.key === ' '" in html
    assert ".pill.clickable,.active-tasks.clickable{cursor:pointer}" in html
    assert ".flash{animation:flashCard" in html
    assert "@keyframes flashCard{" in html
    # Подсказка не должна перехватывать клик по плашке.
    assert "pointer-events:none" in html


def test_eta_box_counts_down_on_home_page():
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'id="eta-box"' in html
    assert 'id="eta-text"' in html
    assert "function etaSeconds(kind, s, now)" in html
    # Скорость — только по успешным скачиваниям за окно в 3 минуты: повторы
    # сбойных не считаются работой (иначе «~1 мин» при нуле скачанного).
    assert "const ETA_WINDOW = 180;" in html
    assert "if(dOk > 0) return d.remaining / (dOk / span);" in html
    assert "const ETA_MIN_ELAPSED = 120;" in html
    assert "function renderEta(s)" in html
    assert "function fmtEta(sec)" in html
    assert "#eta-box{" in html
    # Одна цифра на всё задание = максимум по линиям, а не сумма: линии идут
    # параллельно. Детали по линиям — в подсказке.
    assert "lines.reduce((a, b) => (b.sec > a.sec ? b : a))" in html
    assert "box.dataset.tip" in html
    # Остаток считает сервер: UI не выдумывает, сколько файлов ещё впереди.
    assert "session[\"remaining\"]" in (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")


def test_pills_show_remaining_queue_and_report_is_collapsible():
    """Плашки показывают остаток очереди, результат отчёта сворачивается."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")

    # Плашка видна всегда: в покое — сколько осталось докачать (файлы и объём).
    assert "function queueLeft(db, kind)" in html
    assert "if(!d){" in html
    assert "⚠ ${leftText}" in html
    # Остаток в байтах считает сервер, а не UI.
    assert '"bytes_by_status"' in py
    assert 'session["remaining_bytes"]' in py
    # В журнале полного цикла видно, сколько файлов вернулось в очередь.
    assert "сброшено в очередь" in py
    assert "в повтор пойдут" in py
    # Результат отправки и список отчётов сворачиваются.
    assert 'id="diag-result-toggle"' in html
    assert 'id="reports-toggle"' in html
    assert "function toggleDiagResult(force)" in html
    assert "function toggleReportsList(force)" in html
    assert "showDiagnostics(d, true)" in html


def test_update_download_button_is_one_shot():
    """После скачивания остаётся только «Перезапустить и установить»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "install.style.display = 'none'; install.disabled = true;" in html
    assert "const alreadyReady = _lastUpdate && _lastUpdate.state === 'ready';" in html
    # В ошибке кнопку возвращаем — иначе обновление не повторить.
    assert "if(install && u.version){" in html


def test_waiting_files_banner_and_retry_now():
    """«Ждут Яндекса» — отдельная плашка, отложенные файлы не пугают как сбойные."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")

    assert 'id="waiting-banner"' in html
    assert 'id="waiting-banner-count"' in html
    assert "function retryWaiting()" in html
    assert "Попробовать сейчас" in html
    # Счётчик считает обе базы: Диск и Безлимит.
    assert "function waitingTotal(s)" in html
    assert "waiting" in py and '"waiting": 0' in py
    assert "storage_down%" in py


def test_disk_offline_banner_explains_what_to_do():
    """B0: диск отключён посреди прохода — понятная плашка, а не красный сбой."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")

    assert 'id="disk-offline-banner"' in html
    assert "Диск отключён" in html
    assert "disk_offline" in html
    assert '"disk_state": disk_status.get("state")' in py
    assert "disk_offline" in (PROJECT_DIR / "downloader.py").read_text(encoding="utf-8")


def test_folder_check_button_explains_the_path():
    """A1/A2: подсказка про полный путь и проверка папки без её создания."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    utils = (PROJECT_DIR / "utils.py").read_text(encoding="utf-8")

    assert html.count("Проверить папку") >= 4  # две карточки + два шага мастера
    assert "function checkFolder(inputId, statusId)" in html
    assert "function _looksAbsolute(p)" in html
    assert "Полный путь с буквой диска" in html
    assert "/api/check_dir" in html and "/api/check_dir" in py
    assert "def check_target_path(path" in utils
    # Проверка не создаёт папку: пользователь может ошибиться в пути.
    assert "os.makedirs" not in utils.split("def check_target_path")[1].split("def ")[0]


def test_previews_toggle_lives_in_settings():
    """B3: превью отложенных фото настраиваются и по умолчанию включены."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")

    assert 'id="previews-btn"' in html
    assert "function togglePreviews()" in html
    assert "/api/config/previews" in html and "/api/config/previews" in py
    assert 'set_key(str(ENV_FILE), "PREVIEWS"' in py
    assert '"--no-previews"' in py


def test_waiting_banner_is_a_compact_single_line():
    """35.6.6: «ждут Яндекса» — одна узкая строка под шапкой, а не блок на пол-экрана."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    start = html.index('id="waiting-banner"')
    chunk = html[start:start + 1200]
    # Подробности переехали в подсказку, в самой плашке остались цифра, сервер и кнопка.
    assert "data-tip=" in chunk
    assert "padding:6px 10px 6px 12px" in chunk
    assert 'id="waiting-banner-host"' in chunk
    assert ">Попробовать</button>" in chunk
    assert "waitingNow > 0 ? 'flex' : 'none'" in html


def test_retry_waiting_starts_both_lines():
    """Отложенные есть и на Диске, и в Безлимите — запускаем обе линии, а не одну."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function retryWaiting()")
    chunk = html[start:start + 2600]
    assert "if (unlimW > 0) targets.push" in chunk
    assert "if (diskW > 0) targets.push" in chunk
    assert "for (const t of targets)" in chunk


def test_waiting_count_includes_in_progress_files():
    """Цифра не должна прыгать: файлы батча лежат в in_progress, но всё ещё ждут."""
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    assert "status IN ('pending','in_progress')" in py
    assert "def _storage_host(" in py
    assert '"waiting_host"' in py


def test_cards_can_be_stretched_by_height():
    """Все перечисленные карточки тянутся по высоте без обёртки (не ломает сетку)."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    for card_id in ("unlim-dir-card", "token-card", "disk-dir-card",
                    "previews-card", "cloud-card", "launch-all-card",
                    "launch-manual-card"):
        assert f'id="{card_id}"' in html
    assert "opts.inline ? el : document.createElement('div')" in html
    assert "{axis: 'y', inline: true, key: id}" in html
    assert ".resizable-inline{padding-bottom:22px}" in html
    assert ".resizable-sized{overflow-y:auto;overflow-x:hidden}" in html


def test_watchdog_is_on_by_default_with_20gb():
    """Сторож включён «из коробки» с порогом 20 ГБ; выключение запоминается."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    assert 'DEFAULT_THRESHOLD_GB = 20.0' in py
    assert '"watchdog": _env_flag("WATCHDOG", True)' in py
    assert '_env_float("WATCHDOG_GB", 20.0)' in py
    assert 'watchdog.start(float(' in py
    assert '_remember_watchdog(False)' in py
    assert '<option value="20" selected>20 ГБ</option>' in html


def test_updates_are_checked_on_startup_with_a_centered_dialog():
    """Автопроверка при старте: окно по центру «Обновить / Позже», без сети — молча."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert "async function startupUpdateCheck()" in html
    assert "setTimeout(startupUpdateCheck, 2500)" in html
    start = html.index("async function startupUpdateCheck()")
    chunk = html[start:start + 1800]
    assert "askConfirm({" in chunk
    assert "ok: 'Обновить'" in chunk
    assert "cancel: 'Позже'" in chunk
    assert "catch(e){ /* нет сети — молча */ }" in chunk


def test_stop_button_is_available_in_the_quick_launch_card():
    """«Скачать всё» во время работы меняется на «Остановить»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert 'id="ctrl-all-stop"' in html
    assert "onclick=\"stopCustomDownload()\" style=\"display:none\"" in html
    assert "if (allStopBtn) allStopBtn.style.display = 'inline-flex';" in html


def test_previews_dir_can_be_chosen_and_defaults_to_drive_c():
    """Папка превью настраивается, по умолчанию — рядом с базами на диске C."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    prev = (PROJECT_DIR / "previews.py").read_text(encoding="utf-8")

    assert 'id="previews-dir-input"' in html
    assert "function savePreviewsDir()" in html
    assert "/api/config/previews_dir" in html and "/api/config/previews_dir" in py
    assert "def migrate_previews_dir(" in py
    assert '"PREVIEWS_DIR"' in py
    assert "Рекомендуем оставить на диске" in html
    assert "def preview_root()" in prev
    assert 'os.getenv("PREVIEWS_DIR")' in prev
