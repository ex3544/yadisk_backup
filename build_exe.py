#!/usr/bin/env python3
"""Скрипт сборки автономного бинарника YandexDiskBackup.exe под Windows."""

import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def build():
    print("=" * 60)
    print("  Сборка автономного YandexDiskBackup.exe")
    print("=" * 60)

    # Проверяем наличие PyInstaller
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[setup] PyInstaller не установлен. Устанавливаю...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller>=6.0.0"])

    spec_file = PROJECT_DIR / "yadisk_backup.spec"
    if not spec_file.exists():
        sys.exit(f"Ошибка: не найден {spec_file}")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        str(spec_file),
    ]

    print("\nЗапуск PyInstaller...")
    ret = subprocess.call(cmd, cwd=str(PROJECT_DIR))
    if ret != 0:
        sys.exit(f"Сборка завершилась с ошибкой (код {ret})")

    out_dir = PROJECT_DIR / "dist" / "YandexDiskBackup"
    out_exe = out_dir / "YandexDiskBackup.exe"
    print("\n" + "=" * 60)
    if out_exe.exists():
        size_mb = out_exe.stat().st_size / (1024 * 1024)
        total_mb = sum(
            p.stat().st_size for p in out_dir.rglob("*") if p.is_file()
        ) / (1024 * 1024)
        print(f"✅ УСПЕШНО СОБРАН: {out_exe}")
        print(f"   exe: {size_mb:.1f} МБ, вся папка: {total_mb:.1f} МБ")
    else:
        print("✅ Сборка завершена (проверьте папку dist/)")
    print("=" * 60)


if __name__ == "__main__":
    build()
