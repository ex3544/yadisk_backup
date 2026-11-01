"""Пользовательская инструкция «Быстрый старт и все функции».

Текст хранится в INSTRUCTION.md рядом с программой (в собранном exe — внутри
_internal). Отдельная отметка .guide_seen нужна, чтобы окно само открылось
только один раз — после первой установки, а не при каждом запуске.
"""

from __future__ import annotations

import time

import paths

GUIDE_FILE = paths.BUNDLE_DIR / "INSTRUCTION.md"
MISSING_TEXT = (
    "Файл инструкции не найден. Переустановите программу или откройте "
    "INSTRUCTION.md в папке программы."
)


def read_text() -> str:
    """Текст инструкции. При отсутствии файла — понятная подсказка."""
    try:
        text = GUIDE_FILE.read_text(encoding="utf-8")
    except OSError:
        return MISSING_TEXT
    return text


def seen() -> bool:
    """Открывалась ли инструкция сама после установки."""
    return paths.GUIDE_SEEN_FILE.exists()


def mark_seen() -> None:
    try:
        paths.GUIDE_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.GUIDE_SEEN_FILE.write_text(
            time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8"
        )
    except OSError:
        # Отметка не критична: в худшем случае окно откроется ещё раз.
        pass


def status() -> dict:
    """Всё, что нужно окну: текст, флаг «уже показывали» и готов ли мастер setup.

    До завершения первичной настройки инструкцию не показываем — иначе два окна
    откроются друг на друге.
    """
    text = read_text()
    return {
        "ok": text != MISSING_TEXT,
        "text": text,
        "seen": seen(),
        "setup_done": paths.SETUP_DONE_FILE.exists(),
    }
