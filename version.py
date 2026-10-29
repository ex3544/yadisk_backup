"""Версия приложения в одном месте — для UI, диагностики и установщика."""

APP_NAME = "Yad.backup"
APP_VERSION = "35.6.3"
APP_PUBLISHER = "Sergey Pukhir (ex3544, vovapuh's son)"
# Публичный репозиторий с релизами и обновлениями.
APP_REPO = "ex3544/yadisk_backup"
APP_REPO_URL = f"https://github.com/{APP_REPO}"


def version_info() -> dict:
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "publisher": APP_PUBLISHER,
    }
