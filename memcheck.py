"""Печатает свободную ОЗУ в ГБ (одно число) — для сторожевого автопилота докачки.

Используется cron-проверкой: если свободной памяти мало, не запускаем новый
загрузчик, чтобы репер памяти Claude Code не снял фоновые процессы.
"""
import ctypes


class _MEMORYSTATUSEX(ctypes.Structure):
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


def main() -> None:
    m = _MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        print("0.0")
        return
    print(f"{m.ullAvailPhys / (1024 ** 3):.2f}")


if __name__ == "__main__":
    main()
