"""Одна копия программы на пользователя.

Зачем
-----
Вторая копия запускается молча: `launcher.choose_port()` подбирает свободный
порт, и оба окна работают одновременно — со своим профилем и своими задачами,
но в одних и тех же папках выгрузки. Файлы перемешиваются, базы расходятся.

Как устроено
------------
На Windows — именованный мьютекс в пространстве пользователя (`Local\\`).
Имя не зависит от профиля: иначе ограничение обходилось бы запуском с другим
`YADISK_DATA_DIR`, а нам нужна ровно одна программа на машину.

На остальных системах — lock-файл рядом с каталогом данных: `flock` не даёт
взять его второй копии, а при падении процесса ядро снимает блокировку само.

Держать блокировку нужно до конца процесса; освобождать не обязательно, но
`release()` есть для тестов.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

MUTEX_NAME = "Local\\YadBackup-single"
ERROR_ALREADY_EXISTS = 183

_handle = None      # HANDLE мьютекса Windows
_lock_fd: Optional[int] = None


def _acquire_windows(name: str) -> Optional[bool]:
    """True — мы первые, False — уже запущено, None — проверить не удалось."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (
        wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR,
    )
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        # Не смогли создать мьютекс (крайне редко): не мешаем запуску —
        # остаётся старая проверка «порт уже отвечает» в launcher.
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    global _handle
    _handle = handle
    return True


def _acquire_lockfile(lock_file: Path) -> bool:
    global _lock_fd
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows без fcntl
        return True
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass
    _lock_fd = fd
    return True


def acquire(name: str = MUTEX_NAME, lock_file: Optional[Path] = None) -> bool:
    """True — это первая копия программы. Блокировка живёт до конца процесса."""
    if os.name == "nt":
        result = _acquire_windows(name)
        if result is None:
            return True
        return result
    if lock_file is None:
        lock_file = Path(os.environ.get("YADISK_DATA_DIR", ".")) / "app.lock"
    return _acquire_lockfile(lock_file)


def release() -> None:
    """Снимает блокировку. Нужен тестам и безопасному перезапуску."""
    global _handle, _lock_fd
    if os.name == "nt" and _handle:
        import ctypes

        ctypes.WinDLL("kernel32").CloseHandle(_handle)
        _handle = None
    if _lock_fd is not None:
        try:
            import fcntl

            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        try:
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None


def already_running_message() -> str:
    return (
        "Yad.backup уже запущен.\n\n"
        "Одновременно может работать только одна копия программы: "
        "иначе две копии пишут в одни и те же файлы.\n\n"
        "Найдите окно программы или её значок в трее."
    )
