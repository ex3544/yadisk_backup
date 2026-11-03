"""Строки журнала о ходе скачивания: имя, вес, процент, скорость, сервер.

Строка пишется и в файл журнала (logging), и в консоль задачи (print): Web UI
читает stdout дочернего процесса, а диагностика — файл. Поэтому одна и та же
строка видна и в «Главной», и в logs/backup.log.

Файлы от 1 МБ объявляют себя строкой «начинаю»: видно, что именно качается
сейчас. Совсем мелочь даёт только итоговую строку — проценты по ней мелькают
быстрее, чем человек успевает прочитать. Крупные дополнительно обновляются
раз в `STEP_SEC` секунд, чтобы было видно, что загрузка жива: на одном
20-гигабайтном файле иначе журнал молчит четверть часа.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

log = logging.getLogger("yadisk_backup")

KB = 1024
MB = 1024 * KB
GB = 1024 * MB

# С этого веса пишем строку «начинаю»: даже у мелкого файла видно, что именно
# качается прямо сейчас. Совсем мелочь (иконки, документы) остаётся с итогом.
START_BYTES = 1 * MB
# Известный вес: промежуточные строки нужны только крупным файлам.
BIG_BYTES = 100 * MB
# Неизвестный вес (Безлимит): процентов нет, но рост в мегабайтах полезен уже
# с 10 МБ — иначе видео молчит до сотни мегабайт.
UNKNOWN_BYTES = 10 * MB
# Как часто обновлять строку крупного файла.
STEP_SEC = 10.0


def _num(x: float) -> str:
    """Русская запись числа: 20,9 ГБ, 512 КБ."""
    if x >= 100:
        return f"{x:.0f}"
    return f"{x:.1f}".replace(".", ",")


def human_size(n: Optional[float]) -> str:
    """Вес файла/скорости по-русски. None → «?»."""
    if n is None:
        return "?"
    n = float(n)
    if n >= GB:
        return _num(n / GB) + " ГБ"
    if n >= MB:
        return _num(n / MB) + " МБ"
    if n >= KB:
        return _num(n / KB) + " КБ"
    return f"{int(n)} Б"


def human_span(seconds: Optional[float]) -> str:
    """Длительность по-русски: 0,4 с · 27 мин · 2 ч 10 мин."""
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        # Доли секунды у мелких файлов — «0,4 с», а ровно 5 — «5 с».
        if abs(seconds - round(seconds)) < 0.05:
            return f"{int(round(seconds))} с"
        return f"{_num(round(seconds, 1))} с"
    if seconds < 60:
        return f"{int(round(seconds))} с"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


def _emit_line(line: str) -> None:
    """Пишет строку в файл журнала и в stdout задачи (его читает Web UI)."""
    log.info(line)
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass


class FileProgress:
    """Строки «что качается» для одного файла.

    Использование:
        p = FileProgress(name, size)
        p.start()
        ... p.update(written, host) на каждом чанке ...
        p.done(written, host)

    `emit` подменяется в тестах: так проверяется текст без перехвата stdout.
    """

    def __init__(
        self,
        name: str,
        size: Optional[int] = None,
        *,
        emit: Optional[Callable[[str], None]] = None,
        start_bytes: int = START_BYTES,
        big_bytes: int = BIG_BYTES,
        unknown_bytes: int = UNKNOWN_BYTES,
        step_sec: float = STEP_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        raw = str(name or "")
        self.name = os.path.basename(raw.rstrip("/\\")) or raw or "файл"
        self.size = int(size) if size else None
        self.host: Optional[str] = None
        self._emit = emit or _emit_line
        self._start_bytes = start_bytes
        self._big_bytes = big_bytes
        self._unknown_bytes = unknown_bytes
        self._step_sec = step_sec
        self._clock = clock
        self._t0: Optional[float] = None
        self._last_line = 0.0
        self._written = 0
        # Сколько байт уже лежало в `.part` до этой попытки. Скорость считаем
        # только по новым байтам: иначе докачка с 90% мгновенно показала бы
        # «5 ГБ/с».
        self._base = 0
        # Строка «начинаю» для файла без известного веса появляется не в
        # start(), а когда скачано достаточно байт, — и только один раз.
        self._started_line = False

    @property
    def is_big(self) -> bool:
        """Заметный ли файл: от 1 МБ у него есть строка «начинаю»."""
        if self.size is not None:
            return self.size >= self._start_bytes
        # У Безлимита вес неизвестен: судим по уже скачанному.
        return self._written >= self._start_bytes

    @property
    def _show_updates(self) -> bool:
        """Пора ли печатать промежуточные строки (проценты/мегабайты)."""
        if self.size is not None:
            return self.size >= self._big_bytes
        return self._written >= self._unknown_bytes

    def resumed(self, written: int) -> None:
        """Файл продолжается с недокачанного `.part`, а не качается заново.

        Вызывать до `start()`/`update()`: дальше `written` снова абсолютный,
        а скорость считается только по новым байтам.
        """
        self._base = int(written or 0)
        self._written = self._base

    def _start_line(self) -> str:
        if self._base > 0:
            if self.size:
                pct = min(100, int(self._base * 100 / self.size))
                return (f"↻ {self.name} · продолжаю с {pct}% · "
                        f"{human_size(self._base)} из {human_size(self.size)}")
            return f"↻ {self.name} · продолжаю с {human_size(self._base)}"
        if self.size is not None:
            return f"⬇ {self.name} · {human_size(self.size)} · начинаю"
        # Вес не сообщили: показываем, сколько уже скачано.
        return (f"⬇ {self.name} · {human_size(self._written)} · начинаю · "
                f"вес неизвестен")

    def start(self) -> None:
        """Первая строка. Совсем мелкие файлы молчат — у них будет только итог."""
        now = self._clock()
        if self._t0 is None:
            self._t0 = now
        if self._started_line or not self.is_big:
            return
        self._started_line = True
        self._emit(self._start_line())

    def update(
        self,
        written: int,
        host: Optional[str] = None,
        total: Optional[int] = None,
    ) -> None:
        """Новый кусок скачан. Для мелких файлов ничего не пишет."""
        now = self._clock()
        if self.size is None and total:
            self.size = int(total)
        if self._t0 is None:
            self._t0 = now
        if host:
            self.host = host
        written = int(written or 0)
        if written < self._written:
            # Новая попытка по свежей ссылке (повтор после 5xx): проценты и
            # скорость считаем заново, старые цифры не должны утекать в строку.
            self._t0 = now
            self._last_line = 0.0
            self._base = 0
        self._written = written
        if not self._started_line and self.is_big:
            # Вес был неизвестен на старте — объявляем файл, как только он
            # перешагнул порог. Строку прогресса в эту же секунду не пишем.
            self._started_line = True
            self._emit(self._start_line())
            self._last_line = now
            return
        if not self._show_updates:
            return
        if self._last_line and (now - self._last_line) < self._step_sec:
            return
        self._last_line = now
        self._emit(self._line(now))

    def _line(self, now: float) -> str:
        elapsed = now - self._t0 if self._t0 is not None else 0.0
        # Сразу после старта (или после повтора по новой ссылке) время почти
        # нулевое — скорость по нему была бы фантастической. Лучше промолчать.
        gained = max(0, self._written - self._base)
        speed = gained / elapsed if elapsed >= 0.5 else 0.0
        parts = [f"⬇ {self.name}"]
        if self.size:
            pct = min(100, int(self._written * 100 / self.size))
            parts.append(f"{pct}%")
            parts.append(f"{human_size(self._written)} из {human_size(self.size)}")
        else:
            parts.append(human_size(self._written))
        if speed > 0:
            parts.append(f"{human_size(speed)}/с")
        if self.host:
            parts.append(self.host)
        if self.size and speed > 0 and self._written < self.size:
            parts.append("осталось ~" + human_span((self.size - self._written) / speed))
        elif self.size is None:
            # Без процентов непонятно, почему их нет: это не сбой, а Безлимит.
            parts.append("вес неизвестен")
        return " · ".join(parts)

    def done(
        self,
        written: Optional[int] = None,
        host: Optional[str] = None,
        seconds: Optional[float] = None,
    ) -> None:
        """Итог файла: имя, вес, сколько заняло и с какого сервера."""
        if host:
            self.host = host
        if written is not None:
            self._written = int(written or 0)
        if seconds is None and self._t0 is not None:
            seconds = self._clock() - self._t0
        size = human_size(self.size if self.size is not None else self._written)
        parts = [f"✅ {self.name}", size]
        if seconds is not None:
            parts.append(human_span(seconds))
        if self.host:
            parts.append(self.host)
        self._emit(" · ".join(parts))

    def stopped(self, reason: Optional[str] = None) -> None:
        """Крупный файл оборвался: видно, на чём именно остановился.

        Если не пришло ни байта, строка «начинаю» не должна висеть в журнале
        без объяснения: пишем, что сервер файл не отдал.
        """
        if not self.is_big:
            return
        if self._written <= 0:
            parts = [f"⏸ {self.name}", "сервер не отдал файл"]
            if reason:
                parts[1] += f": {reason}"
            if self.host:
                parts.append(self.host)
            self._emit(" · ".join(parts))
            return
        parts = [f"⏸ {self.name}"]
        if self.size:
            pct = int(self._written * 100 / self.size)
            parts.append(f"остановился на {pct}%")
            parts.append(f"{human_size(self._written)} из {human_size(self.size)}")
        else:
            parts.append(human_size(self._written))
        if self.host:
            parts.append(self.host)
        if reason:
            parts.append(str(reason)[:160])
        self._emit(" · ".join(parts))
