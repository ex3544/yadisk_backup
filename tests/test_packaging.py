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

    # 35.8.5: автосортировки после скачивания больше нет ни в одном режиме —
    # раскладка по датам осталась только ручной задачей на «Организации».
    assert 'id="all-sort-after"' not in html
    assert 'id="unlim-sort-after"' not in html
    assert "Может занять много времени. Можно не включать" not in html

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

    # Переезд на другой диск: поиск файлов с предпросмотром и повторная загрузка.
    assert 'id="db-new-dir"' in diag_html
    assert "onclick=\"redownloadDb()\"" in diag_html
    assert "/api/db/relocate" in html
    assert "/api/db/redownload" in html
    assert "/api/db/restore_cloud" in html

    # Карточка «Файлы переехали?»: предпросмотр → применить, без скачивания.
    assert 'id="find-files-card"' in diag_html
    assert "onclick=\"previewFindFiles()\"" in diag_html
    assert "onclick=\"previewRestoreCloud()\"" in diag_html
    assert "preview: true" in html and "preview: false" in html
    assert "missing_keys" in html  # вернуть отсутствующие в очередь

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
    """Сторож — одна строка между «Папки выгрузки» и «Доступ к Яндексу»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    setup = _panel_html(html, "setup")

    folders_pos = setup.index('id="export-dirs-card"')
    watchdog_pos = setup.index('id="watchdog-card"')
    access_pos = setup.index('id="access-card"')
    assert folders_pos < watchdog_pos < access_pos

    # Компактная строка, а не карточка с абзацем описания.
    assert 'class="watchdog-line"' in setup
    assert "Проверяет диск раз в 30 секунд и останавливает выгрузку" not in setup
    assert "проверяет свободное место раз в 30 секунд" in setup  # это уже подсказка
    assert "#watchdog-card{padding:10px 14px}" in html
    # Две независимые колонки: сторож в левой, «Доступ к Яндексу» — в правой.
    assert 'class="tab-col" data-col="left"' in setup
    assert 'class="tab-col" data-col="right"' in setup
    assert ".tab-col{display:flex;flex-direction:column;gap:12px;min-width:0}" in html
    assert 'data-panel="setup"] .card:nth-child(' not in html


def test_settings_cards_are_merged_with_tabs():
    """35.9.2: токен/куки — одна карточка с вкладками; папки — рядом без вкладок."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    setup = _panel_html(html, "setup")

    # Старые отдельные карточки убраны, новые — на месте.
    for old in ("unlim-dir-card", "disk-dir-card", "token-card", "cookies-card"):
        assert f'id="{old}"' not in setup, old
    assert 'id="export-dirs-card"' in setup
    assert 'id="access-card"' in setup

    # Поля не задвоились при переносе.
    for field_id in ("disk-dir-input", "unlim-dir-input", "token-input", "cookies-input"):
        assert setup.count(f'id="{field_id}"') == 1, field_id

    # Вкладки остались только у карточки доступа; папки видны обе сразу.
    for tab in ('data-ctab="token"', 'data-ctab="cookies"'):
        assert tab in setup
    assert 'data-ctab="disk"' not in setup
    assert 'data-ctab="unlim"' not in setup
    assert setup.count('role="tab"') >= 2
    assert "function switchCardTab(cardId, name){" in html
    assert "card.querySelectorAll('.ctab-panel')" in html
    assert ".ctab-panel.active{display:block" in html

    # Плашки ведут на нужную вкладку, иначе поле окажется в скрытой панели.
    assert "card:'access-card', tab:'token'" in html
    assert "card:'access-card', tab:'cookies'" in html
    assert "card:'export-dirs-card'" not in html


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
    assert "JSON.stringify({ workers: workers, passes: passes })" in html
    assert "sort_after" not in html


def test_exit_code_one_is_not_treated_as_user_stop():
    """main.py возвращает 1 при сбойных файлах — это не «остановлено»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    marker = html.index("function isStopped(code){")
    body = html[marker : html.index("}", marker) + 1]
    import re

    codes = [int(n) for n in re.findall(r"code === (-?\d+)", body)]
    assert codes == [-15, -2, 143, 130, 15]


def test_console_follows_bottom_without_jumping():
    """35.9.2: новые строки внизу, автопрокрутка не срывает чтение старых."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'id="pipeline-console" tabindex="0"' in html
    assert "const CONSOLE_STICK_PX = 24;" in html
    assert "function consoleAtBottom(el)" in html
    assert "_consoleStick[id] = consoleAtBottom(el);" in html

    marker = html.index("function renderConsole(id, lines, placeholder){")
    body = html[marker : html.index("\n}\n", marker)]
    assert "const stick = _consoleStick[id] !== false;" in body
    assert "const drop = prev.length - keep;" in body
    assert "prev[prev.length - k + i] !== lines[i]" in body
    assert "el.appendChild(frag)" in body
    assert "el.replaceChildren(frag)" in body
    # Изменился только хвост — начало журнала не пересобираем, иначе слетает
    # выделение текста мышью.
    assert "let same = 0;" in body
    assert "while(el.children.length > same) el.lastElementChild.remove();" in body
    assert "if(stick) el.scrollTop = el.scrollHeight;" in body
    # Перевёрнутого журнала больше нет: старые строки сверху.
    assert "reverse().join('\\n')" not in body
    assert "el.scrollTop = 0;" not in body

    # Строки раскрашиваются по типу операции.
    for cls in ("function consoleLineClass(line)",
                ".console-line.err{", ".console-line.warn{",
                ".console-line.ok{", ".console-line.run{"):
        assert cls in html, cls


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
    assert '<symbol id="i-shield"' in html
    assert '<use href="#i-shield"/>' in html
    assert "pillText.textContent = `${w.threshold_gb} ГБ`" in html
    assert "pillText.textContent = 'выкл'" in html


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


def test_updates_open_from_the_header_button():
    """35.9.2: «Обновления» — кнопка в шапке, прогресс — в окне «Что нового»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    # Карточки «Обновления» в «Диагностике» больше нет.
    assert 'id="update-card"' not in html
    assert 'id="update-check-btn"' not in html

    # Кнопка стоит рядом с «Инструкцией» и «Написать» и открывает то же окно.
    for marker in ('id="updates-btn"', 'onclick="openUpdateFromPill()"',
                   'id="update-modal"', 'id="update-modal-progress"',
                   'id="update-modal-bar"', 'id="update-modal-status"',
                   'id="update-modal-action"', 'id="update-modal-close"'):
        assert marker in html, marker

    # Живой прогресс рисуется в окне, даже когда окно свернули (потом видно).
    assert "renderUpdate(s.update);" in html
    assert "setUpdateModalState('downloading', u)" in html
    assert "setUpdateModalState('ready', u)" in html
    assert "setUpdateModalState('error', u)" in html
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


def test_scans_and_beast_strategy_changes_do_not_reset_pills():
    """С 35.8.9 сессию ведут только выгрузки, а «Зверь» копит итог по линиям."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    webui_src = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")

    assert "def task_kind(" in webui_src
    assert 'str(task.get("kind") or "download") == "download"' in webui_src
    assert '"gained_by_kind"' in webui_src
    assert '"beast": True' in webui_src
    # Подсказка плашки во время «Зверя» — про круги, а не проходы.
    assert "Круг ${d.round || 1}" in html
    # Имена качающихся файлов: лог «Зверя» + хвосты линий.
    assert "const beastLines" in html


def test_service_pills_look_different_from_download_totals():
    """Токен, куки и сторож отделены от зелёных итоговых плашек."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'class="pill service clickable" id="pill-token"' in html
    assert 'class="pill service clickable" id="pill-cookies"' in html
    # С 35.7.4 плашки идут в два ряда: постоянные сверху, живые вторым рядом.
    # Разделитель в общей строке больше не нужен — ряд сам отделяет группы.
    assert 'class="pills pills-main" id="pills-main"' in html
    assert 'class="pills pills-live" id="pills-live"' in html
    assert ".pills-live:empty{display:none}" in html
    assert 'class="pills-sep"' not in html
    assert ".pill.service{" in html
    assert ".pill.info{" in html
    # Сторож включён — тот же овал без заливки и рамки: состояние показывают
    # только щит и цвет текста, поэтому плашка не выглядит выше соседних.
    # Меняем только классы состояния, иначе слетает clickable.
    assert "pill.classList.toggle('on', active);" in html
    assert "pill.classList.toggle('err', triggered);" in html
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
    """35.9.1: главная — две колонки карточек, обе заполнены, пустой половины нет."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    home = _panel_html(html, "home")

    assert 'class="tab-grid" data-tab-grid="home"' in home
    assert ".tab-grid{display:grid;grid-template-columns:1fr 1.15fr;gap:16px;align-items:start}" in html
    left = home[home.index('class="tab-col" data-col="left"'):home.index('class="tab-col home-stats" data-col="right"')]
    right = home[home.index('class="tab-col home-stats" data-col="right"'):]
    assert 'id="launch-all-card"' in left
    assert 'id="home-unlim-card"' in right
    assert 'id="pipeline-card"' in right
    # Две плашки итогов снова стоят рядом, журнал — под ними во всю ширину.
    assert ".tab-col.home-stats{display:grid;grid-template-columns:1fr 1fr;align-content:start}" in html
    assert ".tab-col.home-stats > #pipeline-card{grid-column:1 / -1}" in html


def test_home_title_lives_inside_the_quick_launch_card():
    """35.9.2: заголовок переезжает вместе с карточкой быстрого запуска."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    home = _panel_html(html, "home")
    card = home[home.index('id="launch-all-card"'):]
    card = card[:card.index('id="launch-manual-card"')]

    assert "Панель управления выгрузкой" in card
    assert "Прямое скоростное скачивание оригиналов без прокси (Sergey Pukhir)" in card
    # Отдельного заголовка над карточкой больше нет.
    assert home.index('class="launch-head"') > home.index('id="launch-all-card"')


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
    assert "function gotoSetting(panel, selector, cardId, tab)" in html
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


def test_eta_plate_removed_from_home_page():
    """35.9.3: плашки с шагом, временем и ETA убраны — остался только журнал."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    for gone in ('id="eta-box"', 'id="eta-text"', "function etaSeconds(",
                 "function renderEta(", "function fmtEta(", "#eta-box{",
                 "const ETA_WINDOW = 180;"):
        assert gone not in html, gone
    # Остаток по-прежнему считает сервер: UI не выдумывает, сколько файлов впереди.
    assert "session[\"remaining\"]" in (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")


def test_pills_show_remaining_queue_and_report_is_collapsible():
    """Плашки показывают остаток очереди, результат отчёта сворачивается."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")

    # Плашка видна всегда: в покое — сколько осталось докачать (файлы и объём).
    assert "function queueLeft(db, kind)" in html
    assert "if(!d){" in html
    # Нет сессии — в самой плашке «—», а остаток докачки в подсказке.
    assert "const leftText = left.bytes > 0" in html
    assert "Осталось докачать: ${leftText}." in html
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


def test_update_modal_button_is_one_shot():
    """После скачивания окно предлагает только «Закрыть и обновить»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "function setUpdateModalState(mode, u){" in html
    # Во время скачивания кнопки «Скачать» нет, после — одна кнопка установки.
    assert "action.style.display = 'none';" in html
    assert "action.textContent = 'Закрыть и обновить';" in html
    assert "action.style.display = u.can_install === false ? 'none' : '';" in html
    # В ошибке кнопка возвращается — иначе обновление не повторить.
    assert "action.textContent = 'Скачать ещё раз';" in html


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

    assert html.count("Проверить папку") >= 2  # два шага мастера
    assert html.count("Проверить и сохранить") >= 2  # две карточки папок
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
    """35.6.7: плашка «ждут Яндекса» — узкая строка внутри карточки быстрого запуска."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    start = html.index('id="waiting-banner"')
    chunk = html[start:start + 1200]
    # Подробности переехали в подсказку, в самой плашке остались цифра, сервер и кнопка.
    assert "data-tip=" in chunk
    assert "padding:4px 8px 4px 10px" in chunk
    assert 'id="waiting-banner-host"' in chunk
    assert ">Попробовать</button>" in chunk
    assert "(waitingNow + lostNow) > 0 ? 'flex' : 'none'" in html
    # Отдельного блока под шапкой больше нет: плашка внутри карточки запуска.
    card = html.index('id="launch-all-card"')
    assert card < start < html.index('id="launch-manual-card"')
    assert html.count('id="waiting-banner"') == 1


def test_retry_waiting_starts_both_lines():
    """Отложенные есть и на Диске, и в Безлимите — запускаем обе линии, а не одну."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function retryWaiting()")
    chunk = html[start:start + 2600]
    assert "if (unlimW + unlimL > 0) targets.push" in chunk
    assert "if (diskW + diskL > 0) targets.push" in chunk
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
    for card_id in ("export-dirs-card", "access-card",
                    "previews-card", "picker-card", "launch-all-card",
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


def test_updates_are_checked_on_startup_and_shown_as_a_header_pill():
    """35.8.2: при старте окно само не всплывает — о версии говорит плашка."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert "async function startupUpdateCheck()" in html
    assert "setTimeout(startupUpdateCheck, 2500)" in html
    start = html.index("async function startupUpdateCheck()")
    chunk = html[start:html.index("// ── Общий вывод текста", start)]
    assert "renderUpdatePill(d);" in chunk
    # Автовсплытия больше нет: окно открывается только кликом по плашке.
    assert "showUpdateModal" not in chunk
    assert "catch(e){ /* нет сети — молча */ }" in chunk


def test_update_pill_stays_until_the_user_updates():
    """Плашка в шапке: скрыта без обновления, показывает версию, клик — окно."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert 'id="update-pill"' in html
    assert 'id="update-pill-text"' in html
    assert "function renderUpdatePill(info)" in html
    assert "async function openUpdateFromPill()" in html
    start = html.index("function renderUpdatePill(info)")
    chunk = html[start:start + 900]
    assert "info.update_available && latest" in chunk   # без обновления скрыта
    assert "textContent = `Доступна ${latest}`" in chunk
    # Крестика нет: плашку нельзя закрыть, не обновившись.
    assert "update-pill-close" not in html
    # Плашка живёт в шапке рядом с остальными постоянными плашками.
    assert html.index('id="update-pill"') > html.index('id="pills-main"')
    assert "renderUpdatePill(s.update_check)" in html


def test_update_window_shows_what_is_new_and_stays_during_download():
    """35.7.1: окно «Что нового в версии X» с кнопками и справкой."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    for marker in ('id="update-modal"', 'id="update-modal-title"',
                   'id="update-modal-action"', 'id="update-modal-close"',
                   'id="update-notes"'):
        assert marker in html
    assert "function showUpdateModal(" in html
    assert "function closeUpdateModal(" in html
    assert "function _notesText(" in html
    assert "Закрыть и обновить" in html
    assert "notes.style.display = 'block'" in html
    # Справка — из описания релиза на GitHub, без исполнения HTML.
    assert "d.notes" in html
    close_start = html.index("function closeUpdateModal(")
    close_chunk = html[close_start:close_start + 400]
    assert "cancelUpdateDownload" not in close_chunk


def test_profiles_card_lists_and_switches_users():
    """35.7.1: профили клиентов — карточка, пилюля, создание и переключение."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    for marker in ('id="profiles-card"', 'id="profiles-list"',
                   'id="pill-profile"', 'id="profile-modal"',
                   'id="profile-name"', 'id="profile-disk-dir"',
                   'id="profile-unlim-dir"'):
        assert marker in html
    assert "async function loadProfiles(" in html
    assert "async function switchProfile(" in html
    assert "async function deleteProfile(" in html
    assert "function openProfileCreate(" in html
    assert "async function submitProfileModal(" in html
    assert "'pill-profile':      {panel:'setup', selector:'#profiles-card'}" in html
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    assert '@app.get("/api/profiles")' in py
    assert '@app.post("/api/profiles/switch")' in py
    assert '@app.post("/api/profiles/avatar")' in py
    assert "def write_profile_restart_helper(" in py


def test_profile_avatars_live_in_the_profile_pill_and_under_a_spoiler():
    """35.8.2: аватар внутри плашки профиля, выбор аватарок — под катом."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    # Отдельной кнопки слева от темы больше нет.
    assert 'id="avatar-btn"' not in html
    assert "avatar-btn" not in html
    # Аватар — внутри плашки профиля, рядом с точкой и именем.
    pill_start = html.index('id="pill-profile"')
    pill_end = html.index("</span></span>", pill_start)
    pill = html[pill_start:pill_end]
    assert 'id="avatar-use"' in pill
    assert 'class="pill-profile-avatar"' in pill
    assert 'id="pill-profile-text"' in pill
    # Имя без лишней фигурки: аватар + точка + имя.
    assert "pn.textContent = '👤 " not in html
    # Выбор аватарки свёрнут в раскрывающийся блок.
    assert '<details class="info" id="avatar-details"' in html
    details = html[html.index('id="avatar-details"'):]
    assert 'id="avatar-grid"' in details[:700]
    assert "Сменить аватарку" in details[:200]
    assert "function applyAvatar(" in html
    assert "function renderAvatarPicker(" in html

    profiles_src = (PROJECT_DIR / "profiles.py").read_text(encoding="utf-8")
    import profiles as profiles_mod
    assert len(profiles_mod.AVATARS) == 41
    for avatar in profiles_mod.AVATARS:
        assert avatar in profiles_src
        assert f'<symbol id="{avatar}"' in html, avatar
    # 35.8.3: спорт, музыка, красная «Я» и шуточный олень.
    for avatar in ("av-volleyball", "av-football", "av-basketball",
                   "av-snowboard", "av-skis", "av-tennis",
                   "av-microphone", "av-note", "av-drum",
                   "av-letter-ya", "av-jager"):
        assert avatar in profiles_mod.AVATARS


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
    # 35.8.3: превью можно посмотреть — кнопка открывает папку в проводнике.
    assert "function openPreviewsFolder()" in html
    assert "/api/previews/open" in html and "/api/previews/open" in py
    start = html.index('id="previews-card"')
    assert "Открыть папку" in html[start:start + 1500]


def test_stop_button_text_is_visible_on_red():
    """35.6.8: `button.danger` перебивал белый текст `.big-btn` — «Стоп» был
    красным по красному, и надписи не было видно во время работы."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    start = html.index(".big-btn.danger{")
    chunk = html[start:html.index("\n", start)]
    assert "color:#fff" in chunk


def test_queue_estimate_shows_left_and_free_space():
    """35.6.8: до запуска видно, что очередь не помещается на диск."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert 'id="queue-estimate"' in html
    assert "function renderQueueEstimate(" in html
    assert "renderQueueEstimate(s);" in html
    assert "не поместится" in html


def test_queue_estimate_does_not_invent_unlim_bytes():
    """35.8.7: у Безлимита вес неизвестен — «≈ 0,8 ГБ» по среднему фото врало."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    start = html.index("function renderQueueEstimate(")
    body = html[start:start + 2400]
    assert "uBytes" not in body
    assert "_pluralFiles(uLeft)" in body


def test_notifications_use_the_tray_not_the_browser():
    """35.6.8: в WebView2 браузерные Notification запрещены навсегда —
    «Включить» ничего не могла сделать. Показываем системный балун из трея."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert "function _notifyChannel(" in html
    assert "/api/notify" in html
    assert "s.notify_native" in html
    # Старой ветки «запрещены в настройках окна» быть не должно.
    assert "запрещены в настройках окна" not in html
    launcher = (PROJECT_DIR / "launcher.py").read_text(encoding="utf-8")
    assert "webui.set_notifier(" in launcher


def test_5xx_never_marks_a_storage_host_dead():
    """35.6.8: ответ 500 не гасит сервер хранения — ни после какого числа.

    В 35.6.7 после десяти 500 подряд хост выключался на 30 минут, и у тестера
    из 1035 файлов скачалось три.
    """
    for name in ("fetch.py", "downloader.py", "unlim_backup.py"):
        src = (PROJECT_DIR / name).read_text(encoding="utf-8")
        assert "note_reply_error" not in src, name


def test_console_follows_theme():
    """35.6.7: журнал не должен оставаться чёрным в светлой теме."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert "--console-bg:#000000" in html
    assert "--console-bg:#fbfbfd" in html
    start = html.index("\n  .console{")
    chunk = html[start:start + 300]
    assert "background:var(--console-bg)" in chunk
    assert "color:var(--console-text)" in chunk
    # Литералов в самом блоке быть не должно — из-за них окно и чернело.
    assert "background:#000" not in chunk
    # Рамка второго окошка журнала — тоже из переменной темы.
    assert ".pipe-log{" in html and "background:var(--console-bg);" in html


def test_start_button_text_is_visible_in_light_theme():
    """35.6.9: на светлой теме `#fff` от `.big-btn` не перебивается у
    неактивной кнопки — «Старт» белел на почти белом `--surface2`."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    start = html.index(".big-btn:disabled{")
    chunk = html[start:html.index("\n", start)]
    assert "color:var(--text-tertiary)" in chunk
    # «Призрачные» кнопки («Скан», «Отчёт») берут цвет текста темы.
    ghost = html[html.index(".big-btn.ghost{"):]
    ghost = ghost[:ghost.index("\n")]
    assert "color:var(--text)" in ghost


def test_home_stats_sit_above_the_log_in_header_order():
    """35.9.1: плашки итогов — в правой колонке, над журналом, Безлимит первым."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    home = _panel_html(html, "home")
    right = home[home.index('class="tab-col home-stats" data-col="right"'):]

    stats = right.index('id="home-unlim-card"')
    log = right.index('id="pipeline-card"')
    assert stats < log, "плашки должны идти до окна «Выполнение процесса»"
    assert right.index('id="home-disk-card"') < log
    chunk = right[stats:log]
    assert chunk.index('id="home-unlim-done"') < chunk.index('id="home-disk-done"')
    assert "photoslice" not in html
    # Плашки и журнал — одна правая колонка: журнал вплотную под плашками.
    # Старая сетка с отдельным .home-right больше не используется.
    assert ".home-right{" not in html
    assert "grid-row:1 / span 2" not in html
    assert '"grid-row:2"' not in html


def test_missing_files_line_is_calm_not_alarming():
    """35.7.0: строка «не получено» — справка, а не красный сигнал."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert (".stat-left{font-size:11px;margin-top:3px;font-weight:400;"
            "color:var(--text-tertiary)}") in html
    assert "⚠ не получено" not in html


def test_plates_show_how_many_files_are_still_missing():
    """35.6.9: «не получено» — включая сбойные, сгоревшие и ждущие Яндекса."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert 'id="home-unlim-left"' in html
    assert 'id="home-disk-left"' in html
    assert "function renderLeftLine(" in html
    assert "function _pluralFiles(" in html
    assert "не получено " in html
    # У Диска `skipped` — уже скачанные файлы, их в остаток не берём.
    disk_start = html.index("const diskLeft =")
    disk_chunk = html[disk_start:disk_start + 300]
    assert "'skipped'" not in disk_chunk
    unlim_start = html.index("const unlimLeft =")
    unlim_chunk = html[unlim_start:unlim_start + 300]
    assert "'skipped'" in unlim_chunk


def test_batch_line_shows_files_waiting_for_yandex():
    """35.6.9: «✓ 0» без счётчика ждущих выглядел как простой программы."""
    src = (PROJECT_DIR / "unlim_backup.py").read_text(encoding="utf-8")
    assert "🕓 {batch_deferred} ждут" in src
    assert "Ждут Яндекса: {batch_deferred}" in src


def test_report_server_address_is_editable_in_the_window():
    """35.6.9: адрес сервера отчётов меняется из «Диагностики» — у тестера
    провайдер блокирует штатный домен."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert 'id="diag-report-url"' in html
    assert "function saveReportUrl(" in html
    assert "function pingReportServer(" in html
    assert "/api/diagnostics/ping" in html
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    assert '@app.get("/api/diagnostics/url")' in py
    assert '@app.post("/api/diagnostics/url")' in py
    assert '@app.get("/api/diagnostics/ping")' in py
    diag = (PROJECT_DIR / "diagnostics.py").read_text(encoding="utf-8")
    assert "def save_report_url(" in diag
    assert "def ping_report_server(" in diag


def test_cloud_picker_lets_choose_panels_and_files():
    """35.7.2: проводник облака — папки и отдельные файлы с галочками."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    for marker in ('id="picker-card"', 'id="picker-list"', 'id="picker-path"',
                   'id="picker-up"', 'id="picker-summary"', 'id="picker-status"'):
        assert marker in html
    assert "async function pickerOpen(" in html
    assert "function pickerToggle(" in html
    assert "async function pickerDownload(" in html
    assert "async function pickerClear(" in html
    assert "/api/cloud/list" in html
    assert "/api/disk/download_selection" in html
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    assert '@app.get("/api/cloud/list")' in py
    assert '@app.post("/api/disk/download_selection")' in py
    main_py = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")
    assert '"--selection"' in main_py


def test_downloaded_files_cannot_be_selected_again():
    """Уже скачанное не отмечается: в работу попадают только нескачанные."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert "item.status === 'downloaded' || item.status === 'skipped'" in html
    assert "уже скачан" in html
    # В задания выбор берёт только незавершённые статусы.
    state_py = (PROJECT_DIR / "state.py").read_text(encoding="utf-8")
    assert "def get_files_under(" in state_py
    assert '("pending", "failed", "lost")' in state_py


def test_selection_does_not_change_queue_statuses():
    """Выбор — фильтр при запуске, а не пометка в базе."""
    src = (PROJECT_DIR / "selection.py").read_text(encoding="utf-8")
    assert "def build_disk_jobs(" in src
    # Задания отдаются наружу: сама функция ничего не запускает и не пишет в базу.
    assert "state.mark_downloaded" not in src
    assert "reset" not in src          # никаких сбросов статусов
    assert "UPDATE files" not in src


def test_second_copy_of_the_program_cannot_start():
    """35.7.2: одна копия программы на пользователя."""
    launcher = (PROJECT_DIR / "launcher.py").read_text(encoding="utf-8")
    assert "single_instance.acquire(" in launcher
    assert "already_running_message()" in launcher
    guard = (PROJECT_DIR / "single_instance.py").read_text(encoding="utf-8")
    assert "CreateMutexW" in guard
    assert "ErrorActionPreference" not in guard
    # Блокировка не зависит от профиля: имя мьютекса постоянное.
    assert r'MUTEX_NAME = "Local\\YadBackup-single"' in guard


def test_profile_switch_waits_then_forces_only_if_stuck():
    """Переключение профиля: мягкий выход, принудительно — только если завис."""
    webui_py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    start = webui_py.index("def write_profile_restart_helper(")
    chunk = webui_py[start:start + 2200]
    assert "WaitForExit(10000)" in chunk
    assert "Stop-Process" in chunk
    assert chunk.index("WaitForExit(10000)") < chunk.index("Stop-Process")


def test_database_snapshots_are_merged_into_backups():
    """35.8.0: «снимки» и «резервные копии» — одно понятие и одна карточка."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    # Отдельной карточки снимков больше нет.
    assert 'id="snapshots-card"' not in html
    assert "function makeSnapshot(" not in html
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    assert '@app.post("/api/db/snapshot")' not in py
    assert '@app.get("/api/db/snapshots")' not in py
    assert "dbsnapshots" not in py
    # Старые снимки переносятся в backups/ при запуске и при открытии списка.
    assert "backups.migrate_snapshots(paths.DATA_DIR)" in py
    mod = (PROJECT_DIR / "backups.py").read_text(encoding="utf-8")
    assert "src_con.backup(dst_con)" in mod   # согласованная копия, а не copy файла
    assert "def migrate_snapshots(" in mod
    assert "AUTO_KEEP = 5" in mod
    assert "before-sort" in mod and "before-rebuild" in mod

    organize = _panel_html(html, "organize")
    assert "Автоматических копий" in organize
    assert 'id="backups-keep"' in organize
    assert "b.reason_title" in html


def test_unlim_calendar_card_is_in_the_window():
    """35.7.3: календарь дат Безлимита — клетки дней, сводка и кнопки."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    for marker in ('id="unlim-dates-card"', 'id="udates-grid"',
                   'id="udates-month"', 'id="udates-summary"', 'id="udates-status"',
                   'id="udates-prev"', 'id="udates-next"'):
        assert marker in html
    assert "function loadUnlimCalendar(" in html
    assert "function udatesDownload(" in html
    assert "function udatesSave(" in html
    assert "async function udatesClear(" in html
    # Пустые дни и дни, где всё скачано, отметить нельзя.
    assert "btn.disabled = !info || (done && !selected);" in html
    assert "'✓ всё'" in html
    # Календарь подгружается при открытии окна.
    assert "loadUnlimCalendar();" in html


def test_unlim_selection_routes_exist():
    """35.7.3: роуты календаря, выбора дат и запуска по выбору."""
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    assert '@app.get("/api/unlim/calendar")' in py
    assert '@app.get("/api/selection/unlim")' in py
    assert '@app.post("/api/selection/unlim")' in py
    assert '@app.post("/api/selection/unlim/clear")' in py
    assert '@app.post("/api/unlim/download_selection")' in py


def test_all_three_unlim_loaders_accept_selection():
    """35.7.3: каждый загрузчик умеет «только выбранные дни»."""
    for name in ("unlim_backup.py", "unlim_worker.py", "unlim_direct.py"):
        src = (PROJECT_DIR / name).read_text(encoding="utf-8")
        assert "cluster_ranges" in src, name
        assert '"--selection"' in src, name
        assert "unlim_dates.range_clause(" in src, name


def test_unlim_dates_filter_is_a_whitelist_not_a_status_switch():
    """35.7.3: пустой выбор ничего не качает, статусы базы не меняются."""
    src = (PROJECT_DIR / "unlim_dates.py").read_text(encoding="utf-8")
    assert '"1 = 0"' in src
    assert "@app" not in src
    assert "UPDATE " not in src.upper()
    # Основной загрузчик только сбрасывает skipped внутри выбранных дней.
    backup = (PROJECT_DIR / "unlim_backup.py").read_text(encoding="utf-8")
    start = backup.index("def reset_skipped_files(")
    chunk = backup[start:start + 1600]
    assert "cluster_ranges" in chunk


def test_unlim_schema_has_cluster_index():
    """35.7.3: индекс по cluster_id делает фильтр по дням быстрым."""
    schema = (PROJECT_DIR / "unlim_schema.py").read_text(encoding="utf-8")
    assert "idx_unlim_cluster" in schema
    assert "(6, _migration_6)" in schema


# ── 35.7.4: итог выполнения в панели, вкладки-кнопки, два ряда плашек ───────


def test_run_summary_plate_removed_from_home_page():
    """35.9.3: отдельной плашки «итог запуска» больше нет.

    Итоги читаются прямо в верхнем окне «Выполнение процесса»: строки
    «ИТОГО В ЭТОМ ЗАПУСКЕ» приходят из журнала воркера.
    """
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    for gone in ('id="run-summary"', "function renderRunSummary(",
                 "function runNumbers(", "function _runItem("):
        assert gone not in html, gone


def test_selection_shows_custom_total_and_no_db_ready_badge():
    """«Выбрано N, скачано X из N» и никакой «базы готово» у выгрузки по выбору."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "function renderPickerResult(s)" in html
    assert "Выгрузка идёт: скачано" in html
    assert "Готово: выбрано" in html
    # Бейдж базы — только у сканирования, у выбора — «выгрузка завершена».
    assert "isScanTask ? 'база готова'" in html
    assert ": isSelection ? 'выгрузка завершена' : 'скачивание завершено'" in html


def test_tabs_are_buttons_with_icons():
    """Вкладки видно с первого взгляда: кнопки с иконками, активная подсвечена."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "<button class=\"tab active\" data-tab=\"home\"" in html
    assert '<svg class="ico-sm"><use href="#i-cloud"/></svg>Главная' in html
    assert ".tab.active{" in html
    assert ".tab.active{" in html
    assert "background:linear-gradient(135deg,rgba(10,132,255,.22)" in html


def test_relocate_sorting_and_lost_queue_are_visible_in_ui():
    """35.7.5: «Файлы переехали?», отчёт сортировки и кнопка «Ждут Яндекса»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    diag = _panel_html(html, "diagnostics")
    organize = _panel_html(html, "organize")

    # Сброс очереди: главная кнопка — «Отложенные сервером» (35.8.5).
    assert "onclick=\"resetStatus('lost')\"" in diag
    assert 'id="reset-lost"' in diag
    assert "Отложенные сервером" in diag
    assert "lost > 0" in html

    # Файлы переехали: предпросмотр, применение, возврат структуры облака.
    for fn in ("previewFindFiles()", "applyFindFiles()", "previewRestoreCloud()",
               "applyRestoreCloud()", "requeueMissingFromPlan()", "findScopeChanged()"):
        assert fn in html or fn in diag

    # Проверка файлов: отключённый диск отдельно, отмеченные строки — в очередь.
    assert 'id="verify-requeue-btn"' in diag
    assert "requeueVerifySelected()" in html
    assert "requeueVerifyItems(" in html
    assert 'id="verify-problems-list"' in diag
    assert "v.offline" in html

    # Сортировка: предупреждение, обновление базы и панель отчёта.
    assert 'id="organize-warn"' in organize
    assert 'id="organize-update-db"' in organize
    assert "update_db:updateDb" in html
    assert "confirmed:!isUnlim" in html
    assert 'id="organize-report-panel"' in organize
    assert "/api/organize/report" in html
    assert "renderOrganizeDefaults" in html


def test_cloud_audit_card_sits_between_verify_and_relocate():
    """35.7.6: «Сверка с облаком» — в «Диагностике», после проверки файлов."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    diag = _panel_html(html, "diagnostics")

    assert 'id="cloud-audit-card"' in diag
    assert (diag.index('id="verify-card"')
            < diag.index('id="cloud-audit-card"')
            < diag.index('id="find-files-card"'))

    # Выбор баз за один проход и обе кнопки действий.
    for option in ('value="both"', 'value="disk"', 'value="unlim"'):
        assert option in diag
    # 35.9.0: одна переключающаяся кнопка вместо пары старт/стоп.
    assert 'id="audit-stop-btn"' not in diag
    assert 'id="audit-start-btn" onclick="toggleCloudAudit()"' in diag
    for fn in ("toggleCloudAudit()", "auditAddNew()",
               "auditMarkGone()", "toggleAuditList('same')",
               "toggleAuditList('new')", "toggleAuditList('changed')",
               "toggleAuditList('gone')", "toggleAuditReport()"):
        assert fn in diag

    # Цифры четырёх состояний, разбивка по базам и отчёт на месте.
    for ident in ("audit-same", "audit-new", "audit-changed", "audit-gone",
                  "audit-bases", "audit-list", "audit-report", "audit-actions"):
        assert f'id="{ident}"' in diag
    assert 'id="audit-bases"' in html
    assert "cloud_size_unknown" in html
    assert "сверено по наличию" in html

    # Подсказки: у карточки и у каждой кнопки — что делает и для чего нужно.
    assert 'data-tip="Проверяет, что сейчас реально лежит в облаке' in diag
    assert 'data-tip="Обходит облако онлайн и только читает список файлов' in diag
    assert 'data-tip="Новые и изменившиеся файлы встанут в очередь' in diag
    assert 'data-tip="Файлы, которые удалили из облака' in diag
    assert 'data-tip="Диск — обычные папки' in diag


def test_cloud_audit_worker_is_in_frozen_build():
    """Воркер сверки должен запускаться из собранного exe."""
    spec = (PROJECT_DIR / "yadisk_backup.spec").read_text(encoding="utf-8")
    assert "'cloud_audit'" in spec
    assert "'cloud_audit_worker'" in spec

    import webui
    assert webui.WORKER_SCRIPTS["cloud_audit_worker.py"] == "cloud_audit_worker"


def test_update_window_lists_all_skipped_versions_with_clickable_links():
    """35.7.7: пропустил три обновления — видишь описание каждого."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    assert "function renderUpdateNotes(" in html
    assert "function renderUpdateNotes(d)" in html
    assert "d.history" in html
    assert 'class="notes-version"' in html or "className = 'notes-version'" in html
    assert "function openUpdateLink(" in html
    assert "/api/updates/open" in html
    assert "Пропущено обновлений:" in html
    # Подсветка ссылок и прокрутка списка версий.
    assert ".update-notes a{" in html
    assert "max-height:42vh" in html

    updater_src = (PROJECT_DIR / "updater.py").read_text(encoding="utf-8")
    assert "def release_history(" in updater_src
    assert "RELEASES_API" in updater_src
    assert '"history": []' in updater_src


def test_every_card_has_a_resize_grip_and_hover_line_is_thin():
    """35.7.7: ручка раздвижения есть у всех карточек, подсветка — тонкая."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    cards = [
        "home-unlim-card", "home-disk-card", "launch-all-card",
        "launch-manual-card", "pipeline-card", "organize-card", "backups-card",
        "profiles-card", "export-dirs-card", "watchdog-card", "access-card",
        "previews-card", "picker-card", "diag-report-card",
        "verify-card", "failed-card", "cloud-audit-card", "reports-card",
        "reset-queue-card", "db-card", "find-files-card",
    ]
    start = html.index("['home-unlim-card'")
    resize_list = html[start:html.index("].forEach", start)]
    for card in cards:
        assert f'id="{card}"' in html, card
        assert f"'{card}'" in resize_list, card

    # Видимая линия при наведении в пять раз тоньше прежней полосы 13 px;
    # сама зона захвата осталась 13 px, поэтому тянуть по-прежнему удобно.
    assert "height:2.5px" in html
    assert ".resize-grip:hover::before{background:rgba(10,132,255,.55)}" in html
    assert ".resize-grip:hover{color:var(--accent);background:" not in html


def test_instruction_opens_once_after_install_and_has_a_header_button():
    """35.7.7: инструкция сама при первом запуске, потом — кнопка в шапке."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    for marker in ('id="guide-modal"', 'id="guide-content"', 'id="guide-btn"',
                   'onclick="openGuide()"'):
        assert marker in html
    for fn in ("function openGuide(", "function closeGuideModal(",
               "function maybeOpenGuide(", "function _renderGuide("):
        assert fn in html
    assert "setTimeout(maybeOpenGuide, 1500)" in html
    assert "/api/guide/seen" in html
    assert "📖 Инструкция" in html

    assert (PROJECT_DIR / "INSTRUCTION.md").exists()
    spec = (PROJECT_DIR / "yadisk_backup.spec").read_text(encoding="utf-8")
    assert "'guide'" in spec
    assert "('INSTRUCTION.md', '.')" in spec

    webui_src = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    assert '@app.get("/api/guide")' in webui_src
    assert '@app.post("/api/guide/seen")' in webui_src


def test_token_is_fetched_automatically_manual_input_is_secondary():
    """35.8.1: токен получается через браузер, ручной ввод — в «Дополнительно»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'id="token-fetch-area"' in html
    assert 'id="btn-token-fetch"' in html
    assert 'onclick="startTokenFetch()"' in html
    assert "function startTokenFetch(" in html
    assert "function renderTokenFetch(" in html
    assert "/api/token/fetch" in html
    assert "Дополнительно: вставить токен вручную" in html
    # Ручное поле живёт внутри свёрнутого блока «Дополнительно», а кнопка
    # автоматического получения — над ним.
    manual = html.index("Дополнительно: вставить токен вручную")
    assert html.index('id="token-input"') > manual
    assert html.index('id="token-fetch-area"') < manual
    # Плашка ведёт к автоматическому получению, а не к ручному полю.
    assert "'pill-token':        {panel:'setup', selector:'#btn-token-fetch'" in html

    spec = (PROJECT_DIR / "yadisk_backup.spec").read_text(encoding="utf-8")
    assert "'fetch_token'" in spec
    import webui
    assert webui.WORKER_SCRIPTS["fetch_token.py"] == "fetch_token"


def test_feedback_form_is_a_header_modal_not_a_diag_card():
    """35.9.0: «Написать разработчику» — кнопка в шапке и отдельное окно."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    modal_start = html.index('id="feedback-modal"')
    modal_end = html.index('id="guide-modal"')
    card = html[modal_start:modal_end]

    assert 'id="feedback-btn"' in html
    assert 'onclick="openFeedback()"' in html
    assert "function openFeedback(" in html
    assert "function closeFeedback(" in html
    diag = _panel_html(html, "diagnostics")
    assert 'id="feedback-name"' not in diag

    for marker in ('id="feedback-name"', 'id="feedback-email"',
                   'id="feedback-phone"', 'id="feedback-message"',
                   'id="feedback-consent"', 'id="feedback-attach"',
                   'id="feedback-send-btn"', 'onclick="sendFeedback()"'):
        assert marker in card, marker
    assert "function sendFeedback(" in html
    assert "/api/diagnostics/feedback" in html
    phrase = ("Имя и контакт нужны только чтобы ответить. Отдельной базы или "
              "файла с вашими данными у разработчика нет: сообщение придёт в "
              "Telegram и останется в переписке.")
    assert phrase in card

    worker = (PROJECT_DIR / "worker" / "src" / "index.js").read_text(encoding="utf-8")
    assert 'url.pathname === "/feedback"' in worker
    assert "async function feedback(" in worker
    # Сообщение без ZIP приходит как обычная HTML-форма (requests с files=None),
    # поэтому Worker разбирает и urlencoded, а не только multipart.
    assert 'contentType.includes("application/x-www-form-urlencoded")' in worker
    # Публичный маршрут стоит до проверки ключа: иначе клиент без ключа
    # не сможет написать разработчику.
    assert worker.index('url.pathname === "/feedback"') < worker.index("if (!authorized(")


def test_beast_mode_is_wired_end_to_end():
    """Кнопка «зверь»: разметка, запросы к серверу и защита от параллельных задач."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    dl = (PROJECT_DIR / "downloader.py").read_text(encoding="utf-8")
    unlim = (PROJECT_DIR / "unlim_backup.py").read_text(encoding="utf-8")
    paths_src = (PROJECT_DIR / "paths.py").read_text(encoding="utf-8")

    assert 'id="beast-btn"' in html
    assert "function toggleBeast()" in html
    assert "function renderBeast(b)" in html
    assert "renderBeast(s.beast)" in html
    assert "BEAST_TIP" in html
    assert "/api/beast/start" in html and "/api/beast/stop" in html
    assert 'app.post("/api/beast/start")' in py
    assert 'app.post("/api/beast/stop")' in py
    assert "class Beast:" in py
    # Обычные кнопки не запускают второй процесс поверх «Зверя».
    assert py.count("beast.is_running()") >= 3
    # Пока он работает, ручные кнопки заблокированы, а журнал — его.
    assert "#launch-manual-card.beast-locked button" in html
    # Журнал «Зверя» дополняется хвостами линий: иначе имя качающегося файла
    # терялось за строками тактик.
    assert "const beastLines = (b.log || []).slice();" in html
    assert "── ${title} ──" in html
    assert "log_tail" in html

    # Heartbeat: статусные файлы пишутся только по завершении файла, поэтому
    # признак «данные идут» воркеры ставят на каждом чанке.
    assert "heartbeat=_touch_heartbeat" in dl
    assert "_touch_heartbeat()" in unlim
    assert "DISK_HEARTBEAT_FILE" in paths_src
    assert "UNLIM_HEARTBEAT_FILE" in paths_src


def test_beast_button_is_red_and_says_stop_while_running():
    """35.8.7: работающий «Зверь» — ярко-красная кнопка с надписью «стоп»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    start = html.index("function renderBeast(b)")
    body = html[start:start + 1400]
    assert "btn.textContent = on ? 'стоп' : 'зверь';" in body

    css = html[html.index(".beast-btn.on{"):]
    rule = css[:css.index("}")]
    assert "#d92b3f" in rule
    assert "box-shadow" in rule


def test_last_session_is_saved_and_restored_in_ui():
    """Плашки после перезапуска берут итог прошлой сессии, а не показывают ноль."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    py = (PROJECT_DIR / "webui.py").read_text(encoding="utf-8")
    paths_src = (PROJECT_DIR / "paths.py").read_text(encoding="utf-8")

    assert "LAST_SESSION_FILE" in paths_src
    assert "def remember_sessions(" in py
    assert "def restore_sessions(" in py
    assert "session = restore_sessions(session, disk_db, unlim_db)" in py
    assert "remember_sessions(session)" in py
    # Нет сессии — прочерк и подсказка, в которой видно остаток докачки.
    assert "setText(text, '—')" in html
    assert "сессия ещё не запускалась" in html
    assert "d.restored" in html


def test_settext_helpers_keep_selection_alive():
    """Обновление статуса не переписывает DOM без изменений — выделение живёт."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "function setText(el, value)" in html
    assert "function setSafeHTML(el, html)" in html
    assert "if(el.textContent !== v) el.textContent = v;" in html
    assert "if(el._lastHTML === v) return;" in html


def test_watchdog_threshold_saves_without_enabling():
    """35.8.5: порог сторожа сохраняется даже у выключенного сторожа."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "/api/watchdog/threshold" in html
    assert "Number(st.threshold_gb) === threshold" in html
    assert "if(!st || !st.running) return;" not in html
    # Порог показывается и у выключенного сторожа.
    assert "document.activeElement !== sel" in html


def test_35_9_0_exclusions_resume_and_old_folder_ui():
    """Новые элементы 35.9.0: дерево исключений, «Продолжить», старая папка."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    # Исключения: кнопка на «Главной», модальное дерево, сохранение.
    assert 'onclick="openExclusions()"' in html
    assert 'id="exclusions-modal"' in html
    assert "function openExclusions(" in html
    assert "function saveExclusions(" in html
    assert "/api/config/exclusions" in html

    # Продолжение после заполнения/отключения диска — с проверкой на сервере.
    assert 'onclick="resumeAfterDisk(this)"' in html
    assert "function resumeAfterDisk(" in html
    assert "/api/disk/resume" in html

    # «Старая папка» уходит в запрос перепривязки.
    assert 'id="find-old-dir"' in html
    assert "old_dir: _findOldDirValue()" in html
    assert "function _findOldDirValue(" in html

    # Проводник складывается той же кнопкой.
    assert 'id="picker-open-btn" onclick="pickerToggle()"' in html
    assert "Свернуть облако" in html
    assert "function pickerSetExpanded(" in html

    # Одна кнопка сверки и колокольчик-пилюля в шапке.
    assert 'id="audit-start-btn" onclick="toggleCloudAudit()"' in html
    assert 'id="notify-btn"' in html and 'id="notify-card"' not in html

    # Обратная связь — в шапке, отдельным окном.
    assert 'id="feedback-btn"' in html
    assert 'id="feedback-modal"' in html


def test_cards_can_move_between_columns_in_all_four_tabs():
    """35.9.1: карточки перетаскиваются между колонками, раскладка запоминается."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    # В каждой из четырёх вкладок — две колонки и своя сетка.
    for panel in ("home", "organize", "setup", "diagnostics"):
        chunk = _panel_html(html, panel)
        assert f'class="tab-grid" data-tab-grid="{panel}"' in chunk
        assert 'class="tab-col" data-col="left"' in chunk
        # На «Главной» правая колонка — сетка для двух плашек итогов.
        right_cls = ("tab-col home-stats" if panel == "home" else "tab-col")
        assert f'class="{right_cls}" data-col="right"' in chunk
        assert f"resetCardLayout('{panel}')" in chunk

    # Раскладка хранится по вкладке, тянется за ручку, а не за всё окно.
    assert "const CARD_LAYOUT_PREFIX = 'ui-card-layout:v1:';" in html
    assert "function initCardLayouts()" in html
    assert "initCardLayouts();" in html
    assert "function resetCardLayout(name)" in html
    assert "pointerdown" in html and "pointermove" in html
    assert ".card-drag{" in html and ".card-drag-ghost{" in html
    assert ".card-drop-line{" in html

    # На узком окне колонки складываются в одну, ручки не мешают полям.
    assert "@media (max-width:1199px){" in html
    assert ".tab-grid{grid-template-columns:1fr}" in html


def test_folder_fields_save_themselves():
    """35.9.1: «Проверить» больше не выглядит сохранением — поле сохраняется само."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "function saveDirSetting(target, opts)" in html
    assert "function checkAndSaveDir(target, opts)" in html
    assert "function autoSaveDir(target)" in html
    assert "function _anyDownloadRunning()" in html

    # Проверка пути и сохранение — один шаг; кнопка так и называется.
    assert "/api/check_dir" in html
    assert "/api/config/dirs" in html
    assert html.count("Проверить и сохранить") >= 2
    assert html.count('onblur="autoSaveDir(') >= 2
    assert "checkAndSaveDir('disk')" in html and "checkAndSaveDir('unlim')" in html

    # Выбор папки через «Обзор…» сохраняется сразу, без второй кнопки.
    assert "if(!explicitInputId && (target === 'disk' || target === 'unlim'))" in html
    assert "checkAndSaveDir(target);" in html

    # Пока идут задачи — спрашиваем, а не меняем папку молча.
    assert "_anyDownloadRunning()" in html

    # Строка «Сейчас сохранено: …» показывает, куда реально пойдут новые файлы.
    assert 'id="disk-dir-current"' in html and 'id="unlim-dir-current"' in html
    assert "class=\"setup-hint dir-current\"" in html


def test_find_files_offers_to_switch_download_folder():
    """35.9.1: после перепривязки предлагаем переключить папку выгрузки."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'id="find-folder-warning"' in html
    assert 'id="find-folder-switch-btn"' in html
    assert 'onclick="switchDownloadDirFromFind()"' in html
    assert "function switchDownloadDirFromFind()" in html
    assert "function updateFindFolderWarning(" in html
    assert "function hideFindFolderWarning()" in html
    # Переключение — через общее сохранение папки, без отдельного .env-хака.
    assert "saveDirSetting(" in html


def test_export_folders_are_side_by_side_without_tabs():
    """35.9.2: Безлимит и Диск видны одновременно: слева Безлимит, справа Диск."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    card = html[html.index('id="export-dirs-card"'):]
    card = card[:card.index('id="watchdog-card"')]

    # Вкладок больше нет — оба пути всегда на виду.
    assert 'data-ctab="unlim"' not in card
    assert 'data-ctab="disk"' not in card
    assert 'class="dirs-grid"' in card
    assert ".dirs-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}" in html
    assert ".dir-row input{flex:1;min-width:0}" in html
    assert "@media (max-width:900px){.dirs-grid{grid-template-columns:1fr}}" in html

    # Слева — Безлимит, справа — Диск; у каждого своя кнопка сохранения.
    assert card.index('id="unlim-dir-input"') < card.index('id="disk-dir-input"')
    assert card.index("checkAndSaveDir('unlim')") < card.index("checkAndSaveDir('disk')")
    assert card.count('Проверить и сохранить') == 2


def test_drag_ghost_hides_its_own_handles():
    """35.9.2: у клона карточки при переносе не видно вторых ⠿ и полоски."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert ".card-drag-ghost .card-drag," in html
    assert ".card-drag-ghost .resize-grip{display:none}" in html


def test_cookies_pane_mirrors_token_pane():
    """35.9.2: в куках тот же порядок, что у токена: авто → сохранённые → вручную."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")
    setup = _panel_html(html, "setup")
    token = setup[setup.index('id="token-details"'):]
    cookies = setup[setup.index('id="cookie-fetch-area"'):]

    # Автоматическое получение — сверху, ручное — в раскрывающемся блоке.
    assert cookies.index('id="cookie-fetch-area"') < cookies.index('id="saved-cookies-card"')
    assert cookies.index('id="saved-cookies-card"') < cookies.index('id="cookies-details"')
    assert cookies.index('id="cookies-details"') < cookies.index('id="cookies-input"')
    assert token.index('id="token-details"') < token.index('id="token-input"')

    # Те же элементы управления, что у токена.
    assert 'id="btn-cookie-fetch"' in cookies
    assert 'onclick="testCookies()"' in cookies
    assert 'onclick="saveCookies()"' in cookies
    assert 'id="check-cookies"' in cookies
    assert 'onclick="testToken()"' in token
    assert 'id="check-token"' in token


def test_pipeline_two_terminals_process_and_files():
    """35.9.3: сверху «Выполнение процесса», снизу «Журнал скачивания»."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "#pipeline-card{display:flex;flex-direction:column;gap:12px}" in html
    assert ".pipe-log .console{" in html
    # Верх — вывод процесса, низ — файлы; низ забирает оставшуюся высоту.
    assert 'id="pipeline-process-pane"' in html
    assert 'id="pipeline-files-pane"' in html
    assert "Выполнение процесса" in html
    assert "Журнал скачивания" in html
    assert "#pipeline-process-pane{flex:0 1 auto}" in html
    assert "#pipeline-files-pane{flex:1 1 auto}" in html
    assert 'id="pipeline-console"' in html
    assert 'id="download-console"' in html
    # Строки делятся на два окна по виду: ⬇/✅/⏸/↻ — файлы, остальное — процесс.
    assert "function setPipelineConsole(text)" in html
    assert "const FILE_LINE_RE = /^(⬇|✅|⏸|↻)\\s/;" in html
    # Карточка тянется вниз почти без ограничения (с 35.9.2).
    assert "el.id === 'pipeline-card'" in html
    assert "? Math.max(minH, 5000)" in html
    # Внутренние окна своей ручкой не растягиваются.
    assert "makeResizable(document.getElementById('pipeline-console')" not in html
    # Полоска захвата короче карточки на скругления.
    assert "left:12px;right:12px;bottom:2px;height:2.5px" in html


def test_card_borders_are_thicker_and_have_shadow():
    """35.9.1: рамки карточек заметнее, у карточки мягкая тень."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert "--card-line:rgba(255,255,255,.30);" in html
    assert "--card-line:rgba(0,0,0,.28);" in html
    assert "--card-line-hover:rgba(255,255,255,.46);" in html
    assert "--card-line-hover:rgba(0,0,0,.44);" in html
    assert "border:2px solid var(--card-line);" in html
    assert "box-shadow:var(--card-shadow);" in html
    assert ".card:hover{border-color:var(--card-line-hover)}" in html


def test_failed_files_card_is_button_only_and_builds_reports():
    """35.9.2: «Что не скачалось» — только кнопки, список уходит в файлы."""
    html = (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")

    assert 'id="failed-card"' in html
    assert 'id="failed-build-btn"' in html
    assert "onclick=\"buildFailedList()\"" in html
    assert 'id="failed-summary"' in html
    assert "Открыть папку отчётов" in html

    # Встроенного окна со списком и старых вкладок больше нет.
    for old in ('id="failed-list"', 'data-ftab="disk"', 'data-ftab="unlim"'):
        assert old not in html, old
    for old_fn in ("function switchFailedTab(", "async function loadFailedList(",
                   "function renderFailedList(", "async function openFailedUrl(",
                   "async function exportFailed("):
        assert old_fn not in html, old_fn

    # Один запрос собирает TXT, CSV, XLSX и HTML разом.
    assert "fetch('/api/failed/build'" in html
    assert "function buildFailedList()" in html
