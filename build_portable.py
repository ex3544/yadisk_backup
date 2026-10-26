"""Сборка переносимого ZIP из готовой onedir-сборки PyInstaller.

Запускать после `python build_exe.py`:

    python build_portable.py

Результат: dist\\YandexDiskBackup-<версия>-portable.zip.
ZIP распаковывается в папку YandexDiskBackup и запускается без установки.
Рабочие данные (базы, .env, куки, логи) в архив не попадают.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import version

PROJECT_DIR = Path(__file__).resolve().parent
DIST_DIR = PROJECT_DIR / "dist"
APP_DIR = DIST_DIR / "YandexDiskBackup"
OUT_FILE = DIST_DIR / f"YandexDiskBackup-{version.APP_VERSION}-portable.zip"

# Имена, которые не должны попасть в раздачу даже случайно.
EXCLUDED_NAMES = {
    ".env",
    "cookies.json",
    ".report_key.txt",
    ".install_id",
    ".setup_done",
}
EXCLUDED_SUFFIXES = {".db", ".db-wal", ".db-shm", ".log"}
# Лицензия и README должны ехать вместе с portable-сборкой: MIT требует
# сохранять уведомление об авторских правах в каждой копии.
EXTRA_FILES = ("LICENSE", "LICENSE.ru.txt", "README.md", "CHANGELOG.md")


def excluded(path: Path) -> bool:
    if path.name in EXCLUDED_NAMES:
        return True
    if any(path.name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
        return True
    return "__pycache__" in path.parts


def main() -> int:
    exe = APP_DIR / "YandexDiskBackup.exe"
    if not exe.exists():
        print(f"Нет сборки: {exe}\nСначала выполните: python build_exe.py")
        return 1

    files = [p for p in APP_DIR.rglob("*") if p.is_file() and not excluded(p)]
    if not files:
        print("В папке сборки нет файлов.")
        return 1

    extra_files = [PROJECT_DIR / name for name in EXTRA_FILES if (PROJECT_DIR / name).is_file()]

    OUT_FILE.unlink(missing_ok=True)
    with zipfile.ZipFile(OUT_FILE, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(files):
            arcname = Path(APP_DIR.name) / path.relative_to(APP_DIR)
            zf.write(path, arcname.as_posix())
        for path in extra_files:
            arcname = Path(APP_DIR.name) / path.name
            zf.write(path, arcname.as_posix())

    size_mb = OUT_FILE.stat().st_size / (1024 * 1024)
    print(
        f"✅ Переносимый архив готов: {OUT_FILE} "
        f"({size_mb:.1f} МБ, файлов: {len(files) + len(extra_files)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
