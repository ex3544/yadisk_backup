"""Утилиты: санитизация путей, MD5, работа с длинными путями Windows."""

from __future__ import annotations

import hashlib
import os
import sys

# Символы, запрещённые в именах файлов на Windows.
# Слеши тоже сюда: имя файла с '\' или '/' на Яндекс.Диске легально, а локально
# превратилось бы в разделитель пути и увело файл мимо DOWNLOAD_DIR.
_WIN_FORBIDDEN = '<>:"|?*\\/'
# Зарезервированные имена Windows
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

IS_WINDOWS = sys.platform.startswith("win")
# Порог, после которого включаем префикс \\?\ для длинных путей
WIN_LONG_PATH_THRESHOLD = 240


def sanitize_component(name: str) -> str:
    """Чистит ОДИН компонент пути (имя файла или папки) от запрещённых символов."""
    if not name:
        return "_"
    cleaned = []
    for ch in name:
        if ch in _WIN_FORBIDDEN or ord(ch) < 32:
            cleaned.append("_")
        else:
            cleaned.append(ch)
    out = "".join(cleaned).rstrip(" .")  # Windows не любит хвостовые пробелы и точки
    if not out or out in (".", ".."):
        out = "_"
    # Зарезервированные имена
    stem = out.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        out = "_" + out
    return out


def sanitize_remote_path(remote_path: str) -> str:
    """Превращает /disk/foo:bar/file?.jpg → foo_bar/file_.jpg (относительный путь)."""
    # Убираем префикс disk: если есть
    p = remote_path
    if p.startswith("disk:"):
        p = p[5:]
    p = p.lstrip("/")
    parts = [sanitize_component(part) for part in p.split("/") if part]
    return "/".join(parts)


def ensure_dir(path: str | os.PathLike) -> None:
    """Создаёт директорию и все родительские, если их нет."""
    os.makedirs(path, exist_ok=True)


def to_long_path(path: str) -> str:
    r"""Для Windows — добавляет префикс \\?\ если путь длинный.
    На других платформах возвращает как есть.
    """
    if not IS_WINDOWS:
        return path
    abs_path = os.path.abspath(path)
    if len(abs_path) < WIN_LONG_PATH_THRESHOLD:
        return abs_path
    if abs_path.startswith("\\\\?\\"):
        return abs_path
    # UNC-пути (\\server\share\...) требуют другой префикс
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path[2:]
    return "\\\\?\\" + abs_path


def resolve_local_path(download_dir: str, remote_path: str) -> str:
    """Собирает локальный путь для удалённого файла с санитизацией и поддержкой длинных путей."""
    rel = sanitize_remote_path(remote_path)
    local = os.path.join(download_dir, rel.replace("/", os.sep))
    return local


def avoid_case_collision(local_path: str) -> str:
    """На Windows два файла foo.txt и FOO.txt — одно и то же.
    Если файл с таким именем (без учёта регистра) уже занят другим путём — добавляем суффикс _1, _2 и т.д.
    Учитываем только реально существующие файлы.
    """
    if not IS_WINDOWS:
        return local_path
    if not os.path.exists(local_path):
        # Проверим, не лежит ли в этой папке файл с тем же именем в другом регистре
        parent = os.path.dirname(local_path)
        name = os.path.basename(local_path)
        if not parent or not os.path.isdir(parent):
            return local_path
        try:
            existing = os.listdir(parent)
        except OSError:
            return local_path
        existing_lower = {e.lower(): e for e in existing}
        if name.lower() in existing_lower and existing_lower[name.lower()] != name:
            return _suffix_path(local_path, existing_lower)
        return local_path
    return local_path


def _suffix_path(local_path: str, existing_lower: dict[str, str]) -> str:
    base, ext = os.path.splitext(local_path)
    i = 1
    while True:
        candidate = f"{base}_{i}{ext}"
        if os.path.basename(candidate).lower() not in existing_lower:
            return candidate
        i += 1


def sqlite_ro_uri(db_path: str | os.PathLike) -> str:
    """URI для read-only подключения к SQLite.

    Экранирует спецсимволы пути: '#' в URI начинает фрагмент, и sqlite молча
    открыл бы ПУСТУЮ базу вместо нашей (проверено на Windows-пути с решёткой).
    """
    from urllib.parse import quote

    p = str(db_path).replace("\\", "/")
    return "file:" + quote(p, safe="/:") + "?mode=ro"


def compute_md5(local_path: str, chunk: int = 1024 * 1024) -> str:
    """Считает MD5 локального файла стримом — не грузит файл целиком в память."""
    h = hashlib.md5()
    path = to_long_path(local_path)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def file_size_safe(local_path: str) -> int | None:
    """Размер локального файла или None если файла нет."""
    try:
        return os.path.getsize(to_long_path(local_path))
    except OSError:
        return None


def human_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _total_ram_gb() -> float:
    """Общий объём ОЗУ в гигабайтах (кроссплатформенно, без внешних зависимостей).

    Нужно для auto_workers(): на малопамятных машинах снижаем число потоков,
    чтобы выгрузка + веб-интерфейс + браузер не выбили процесс по памяти.
    """
    if IS_WINDOWS:
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.ullTotalPhys / (1024 ** 3)
        except Exception:
            pass
    # Linux/macOS: читаем /proc/meminfo (Linux) или утилиты нет — вернём 4 ниже.
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 * 1024)  # kB → GB
    except Exception:
        pass
    return 4.0  # неизвестно — консервативное значение


def auto_workers() -> int:
    """Подбирает число потоков скачивания под объём оперативной памяти.

    Скачивание стримит данные на диск, поэтому память на поток тратится мало —
    лимит в основном про общую стабильность системы, а не про буферы.

    Правило (по общему ОЗУ):
       ≤4 ГБ → 1 поток
       ≤6 ГБ → 3 потока
       иначе → 4 потока
    """
    gb = _total_ram_gb()
    if gb <= 4:
        return 1
    if gb <= 6:
        return 3
    return 4


def _available_ram_gb() -> float | None:
    """Доступная (свободная) ОЗУ в гигабайтах; None — если определить не удалось."""
    if IS_WINDOWS:
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.ullAvailPhys / (1024 ** 3)
        except Exception:
            pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)  # kB → GB
    except Exception:
        pass
    return None


def ram_info() -> dict:
    """Сводка по памяти для интерфейса: объёмы и рекомендованное число потоков.

    Рекомендация — auto_workers(), но при малом объёме свободной памяти она
    понижается, чтобы параллельная выгрузка не ушла в своп.
    """
    total = _total_ram_gb()
    avail = _available_ram_gb()
    base = auto_workers()
    rec = base
    if avail is not None:
        if avail < 1.0:
            rec = 1
        elif avail < 2.0:
            rec = min(base, 2)
    return {
        "total_gb": round(total, 1),
        "available_gb": round(avail, 1) if avail is not None else None,
        "recommended_workers": rec,
        "base_workers": base,
    }
