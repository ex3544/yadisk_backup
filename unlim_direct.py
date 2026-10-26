#!/usr/bin/env python3
"""
unlim_direct.py — Высокоскоростной прямой загрузчик медиа из «Безлимита» Яндекс.Диска.
Скачивает оригиналы через mpfs/url напрямую с CDN Яндекса в обход прокси и архиватора.
Ошибочные файлы мгновенно пропускает (status='skipped') без повторов и задержек.
Отображает реальную скорость (МБ/с и Мбит/с) и транслирует статус в дашборд.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import urllib3.util.connection as urllib3_cn
from dotenv import load_dotenv

import paths

# Принудительно отключаем IPv6, чтобы избежать таймаутов на неработающем IPv6 VirtualBox
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

# Настройка кодировки
sys.stdout.reconfigure(encoding="utf-8")

# Принудительно отключаем переменные прокси для процесса скачивания
for _p in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_p, None)

load_dotenv(paths.ENV_FILE)

COOKIES_FILE = paths.COOKIES_FILE
DB_FILE = paths.UNLIM_DB_FILE
LOG_DIR = paths.LOG_DIR
LOG_FILE = paths.UNLIM_DIRECT_LOG
STATUS_FILE = paths.UNLIM_STATUS_FILE

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s | %(levelname)s | %(message)s",
)

BASE_URL = "https://disk.yandex.ru"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

PHOTO_EXTS = (".jpg", ".jpeg", ".heic", ".png", ".webp")


# ── Измеритель скорости ──────────────────────────────────────────────────────


class SpeedMeter:
    """Измеритель скорости со скользящим окном (последние 3 сек) и средним значением."""

    def __init__(self, window_sec: float = 3.0):
        self.window_sec = window_sec
        self._lock = threading.Lock()
        self._samples: deque[tuple[float, int]] = deque()
        self.total_bytes = 0
        self.start_time = time.perf_counter()

    def add(self, nbytes: int):
        now = time.perf_counter()
        with self._lock:
            self.total_bytes += nbytes
            self._samples.append((now, nbytes))
            cutoff = now - self.window_sec
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def current_speed(self) -> tuple[float, float]:
        """Возвращает текущую мгновенную скорость: (MB_s, Mbit_s)."""
        now = time.perf_counter()
        with self._lock:
            cutoff = now - self.window_sec
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            if not self._samples:
                return 0.0, 0.0
            bytes_in_window = sum(b for _, b in self._samples)
            time_span = max(0.2, now - self._samples[0][0])
            mb_s = (bytes_in_window / (1024 * 1024)) / time_span
            return mb_s, mb_s * 8.0

    def average_speed(self) -> tuple[float, float]:
        """Возвращает среднюю скорость за весь запуск: (MB_s, Mbit_s)."""
        elapsed = max(0.1, time.perf_counter() - self.start_time)
        mb_s = (self.total_bytes / (1024 * 1024)) / elapsed
        return mb_s, mb_s * 8.0


_global_speed_meter = SpeedMeter()


def update_status_file(
    active: bool,
    batch_idx: int = 0,
    batches_total: int = 0,
    item_current: int = 0,
    items_total: int = 0,
    success: int = 0,
    skipped: int = 0,
    mb_s: float = 0.0,
    mbit_s: float = 0.0,
    total_bytes: int = 0,
):
    """Пишет статус для Web UI в logs/download_status.json."""
    data = {
        "active": active,
        "batch_idx": batch_idx,
        "batches_total": batches_total,
        "item_current": item_current,
        "items_total": items_total,
        "success": success,
        "skipped": skipped,
        "speed_mb": round(mb_s, 2),
        "speed_mbit": round(mbit_s, 1),
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "updated_at": time.time(),
    }
    try:
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except Exception:
        pass


# ── Сессия и авторизация ────────────────────────────────────────────────────


def load_cookies() -> dict[str, str]:
    if not COOKIES_FILE.exists():
        sys.exit("[-] Нет cookies.json — сначала экспортируйте куки из браузера.")
    raw = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in raw if c.get("name") and c.get("value")}


def make_session(cookies: dict[str, str], use_proxy: bool = False) -> requests.Session:
    s = requests.Session()
    if use_proxy:
        s.trust_env = True
        s.proxies = {"http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"}
    else:
        s.trust_env = False
        s.proxies = {"http": None, "https": None}
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "ru,en;q=0.9",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/client/photo",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".yandex.ru")
    return s


def get_sk_uid(s: requests.Session) -> tuple[str, str]:
    r = s.get(f"{BASE_URL}/client/disk", timeout=30)
    if "passport.yandex" in r.url or r.status_code != 200:
        sys.exit("[-] Куки невалидны — необходимо обновить cookies.json")
    sk = (
        re.search(r'"sk"\s*:\s*"([^"]+)"', r.text)
        or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', r.text)
    )
    uid = (
        re.search(r'"uid"\s*:\s*"?(\d+)"?', r.text)
        or re.search(r'"default_uid"\s*:\s*"?(\d+)"?', r.text)
    )
    if not sk or not uid:
        sys.exit("[-] Не найдены sk/uid в HTML сессии")
    return sk.group(1), uid.group(1)


# ── Работа с базой данных ───────────────────────────────────────────────────


def get_db():
    con = sqlite3.connect(DB_FILE, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    return con


def recover_stuck():
    """Сбрасывает зависшие файлы из in_progress обратно в pending."""
    con = get_db()
    with con:
        con.execute("UPDATE unlim_files SET status='pending' WHERE status='in_progress'")
    con.close()


def take_batch(batch_size: int, photos_only: bool = True, from_end: bool = False) -> list[dict]:
    con = get_db()
    with con:
        cur = con.cursor()
        ext_filter = ""
        if photos_only:
            conds = [f"name LIKE '%{ext}'" for ext in PHOTO_EXTS]
            ext_filter = f"AND ({' OR '.join(conds)})"

        # Берём строго pending файлы
        order_clause = "attempts ASC, rowid DESC" if from_end else "attempts ASC, cluster_id ASC, file_id ASC"
        query = f"""
            SELECT file_id, name, size
            FROM unlim_files
            WHERE status = 'pending' {ext_filter}
            ORDER BY {order_clause}
            LIMIT ?
        """
        rows = list(cur.execute(query, (batch_size,)))
        if not rows:
            return []

        file_ids = [r["file_id"] for r in rows]
        cur.executemany(
            """UPDATE unlim_files 
               SET status='in_progress', attempts=attempts+1, assigned_at=CURRENT_TIMESTAMP 
               WHERE file_id=?""",
            [(fid,) for fid in file_ids],
        )
        return [{"file_id": r["file_id"], "name": r["name"], "size": r["size"]} for r in rows]


def mark_done(file_id: str, local_path: str, size: int):
    con = get_db()
    with con:
        con.execute(
            """UPDATE unlim_files
               SET status='downloaded', local_path=?, size=?, downloaded_at=CURRENT_TIMESTAMP, error=NULL
               WHERE file_id=?""",
            (local_path, size, file_id),
        )
    con.close()


def mark_skipped(file_id: str, error: str):
    """Помечает файл как skipped (пропущен), сохраняя точную причину ошибки."""
    con = get_db()
    with con:
        con.execute(
            """UPDATE unlim_files
               SET status='skipped', error=?
               WHERE file_id=?""",
            (error[:500], file_id),
        )
    con.close()


# ── Скачивание отдельного файла (БЕЗ ПОВТОРОВ, МГНОВЕННЫЙ ПРОПУСК) ──────────


def check_free_space(target_dir: str, min_mb: int = 500) -> bool:
    try:
        usage = shutil.disk_usage(target_dir)
        return (usage.free / (1024 * 1024)) >= min_mb
    except Exception:
        return True


_thread_local = threading.local()


def get_thread_session(cookies: dict[str, str], use_proxy: bool = False) -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session(cookies, use_proxy=use_proxy)
    return _thread_local.session


def download_one_file(
    cookies: dict[str, str],
    sk: str,
    uid: str,
    item: dict,
    out_dir: Path,
    use_proxy: bool = False,
    timeout: float = 7.0,
) -> tuple[bool, int, str]:
    fid = item["file_id"]
    name = item["name"]

    if not check_free_space(str(out_dir)):
        err = "Недостаточно свободного места на диске (<500MB)"
        mark_skipped(fid, err)
        return False, 0, err

    s = get_thread_session(cookies, use_proxy=use_proxy)

    # 1. Получаем прямую ссылку на оригинальный файл через mpfs/url
    cid = f"{uid}{int(time.time()*1000)}{random.randint(100, 999)}"
    body = {
        "sk": sk,
        "connection_id": cid,
        "apiMethod": "mpfs/url",
        "requestParams": {"path": fid},
    }

    try:
        r = s.post(f"{BASE_URL}/models-v2", params={"m": "mpfs/url"}, json=body, timeout=(3.0, timeout))
        if r.status_code != 200:
            err = f"mpfs/url HTTP {r.status_code}"
            mark_skipped(fid, err)
            return False, 0, err
        data = r.json()
        download_url = data.get("file", "")
    except Exception as e:
        err = f"mpfs/url error: {e}"
        mark_skipped(fid, err)
        return False, 0, err

    if not download_url:
        err = "URL файла не найден в ответе mpfs/url"
        mark_skipped(fid, err)
        return False, 0, err

    if download_url.startswith("//"):
        download_url = "https:" + download_url

    # 2. Проверяем локальный файл
    dst_path = out_dir / name
    if dst_path.exists():
        expected_size = item.get("size")
        if expected_size and dst_path.stat().st_size == expected_size:
            mark_done(fid, str(dst_path), dst_path.stat().st_size)
            return True, dst_path.stat().st_size, "Уже скачан"
        safe_fid = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', fid.strip("/"))
        dst_path = out_dir / f"{safe_fid}_{name}"

    tmp_path = dst_path.with_suffix(dst_path.suffix + ".part")
    bytes_downloaded = 0

    # 3. Скачиваем (без повторов при ошибках нод CDN)
    try:
        resp = s.get(download_url, stream=True, timeout=(3.0, timeout))
        if resp.status_code == 200:
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1 MB чанки
                    if chunk:
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        _global_speed_meter.add(len(chunk))
            tmp_path.replace(dst_path)
            mark_done(fid, str(dst_path), bytes_downloaded)
            return True, bytes_downloaded, "OK"
        else:
            if tmp_path.exists():
                try: tmp_path.unlink()
                except OSError: pass
            err = f"CDN HTTP {resp.status_code}"
            mark_skipped(fid, err)
            return False, 0, err
    except Exception as e:
        if tmp_path.exists():
            try: tmp_path.unlink()
            except OSError: pass
        err = f"Download error: {e}"
        mark_skipped(fid, err)
        return False, 0, err


# ── Основной цикл обработки батчей ──────────────────────────────────────────


def run(
    batch_size: int = 1000,
    batches_count: int = 1,
    workers: int = 10,
    out_dir_path: str = str(paths.DEFAULT_UNLIM_DIR),
    photos_only: bool = True,
    use_proxy: bool = False,
    from_end: bool = False,
    timeout: float = 7.0,
):
    out_dir = Path(out_dir_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 68)
    print("  Yandex.Disk — Высокоскоростной выгрузчик Безлимита (Direct CDN)")
    print("=" * 68)
    print(f"  Целевая папка : {out_dir}")
    print(f"  Размер батча  : {batch_size} файлов")
    print(f"  Кол-во батчей : {'Все до конца' if batches_count <= 0 else batches_count}")
    print(f"  Потоков       : {workers}")
    print(f"  Таймаут       : {timeout} сек (connect: 3.0s, read: {timeout}s)")
    print(f"  Порядок       : {'С КОНЦА (старые файлы / photounlim)' if from_end else 'С начала (новые файлы)'}")
    print("  Режим ошибок  : БЕЗ ПОВТОРОВ (мгновенный пропуск в skipped)")
    print(f"  Прокси        : {'Через 127.0.0.1:10808' if use_proxy else 'Прямой доступ (мимо прокси)'}")
    print(f"  Фильтр        : {'Только фото' if photos_only else 'Все медиа (фото + видео)'}")
    print("=" * 68)

    cookies = load_cookies()
    s = make_session(cookies, use_proxy=use_proxy)
    sk, uid = get_sk_uid(s)
    print(f"[+] Авторизация успешна. UID: {uid}, SK: {sk[:8]}...")

    recover_stuck()

    batch_idx = 0
    total_downloaded_files = 0
    total_downloaded_bytes = 0
    total_skipped_files = 0

    try:
        while True:
            if batches_count > 0 and batch_idx >= batches_count:
                print(f"\n[✓] Завершено заданное количество батчей ({batches_count}).")
                break

            items = take_batch(batch_size, photos_only=photos_only, from_end=from_end)
            if not items:
                print("\n[✓] Нет файлов в очереди (pending). Все медиа обработаны!")
                break

            batch_idx += 1
            print(f"\n>>> Запуск батча #{batch_idx} ({len(items)} файлов)...")

            batch_start_time = time.perf_counter()
            batch_bytes = 0
            batch_success = 0
            batch_skipped = 0

            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_item = {
                    executor.submit(download_one_file, cookies, sk, uid, it, out_dir, use_proxy, timeout): it
                    for it in items
                }

                for i, future in enumerate(as_completed(future_to_item), start=1):
                    it = future_to_item[future]
                    ok, sz, msg = future.result()
                    if ok:
                        batch_success += 1
                        batch_bytes += sz
                    else:
                        batch_skipped += 1
                        logging.warning(f"Пропуск {it['name']}: {msg}")

                    # Текущая скорость (окно 3 сек)
                    cur_mb, cur_mbit = _global_speed_meter.current_speed()
                    percent = (i / len(items)) * 100

                    # Обновление файла статуса для WebUI
                    update_status_file(
                        active=True,
                        batch_idx=batch_idx,
                        batches_total=batches_count,
                        item_current=i,
                        items_total=len(items),
                        success=total_downloaded_files + batch_success,
                        skipped=total_skipped_files + batch_skipped,
                        mb_s=cur_mb,
                        mbit_s=cur_mbit,
                        total_bytes=total_downloaded_bytes + batch_bytes,
                    )

                    # Отображение прогресса в консоли с индикатором скорости
                    print(
                        f"\r  Батч #{batch_idx}: [{i}/{len(items)}] {percent:5.1f}% | "
                        f"✓ {batch_success} | ↷ {batch_skipped} пропущено | "
                        f"⚡ {cur_mb:5.1f} MB/s ({cur_mbit:6.1f} Mbps) | "
                        f"Объём: {batch_bytes / (1024*1024):.1f} MB",
                        end="",
                        flush=True,
                    )

            batch_dur = max(0.01, time.perf_counter() - batch_start_time)
            avg_mb = (batch_bytes / (1024 * 1024)) / batch_dur
            avg_mbit = avg_mb * 8.0
            total_downloaded_files += batch_success
            total_downloaded_bytes += batch_bytes
            total_skipped_files += batch_skipped

            print()
            print(
                f"[+] Батч #{batch_idx} завершён за {batch_dur:.1f} сек. "
                f"Успешно: {batch_success}/{len(items)}, "
                f"Пропущено: {batch_skipped}, "
                f"Объём: {batch_bytes / (1024*1024):.2f} MB "
                f"(Средняя скорость: {avg_mb:.2f} MB/s / {avg_mbit:.1f} Mbps)"
            )

    finally:
        # При выходе сбрасываем статус
        update_status_file(active=False)

    print("\n" + "=" * 68)
    print(f"ИТОГО В ЭТОМ ЗАПУСКЕ: {total_downloaded_files} скачано, {total_skipped_files} пропущено / {total_downloaded_bytes / (1024*1024*1024):.2f} GB")
    print(f"Файлы сохранены в: {out_dir}")
    print("=" * 68)


def main():
    parser = argparse.ArgumentParser(description="Высокоскоростной загрузчик Безлимита Яндекс.Диска")
    parser.add_argument("--batch-size", type=int, default=1000, help="Размер одной пачки (по умолчанию 1000)")
    parser.add_argument("--batches", type=int, default=1, help="Сколько батчей скачать (0 = все до конца)")
    parser.add_argument("--workers", type=int, default=10, help="Количество параллельных потоков (по умолчанию 10)")
    parser.add_argument("--timeout", type=float, default=7.0, help="Таймаут скачивания в секундах (по умолчанию 7.0)")
    parser.add_argument("--out-dir", type=str, default=str(paths.DEFAULT_UNLIM_DIR), help="Папка на HDD")
    parser.add_argument("--all-media", action="store_true", help="Качать все медиа (по умолчанию только фото)")
    parser.add_argument("--use-proxy", action="store_true", help="Скачивать через локальный прокси 127.0.0.1:10808")
    parser.add_argument("--from-end", action="store_true", help="Скачивать с конца списка (старые файлы / photounlim)")
    args = parser.parse_args()

    run(
        batch_size=args.batch_size,
        batches_count=args.batches,
        workers=args.workers,
        out_dir_path=args.out_dir,
        photos_only=not args.all_media,
        use_proxy=args.use_proxy,
        from_end=args.from_end,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
