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
import hashlib
import os
import re
import shutil
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--action", default="report-only",
                    choices=["report-only", "move"])
    ap.add_argument("--scheme", default="type-year-month",
                    choices=["type-year-month", "year-month", "year"],
                    help="Схема раскладки: по типам+дате, по годам+месяцам или только по годам")
    ap.add_argument("--quick", action="store_true",
                    help="Быстрый отчёт: пропустить поиск дубликатов (MD5)")
    args = ap.parse_args()

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
        print("\n[INFO] Отчёт. Запусти --action move чтобы реально сделать.")
        return 0

    print(f"\nПеремещаю {len(plan)}...")
    moved = 0
    err = 0
    last = time.time()
    for src, dst, is_dup in plan:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, unique_path(dst))
            moved += 1
        except Exception as e:
            err += 1
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

    print("\n✅ ГОТОВО:")
    print(f"  Перемещено: {moved}")
    print(f"  Ошибок:     {err}")
    print(f"  Удалено пустых папок: {cleaned}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
