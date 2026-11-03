"""Строки журнала о ходе скачивания: имя, вес, проценты, скорость, сервер."""

from __future__ import annotations

from progress import FileProgress, human_size, human_span

MB = 1024 * 1024
GB = 1024 * MB


class Clock:
    """Управляемые часы: строки прогресса не зависят от реального времени."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


def make(name="f.bin", size=None, lines=None, **kwargs):
    lines = [] if lines is None else lines
    clock = kwargs.pop("clock", None) or Clock()
    return FileProgress(name, size, emit=lines.append, clock=clock, **kwargs), lines, clock


# ── форматирование ──────────────────────────────────────────────────────────


def test_human_size_is_russian_and_compact():
    assert human_size(900) == "900 Б"
    assert human_size(1536) == "1,5 КБ"
    assert human_size(5 * MB) == "5,0 МБ"
    assert human_size(20 * MB) == "20,0 МБ"
    assert human_size(200 * MB) == "200 МБ"
    assert human_size(round(20.9 * GB)) == "20,9 ГБ"
    assert human_size(None) == "?"


def test_human_span():
    assert human_span(0.4) == "0,4 с"
    assert human_span(18) == "18 с"
    assert human_span(90) == "1 мин"
    assert human_span(7200) == "2 ч"
    assert human_span(7800) == "2 ч 10 мин"


# ── мелкие файлы ────────────────────────────────────────────────────────────


def test_small_file_prints_only_the_result_line():
    p, lines, clock = make("photo.jpg", 5 * MB)
    p.start()
    clock.tick(0.4)
    p.update(5 * MB, "downloader.disk.yandex.ru")
    assert lines == []
    p.done(5 * MB, "downloader.disk.yandex.ru")
    assert lines == ["✅ photo.jpg · 5,0 МБ · 0,4 с · downloader.disk.yandex.ru"]


def test_unknown_size_never_shows_progress():
    p, lines, clock = make("файл.zip", None)
    p.start()
    clock.tick(5)
    p.update(3 * MB, "h")
    p.done()
    assert lines == ["✅ файл.zip · 3,0 МБ · 5 с · h"]


def test_name_is_the_file_name_not_the_whole_path():
    p, lines, _ = make("disk:/Загрузки/Realme-Note-50-RMX3834.zip", 5 * MB)
    p.done(5 * MB)
    assert lines[0].startswith("✅ Realme-Note-50-RMX3834.zip · ")


# ── крупные файлы ───────────────────────────────────────────────────────────


def test_big_file_announces_start_and_updates_every_ten_seconds():
    p, lines, clock = make("video.mp4", 200 * MB)
    p.start()
    assert lines == ["⬇ video.mp4 · 200 МБ · начинаю"]

    clock.tick(2)
    p.update(20 * MB, "downloader.disk.yandex.ru")
    assert lines[-1] == (
        "⬇ video.mp4 · 10% · 20,0 МБ из 200 МБ · 10,0 МБ/с · "
        "downloader.disk.yandex.ru · осталось ~18 с"
    )

    # Меньше шага — не пишет, иначе журнал превратится в простыню.
    clock.tick(5)
    p.update(70 * MB)
    assert len(lines) == 2

    clock.tick(6)
    p.update(80 * MB)
    assert len(lines) == 3
    assert "40%" in lines[-1]


def test_progress_text_has_percent_written_speed_and_server():
    p, lines, clock = make("big.iso", 100 * MB)
    p.start()
    clock.tick(4)
    p.update(10 * MB, "storage.yandex.net")
    text = lines[-1]
    assert text.startswith("⬇ big.iso · 10% · 10,0 МБ из 100 МБ · ")
    assert "2,5 МБ/с" in text
    assert "storage.yandex.net" in text


def test_new_attempt_after_five_xx_starts_counting_from_zero():
    p, lines, clock = make("big.iso", 100 * MB)
    p.start()
    clock.tick(5)
    p.update(50 * MB, "h")
    assert "50%" in lines[-1]

    # Повтор по свежей ссылке: счётчик пошёл с нуля — проценты не прыгают назад.
    clock.tick(5)
    p.update(1 * MB, "h")
    assert "1%" in lines[-1]


def test_finish_line_keeps_name_size_time_and_server():
    p, lines, clock = make("video.mp4", 200 * MB)
    p.start()
    clock.tick(1620)
    p.done(200 * MB, "downloader.disk.yandex.ru")
    assert lines[-1] == (
        "✅ video.mp4 · 200 МБ · 27 мин · downloader.disk.yandex.ru"
    )


def test_stopped_shows_where_big_file_stopped():
    p, lines, clock = make("video.mp4", 100 * MB)
    p.start()
    clock.tick(5)
    p.update(12 * MB, "h")
    p.stopped("timeout: обрыв соединения")
    assert lines[-1] == (
        "⏸ video.mp4 · остановился на 12% · 12,0 МБ из 100 МБ · h · "
        "timeout: обрыв соединения"
    )


def test_stopped_is_silent_when_nothing_was_downloaded():
    p, lines, _ = make("video.mp4", 100 * MB)
    p.start()
    p.stopped("timeout")
    assert lines == ["⬇ video.mp4 · 100 МБ · начинаю"]
