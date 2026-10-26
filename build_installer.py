#!/usr/bin/env python3
"""Сборка установщика Inno Setup из готовой onedir-сборки.

Порядок:
    python build_exe.py          # dist/YandexDiskBackup/
    python build_installer.py    # installer_output/YandexDiskBackup-35.4.4-setup.exe

Ключ отправки отчётов берётся из переменной REPORT_KEY или из локального
`.report_key.txt` и передаётся компилятору Inno Setup. В репозиторий он не
попадает и в консоль не выводится. Отключить встраивание: --no-key.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import version  # noqa: E402

APP_EXE = PROJECT_DIR / "dist" / "YandexDiskBackup" / "YandexDiskBackup.exe"
ISS_FILE = PROJECT_DIR / "installer" / "yadisk_backup.iss"
OUTPUT_DIR = PROJECT_DIR / "installer_output"
VENDOR_DIR = PROJECT_DIR / "installer" / "vendor"
WEBVIEW2_BOOTSTRAPPER = VENDOR_DIR / "MicrosoftEdgeWebView2Setup.exe"
# Официальный Evergreen Bootstrapper WebView2 (страница Microsoft Edge WebView2).
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
MAX_BOOTSTRAPPER_BYTES = 20 * 1024 * 1024
KEY_RE = re.compile(r"^[A-Za-z0-9._\-]{1,256}$")


def find_iscc() -> Path | None:
    env_path = (os.getenv("ISCC") or "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    which = shutil.which("ISCC") or shutil.which("iscc")
    if which:
        candidates.append(Path(which))
    for base in (
        os.getenv("ProgramFiles(x86)"),
        os.getenv("ProgramFiles"),
        os.getenv("LOCALAPPDATA"),
    ):
        if not base:
            continue
        candidates.append(Path(base) / "Inno Setup 6" / "ISCC.exe")
        candidates.append(Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe")
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _authenticode_status(path: Path) -> tuple[str, str]:
    """Возвращает (статус подписи, subject) через Get-AuthenticodeSignature."""
    if os.name != "nt":
        return "", ""
    quoted = str(path).replace("'", "''")
    command = (
        "$s = Get-AuthenticodeSignature -LiteralPath '"
        + quoted
        + "'; "
        + 'Write-Output ("$($s.Status)|$($s.SignerCertificate.Subject)")'
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", ""
    status, _, subject = result.stdout.strip().partition("|")
    return status.strip(), subject.strip()


def ensure_webview2_bootstrapper() -> Path:
    """Скачивает официальный WebView2 Bootstrapper, если его ещё нет.

    Файл нужен установщику, чтобы на новой машине WebView2 поставился сам.
    Подпись Microsoft проверяется перед использованием.
    """
    if WEBVIEW2_BOOTSTRAPPER.is_file() and WEBVIEW2_BOOTSTRAPPER.stat().st_size > 0:
        return WEBVIEW2_BOOTSTRAPPER

    print("Скачивание официального WebView2 Bootstrapper...")
    import requests

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with requests.get(
            WEBVIEW2_URL, stream=True, timeout=(15, 120), allow_redirects=True
        ) as response:
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(
                dir=VENDOR_DIR, prefix=".webview2-", suffix=".part", delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)
                total = 0
                for chunk in response.iter_content(65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_BOOTSTRAPPER_BYTES:
                        raise RuntimeError("WebView2 Bootstrapper слишком большой")
                    tmp.write(chunk)
        if total < 100_000:
            raise RuntimeError("WebView2 Bootstrapper скачался не полностью")

        status, subject = _authenticode_status(tmp_path)
        if status != "Valid" or "Microsoft Corporation" not in subject:
            raise RuntimeError(
                "Не удалось проверить подпись WebView2 Bootstrapper "
                f"(статус: {status or 'нет'})"
            )
        os.replace(tmp_path, WEBVIEW2_BOOTSTRAPPER)
        tmp_path = None
        print(f"WebView2 Bootstrapper готов: {WEBVIEW2_BOOTSTRAPPER}")
        return WEBVIEW2_BOOTSTRAPPER
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def read_report_key(use_key: bool) -> str:
    if not use_key:
        return ""
    key = (os.getenv("REPORT_KEY") or "").strip()
    if not key:
        key_file = PROJECT_DIR / ".report_key.txt"
        try:
            if key_file.exists():
                key = key_file.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
    if key and not KEY_RE.fullmatch(key):
        raise SystemExit(
            "REPORT_KEY содержит недопустимые символы. "
            "Разрешены буквы, цифры, точка, дефис и подчёркивание."
        )
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сборка установщика Inno Setup")
    parser.add_argument(
        "--no-key",
        action="store_true",
        help="не встраивать ключ отправки отчётов",
    )
    args = parser.parse_args(argv)

    if not APP_EXE.exists():
        print(f"Нет собранного приложения: {APP_EXE}")
        print("Сначала выполните: python build_exe.py")
        return 1
    if not ISS_FILE.exists():
        print(f"Нет файла установщика: {ISS_FILE}")
        return 1

    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup 6 не найден (ISCC.exe).")
        print("Установите Inno Setup 6: https://jrsoftware.org/isinfo.php")
        print("Либо укажите путь переменной окружения ISCC.")
        print("Скрипт установщика готов: installer/yadisk_backup.iss")
        return 2

    try:
        key = read_report_key(not args.no_key)
    except SystemExit as exc:
        print(exc)
        return 1

    try:
        ensure_webview2_bootstrapper()
    except Exception as exc:
        print(f"Не удалось подготовить WebView2 Bootstrapper: {exc}")
        print("Скачайте его вручную:")
        print("  https://developer.microsoft.com/microsoft-edge/webview2/")
        print(f"  и положите в {VENDOR_DIR}")
        return 3

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(iscc),
        f"/DAppVersion={version.APP_VERSION}",
    ]
    if key:
        cmd.append(f"/DReportKey={key}")
    cmd.append(str(ISS_FILE))
    print(f"Сборка установщика {version.APP_VERSION} ...")
    print(f"  PyInstaller: {APP_EXE}")
    print(f"  Inno Setup:  {iscc}")
    print(f"  Ключ отчётов: {'встроен' if key else 'не встроен'}")
    result = subprocess.call(cmd, cwd=str(PROJECT_DIR))
    if result != 0:
        print(f"ISCC завершился с ошибкой (код {result})")
        return result

    out = OUTPUT_DIR / f"YandexDiskBackup-{version.APP_VERSION}-setup.exe"
    if out.exists():
        size_mb = out.stat().st_size / (1024 * 1024)
        print(f"✅ Установщик готов: {out} ({size_mb:.1f} МБ)")
    else:
        print(f"✅ Сборка завершена, проверьте {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
