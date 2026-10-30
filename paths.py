"""Единая точка правды для путей: код отдельно, данные отдельно.

Зачем это нужно
---------------
В исходной версии БД, логи, `.env` и куки лежали рядом со скриптами. Для
установки в `Program Files` это не работает: папка программы только для чтения.
Здесь программа (APP_DIR) и изменяемые данные (DATA_DIR) разведены.

Правила выбора DATA_DIR
-----------------------
1. `YADISK_DATA_DIR` — если задана, используется всегда (удобно для переноса и
   тестов).
2. Запуск из исходников: если рядом со скриптами уже есть `.env`, `state.db`,
   `unlim_state.db` или `cookies.json` — остаёмся в папке проекта. Так текущая
   рабочая установка продолжает работать без переноса.
3. Иначе (и всегда в собранном exe) — `%LOCALAPPDATA%\\YandexDiskBackup`
   (Linux/macOS: `~/.local/share/yadisk-backup`).

Все остальные модули должны импортировать пути отсюда, а не собирать их из
`__file__` самостоятельно.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "YandexDiskBackup"

IS_FROZEN = bool(getattr(sys, "frozen", False))

# Каталог программы: рядом с exe в сборке, рядом со скриптами в исходниках.
APP_DIR = (
    Path(sys.executable).resolve().parent
    if IS_FROZEN
    else Path(__file__).resolve().parent
)

# Каталог ресурсов внутри бандла PyInstaller (webui_assets и т.п.).
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))

# Иконки окна, трея и установщика.
ASSETS_DIR = BUNDLE_DIR / "assets"


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local"
        )
        return Path(base) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "yadisk-backup"


def _has_legacy_data(path: Path) -> bool:
    return any(
        (path / name).exists()
        for name in (".env", "state.db", "unlim_state.db", "cookies.json")
    )


def _resolve_data_dir() -> Path:
    override = (os.environ.get("YADISK_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if not IS_FROZEN and _has_legacy_data(APP_DIR):
        return APP_DIR
    return _default_data_dir()


# ── Основные каталоги ───────────────────────────────────────────────────────

DATA_DIR = _resolve_data_dir()
# «Родной» каталог данных — место, где живут профили пользователей. Он не
# зависит от `YADISK_DATA_DIR`: при работе внутри профиля DATA_DIR указывает на
# папку профиля, а реестр профилей должен оставаться один на всех.
BASE_DATA_DIR = _default_data_dir()
LOG_DIR = DATA_DIR / "logs"
REPORT_DIR = DATA_DIR / "reports"

# ── Конфигурация и состояние ────────────────────────────────────────────────

ENV_FILE = DATA_DIR / ".env"
ENV_EXAMPLE = BUNDLE_DIR / ".env.example"
COOKIES_FILE = DATA_DIR / "cookies.json"
DB_FILE = DATA_DIR / "state.db"
UNLIM_DB_FILE = DATA_DIR / "unlim_state.db"
REPORT_KEY_FILE = DATA_DIR / ".report_key.txt"
# Ключ, который установщик может положить рядом с программой (Program Files).
# Он только читается; пользовательский ключ в DATA_DIR имеет приоритет.
BUNDLED_REPORT_KEY_FILE = APP_DIR / ".report_key.txt"
SETUP_DONE_FILE = DATA_DIR / ".setup_done"
INSTALL_ID_FILE = DATA_DIR / ".install_id"

# ── Логи и статусы задач ────────────────────────────────────────────────────

BACKUP_LOG = LOG_DIR / "backup.log"
UNLIM_LOG = LOG_DIR / "unlim_backup.log"
UNLIM_WORKER_LOG = LOG_DIR / "unlim_worker.log"
UNLIM_COLLECT_LOG = LOG_DIR / "unlim.log"
UNLIM_DIRECT_LOG = LOG_DIR / "unlim_direct.log"
DISK_STATUS_FILE = LOG_DIR / "disk_download_status.json"
UNLIM_STATUS_FILE = LOG_DIR / "download_status.json"
COOKIES_FETCH_STATUS = LOG_DIR / "cookies_fetch_status.json"
# Проверка скачанных файлов (C1): прогресс для окна и читаемый отчёт.
VERIFY_STATUS_FILE = LOG_DIR / "verify_status.json"
VERIFY_REPORT_FILE = LOG_DIR / "verify_report.md"
ACTION_LOG = LOG_DIR / "webui_actions.log"
MIGRATIONS_LOG = LOG_DIR / "migrations.log"
LAUNCHER_LOG = LOG_DIR / "launcher.log"
LAUNCHER_OUTPUT_LOG = LOG_DIR / "launcher_output.log"
DOWNLOAD_REPORT = LOG_DIR / "download_report.md"
# Последние HTTP-ошибки сервера: статус, заголовки и начало тела ответа.
# Пишется воркерами выгрузки, читается диагностическим отчётом.
HTTP_ERRORS_FILE = LOG_DIR / "http_errors.json"
# Состояние мониторинга (heartbeat и что уже отправлено), чтобы дедуп алертов
# переживал перезапуск программы.
MONITOR_STATE_FILE = DATA_DIR / ".monitor_state.json"
# Необработанные исключения — краткая запись для отчёта.
CRASH_LOG = LOG_DIR / "crash.log"

# ── Папки для скачанных файлов по умолчанию ─────────────────────────────────

def _default_backup_root() -> Path:
    """Корень для новых установок: D:\\YandexDiskBackup, иначе профиль."""
    if os.name == "nt":
        if Path("D:/").exists():
            return Path("D:/YandexDiskBackup")
        return Path(os.environ.get("USERPROFILE") or Path.home()) / "YandexDiskBackup"
    return Path.home() / "YandexDiskBackup"


DEFAULT_BACKUP_ROOT = _default_backup_root()
DEFAULT_DISK_DIR = DEFAULT_BACKUP_ROOT / "Disk"
DEFAULT_UNLIM_DIR = DEFAULT_BACKUP_ROOT / "Unlim"
# Историческое имя: старый код называл папку обычного Диска DOWNLOAD_DIR.
DEFAULT_DOWNLOAD_DIR = DEFAULT_DISK_DIR


def ensure_dirs() -> None:
    """Создаёт каталоги данных и логов (идемпотентно)."""
    for d in (DATA_DIR, LOG_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


ensure_dirs()


def describe() -> dict:
    """Сводка путей для диагностического отчёта и журналов."""
    return {
        "app_dir": str(APP_DIR),
        "bundle_dir": str(BUNDLE_DIR),
        "data_dir": str(DATA_DIR),
        "frozen": IS_FROZEN,
        "env_file": str(ENV_FILE),
        "state_db": str(DB_FILE),
        "unlim_db": str(UNLIM_DB_FILE),
        "cookies": str(COOKIES_FILE),
        "logs": str(LOG_DIR),
        "reports": str(REPORT_DIR),
    }
