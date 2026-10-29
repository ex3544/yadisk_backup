"""C3 (уведомления и заголовок вкладки) и C4 (светлая тема, доступность)."""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def html() -> str:
    return (PROJECT_DIR / "webui_assets" / "index.html").read_text(encoding="utf-8")


# ── C3: уведомления и заголовок вкладки ─────────────────────────────────────


def test_notification_card_and_api(html):
    assert 'id="notify-card"' in html
    assert 'id="notify-btn"' in html
    assert "Notification.requestPermission()" in html
    assert "new Notification(" in html
    assert "localStorage.getItem('yad.notify')" in html
    assert "localStorage.setItem('yad.notify'" in html


def test_notifications_only_when_enabled_and_allowed(html):
    # notifyUser выходит, если уведомления выключены или не разрешены.
    body = html[html.index("function notifyUser(") : html.index("function _progressPercent(")]
    assert "_notifyPerm() !== 'granted'" in body
    assert "_notifyWanted()" in body


def test_tab_title_shows_progress(html):
    assert "function updateTabTitle(" in html
    assert "⬇ Выгрузка" in html
    assert "⏳ Подготовка" in html
    assert "updateTabTitle(isRunning, s);" in html


def test_completion_and_error_notifications(html):
    assert "function checkRunFinished(" in html
    assert "function checkRunErrors(" in html
    assert "checkRunFinished(isRunning, s);" in html
    assert "checkRunErrors(s);" in html
    assert "disk_offline" in html
    # одно и то же состояние не уведомляет повторно
    assert "window._notifyErrSig" in html


# ── C4: светлая тема ────────────────────────────────────────────────────────


def test_light_theme_variables(html):
    assert 'html[data-theme="light"]{' in html
    assert "--bg:#f2f2f7;" in html
    assert "color-scheme:light;" in html
    # встроенный скрипт применяет тему до отрисовки
    assert "localStorage.getItem('yad.theme')==='light'" in html


def test_theme_toggle_button(html):
    assert 'id="theme-btn"' in html
    assert 'aria-pressed="false"' in html
    assert "function toggleTheme(" in html
    assert "function applyTheme(" in html
    assert "localStorage.setItem('yad.theme'" in html


# ── C4: доступность ─────────────────────────────────────────────────────────


def test_tabs_are_accessible(html):
    assert 'role="tablist"' in html
    assert 'aria-selected="true"' in html
    assert 'aria-selected="false"' in html
    assert "function syncTabsA11y(" in html
    assert "role', 'tabpanel'" in html
    assert "ArrowRight" in html and "ArrowLeft" in html


def test_focus_and_reduced_motion(html):
    assert ":focus-visible{outline:2px solid var(--accent)" in html
    assert "prefers-reduced-motion:reduce" in html
