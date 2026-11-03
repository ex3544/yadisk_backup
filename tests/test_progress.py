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


def test_tiny_file_prints_only_the_result_line():
    p, lines, clock = make("icon.png", 512 * 1024)
    p.start()
    clock.tick(0.4)
    p.update(512 * 1024, "downloader.disk.yandex.ru")
    assert lines == []
    p.done(512 * 1024, "downloader.disk.yandex.ru")
    assert lines == ["✅ icon.png · 512 КБ · 0,4 с · downloader.disk.yandex.ru"]


def test_file_from_one_megabyte_announces_its_name():
    """Имя качающегося файла видно с первой секунды, а не только в итоге."""
    p, lines, _ = make("photo.jpg", 5 * MB)
    p.start()
    assert lines == ["⬇ photo.jpg · 5,0 МБ · начинаю"]


def test_unknown_tiny_file_never_shows_progress():
    p, lines, clock = make("файл.zip", None)
    p.start()
    clock.tick(5)
    p.update(512 * 1024, "h")
    p.done()
    assert lines == ["✅ файл.zip · 512 КБ · 5 с · h"]


def test_unknown_size_announces_when_it_passes_one_megabyte():
    p, lines, clock = make("video.mp4", None)
    p.start()
    assert lines == []
    clock.tick(1)
    p.update(512 * 1024)
    assert lines == []
    clock.tick(1)
    p.update(2 * MB, "h")
    assert lines == ["⬇ video.mp4 · 2,0 МБ · начинаю · вес неизвестен"]


def test_unknown_size_updates_its_progress_from_ten_megabytes():
    """У Безлимита вес неизвестен, но с 10 МБ видно, что файл растёт."""
    p, lines, clock = make("video.mp4", None)
    p.start()
    assert lines == []

    clock.tick(1)
    p.update(1 * MB, "h")
    assert lines == ["⬇ video.mp4 · 1,0 МБ · начинаю · вес неизвестен"]

    clock.tick(4)
    p.update(5 * MB, "h")
    assert len(lines) == 1                # ниже 10 МБ промежуточных строк нет

    clock.tick(6)
    p.update(12 * MB, "h")
    assert lines[-1].startswith("⬇ video.mp4 · 12,0 МБ · ")
    assert "· h · вес неизвестен" in lines[-1]

    clock.tick(10)
    p.update(20 * MB, "h")
    assert lines[-1].startswith("⬇ video.mp4 · 20,0 МБ · ")


def test_unknown_size_stopped_shows_downloaded_bytes():
    p, lines, clock = make("video.mp4", None)
    p.start()
    clock.tick(10)
    p.update(120 * MB, "h")
    p.stopped("обрыв соединения")
    assert lines[-1] == "⏸ video.mp4 · 120 МБ · h · обрыв соединения"


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


def test_stopped_without_a_single_byte_says_server_did_not_send():
    """Строка «начинаю» не должна висеть в журнале без объяснения."""
    p, lines, _ = make("video.mp4", 100 * MB)
    p.start()
    p.stopped("HTTP 500 от сервера хранения")
    assert lines[-1] == (
        "⏸ video.mp4 · сервер не отдал файл: HTTP 500 от сервера хранения"
    )


def test_stopped_without_a_single_byte_is_silent_for_tiny_files():
    p, lines, _ = make("icon.png", 512 * 1024)
    p.start()
    p.stopped("timeout")
    assert lines == []


# ── докачка недокачанного файла ─────────────────────────────────────────────


def test_resumed_big_file_announces_the_place_and_speed_counts_new_bytes():
    p, lines, clock = make("video.mp4", 400 * MB)
    p.resumed(200 * MB)
    p.start()
    assert lines == ["↻ video.mp4 · продолжаю с 50% · 200 МБ из 400 МБ"]

    clock.tick(10)
    p.update(250 * MB, "h")
    # Проценты — от всего файла, скорость — только по новым 50 МБ.
    assert lines[-1] == (
        "⬇ video.mp4 · 62% · 250 МБ из 400 МБ · 5,0 МБ/с · h · осталось ~30 с"
    )


def test_resumed_without_known_size_shows_downloaded_bytes():
    p, lines, _ = make("video.mp4", None)
    p.resumed(300 * MB)
    p.start()
    assert lines == ["↻ video.mp4 · продолжаю с 300 МБ"]


def test_resumed_file_from_one_megabyte_announces_the_place():
    p, lines, clock = make("photo.jpg", 5 * MB)
    p.resumed(2 * MB)
    p.start()
    assert lines == ["↻ photo.jpg · продолжаю с 40% · 2,0 МБ из 5,0 МБ"]
    clock.tick(1)
    p.done(5 * MB)
    assert lines[-1].startswith("✅ photo.jpg · 5,0 МБ · ")


def test_restart_from_zero_after_resume_resets_the_base():
    """Повтор по свежей ссылке качает заново — скорость считается с нуля."""
    p, lines, clock = make("big.iso", 100 * MB)
    p.resumed(60 * MB)
    p.start()
    clock.tick(5)
    p.update(70 * MB, "h")
    assert lines[-1].startswith("⬇ big.iso · 70% · ")

    # Счётчик пошёл с нуля: проценты не должны прыгнуть назад в 70%.
    clock.tick(5)
    p.update(10 * MB, "h")
    assert lines[-1].startswith("⬇ big.iso · 10% · ")
    assert "МБ/с ·" not in lines[-1]  # скорости по нулевому времени ещё нет

    clock.tick(10)
    p.update(20 * MB, "h")
    assert "2,0 МБ/с" in lines[-1]
