#!/usr/bin/env python3
"""
unlim_backup.py — Универсальный автономный загрузчик «Безлимита» Яндекс.Диска.
Разработано: by PUH

Кроссплатформенный (Windows / Linux / macOS).
Полностью автономный:
  - Если рядом нет базы данных unlim_state.db — автоматически создаёт её и собирает
    полный список всех медиафайлов из Безлимита через API Яндекс.Диска со статусом 'pending'.
  - Поддерживает выбор типа контента: фото (--media photos), видео (--media videos), все (--media all).
  - Поддерживает выбор очереди: новые файлы (--target pending) или повторный проход
    по ранее сбойным (--target skipped) для повторной попытки выкачки.
  - Позволяет сбросить ошибочные файлы обратно в очередь: --reset-skipped.
  - Направление скачивания: с начала или с конца (--from-end).
  - Высокая скорость: настраиваемое число потоков (--workers) и агрессивный таймаут (--timeout).
  - Политика «Без повторов»: сбойные файлы сразу помечаются 'skipped' с записью ошибки,
    не блокируя остальные потоки.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import urllib3.util.connection as urllib3_cn

import paths
import unlim_schema

# Принудительно отключаем IPv6 для исключения таймаутов на неработающем IPv6 (VirtualBox / NAT)
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

# Кодировка вывода
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# Флаг «сессия истекла»: ставится при HTTP 401/403 на mpfs/url. По нему загрузка
# обрывается целиком, а не уходит в бесконечный повтор сбойных файлов.
AUTH_EXPIRED = threading.Event()

# Флаг «диск заполнен/отключился»: по нему загрузка обрывается целиком, а файлы
# не помечаются skipped массово (остаются in_progress → recover_stuck вернёт в pending).
DISK_FULL = threading.Event()


class TeeLogger:
    """Дублирует весь вывод терминала в лог-файл logs/unlim_backup.log"""
    def __init__(self, filepath: Path, orig_stream):
        self.filepath = filepath
        self.orig = orig_stream
        self.file = None
        try:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            self.file = open(self.filepath, "a", encoding="utf-8", buffering=1)
        except Exception:
            pass

    def write(self, data):
        if self.orig:
            self.orig.write(data)
            try:
                self.orig.flush()
            except Exception:
                pass
        if self.file:
            try:
                self.file.write(data)
                self.file.flush()
            except Exception:
                pass

    def flush(self):
        if self.orig:
            self.orig.flush()
        if self.file:
            try:
                self.file.flush()
            except Exception:
                pass


# Очищаем системные переменные прокси для процесса скачивания (прямой интернет)
DEFAULT_NO_PROXY = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

BASE_URL = "https://disk.yandex.ru"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".raw", ".dng", ".webp", ".bmp", ".gif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".webm", ".flv", ".lrv"}

# Схема и миграции — в unlim_schema.py, чтобы все процессы видели одну историю.


# ── Измеритель скорости ──────────────────────────────────────────────────────


class SpeedMeter:
    def __init__(self, window_sec: float = 3.0):
        self.window_sec = window_sec
        self.samples = deque()  # (timestamp, bytes)
        self.lock = threading.Lock()
        self.session_bytes = 0
        self.session_start = time.perf_counter()

    def add(self, nbytes: int):
        now = time.perf_counter()
        with self.lock:
            self.samples.append((now, nbytes))
            self.session_bytes += nbytes
            cutoff = now - self.window_sec
            while self.samples and self.samples[0][0] < cutoff:
                self.samples.popleft()

    def current_speed(self) -> tuple[float, float]:
        now = time.perf_counter()
        with self.lock:
            cutoff = now - self.window_sec
            while self.samples and self.samples[0][0] < cutoff:
                self.samples.popleft()
            if not self.samples:
                return 0.0, 0.0
            total_b = sum(b for _, b in self.samples)
            dt = now - self.samples[0][0]
            if dt < 0.2:
                dt = 0.2
            mb_s = (total_b / (1024 * 1024)) / dt
            mbit_s = mb_s * 8.0
            return mb_s, mbit_s


_global_speed_meter = SpeedMeter()


# ── Работа с куки и авторизацией ─────────────────────────────────────────────


def load_cookies(cookies_path: Path) -> dict[str, str]:
    if not cookies_path.exists():
        sys.exit(
            f"[-] Файл куки не найден: {cookies_path}\n"
            f"    Экспортируйте куки из браузера с disk.yandex.ru в формате JSON (расширение Cookie-Editor)\n"
            f"    и сохраните файл как {paths.COOKIES_FILE}"
        )
    try:
        raw = json.loads(cookies_path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"[-] Ошибка чтения {cookies_path}: {e}")

    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if c.get("name") and c.get("value") is not None}
    elif isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    else:
        sys.exit(f"[-] Неверный формат {cookies_path}. Ожидался список объектов куки Cookie-Editor.")


def make_session(cookies: dict[str, str], use_proxy: bool = False, proxy_url: str = "http://127.0.0.1:10808") -> requests.Session:
    s = requests.Session()
    if use_proxy:
        s.trust_env = True
        s.proxies = {"http": proxy_url, "https": proxy_url}
    else:
        s.trust_env = False
        s.proxies = {"http": None, "https": None}
        for p in DEFAULT_NO_PROXY:
            os.environ.pop(p, None)

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
        sys.exit("[-] Куки невалидны или сессия истекла. Обновите cookies.json.")
    sk = re.search(r'"sk"\s*:\s*"([^"]+)"', r.text) or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', r.text)
    uid = re.search(r'"uid"\s*:\s*"?(\d+)"?', r.text) or re.search(r'"default_uid"\s*:\s*"?(\d+)"?', r.text)
    if not sk or not uid:
        sys.exit("[-] Не удалось извлечь sk/uid из HTML страницы Яндекс.Диска. Проверьте авторизацию.")
    return sk.group(1), uid.group(1)


# ── База данных и авто-сканирование списка файлов ───────────────────────────


def get_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=60.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


def init_db(db_path: Path):
    con = get_db(db_path)
    try:
        unlim_schema.migrate(con)
    finally:
        con.close()


def api_call(s: requests.Session, sk: str, uid: str, method: str, params: dict | None = None) -> dict:
    cid = f"{uid}{int(time.time() * 1000)}{random.randint(100, 999)}"
    body = {"sk": sk, "connection_id": cid, "apiMethod": method, "requestParams": params or {}}
    for att in range(5):
        try:
            r = s.post(f"{BASE_URL}/models-v2", params={"m": method}, json=body, timeout=60)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    return data
                raise RuntimeError(f"{method}: неожиданный формат: {type(data)}")
            if r.status_code in (401, 403):
                sys.exit(f"[-] Сессия истекла (HTTP {r.status_code}). Обновите cookies.json.")
            if r.status_code == 429:
                wait = 20 + att * 20
                print(f"[!] Превышен лимит запросов (HTTP 429), ожидание {wait}с...")
                time.sleep(wait)
                continue
        except Exception as e:
            time.sleep(2 ** att)
    raise RuntimeError(f"Ошибка вызова {method} после 5 попыток")


def populate_db_from_cloud(db_path: Path, s: requests.Session, sk: str, uid: str, force: bool = False):
    """Автоматически наполняет unlim_state.db списком файлов Безлимита через API."""
    if db_path.exists() and db_path.stat().st_size > 0 and not force:
        try:
            con = get_db(db_path)
            has_table = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='unlim_files'").fetchone()[0]
            if has_table:
                cnt = con.execute("SELECT count(*) FROM unlim_files").fetchone()[0]
                con.close()
                if cnt > 0:
                    print("\n" + "=" * 70)
                    print(f"  [!] База данных {db_path.name} уже присутствует на диске!")
                    print(f"  [!] В ней уже зарегистрировано {cnt:,} файлов Безлимита.")
                    print("  [!] Сканирование пропущено, используется существующая база данных.")
                    print("  [!] ЧТО ДЕЛАТЬ:")
                    print("      • Чтобы использовать готовую базу: просто запустите скачивание.")
                    print(f"      • Чтобы пересоздать базу заново с нуля: удалите файл {db_path.name}")
                    print("        вручную или запустите скрипт с флагом --force.")
                    print("=" * 70 + "\n", flush=True)
                    return
        except Exception:
            pass

    if force and db_path.exists():
        print(f"[!] Флаг --force: пересоздаём базу {db_path.name} заново с нуля...", flush=True)
        try:
            con = get_db(db_path)
            with con:
                con.execute("DROP TABLE IF EXISTS unlim_files")
                con.execute("DROP TABLE IF EXISTS unlim_clusters")
            con.close()
        except Exception:
            pass

    print("\n" + "=" * 70)
    print("  Начинаем сбор списка файлов Безлимита из облака...")
    print("=" * 70)

    init_db(db_path)
    con = get_db(db_path)

    print("[1/3] Инициализация слепка Безлимита (photo-init-snapshot)...", flush=True)
    res = api_call(s, sk, uid, "intapi/photo-init-snapshot")
    psid = res.get("photoslice_id")
    if not psid:
        sys.exit(f"[-] Не получен photoslice_id: {res}")

    print(f"[+] Получен слепок ID: {psid}", flush=True)
    print("[2/3] Загрузка списка кластеров дат...", flush=True)
    snap = api_call(s, sk, uid, "intapi/photo-get-snapshot", {"id": psid, "locale": "ru", "amount": 100000, "offset": 0})
    clusters = snap.get("items", [])
    if not clusters:
        sys.exit("[-] В Безлимите не найдено ни одного кластера файлов.")

    with con:
        for it in clusters:
            con.execute(
                "INSERT OR IGNORE INTO unlim_clusters (cluster_id, size) VALUES (?, ?)",
                (it.get("id"), int(it.get("size", 0) or 0)),
            )

    batch_size = 50
    total_clusters = len(clusters)
    est_minutes = round((total_clusters / batch_size * 1.3) / 60, 1)
    print(f"[+] Найдено {total_clusters} кластеров дат в Безлимите.", flush=True)
    print(f"[3/3] Сбор списков файлов в кластерах (расчётное время: ~{est_minutes} мин)...", flush=True)

    total_added = 0
    cur = con.cursor()
    fetched_clusters = 0

    while True:
        rows = list(cur.execute("SELECT cluster_id, size FROM unlim_clusters WHERE fetched = 0 LIMIT ?", (batch_size,)))
        if not rows:
            break

        clusters_map = {r["cluster_id"]: {"range": [0, max(int(r["size"] or 0) - 1, 0)]} for r in rows}
        res = api_call(s, sk, uid, "intapi/photo-get-clusters-with-resources", {
            "photosliceId": psid,
            "clusters": clusters_map,
            "hideScreenshots": False,
        })
        fetched = (res.get("clusters") or {}).get("fetched") or []

        with con:
            for cl in fetched:
                cid = cl.get("id")
                for f in cl.get("items", []) or []:
                    fid = f.get("id") or f.get("resource_id")
                    if not fid:
                        continue
                    name = f.get("name") or fid.rsplit("/", 1)[-1]
                    size = int(f.get("size", 0) or 0)
                    md5 = f.get("md5") or f.get("etag")
                    mime = f.get("mimetype") or f.get("type")
                    con.execute(
                        """INSERT INTO unlim_files (file_id, cluster_id, name, size, md5, mime, status)
                           VALUES (?, ?, ?, ?, ?, ?, 'pending')
                           ON CONFLICT(file_id) DO NOTHING""",
                        (fid, cid, name, size, md5, mime),
                    )
                    total_added += 1
                con.execute("UPDATE unlim_clusters SET fetched=1 WHERE cluster_id=?", (cid,))

        fetched_clusters += len(rows)
        pct = int(fetched_clusters / total_clusters * 100) if total_clusters else 0
        print(f"  [Кластеры: {fetched_clusters}/{total_clusters} ({pct}%)] Собрано файлов в базу: {total_added:,}...", flush=True)

    cnt_total = con.execute("SELECT count(*) FROM unlim_files").fetchone()[0]
    con.close()
    print(f"\n[✓] Сбор базы данных завершён! Всего в базе: {cnt_total:,} файлов (статус: pending).\n", flush=True)


def recover_stuck(db_path: Path):
    con = get_db(db_path)
    with con:
        con.execute("UPDATE unlim_files SET status='pending' WHERE status='in_progress'")
    con.close()


def reset_skipped_files(db_path: Path, media_type: str = "all") -> int:
    con = get_db(db_path)
    ext_cond = ""
    if media_type == "photos":
        conds = [f"name LIKE '%{ext}'" for ext in PHOTO_EXTS]
        ext_cond = f"AND ({' OR '.join(conds)})"
    elif media_type == "videos":
        conds = [f"name LIKE '%{ext}'" for ext in VIDEO_EXTS]
        ext_cond = f"AND ({' OR '.join(conds)})"

    with con:
        cur = con.cursor()
        cur.execute(f"UPDATE unlim_files SET status='pending', attempts=0, error=NULL WHERE status='skipped' {ext_cond}")
        count = cur.rowcount
    con.close()
    return count


def check_free_space(path_str: str, min_mb: int = 500) -> bool:
    try:
        usage = shutil.disk_usage(path_str)
        return (usage.free / (1024 * 1024)) >= min_mb
    except Exception:
        return True


# ── Выборка батчей ──────────────────────────────────────────────────────────


def take_batch(
    db_path: Path,
    batch_size: int,
    media_type: str = "all",
    target_status: str = "pending",
    from_end: bool = False,
) -> list[dict]:
    con = get_db(db_path)
    with con:
        cur = con.cursor()
        ext_filter = ""
        if media_type == "photos":
            conds = [f"name LIKE '%{ext}'" for ext in PHOTO_EXTS]
            ext_filter = f"AND ({' OR '.join(conds)})"
        elif media_type == "videos":
            conds = [f"name LIKE '%{ext}'" for ext in VIDEO_EXTS]
            ext_filter = f"AND ({' OR '.join(conds)})"

        order_clause = "attempts ASC, rowid DESC" if from_end else "attempts ASC, cluster_id ASC, file_id ASC"
        query = f"""
            SELECT file_id, name, size
            FROM unlim_files
            WHERE status = ? {ext_filter}
            ORDER BY {order_clause}
            LIMIT ?
        """
        rows = list(cur.execute(query, (target_status, batch_size)))
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


def mark_done(db_path: Path, file_id: str, local_path: str, size: int):
    con = get_db(db_path)
    with con:
        con.execute(
            """UPDATE unlim_files
               SET status='downloaded', local_path=?, size=?, downloaded_at=CURRENT_TIMESTAMP, error=NULL
               WHERE file_id=?""",
            (local_path, size, file_id),
        )
    con.close()


def mark_skipped(db_path: Path, file_id: str, error: str):
    con = get_db(db_path)
    with con:
        con.execute(
            """UPDATE unlim_files
               SET status='skipped', error=?, assigned_at=CURRENT_TIMESTAMP
               WHERE file_id=?""",
            (error[:300], file_id),
        )
    con.close()


# ── Потоковая загрузка одного файла ──────────────────────────────────────────


_thread_local = threading.local()


def get_thread_session(cookies: dict[str, str], use_proxy: bool = False, proxy_url: str = "") -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session(cookies, use_proxy=use_proxy, proxy_url=proxy_url)
    return _thread_local.session


def download_one_file(
    db_path: Path,
    cookies: dict[str, str],
    sk: str,
    uid: str,
    item: dict,
    out_dir: Path,
    use_proxy: bool = False,
    proxy_url: str = "",
    timeout: float = 7.0,
) -> tuple[bool, int, str]:
    fid = item["file_id"]
    name = item["name"]

    # Сессия уже протухла — не ходим в сеть, сразу сдаёмся (загрузка гасится наверху).
    if AUTH_EXPIRED.is_set():
        return False, 0, "session_expired"

    # Диск уже заполнен — не ходим в сеть, гасим загрузку целиком.
    if DISK_FULL.is_set():
        return False, 0, "disk_full"

    if not check_free_space(str(out_dir)):
        # Диск заполнен: не помечаем файл skipped (остаётся in_progress), а гасим
        # загрузку; recover_stuck вернёт его в pending при следующем старте.
        DISK_FULL.set()
        return False, 0, "disk_full"

    s = get_thread_session(cookies, use_proxy=use_proxy, proxy_url=proxy_url)

    # 1. Получаем прямую ссылку на оригинал через mpfs/url
    cid = f"{uid}{int(time.time() * 1000)}{random.randint(100, 999)}"
    body = {"sk": sk, "connection_id": cid, "apiMethod": "mpfs/url", "requestParams": {"path": fid}}

    try:
        r = s.post(f"{BASE_URL}/models-v2", params={"m": "mpfs/url"}, json=body, timeout=(3.0, timeout))
        if r.status_code != 200:
            if r.status_code in (401, 403):
                # Сессия Яндекса истекла — это не «сбойный файл», а протухшие cookies.
                # Не помечаем файл skipped (оставляем in_progress — recover_stuck вернёт
                # его в pending при следующем старте), ставим флаг и обрываем загрузку.
                AUTH_EXPIRED.set()
                return False, 0, f"session_expired: HTTP {r.status_code}"
            err = f"mpfs/url HTTP {r.status_code}"
            mark_skipped(db_path, fid, err)
            return False, 0, err
        data = r.json()
        download_url = data.get("file", "")
    except Exception as e:
        err = f"mpfs/url: {e}"
        mark_skipped(db_path, fid, err)
        return False, 0, err

    if not download_url:
        err = "Ссылка не найдена в ответе mpfs/url"
        mark_skipped(db_path, fid, err)
        return False, 0, err

    if download_url.startswith("//"):
        download_url = "https:" + download_url

    # 2. Локальный путь
    dst_path = out_dir / name
    if dst_path.exists():
        expected_size = item.get("size")
        if expected_size and dst_path.stat().st_size == expected_size:
            mark_done(db_path, fid, str(dst_path), dst_path.stat().st_size)
            return True, dst_path.stat().st_size, "Уже скачан"
        safe_fid = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', fid.strip("/"))
        dst_path = out_dir / f"{safe_fid}_{name}"

    tmp_path = dst_path.with_suffix(dst_path.suffix + ".part")
    bytes_downloaded = 0

    # 3. Скачивание (нулевые повторы)
    try:
        resp = s.get(download_url, stream=True, timeout=(3.0, timeout))
        if resp.status_code == 200:
            # Мёртвые шарды могут «сочиться» байтами, не превышая read-таймаут, и
            # вешать поток навсегда. Общий лимит на файл разрывает такое зависание:
            # файл помечается skipped и поток освобождается. 420 сек при --timeout 7
            # хватает даже на крупное видео (~2.5 ГБ при 6 МБ/с), но ограничивает
            # худший случай.
            deadline = time.monotonic() + max(120.0, timeout * 60.0)
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        _global_speed_meter.add(len(chunk))
                    if time.monotonic() > deadline:
                        raise TimeoutError("общий таймаут скачивания файла")
            tmp_path.replace(dst_path)
            mark_done(db_path, fid, str(dst_path), bytes_downloaded)
            return True, bytes_downloaded, "OK"
        else:
            if tmp_path.exists():
                try: tmp_path.unlink()
                except OSError: pass
            err = f"CDN HTTP {resp.status_code}"
            mark_skipped(db_path, fid, err)
            return False, 0, err
    except Exception as e:
        # Диск заполнился прямо во время записи (ENOSPC) — гасим загрузку целиком.
        if isinstance(e, OSError) and getattr(e, "errno", None) == errno.ENOSPC:
            DISK_FULL.set()
            return False, 0, "disk_full"
        if tmp_path.exists():
            try: tmp_path.unlink()
            except OSError: pass
        err = f"Download error: {e}"
        mark_skipped(db_path, fid, err)
        return False, 0, err


# ── Статус для Web UI ────────────────────────────────────────────────────────


def update_status_file(status_file: Path, active: bool, **kwargs):
    if not status_file:
        return
    data = {
        "active": active,
        "batch_idx": kwargs.get("batch_idx", 0),
        "batches_total": kwargs.get("batches_total", 0),
        "item_current": kwargs.get("item_current", 0),
        "items_total": kwargs.get("items_total", 0),
        "success": kwargs.get("success", 0),
        "skipped": kwargs.get("skipped", 0),
        "speed_mb": round(kwargs.get("mb_s", 0.0), 2),
        "speed_mbit": round(kwargs.get("mbit_s", 0.0), 1),
        "total_mb": round(kwargs.get("total_bytes", 0) / (1024 * 1024), 2),
        "error": kwargs.get("error"),
        "updated_at": time.time(),
    }
    try:
        tmp = status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(status_file)
    except Exception:
        pass


# ── Основной раннер ──────────────────────────────────────────────────────────


def run_backup(
    db_path: Path,
    cookies_path: Path,
    out_dir_path: Path,
    media_type: str = "photos",
    target_status: str = "pending",
    from_end: bool = False,
    workers: int = 10,
    timeout: float = 7.0,
    batch_size: int = 1000,
    batches_count: int = 0,
    use_proxy: bool = False,
    proxy_url: str = "http://127.0.0.1:10808",
    status_file: Path | None = None,
    sort_after: bool = False,
):
    out_dir = Path(out_dir_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  Yandex.Disk — Универсальный выгрузчик Безлимита (by PUH)")
    print("=" * 70)
    print(f"  База данных  : {db_path}")
    print(f"  Куки         : {cookies_path}")
    print(f"  Папка выгрузки: {out_dir}")
    print(f"  Контент      : {media_type.upper()} ({'Только фото' if media_type == 'photos' else 'Только видео' if media_type == 'videos' else 'Все медиа'})")
    if target_status == "all":
        queue_desc = "Сначала новые, затем пропущенные"
    else:
        queue_desc = "Новые файлы" if target_status == "pending" else "Повтор ранее пропущенных"
    print(f"  Очередь      : {target_status.upper()} ({queue_desc})")
    print(f"  Порядок      : {'С конца очереди (rowid DESC)' if from_end else 'С начала очереди (ASC)'}")
    print(f"  Потоков      : {workers}")
    print(f"  Таймаут      : {timeout} сек")
    print(f"  Размер батча : {batch_size} файлов")
    print(f"  Кол-во батчей: {'Все до конца' if batches_count <= 0 else batches_count}")
    print(f"  Прокси       : {proxy_url if use_proxy else 'Прямое соединение (без прокси)'}")
    print("=" * 70)

    # 1. Авторизация
    cookies = load_cookies(cookies_path)
    s = make_session(cookies, use_proxy=use_proxy, proxy_url=proxy_url)
    sk, uid = get_sk_uid(s)
    print(f"[+] Авторизация успешна. UID: {uid}, SK: {sk[:8]}...")

    # 2. Проверка базы данных
    if not db_path.exists() or db_path.stat().st_size == 0:
        print("[!] База данных не обнаружена. Запускаем автоматический сбор списка Безлимита...")
        populate_db_from_cloud(db_path, s, sk, uid)
    else:
        init_db(db_path)
        # Проверяем, есть ли записи в таблице unlim_files
        con = get_db(db_path)
        cnt = con.execute("SELECT count(*) FROM unlim_files").fetchone()[0]
        con.close()
        if cnt == 0:
            print("[!] Таблица unlim_files пуста. Запускаем сбор списка Безлимита...")
            populate_db_from_cloud(db_path, s, sk, uid)
        else:
            print(f"[+] База данных {db_path.name} уже присутствует на диске ({cnt:,} файлов). Используем её.")

    recover_stuck(db_path)
    AUTH_EXPIRED.clear()
    DISK_FULL.clear()

    batch_idx = 0
    total_downloaded_files = 0
    total_downloaded_bytes = 0
    total_skipped_files = 0
    fatal_error: str | None = None
    fatal_code: str | None = None

    # Режим «all»: сначала все новые (pending), затем одна повторная попытка для
    # ранее пропущенных (skipped → pending). Ошибки во второй фазе снова уходят в
    # skipped и больше не подбираются — повтор ограничен ровно одним проходом.
    phase = "pending" if target_status == "all" else target_status
    retry_phase_done = (target_status != "all")

    try:
        while True:
            if batches_count > 0 and batch_idx >= batches_count:
                print(f"\n[✓] Завершено заданное количество батчей ({batches_count}).")
                break

            items = take_batch(
                db_path,
                batch_size=batch_size,
                media_type=media_type,
                target_status=phase,
                from_end=from_end,
            )
            if not items:
                if not retry_phase_done:
                    n = reset_skipped_files(db_path, media_type)
                    retry_phase_done = True
                    if n > 0:
                        print(f"\n=== Фаза 2: повтор пропущенных ({n} файлов → pending) ===", flush=True)
                        continue
                    print("\n[✓] Пропущенных файлов нет — повтор не требуется.")
                print(f"\n[✓] Нет файлов в выборке (status='{phase}', media='{media_type}'). Обработка завершена!")
                break

            batch_idx += 1
            print(f"\n>>> Запуск батча #{batch_idx} ({len(items)} файлов)...")

            batch_start_time = time.perf_counter()
            batch_bytes = 0
            batch_success = 0
            batch_skipped = 0

            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_item = {
                    executor.submit(
                        download_one_file,
                        db_path,
                        cookies,
                        sk,
                        uid,
                        it,
                        out_dir,
                        use_proxy,
                        proxy_url,
                        timeout,
                    ): it
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

                    cur_mb, cur_mbit = _global_speed_meter.current_speed()
                    percent = (i / len(items)) * 100

                    if status_file:
                        update_status_file(
                            status_file,
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

            # Сессия истекла — обрываем загрузку целиком, не крутим очередь вхолостую.
            if AUTH_EXPIRED.is_set():
                fatal_error = "Сессия Яндекса истекла (HTTP 401/403). Обновите cookies.json и запустите загрузку заново."
                fatal_code = "cookies_expired"
                break

            # Диск заполнен/отключился — обрываем загрузку, остальное остаётся в pending.
            if DISK_FULL.is_set():
                fatal_error = "Недостаточно места на диске (или диск отключился). Загрузка остановлена; остальные файлы остались в очереди (pending). Освободите место и запустите заново."
                fatal_code = "disk_full"
                break

    finally:
        if status_file:
            update_status_file(status_file, active=False, error=fatal_code)

    if fatal_error:
        print()
        print("[!] " + fatal_error, flush=True)

    print("\n" + "=" * 70)
    print(f"ИТОГО В ЭТОМ ЗАПУСКЕ: {total_downloaded_files} скачано, {total_skipped_files} пропущено / {total_downloaded_bytes / (1024*1024*1024):.2f} GB")
    print(f"Файлы сохранены в: {out_dir}")
    print("=" * 70)

    # Галочка «разложить по датам сразу после скачивания»: запускаем organize.py
    # на уже скачанной папке. Быстрый режим (--quick) — без MD5, т.к. свежие файлы.
    if sort_after:
        script_dir = paths.APP_DIR
        organize_script = script_dir / "organize.py"
        print("\n" + "=" * 70)
        print("  Сортировка скачанного по датам (organize.py)…")
        print("=" * 70, flush=True)
        if organize_script.exists():
            rc = subprocess.run(
                [sys.executable, "-u", str(organize_script),
                 "--input", str(out_dir),
                 "--action", "move",
                 "--scheme", "type-year-month",
                 "--quick"],
                cwd=str(script_dir),
            ).returncode
            if rc == 0:
                print("\n[✓] Сортировка по датам завершена.", flush=True)
            else:
                print(f"\n[!] Сортировка завершилась с кодом {rc}.", flush=True)
        else:
            print(f"[!] Не найден organize.py рядом с {__file__}", flush=True)


def find_python_interpreter(script_dir: Path) -> str:
    # 1. Проверяем текущий интерпретатор
    try:
        import uvicorn, fastapi, dotenv  # type: ignore
        return sys.executable
    except ImportError:
        pass

    # 2. Ищем доступные virtualenv
    candidates = [
        script_dir / ".venv" / "bin" / "python",
        script_dir / ".venv" / "bin" / "python3",
        script_dir / ".venv" / "Scripts" / "python.exe",
        script_dir.parent / "yadisk_backup" / ".venv" / "bin" / "python",
        script_dir.parent / "yadisk_backup" / ".venv" / "bin" / "python3",
        script_dir.parent / "yadisk_backup" / ".venv" / "Scripts" / "python.exe",
        Path.home() / "SUNDRY" / "yadisk_backup" / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def main():
    script_dir = paths.APP_DIR

    parser = argparse.ArgumentParser(
        description="unlim_backup.py — Универсальный автономный выгрузчик Безлимита Яндекс.Диска (by PUH)"
    )
    parser.add_argument("--db", type=str, default=str(paths.UNLIM_DB_FILE), help="Путь к файлу unlim_state.db")
    parser.add_argument("--cookies", type=str, default=str(paths.COOKIES_FILE), help="Путь к cookies.json")
    parser.add_argument("--out-dir", type=str, default=str(paths.DEFAULT_UNLIM_DIR), help="Папка для сохранения файлов")
    parser.add_argument("--media", choices=["photos", "videos", "all"], default="photos", help="Тип контента: photos (по умолчанию), videos или all")
    parser.add_argument("--target", choices=["pending", "skipped", "all"], default="pending", help="Какую очередь обрабатывать: pending (новые), skipped (повтор сбойных) или all (сначала новые, затем сбойные)")
    parser.add_argument("--from-end", action="store_true", help="Скачивать с конца списка (старые файлы / photounlim)")
    parser.add_argument("--workers", type=int, default=10, help="Количество параллельных потоков (по умолчанию 10)")
    parser.add_argument("--timeout", type=float, default=7.0, help="Таймаут скачивания в секундах (по умолчанию 7.0)")
    parser.add_argument("--batch-size", type=int, default=1000, help="Размер пачки файлов (по умолчанию 1000)")
    parser.add_argument("--batches", type=int, default=0, help="Сколько батчей скачать (0 = все до конца)")
    parser.add_argument("--use-proxy", action="store_true", help="Скачивать через локальный прокси")
    parser.add_argument("--proxy-url", type=str, default="http://127.0.0.1:10808", help="URL прокси (по умолчанию 127.0.0.1:10808)")
    parser.add_argument("--reset-skipped", action="store_true", help="Сбросить все skipped файлы обратно в pending и выйти")
    parser.add_argument("--scan-only", action="store_true", help="Только сформировать/обновить базу данных из облака без скачивания файлов")
    parser.add_argument("--force", action="store_true", help="Принудительно пересоздать базу данных с нуля, даже если unlim_state.db уже существует на диске")
    parser.add_argument("--sort-after", action="store_true", help="После скачивания сразу разложить файлы по датам (organize.py, быстрый режим без MD5)")
    parser.add_argument("--web", action="store_true", help="Запустить веб-интерфейс дашборда (http://127.0.0.1:8000)")
    parser.add_argument("--cli", action="store_true", help="Принудительный запуск в консольном режиме без аргументов")

    # Если скрипт запущен вообще без параметров (например, двойной клик или python unlim_backup.py),
    # по умолчанию автоматически открываем Веб-Дашборд
    no_args = len(sys.argv) == 1
    args = parser.parse_args()

    if (args.web or no_args) and not args.cli:
        import subprocess
        webui_path = script_dir / "webui.py"
        if paths.IS_FROZEN or not webui_path.exists():
            print("[i] Веб-панель запускается основным исполняемым файлом приложения.")
            return
        python_exe = find_python_interpreter(script_dir)
        print("=" * 70)
        print("  Запуск веб-панели управления Yandex.Disk & Безлимит by PUH...")
        print("  Адрес в браузере: http://127.0.0.1:8000")
        print("=" * 70)
        try:
            subprocess.run([python_exe, str(webui_path)])
        except KeyboardInterrupt:
            pass
        return

    log_file = paths.UNLIM_LOG
    sys.stdout = TeeLogger(log_file, sys.stdout)
    sys.stderr = TeeLogger(log_file, sys.stderr)

    db_path = Path(args.db)
    cookies_path = Path(args.cookies)
    out_dir_path = Path(args.out_dir)

    if args.scan_only:
        cookies = load_cookies(cookies_path)
        s = make_session(cookies, use_proxy=args.use_proxy, proxy_url=args.proxy_url)
        sk, uid = get_sk_uid(s)
        populate_db_from_cloud(db_path, s, sk, uid, force=args.force)
        print("\n[✓] Сканирование облака успешно завершено! База unlim_state.db сформирована.")
        return

    if args.reset_skipped:
        if not db_path.exists():
            sys.exit(f"[-] База {db_path} не найдена.")
        cnt = reset_skipped_files(db_path, media_type=args.media)
        print(f"[✓] Успешно сброшено {cnt} файлов из 'skipped' в 'pending' (фильтр: {args.media}).")
        return

    status_file = paths.UNLIM_STATUS_FILE
    status_file.parent.mkdir(parents=True, exist_ok=True)

    run_backup(
        db_path=db_path,
        cookies_path=cookies_path,
        out_dir_path=out_dir_path,
        media_type=args.media,
        target_status=args.target,
        from_end=args.from_end,
        workers=args.workers,
        timeout=args.timeout,
        batch_size=args.batch_size,
        batches_count=args.batches,
        use_proxy=args.use_proxy,
        proxy_url=args.proxy_url,
        status_file=status_file,
        sort_after=args.sort_after,
    )


if __name__ == "__main__":
    main()
