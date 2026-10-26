#!/usr/bin/env python3
"""Генерирует logs/download_report.md — сводку выгрузки + список недоступных файлов.

Запускать без аргументов. Читает state.db и статус веб-интерфейса (порт 8000),
пишет отчёт в logs/download_report.md.
"""
import json
import re
import sqlite3
import urllib.request
from datetime import datetime

import paths

DB = str(paths.DB_FILE)
REPORT = str(paths.DOWNLOAD_REPORT)


def _fmt_bytes(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1 << 30):.2f} ГБ"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.1f} МБ"
    return f"{b / 1024:.0f} КБ"


def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. state.db
    c = sqlite3.connect(DB)
    counts = {}
    for s, n, b in c.execute(
        "SELECT status, COUNT(*), COALESCE(SUM(size),0) FROM files GROUP BY status"
    ).fetchall():
        counts[s] = (n, b)

    failed_rows = c.execute(
        "SELECT remote_path, size, error FROM files WHERE status='failed' "
        "ORDER BY remote_path"
    ).fetchall()
    c.close()

    down_n, down_b = counts.get("downloaded", (0, 0))
    fail_n, fail_b = counts.get("failed", (0, 0))
    pend_n, pend_b = counts.get("pending", (0, 0))
    skip_n, skip_b = counts.get("skipped", (0, 0))
    total_n = down_n + fail_n + pend_n + skip_n
    total_b = down_b + fail_b + pend_b + skip_b

    # 2. статус задачи (running / скорость / ETA из tqdm)
    running = False
    speed = None
    eta = None
    try:
        with urllib.request.urlopen("http://localhost:8000/api/status", timeout=5) as r:
            st = json.load(r)
        t = st.get("tasks", {}).get("disk", {})
        running = bool(t.get("running"))
        for line in reversed(t.get("log_tail", [])):
            clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
            m = re.search(r"(\d+(?:\.\d+)?)\s*([kMG]B/s)", clean)
            if m:
                speed = m.group(1) + m.group(2)
            m2 = re.search(r"<([\d:]+),", clean)
            if m2 and eta is None:
                eta = m2.group(1)
            if speed:
                break
    except Exception:
        pass

    # 3. пишем отчёт
    lines = []
    lines.append("# Отчёт о выгрузке Яндекс.Диска")
    lines.append("")
    lines.append(f"**Обновлено:** {now}")
    lines.append("")
    lines.append("## Прогресс")
    lines.append("")
    pct = (down_b / total_b * 100) if total_b else 0.0
    lines.append(f"- Скачано: **{down_n}** файлов ({_fmt_bytes(down_b)})")
    lines.append(f"- Недоступно: **{fail_n}** файлов ({_fmt_bytes(fail_b)})")
    lines.append(f"- Осталось: **{pend_n}** файлов ({_fmt_bytes(pend_b)})")
    if skip_n:
        lines.append(f"- Пропущено: **{skip_n}** файлов ({_fmt_bytes(skip_b)})")
    lines.append(f"- Всего: **{total_n}** файлов ({_fmt_bytes(total_b)}), готово **{pct:.2f}%**")
    lines.append("")
    lines.append(f"- Загрузка: **{'идёт' if running else 'ОСТАНОВЛЕНА'}**")
    if speed:
        lines.append(f"- Текущая скорость: {speed}")
    if eta:
        lines.append(f"- Расчётное время до конца: {eta}")
    lines.append("")

    lines.append("## Недоступные файлы (докачаем позже командой `retry-failed`)")
    lines.append("")
    if failed_rows:
        lines.append("| Файл | Размер | Сервер |")
        lines.append("|---|---|---|")
        for path, size, err in failed_rows:
            m = re.search(r"host='([^']+)'", err or "")
            host = m.group(1).replace(".storage.yandex.net", "") if m else "?"
            lines.append(f"| `{path}` | {_fmt_bytes(size)} | {host} |")
    else:
        lines.append("Пока нет недоступных файлов.")
    lines.append("")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Отчёт сохранён: {REPORT}")
    print(f"  скачано={down_n} ({_fmt_bytes(down_b)}), "
          f"недоступно={fail_n} ({_fmt_bytes(fail_b)}), "
          f"осталось={pend_n}, готово={pct:.2f}%, "
          f"загрузка={'идёт' if running else 'СТОП'}, скорость={speed or '?'}")


if __name__ == "__main__":
    main()
