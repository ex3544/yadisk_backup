"""Сортирует папку с бэкапом по датам и типам.

Что делает:
  1. Сканирует папку рекурсивно
  2. Дедуп по (size, md5) — дубликаты → _duplicates/ (пропускается с --quick)
  3. Раскладывает по выбранной схеме (--scheme):
     - type-year-month (по умолчанию): Фото/YYYY/YYYY-MM/, Видео/YYYY/YYYY-MM/,
       остальное по типам (Документы/, Архивы/, Программы/, Аудио/, Прочее/)
     - year-month: все файлы → YYYY/YYYY-MM/
     - year:       все файлы → YYYY/

  Дату берёт по приоритету: EXIF фото → атом mvhd видео → дата в имени
  файла (YYYY-MM-DD / IMG_YYYYMMDD) → дата изменения файла (mtime).

Запуск:
  python organize.py --input "E:/папка" --action report-only
  python organize.py --input "E:/папка" --action move
  python organize.py --input "E:/папка" --action move --scheme year
  python organize.py --input "E:/папка" --action report-only --quick   # без MD5, быстрее
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
              ".bmp", ".tiff", ".tif", ".raw", ".arw", ".cr2", ".nef", ".dng", ".orf"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".3gp", ".3g2",
              ".mpg", ".mpeg", ".wmv", ".flv", ".vob", ".mts", ".m2ts"}
DOC_EXTS = {".doc", ".docx", ".pdf", ".txt", ".rtf", ".odt",
            ".xls", ".xlsx", ".ods", ".csv",
            ".ppt", ".pptx", ".odp"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
                ".cab", ".tgz", ".tbz", ".lz", ".lzma"}
PROGRAM_EXTS = {".exe", ".dll", ".msi", ".bat", ".cmd", ".sys", ".com",
                ".scr", ".vbs", ".ps1", ".sh", ".jar"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac", ".wma",
              ".opus", ".aiff", ".alac"}

DIR_PHOTO = "Фото"
DIR_VIDEO = "Видео"
DIR_DOC = "Документы"
DIR_ARCHIVE = "Архивы"
DIR_PROGRAM = "Программы"
DIR_AUDIO = "Аудио"
DIR_OTHER = "Прочее"
DIR_DUP = "_duplicates"

SKIP_DIRS = {DIR_PHOTO, DIR_VIDEO, DIR_DOC, DIR_ARCHIVE, DIR_PROGRAM,
             DIR_AUDIO, DIR_OTHER, DIR_DUP, ".sync"}

DATE_REGEXES = [
    re.compile(r"(20\d{2})[-_.](\d{2})[-_.](\d{2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
    re.compile(r"IMG_(20\d{2})(\d{2})(\d{2})"),
    re.compile(r"VID-(20\d{2})(\d{2})(\d{2})"),
]


def human(n: float) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} PB"


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def get_exif_date(path: str) -> tuple[int, int] | None:
    """Извлекает дату съемки из EXIF метаданных фото без сторонних зависимостей."""
    try:
        with open(path, "rb") as f:
            header = f.read(131072)
            exif_idx = header.find(b"Exif\x00\x00")
            search_buf = header[exif_idx:] if exif_idx != -1 else header
            m = re.search(rb"(20\d{2}|19\d{2})[:\-\.](\d{2})[:\-\.](\d{2})[\sT]\d{2}:\d{2}:\d{2}", search_buf)
            if m:
                y = int(m.group(1))
                mo = int(m.group(2))
                if 1990 <= y <= 2040 and 1 <= mo <= 12:
                    return y, mo
    except OSError:
        pass
    return None


def get_mp4_date(path: str) -> tuple[int, int] | None:
    """Извлекает дату создания из атома mvhd для MP4 / MOV."""
    try:
        with open(path, "rb") as f:
            buf = f.read(131072)
            idx = buf.find(b"mvhd")
            if idx != -1 and idx + 12 <= len(buf):
                version = buf[idx + 4]
                if version == 0 and idx + 12 <= len(buf):
                    import struct
                    creation_time = struct.unpack(">I", buf[idx + 8 : idx + 12])[0]
                    # Секунды от 1904-01-01 (MP4 epoch) к 1970-01-01 (Unix epoch): 2082844800 сек
                    epoch_time = creation_time - 2082844800
                    if 946684800 <= epoch_time <= 2208988800:
                        t = time.gmtime(epoch_time)
                        return t.tm_year, t.tm_mon
    except Exception:
        pass
    return None


def get_date(path: str) -> tuple[int, int]:
    ext = Path(path).suffix.lower()
    # 1. Проверяем метаданные внутри файла
    if ext in PHOTO_EXTS:
        d = get_exif_date(path)
        if d:
            return d
    elif ext in VIDEO_EXTS:
        d = get_mp4_date(path)
        if d:
            return d

    # 2. Поиск даты в названии файла (регулярки)
    name = os.path.basename(path)
    for rx in DATE_REGEXES:
        m = rx.search(name)
        if m:
            try:
                y = int(m.group(1))
                mo = int(m.group(2))
                if 2000 <= y <= 2030 and 1 <= mo <= 12:
                    return y, mo
            except (ValueError, IndexError):
                pass

    # 3. Fallback на системную дату файла
    try:
        t = time.localtime(os.path.getmtime(path))
        return t.tm_year, t.tm_mon
    except OSError:
        return 1970, 1


def classify(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in PHOTO_EXTS: return DIR_PHOTO
    if ext in VIDEO_EXTS: return DIR_VIDEO
    if ext in DOC_EXTS: return DIR_DOC
    if ext in ARCHIVE_EXTS: return DIR_ARCHIVE
    if ext in PROGRAM_EXTS: return DIR_PROGRAM
    if ext in AUDIO_EXTS: return DIR_AUDIO
    return DIR_OTHER


def target_path(root: str, src: str, category: str, scheme: str = "type-year-month") -> str:
    name = os.path.basename(src)
    y, mo = get_date(src)
    if scheme == "year":
        return os.path.join(root, str(y), name)
    if scheme == "year-month":
        return os.path.join(root, str(y), f"{y}-{mo:02d}", name)
    # scheme == "type-year-month" — прежнее поведение по умолчанию
    if category in (DIR_PHOTO, DIR_VIDEO):
        sub = os.path.join(root, category, str(y), f"{y}-{mo:02d}")
    else:
        sub = os.path.join(root, category)
    return os.path.join(sub, name)


def unique_path(dst: str) -> str:
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    i = 1
    while True:
        cand = f"{base}_{i}{ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def scan(root: str) -> list[dict]:
    files = []
    skip = {os.path.join(root, d) for d in SKIP_DIRS}
    print(f"\n[1/4] Сканирую {root}...", flush=True)
    t0 = time.time()
    last = t0
    for dp, dirs, fns in os.walk(root):
        dirs[:] = [d for d in dirs if os.path.join(dp, d) not in skip]
        for fn in fns:
            full = os.path.join(dp, fn)
            try:
                files.append({"path": full, "size": os.path.getsize(full)})
            except OSError:
                pass
            now = time.time()
            if len(files) % 500 == 0 and now - last >= 3:
                rate = len(files) / max(1.0, now - t0)
                print(f"  сканирую: {len(files)} файлов ({rate:.0f} файл/с)", flush=True)
                last = now
    print(f"  Найдено: {len(files)}", flush=True)
    return files


def compute_md5(files: list[dict]):
    print("\n[2/4] MD5 для подозреваемых...", flush=True)
    by_size = defaultdict(list)
    for f in files:
        by_size[f["size"]].append(f)
    suspect = [f for v in by_size.values() if len(v) > 1 for f in v]
    print(f"  Подозреваемых: {len(suspect)}", flush=True)
    t0 = time.time()
    for i, f in enumerate(suspect):
        try:
            f["md5"] = md5_of(f["path"])
        except OSError:
            f["md5"] = None
        if (i + 1) % 500 == 0 or (i + 1) == len(suspect):
            elapsed = max(0.001, time.time() - t0)
            rate = (i + 1) / elapsed
            eta = (len(suspect) - (i + 1)) / max(0.001, rate)
            print(f"    progress: {i+1}/{len(suspect)} · ~{eta/60:.1f} мин осталось", flush=True)


def find_dup_paths(files: list[dict]) -> set[str]:
    g = defaultdict(list)
    for f in files:
        if f.get("md5"):
            g[(f["size"], f["md5"])].append(f)
    out = set()
    for grp in g.values():
        if len(grp) > 1:
            grp.sort(key=lambda x: (len(x["path"]), x["path"]))
            for d in grp[1:]:
                out.add(d["path"])
    return out



def _target_dirs(plan: list[tuple[str, str, bool]]) -> tuple[Counter, Counter]:
    """Сколько файлов и байт ляжет в каждую папку."""
    count: Counter = Counter()
    size: Counter = Counter()
    for src, dst, _is_dup in plan:
        count[os.path.dirname(dst)] += 1
        # После перемещения src уже нет — размер берём у новой копии.
        path = dst if os.path.exists(dst) else src
        try:
            size[os.path.dirname(dst)] += os.path.getsize(path)
        except OSError:
            pass
    return count, size


def _write_reports(
    *,
    root: str,
    action: str,
    scheme: str,
    quick: bool,
    plan: list[tuple[str, str, bool]],
    moved_map: dict[str, str],
    moved: int,
    errors: int,
    error_samples: list[str],
    report_file: str,
    plan_file: str,
) -> None:
    """Пишет понятный отчёт «куда что ляжет» и полный список в CSV."""
    final_plan = [
        (src, moved_map.get(src, dst), is_dup)
        for src, dst, is_dup in plan
    ]
    counts, sizes = _target_dirs(final_plan)
    total = len(final_plan)
    total_b = sum(sizes.values())
    moved_total = sum(
        os.path.getsize(dst) for dst in moved_map.values() if os.path.exists(dst)
    ) if moved_map else 0

    lines = [
        "ОТЧЁТ О СОРТИРОВКЕ",
        f"Папка: {root}",
        f"Схема: {scheme}",
        f"Действие: {'только отчёт, файлы не трогались' if action == 'report-only' else 'перемещение'}",
        f"Всего файлов: {total} ({human(total_b)})",
        "",
        "КУДА ЛЯГУТ ФАЙЛЫ",
    ]
    if not counts:
        lines.append("  файлов для сортировки нет")
    for folder, number in counts.most_common():
        rel = os.path.relpath(folder, root)
        shown = root if rel == os.curdir else rel
        lines.append(f"  {shown}: {number} файлов ({human(sizes[folder])})")
    if quick:
        lines += ["", "Дубликаты не проверялись: включён быстрый режим (без MD5)."]
    else:
        dups = sum(1 for _s, _d, is_dup in final_plan if is_dup)
        lines += ["", f"Дубликаты (по размеру и MD5): {dups} — уедут в {DIR_DUP}/."]

    lines += ["", "ПРИМЕРЫ: ОТКУДА → КУДА (первые 100)"]
    for src, dst, is_dup in final_plan[:100]:
        mark = " [дубликат]" if is_dup else ""
        lines.append(f"  {src} → {dst}{mark}")
    if total > 100:
        lines.append(f"  … и ещё {total - 100} файлов — полный список в CSV.")

    if action == "move":
        lines += [
            "",
            f"ПЕРЕМЕЩЕНО: {moved} файлов ({human(moved_total)}), ошибок: {errors}.",
        ]
        if error_samples:
            lines.append("Первые ошибки:")
            lines.extend(f"  {text}" for text in error_samples[:5])
    else:
        lines += ["", "Файлы НЕ перемещались. Это предпросмотр."]

    if plan_file:
        lines += ["", f"Полный список: {plan_file}"]
    report_text = "\n".join(lines) + "\n"
    if report_file:
        try:
            os.makedirs(os.path.dirname(report_file), exist_ok=True)
            with open(report_file, "w", encoding="utf-8") as fh:
                fh.write(report_text)
        except OSError as exc:
            print(f"  [!] Не удалось записать отчёт: {exc}", flush=True)
    if plan_file:
        try:
            os.makedirs(os.path.dirname(plan_file), exist_ok=True)
            with open(plan_file, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh, delimiter=";")
                writer.writerow(["действие", "откуда", "куда"])
                for src, dst, is_dup in final_plan:
                    writer.writerow(["дубликат" if is_dup else "файл", src, dst])
        except OSError as exc:
            print(f"  [!] Не удалось записать список: {exc}", flush=True)

    print("\n" + report_text, flush=True)


def _update_db_paths(db_path: str, table: str, moves: list[tuple[str, str]]) -> int:
    """Обновляет local_path после перемещения. Молча пропускает чужие строки."""
    if not db_path or not moves:
        return 0
    if table not in ("files", "unlim_files"):
        print(f"  [!] Неизвестная таблица: {table}", flush=True)
        return 0
    if not os.path.exists(db_path):
        print(f"  [!] База не найдена: {db_path}", flush=True)
        return 0
    try:
        con = sqlite3.connect(db_path, timeout=30)
        try:
            index = {}
            for rowid, local in con.execute(
                f"SELECT rowid, local_path FROM {table} "
                f"WHERE local_path IS NOT NULL AND local_path <> ''"
            ):
                index[os.path.normcase(os.path.normpath(str(local)))] = rowid
            updates = []
            for src, dst in moves:
                rowid = index.get(os.path.normcase(os.path.normpath(src)))
                if rowid is not None:
                    updates.append((dst, rowid))
            if updates:
                with con:
                    con.executemany(
                        f"UPDATE {table} SET local_path = ? WHERE rowid = ?",
                        updates,
                    )
            return len(updates)
        finally:
            con.close()
    except sqlite3.Error as exc:
        print(f"  [!] Не удалось обновить пути в базе: {exc}", flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--action", default="report-only",
                    choices=["report-only", "move"])
    ap.add_argument("--scheme", default="type-year-month",
                    choices=["type-year-month", "year-month", "year"],
                    help="Схема раскладки: по типам+дате, по годам+месяцам или только по годам")
    ap.add_argument("--quick", action="store_true",
                    help="Быстрый отчёт: пропустить поиск дубликатов (MD5)")
    ap.add_argument("--report-file", default="",
                    help="Куда записать понятный отчёт «куда что ляжет»")
    ap.add_argument("--plan-file", default="",
                    help="Куда записать полный список «откуда → куда» (CSV)")
    ap.add_argument("--db", default="",
                    help="База, в которой обновить пути после перемещения")
    ap.add_argument("--table", default="",
                    choices=["", "files", "unlim_files"],
                    help="Таблица базы: files или unlim_files")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.input)
    if not os.path.isdir(root):
        sys.exit(f"Нет папки: {root}")

    print("=" * 60)
    print(f"  СОРТИРОВКА: {root}")
    print(f"  Действие: {args.action}")
    print(f"  Схема: {args.scheme}")
    if args.quick:
        print("  Быстрый отчёт: без поиска дубликатов (MD5)")
    print("=" * 60)

    t0 = time.time()
    files = scan(root)
    if args.quick:
        dups = set()
        print("\n[2/4] Поиск дубликатов пропущен (быстрый отчёт).", flush=True)
    else:
        compute_md5(files)
        dups = find_dup_paths(files)

    print("\n[3/4] Раскладываю...", flush=True)
    plan = []
    cat_n = Counter()
    cat_b = Counter()
    dup_b = 0
    t_last = time.time()
    for i, f in enumerate(files):
        src = f["path"]
        if src in dups:
            rel = os.path.relpath(src, root)
            plan.append((src, os.path.join(root, DIR_DUP, rel), True))
            dup_b += f["size"]
        else:
            cat = classify(src)
            plan.append((src, target_path(root, src, cat, args.scheme), False))
            cat_n[cat] += 1
            cat_b[cat] += f["size"]
        now = time.time()
        if (i + 1) % 5000 == 0 and now - t_last >= 3:
            print(f"  раскладываю: {i+1}/{len(files)}", flush=True)
            t_last = now

    total_b = sum(f["size"] for f in files)
    print("\n[4/4] СТАТИСТИКА:")
    print(f"  Всего:           {len(files)} ({human(total_b)})")
    if args.quick:
        print("  Дубликатов:      не проверялись (быстрый отчёт)")
    else:
        print(f"  Дубликатов:      {len(dups)} ({human(dup_b)})")
        print(f"  Уникальных:      {len(files) - len(dups)}")
    print("\n  По полкам:")
    for cat in [DIR_PHOTO, DIR_VIDEO, DIR_AUDIO, DIR_DOC, DIR_ARCHIVE,
                DIR_PROGRAM, DIR_OTHER]:
        if cat_n[cat]:
            print(f"    {cat:<12} {cat_n[cat]:>6} файлов ({human(cat_b[cat])})")
    print(f"\n  Скан: {time.time() - t0:.0f} сек")

    if args.action == "report-only":
        _write_reports(
            root=root, action=args.action, scheme=args.scheme, quick=args.quick,
            plan=plan, moved_map={}, moved=0, errors=0, error_samples=[],
            report_file=args.report_file, plan_file=args.plan_file,
        )
        print("\n[INFO] Файлы не перемещались. Для сортировки нажмите "
              "«Применить сортировку».", flush=True)
        return 0

    print(f"\nПеремещаю {len(plan)}...")
    moved = 0
    err = 0
    moves: list[tuple[str, str]] = []
    error_samples: list[str] = []
    last = time.time()
    for src, dst, is_dup in plan:
        final = unique_path(dst)
        try:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            shutil.move(src, final)
            moves.append((src, final))
            moved += 1
        except Exception as e:
            err += 1
            if len(error_samples) < 5:
                error_samples.append(f"{src}: {e}")
            if err <= 5:
                print(f"  err: {src}: {e}")
        if time.time() - last > 5:
            print(f"  progress: {moved}/{len(plan)} (err={err})")
            last = time.time()

    cleaned = 0
    for dp, dirs, fns in os.walk(root, topdown=False):
        rel = os.path.relpath(dp, root)
        first = rel.split(os.sep)[0] if rel != "." else ""
        if first in SKIP_DIRS:
            continue
        try:
            if not os.listdir(dp) and dp != root:
                os.rmdir(dp)
                cleaned += 1
        except OSError:
            pass

    updated = 0
    if args.db:
        updated = _update_db_paths(args.db, args.table, moves)
    _write_reports(
        root=root, action=args.action, scheme=args.scheme, quick=args.quick,
        plan=plan, moved_map=dict(moves), moved=moved, errors=err,
        error_samples=error_samples,
        report_file=args.report_file, plan_file=args.plan_file,
    )
    print("\n✅ ГОТОВО:")
    print(f"  Перемещено: {moved}")
    print(f"  Ошибок:     {err}")
    print(f"  Удалено пустых папок: {cleaned}")
    if args.db:
        print(f"  Путей в базе обновлено: {updated}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
