"""Веб-дашборд для бэкапа Яндекс.Диска.

Запуск:
  python webui.py
  → http://localhost:8000

Что умеет:
  - Сохранить токен в .env через UI
  - Сохранить cookies.json через UI (для безлимита)
  - Запуск scan / download / verify / retry для обычного диска
  - Запуск collect / worker для photoslice (безлимита)
  - Запуск organize (сортировка по полочкам)
  - Live прогресс через polling
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv, set_key
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import backups
import diagnostics
import paths
import updater
import version
from utils import auto_workers, ram_info

def sqlite_ro_uri(db_path: str | os.PathLike) -> str:
    path = os.path.abspath(db_path)
    if sys.platform.startswith("win"):
        path = path.replace("\\", "/")
    return f"file:{path}?mode=ro"

IS_FROZEN = paths.IS_FROZEN
BUNDLE_DIR = paths.BUNDLE_DIR
PROJECT = paths.APP_DIR

ENV_FILE = paths.ENV_FILE
load_dotenv(ENV_FILE)

DB_FILE = paths.DB_FILE
UNLIM_DB_FILE = paths.UNLIM_DB_FILE
COOKIES_FILE = paths.COOKIES_FILE
COOKIES_FETCH_STATUS = paths.COOKIES_FETCH_STATUS
ACTION_LOG = paths.ACTION_LOG
TEMPLATES_DIR = BUNDLE_DIR / "webui_assets"


def log_action(endpoint: str, payload=None, extra: str = "") -> None:
    """Журнал запусков: какой эндпоинт и с какими параметрами дёрнули.

    Нужен, чтобы разбирать расхождения «нажал одно — запустилось другое»
    (например, браузер выполнил закэшированный старый JS).
    """
    try:
        if payload is not None and hasattr(payload, "model_dump"):
            payload = payload.model_dump()
        body = "" if payload is None else json.dumps(payload, ensure_ascii=False, default=str)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {endpoint}  {body}"
        if extra:
            line += f"  {extra}"
        ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ACTION_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_config() -> dict:
    """Перечитывает .env заново."""
    load_dotenv(ENV_FILE, override=True)
    return {
        "token": (os.getenv("YA_DISK_TOKEN", "") or "").strip(),
        "download_dir": (os.getenv("DOWNLOAD_DIR", str(paths.DEFAULT_DISK_DIR)) or "").strip(),
        "unlim_dir": (os.getenv("UNLIM_DIR", str(paths.DEFAULT_UNLIM_DIR)) or "").strip(),
        "disk_dir": (os.getenv("DISK_DIR", os.getenv("DOWNLOAD_DIR", str(paths.DEFAULT_DISK_DIR))) or "").strip(),
        "max_workers": int(os.getenv("MAX_WORKERS", "4") or "4"),
    }


def mask_token(token: str) -> str:
    """Маскирует токен для отображения: y0_xxxx****xxxx"""
    if not token or len(token) < 8:
        return ""
    if len(token) <= 12:
        return token[:3] + "***"
    return f"{token[:6]}…{token[-4:]} ({len(token)} симв.)"


def public_config(cfg: dict) -> dict:
    """Конфиг для браузера: сам токен наружу не отдаём, только маску."""
    result = {key: value for key, value in cfg.items() if key != "token"}
    result["token_masked"] = mask_token(cfg.get("token", ""))
    return result


app = FastAPI(title="Yandex.Disk Backup UI")


# ── CSRF защита: блокируем POST с чужих сайтов ─────────────────────────────

# Явный список нужен только для стандартного адреса. Любой другой локальный
# порт/хост разрешается ниже по принципу same-origin (Origin == Host).
_ALLOWED_ORIGINS = ("http://localhost:8000", "http://127.0.0.1:8000")


def origin_allowed(origin: str, host: str) -> bool:
    """Разрешает same-origin запросы на любом порту.

    Оконная обёртка может занять 8001/8002, если 8000 занят. Сравнение
    Origin с заголовком Host не зависит от номера порта и при этом
    по-прежнему блокирует чужие сайты.
    """
    if not origin:
        return True
    if origin.rstrip("/") in _ALLOWED_ORIGINS:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    return bool(host) and parts.netloc.lower() == host.lower()


@app.middleware("http")
async def csrf_guard(request, call_next):
    """Блокирует state-changing запросы с явно чужих сайтов (CSRF).
    GET-запросы пропускаем всегда — они не меняют состояние.
    """
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin") or ""
        sec_fetch_site = request.headers.get("sec-fetch-site") or ""
        if sec_fetch_site == "cross-site":
            return JSONResponse({"detail": "Forbidden cross-site request"}, status_code=403)
        # Блокируем только если Origin явно с чужого сайта
        if not origin_allowed(origin, request.headers.get("host") or ""):
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    return await call_next(request)


# ── Категории файлов ────────────────────────────────────────────────────────

CATEGORIES = {
    "photos": {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
               ".bmp", ".tiff", ".tif", ".raw", ".arw", ".cr2", ".nef",
               ".dng", ".orf"},
    "videos": {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".3gp", ".3g2",
               ".mpg", ".mpeg", ".wmv", ".flv", ".vob", ".mts", ".m2ts"},
    "audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac", ".wma",
              ".opus", ".aiff", ".alac"},
    "documents": {".doc", ".docx", ".pdf", ".txt", ".rtf", ".odt",
                  ".xls", ".xlsx", ".ods", ".csv",
                  ".ppt", ".pptx", ".odp"},
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
                 ".cab", ".tgz", ".tbz"},
    "programs": {".exe", ".dll", ".msi", ".bat", ".cmd", ".sys", ".com",
                 ".scr", ".vbs", ".ps1", ".sh", ".jar"},
}

CATEGORY_LABELS = {
    "photos": "Фото",
    "videos": "Видео",
    "audio": "Аудио",
    "documents": "Документы",
    "archives": "Архивы",
    "programs": "Программы",
    "other": "Прочее",
}


def categorize(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"


# ── Менеджер фоновых процессов ──────────────────────────────────────────────


class Task:
    def __init__(self, name: str, proc: subprocess.Popen):
        self.name = name
        self.proc = proc
        self.started_at = time.time()
        self.log: deque[str] = deque(maxlen=2000)
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self):
        try:
            for line in self.proc.stdout:  # type: ignore
                line = line.rstrip("\r\n")
                if line:
                    self.log.append(line)
        except Exception:
            pass
        finally:
            # Забираем код возврата, иначе на Linux/macOS остаётся зомби-процесс
            try:
                self.proc.wait(timeout=10)
            except Exception:
                pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self):
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, Task] = {}  # ключ — название/слот
        self._lock = threading.Lock()

    def start(self, slot: str, script: str, args: list[str]) -> tuple[bool, str]:
        """slot — логическое имя ('disk', 'photoslice_collect', 'worker-1', ...).
        Можно одновременно несколько задач в разных слотах.
        """
        with self._lock:
            existing = self._tasks.get(slot)
            if existing and existing.alive():
                return False, f"Уже идёт: {slot}"
            # Чистка зомби — если предыдущая задача в слоте умерла, закроем pipe и забудем
            if existing and not existing.alive():
                # Сначала дождёмся, что reader-тред допишет последние строки
                try:
                    existing._t.join(timeout=1.0)
                except Exception:
                    pass
                try:
                    if existing.proc.stdout:
                        existing.proc.stdout.close()
                except Exception:
                    pass
                del self._tasks[slot]
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            # Дочерние процессы обязаны писать в тот же каталог данных, даже если
            # приложение установлено в Program Files.
            env["YADISK_DATA_DIR"] = str(paths.DATA_DIR)
            if IS_FROZEN:
                # Тот же exe разбирает argv и запускает нужный скрипт.
                cmd = [sys.executable, script] + args
                # Program Files только для чтения; рабочий каталог — данные.
                work_dir = paths.DATA_DIR
            else:
                cmd = [sys.executable, "-u", script] + args
                work_dir = PROJECT
            proc = subprocess.Popen(
                cmd, cwd=str(work_dir),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
            )
            self._tasks[slot] = Task(slot, proc)
            return True, f"Запущено: {slot}"

    def stop(self, slot: str) -> tuple[bool, str]:
        with self._lock:
            t = self._tasks.get(slot)
            if not t or not t.alive():
                return False, f"Нет активной задачи {slot}"
            t.stop()
            return True, f"Остановлено: {slot}"

    def stop_all(self) -> int:
        n = 0
        with self._lock:
            for t in self._tasks.values():
                if t.alive():
                    t.stop()
                    n += 1
        return n

    def status(self) -> dict:
        out = {}
        with self._lock:
            for slot, t in self._tasks.items():
                out[slot] = {
                    "running": t.alive(),
                    "elapsed": time.time() - t.started_at,
                    "exit_code": t.proc.poll(),
                    "log_tail": list(t.log)[-500:],
                }
        return out


tm = TaskManager()


# ── Pipeline ────────────────────────────────────────────────────────────────


class Pipeline:
    """Автоматическая последовательность: scan → download диска → collect → workers безлимита.
    Сам мониторит окончание каждого шага и переходит к следующему.
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop = False
        self._owned_slots: set[str] = set()  # слоты которые pipeline сам запустил
        self.state: str = "idle"  # idle | running | done | failed
        self.step: str = "—"
        self.log: deque[str] = deque(maxlen=600)
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.skip_disk: bool = False
        self.skip_unlim: bool = False
        self.workers: int = 4

    def _own_start(self, slot: str, script: str, args: list[str]) -> tuple[bool, str]:
        """Запускает задачу через tm и помечает её как принадлежащую pipeline'у."""
        ok, msg = tm.start(slot, script, args)
        if ok:
            self._owned_slots.add(slot)
        return ok, msg

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"{ts}  {msg}")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, workers: int = 4, do_disk: bool = True, do_unlim: bool = True) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return False, "Pipeline уже идёт"
            self._stop = False
            self._owned_slots.clear()
            self.state = "running"
            self.step = "старт"
            self.log.clear()
            self.started_at = time.time()
            self.finished_at = None
            self.workers = max(1, min(workers, 6))
            self.skip_disk = not do_disk
            self.skip_unlim = not do_unlim
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, "Pipeline запущен"

    def stop(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "Pipeline не идёт"
        self._stop = True
        # Гасим ТОЛЬКО задачи которые pipeline сам запустил
        for slot in list(self._owned_slots):
            tm.stop(slot)
        self._log("⏹ остановлено пользователем")
        return True, "Остановлено"

    def _wait_task(self, slot: str, on_finish_msg: str) -> int:
        """Блокирующее ожидание завершения задачи в слоте."""
        while not self._stop:
            with tm._lock:
                t = tm._tasks.get(slot)
            if not t:
                time.sleep(0.5); continue
            if not t.alive():
                code = t.proc.poll()
                self._log(f"{on_finish_msg} (exit={code})")
                return code if code is not None else 0
            time.sleep(1.0)
        return -1

    def _run(self):
        try:
            cfg = get_config()

            # Шаг 0: токен
            self.step = "проверка токена"
            self._log("🔑 Проверяю токен...")
            tok = check_token(cfg["token"])
            if not tok.get("ok"):
                self._log(f"❌ Токен невалиден: {tok.get('msg')}")
                self.state = "failed"; return
            self._log(f"✓ Токен OK: {tok.get('login')}, {tok.get('used',0)/1e9:.1f} ГБ занято")

            # === ДИСК ===
            if not self.skip_disk:
                # scan
                if self._stop: self._abort(); return
                self.step = "сканирую диск"
                self._log("🔍 Запускаю scan...")
                ok, msg = self._own_start("disk", "main.py", ["scan"])
                if not ok:
                    self._log(f"❌ scan: {msg}"); self.state = "failed"; return
                self._wait_task("disk", "✓ scan завершён")
                if self._stop: self._abort(); return

                # download
                self.step = "качаю диск"
                self._log(f"⬇️ Запускаю download (workers={self.workers})...")
                ok, msg = self._own_start("disk", "main.py",
                                          ["download", "--workers", str(self.workers), "--no-scan"])
                if not ok:
                    self._log(f"❌ download: {msg}"); self.state = "failed"; return
                self._wait_task("disk", "✓ download диска завершён")
                if self._stop: self._abort(); return
            else:
                self._log("⏭ Шаг 'диск' пропущен")

            # === БЕЗЛИМИТ ===
            if not self.skip_unlim:
                cookies = check_cookies()
                if not cookies.get("ok"):
                    self._log(f"⚠️ Cookies не загружены, пропускаю безлимит: {cookies.get('msg')}")
                else:
                    # collect
                    if self._stop: self._abort(); return
                    self.step = "собираю список безлимита"
                    self._log("🗂 Запускаю unlim collect...")
                    ok, msg = self._own_start("unlim_collect", "unlim_download.py", ["--collect"])
                    if not ok:
                        self._log(f"❌ unlim collect: {msg}"); self.state = "failed"; return
                    self._wait_task("unlim_collect", "✓ список безлимита собран")
                    if self._stop: self._abort(); return

                    # workers
                    self.step = f"скачиваю безлимит ({self.workers} воркеров)"
                    self._log(f"🚀 Запускаю {self.workers} воркеров...")
                    for i in range(1, self.workers + 1):
                        ok, msg = self._own_start(f"unlim_w{i}", "unlim_worker.py", ["--id", str(i)])
                        if ok:
                            self._log(f"  ✓ воркер {i} запущен")
                        else:
                            self._log(f"  ✗ воркер {i}: {msg}")

                    # Ждём пока все воркеры закончат
                    while not self._stop:
                        any_alive = False
                        with tm._lock:
                            for slot, t in tm._tasks.items():
                                if slot.startswith("unlim_w") and t.alive():
                                    any_alive = True
                                    break
                        if not any_alive:
                            break
                        time.sleep(2)
                    self._log("✓ все воркеры безлимита завершили работу")
            else:
                self._log("⏭ Шаг 'безлимит' пропущен")

            self.state = "done"
            self.step = "финиш"
            self._log("🏆 ВСЁ ГОТОВО!")
        except Exception as e:
            self._log(f"❌ Ошибка pipeline: {e}")
            self.state = "failed"
        finally:
            self.finished_at = time.time()

    def _abort(self):
        self.state = "stopped"
        self.step = "остановлено"

    def status(self) -> dict:
        return {
            "state": self.state,
            "running": self.is_running(),
            "step": self.step,
            "started_at": self.started_at,
            "elapsed": (time.time() - self.started_at) if self.started_at and not self.finished_at else
                       ((self.finished_at - self.started_at) if self.started_at else None),
            "log": list(self.log),
        }


pipeline = Pipeline()


class FullCycle:
    """Полный цикл «Скачать всё»: найти новые файлы и докачать сбойные.

    Диск:     scan → download --no-scan --retry → ещё проходы по failed
    Безлимит: collect → --target all → ещё проходы по skipped

    Линии идут параллельно. В каждой — до ``passes`` проходов: после прохода
    смотрим, стало ли сбойных меньше; между проходами пауза PAUSE_AFTER_PASS.
    Итог по каждой линии считается по базе: сколько добавилось скачанных
    и сколько сбойных осталось.
    """

    PAUSE_AFTER_PASS = 60.0
    MAX_PASSES = 5

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop = False
        self._owned_slots: set[str] = set()
        self.state: str = "idle"  # idle | running | done | stopped | failed
        self.step: str = "—"
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.passes: int = 3
        self.workers: int = 10
        self.sort_after: bool = False
        self.log: deque[str] = deque(maxlen=600)
        self.lanes: dict[str, dict] = {
            "disk": self._empty_lane(),
            "unlim": self._empty_lane(),
        }
        self.summary: Optional[dict] = None

    @staticmethod
    def _empty_lane() -> dict:
        return {
            "state": "ожидание",
            "pass": 0,
            "passes": 0,
            "downloaded": 0,
            "bytes": 0,
            "errors": None,
            "error": None,
        }

    @staticmethod
    def _lane_title(name: str) -> str:
        return "Диск" if name == "disk" else "Безлимит"

    def _log(self, msg: str):
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, workers: int = 10, passes: int = 3,
              sort_after: bool = False) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return False, "Полный цикл уже идёт"
            if pipeline.is_running():
                return False, "Идёт полный прогон (pipeline)"
            with tm._lock:
                busy = [slot for slot, t in tm._tasks.items() if t.alive()]
            if busy:
                return False, f"Уже идёт другая задача: {', '.join(busy)}"

            self._stop = False
            self._owned_slots.clear()
            self.state = "running"
            self.step = "старт"
            self.started_at = time.time()
            self.finished_at = None
            self.passes = max(1, min(int(passes or 3), self.MAX_PASSES))
            self.workers = max(1, min(int(workers or 10), 24))
            self.sort_after = bool(sort_after)
            self.log.clear()
            self.lanes = {"disk": self._empty_lane(),
                          "unlim": self._empty_lane()}
            for lane in self.lanes.values():
                lane["passes"] = self.passes
            self.summary = None
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, f"Полный цикл запущен (до {self.passes} проходов по сбойным)"

    def stop(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "Полный цикл не идёт"
        self._stop = True
        for slot in list(self._owned_slots):
            tm.stop(slot)
        self._log("⏹ остановлено пользователем")
        return True, "Остановлено"

    # ── служебное ───────────────────────────────────────────────────────

    def _own_start(self, slot: str, script: str, args: list[str]):
        ok, msg = tm.start(slot, script, args)
        if ok:
            self._owned_slots.add(slot)
            return ok, msg
        raise RuntimeError(f"{msg}")

    def _wait_task(self, slot: str) -> int:
        while not self._stop:
            with tm._lock:
                t = tm._tasks.get(slot)
            if not t:
                time.sleep(0.2)
                continue
            if not t.alive():
                code = t.proc.poll()
                return code if code is not None else 0
            time.sleep(1.0)
        return -1

    def _pause(self, seconds: float):
        end = time.time() + seconds
        while not self._stop and time.time() < end:
            time.sleep(1.0)

    @staticmethod
    def _db_snapshot(db_file: Path, table: str,
                     error_statuses: tuple[str, ...]) -> dict:
        """Счётчики из базы: сколько скачано, сколько сбойных, объём скачанного."""
        if not db_file.exists():
            return {"downloaded": 0, "errors": 0, "bytes": 0}
        try:
            con = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
            try:
                out = {"downloaded": 0, "errors": 0, "bytes": 0}
                for status, n, size in con.execute(
                    f"SELECT status, COUNT(*), COALESCE(SUM(size), 0) "
                    f"FROM {table} GROUP BY status"
                ):
                    if status == "downloaded":
                        out["downloaded"] += n
                        out["bytes"] += size or 0
                    elif status in error_statuses:
                        out["errors"] += n
                return out
            finally:
                con.close()
        except sqlite3.Error as e:
            raise RuntimeError(f"Не удалось прочитать базу {db_file.name}: {e}")

    def _update_step(self):
        parts = []
        for name in ("disk", "unlim"):
            lane = self.lanes[name]
            parts.append(f"{self._lane_title(name)}: {lane.get('state') or 'ожидание'}")
        self.step = " · ".join(parts)

    def _build_summary(self) -> dict:
        duration = None
        if self.started_at and self.finished_at:
            duration = self.finished_at - self.started_at
        return {
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": duration,
            "lanes": {name: dict(lane) for name, lane in self.lanes.items()},
        }

    # ── линии ───────────────────────────────────────────────────────────

    def _run(self):
        try:
            disk_t = threading.Thread(target=self._lane_guard,
                                      args=("disk", self._disk_lane), daemon=True)
            unlim_t = threading.Thread(target=self._lane_guard,
                                       args=("unlim", self._unlim_lane), daemon=True)
            disk_t.start()
            unlim_t.start()
            disk_t.join()
            unlim_t.join()
            if self._stop:
                self.state = "stopped"
            elif any(lane["state"] == "error" for lane in self.lanes.values()):
                self.state = "failed"
            else:
                self.state = "done"
        except Exception as e:  # noqa: BLE001 — цикл не должен ронять сервер
            self.state = "failed"
            self._log(f"❌ Ошибка полного цикла: {e}")
        finally:
            self.finished_at = time.time()
            if self.state == "running":
                self.state = "done"
            self.summary = self._build_summary()
            self.step = "финиш"

    def _lane_guard(self, name: str, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            lane = self.lanes[name]
            lane["state"] = "error"
            lane["error"] = str(e)
            self._log(f"❌ {self._lane_title(name)}: {e}")

    def _disk_lane(self):
        lane = self.lanes["disk"]
        lane["state"] = "сканирование"
        self._update_step()
        self._log("🔍 Диск: сканирую облако (поиск новых файлов)…")
        self._own_start("disk", "main.py", ["scan"])
        code = self._wait_task("disk")
        if self._stop:
            lane["state"] = "остановлено"
            return
        if code != 0:
            raise RuntimeError(f"сканирование завершилось с кодом {code}")

        disk_workers = auto_workers()
        for p in range(1, self.passes + 1):
            if self._stop:
                lane["state"] = "остановлено"
                return
            lane["state"] = f"скачивание, проход {p}"
            lane["pass"] = p
            self._update_step()
            self._log(f"⬇️ Диск: проход {p}/{self.passes} — новые и сбойные файлы…")
            before = self._db_snapshot(DB_FILE, "files", ("failed",))
            self._own_start("disk", "main.py",
                            ["download", "--workers", str(disk_workers),
                             "--no-scan", "--retry"])
            code = self._wait_task("disk")
            if self._stop:
                lane["state"] = "остановлено"
                return
            after = self._db_snapshot(DB_FILE, "files", ("failed",))
            gained = max(0, after["downloaded"] - before["downloaded"])
            lane["downloaded"] += gained
            lane["bytes"] += max(0, after["bytes"] - before["bytes"])
            lane["errors"] = after["errors"]
            self._log(f"   Диск: +{gained} скачано, осталось сбойных {after['errors']}")
            if after["errors"] >= before["errors"]:
                self._log("   Диск: прогресса нет — проходы прекращены")
                break
            if p < self.passes:
                self._pause(self.PAUSE_AFTER_PASS)
        lane["state"] = "готово"

    def _unlim_lane(self):
        lane = self.lanes["unlim"]
        lane["state"] = "сбор списка"
        self._update_step()

        cookies = check_cookies()
        if not cookies.get("ok"):
            raise RuntimeError(f"cookies не загружены: {cookies.get('msg')}")

        cfg = get_config()
        out_dir = cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR)

        self._log("🗂 Безлимит: собираю список файлов из облака…")
        self._own_start("unlim_collect", "unlim_download.py", ["--collect"])
        code = self._wait_task("unlim_collect")
        if self._stop:
            lane["state"] = "остановлено"
            return
        if code != 0:
            raise RuntimeError(f"сбор списка завершился с кодом {code}")

        for p in range(1, self.passes + 1):
            if self._stop:
                lane["state"] = "остановлено"
                return
            lane["state"] = f"скачивание, проход {p}"
            lane["pass"] = p
            self._update_step()
            self._log(f"⬇️ Безлимит: проход {p}/{self.passes} — новые и пропущенные…")
            before = self._db_snapshot(UNLIM_DB_FILE, "unlim_files", ("skipped",))
            self._own_start("unlim", "unlim_backup.py", [
                "--out-dir", out_dir,
                "--media", "all",
                "--target", "all",
                "--workers", str(self.workers),
            ])
            code = self._wait_task("unlim")
            if self._stop:
                lane["state"] = "остановлено"
                return
            after = self._db_snapshot(UNLIM_DB_FILE, "unlim_files", ("skipped",))
            gained = max(0, after["downloaded"] - before["downloaded"])
            lane["downloaded"] += gained
            lane["bytes"] += max(0, after["bytes"] - before["bytes"])
            lane["errors"] = after["errors"]
            self._log(f"   Безлимит: +{gained} скачано, осталось сбойных {after['errors']}")
            if after["errors"] >= before["errors"]:
                self._log("   Безлимит: прогресса нет — проходы прекращены")
                break
            if p < self.passes:
                self._pause(self.PAUSE_AFTER_PASS)

        if self.sort_after and not self._stop:
            self._sort_unlim()
        lane["state"] = "готово"

    def _sort_unlim(self):
        cfg = get_config()
        out_dir = cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR)
        lane = self.lanes["unlim"]
        lane["state"] = "сортировка"
        self._update_step()
        self._log("🗂 Раскладываю скачанное по датам…")
        ok, msg = tm.start("organize", "organize.py", [
            "--input", out_dir,
            "--action", "move",
            "--scheme", "type-year-month",
            "--quick",
        ])
        if not ok:
            self._log(f"⚠ сортировка не запущена: {msg}")
            return
        self._owned_slots.add("organize")
        code = self._wait_task("organize")
        self._log(f"   сортировка завершена (код {code})")

    def status(self) -> dict:
        elapsed = None
        if self.started_at:
            end = self.finished_at if self.finished_at else time.time()
            elapsed = end - self.started_at
        return {
            "state": self.state,
            "running": self.is_running(),
            "step": self.step,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": elapsed,
            "passes": self.passes,
            "lanes": {name: dict(lane) for name, lane in self.lanes.items()},
            "summary": self.summary,
            "log": list(self.log),
        }


full_cycle = FullCycle()


# ── Обновление программы ─────────────────────────────────────────────────────

# Ключ удаления Inno Setup: {AppId}_is1. Один и тот же для system-wide
# (HKLM) и пер-пользовательской (HKCU) установки.
APP_UNINSTALL_KEY = "{8E4A3F42-6C1D-4A7E-9F2B-3544A1B2C3D4}_is1"
_UNINSTALL_BASES = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)

_install_dir_cache: dict[str, object] = {"at": -1e9, "locations": []}
_INSTALL_DIR_TTL = 60.0  # секунд: /api/status опрашивается каждые 1.5 сек


def _read_install_locations() -> list[Path]:
    """Все InstallLocation ключа удаления Inno Setup (HKCU/HKLM, 32 и 64 бита).

    Записей может быть две: прежняя установка «для всех» в Program Files и
    новая пер-пользовательская. Нужны обе — по одной свою копию от legacy
    не отличить.
    """
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover — Windows без winreg не бывает
        return []
    views = [getattr(winreg, "KEY_WOW64_64KEY", 0),
             getattr(winreg, "KEY_WOW64_32KEY", 0)]
    found: list[Path] = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for base in _UNINSTALL_BASES:
            for view in views:
                try:
                    with winreg.OpenKey(root, base + "\\" + APP_UNINSTALL_KEY,
                                        0, winreg.KEY_READ | view) as key:
                        value, _ = winreg.QueryValueEx(key, "InstallLocation")
                except OSError:
                    continue
                text = str(value or "").strip()
                if not text:
                    continue
                path = Path(text)
                if all(str(path).lower() != str(p).lower() for p in found):
                    found.append(path)
    return found


def installed_app_dirs(max_age: float = _INSTALL_DIR_TTL) -> list[Path]:
    """Папки установки из реестра, с кэшем на `max_age` секунд."""
    now = time.monotonic()
    if now - float(_install_dir_cache.get("at") or 0.0) < max_age:
        return list(_install_dir_cache.get("locations") or [])
    locations = _read_install_locations()
    _install_dir_cache["at"] = now
    _install_dir_cache["locations"] = locations
    return list(locations)


def installed_app_dir(max_age: float = _INSTALL_DIR_TTL) -> Optional[Path]:
    """Первая (текущая) папка установки — совместимый одиночный вариант."""
    locations = installed_app_dirs(max_age)
    return locations[0] if locations else None


def reset_install_dir_cache() -> None:
    _install_dir_cache["at"] = -1e9
    _install_dir_cache["locations"] = []


def _in_program_files(folder: Path) -> bool:
    """Папка внутри Program Files — признак старой установки «для всех»."""
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if not base:
            continue
        try:
            base_dir = Path(base).resolve()
        except OSError:
            continue
        if folder == base_dir or base_dir in folder.parents:
            return True
    return False


def _resolve(path: Path) -> Optional[Path]:
    try:
        return path.resolve()
    except OSError:
        return None


def _exe_dir() -> Optional[Path]:
    try:
        return _resolve(Path(sys.executable).parent)
    except (OSError, ValueError):
        return None


def legacy_install_dir() -> Optional[Path]:
    r"""Старая установка в Program Files, оставшаяся рядом с текущей копией.

    Установщик с 35.5.3 ставит программу в %LOCALAPPDATA%\Programs без прав
    администратора. Прежняя копия в Program Files сама не удалится — её нужно
    один раз снять вручную, иначе на машине живут две версии.
    """
    if os.name != "nt" or not paths.IS_FROZEN:
        return None
    exe_dir = _exe_dir()
    for location in installed_app_dirs():
        resolved = _resolve(location)
        if resolved is None or resolved == exe_dir:
            continue  # реестр указывает на нашу же папку — это не «старая» копия
        if resolved.is_dir() and _in_program_files(resolved):
            return resolved
    return None


def legacy_install_status() -> str:
    """Путь старой установки для /api/status (пустая строка — её нет)."""
    path = legacy_install_dir()
    return str(path) if path else ""


class UpdateManager:
    """Загрузка установщика последнего релиза с проверкой SHA-256.

    Сама ничего не устанавливает: install() пишет помощника PowerShell, который
    ждёт выхода приложения, запускает установщик и снова открывает программу.
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self.state = "idle"  # idle | checking | downloading | ready | error | installing
        self.version = ""
        self.percent = 0.0
        self.done_bytes = 0
        self.total_bytes = 0
        self.error = ""
        self.path: Optional[Path] = None
        self.kind = ""  # setup | portable

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @staticmethod
    def is_portable() -> bool:
        """Portable-сборку обновляем ZIP-архивом, установленную — setup.exe.

        Ориентир — реестр удаления Inno Setup: он есть и у пер-пользовательской
        установки в %LOCALAPPDATA%\\Programs (её нельзя путать с portable),
        и у старой установки в Program Files. Если записи нет и exe не в
        Program Files — это распакованный ZIP.
        """
        if not paths.IS_FROZEN:
            return False
        exe_dir = _exe_dir()
        if exe_dir is None:
            return False
        for location in installed_app_dirs():
            if _resolve(location) == exe_dir:
                return False
        return not _in_program_files(exe_dir)

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return False, "Загрузка обновления уже идёт"
            self._cancel.clear()
            self.state = "checking"
            self.version = ""
            self.percent = 0.0
            self.done_bytes = 0
            self.total_bytes = 0
            self.error = ""
            self.path = None
            self.kind = ""
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, "Проверяю последний релиз…"

    def cancel(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "Загрузка не идёт"
        self._cancel.set()
        return True, "Загрузка отменяется"

    def _progress(self, done: int, total: int):
        with self._lock:
            self.done_bytes = done
            if total:
                self.total_bytes = total
            if self.total_bytes:
                self.percent = min(100.0, done * 100.0 / self.total_bytes)

    def _run(self):
        try:
            release = updater.check_for_updates()
            if not release.get("ok"):
                raise RuntimeError(release.get("error") or "не удалось проверить обновления")
            if not release.get("update_available"):
                with self._lock:
                    self.state = "idle"
                    self.version = release.get("latest") or ""
                return
            portable = self.is_portable()
            asset = release.get("portable") if portable else release.get("installer")
            if not asset:
                raise RuntimeError("в релизе нет файла обновления")
            if not asset.get("sha256_url"):
                raise RuntimeError(
                    "у релиза нет контрольной суммы — скачайте установщик со страницы релиза"
                )
            expected = updater.fetch_sha256(asset["sha256_url"])
            dest = (Path(tempfile.gettempdir()) / "YandexDiskBackup_update" /
                    str(asset["name"]))
            with self._lock:
                self.state = "downloading"
                self.version = release.get("latest") or ""
                self.kind = "portable" if portable else "setup"
                self.total_bytes = int(asset.get("size") or 0)
                self.path = dest
            updater.download_asset(asset["url"], dest, expected,
                                   progress=self._progress, cancel=self._cancel)
            with self._lock:
                self.state = "ready"
                self.percent = 100.0
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self.state = "error"
                self.error = f"{type(e).__name__}: {e}"[:500]

    def status(self) -> dict:
        with self._lock:
            return {"state": self.state, "version": self.version,
                    "percent": round(self.percent, 1),
                    "done_bytes": self.done_bytes, "total_bytes": self.total_bytes,
                    "error": self.error, "kind": self.kind,
                    "running": self.is_running(),
                    "can_install": bool(paths.IS_FROZEN and os.name == "nt")}


update_manager = UpdateManager()

_shutdown_hook = None


def register_shutdown_hook(func) -> None:
    """launcher передаёт сюда свой quit_app, чтобы установка могла закрыть программу."""
    global _shutdown_hook
    _shutdown_hook = func


def _quit_application() -> None:
    hook = _shutdown_hook
    if hook is None:
        os._exit(0)
    try:
        hook()
    except Exception:  # noqa: BLE001
        os._exit(0)


def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def write_update_helper(payload: Path, kind: str, restart: bool = True) -> Path:
    """Помощник, который дождётся выхода приложения и поставит обновление."""
    work = Path(payload).parent
    work.mkdir(parents=True, exist_ok=True)
    helper = work / "install_update.ps1"
    log_file = work / "install_update.log"
    pid = os.getpid()
    exe = sys.executable
    if kind == "portable":
        action = (
            "try { Expand-Archive -LiteralPath " + _ps_quote(payload) +
            " -DestinationPath " + _ps_quote(Path(exe).parent.parent) +
            " -Force -ErrorAction Stop } "
            "catch { ('распаковка не удалась: ' + $_) | "
            "Out-File -Append -Encoding utf8 " + _ps_quote(log_file) + " }"
        )
    else:
        action = (
            "$setup = Start-Process -FilePath " + _ps_quote(payload) +
            " -ArgumentList '/SILENT','/CLOSEAPPLICATIONS','/NOCANCEL',"
            "'/SUPPRESSMSGBOXES' -Wait -PassThru; "
            "('код установщика: ' + $setup.ExitCode) | "
            "Out-File -Append -Encoding utf8 " + _ps_quote(log_file) + "; "
            # Молчаливый сбой обновления выглядел как «нажал — ничего не произошло»,
            # поэтому о ненулевом коде установщика сообщаем окном поверх остальных.
            "if ($setup.ExitCode -ne 0) { try { "
            "Add-Type -AssemblyName PresentationFramework; "
            "[System.Windows.MessageBox]::Show("
            "'Не удалось установить обновление (код ' + $setup.ExitCode + "
            "'). Программа осталась прежней версии.', "
            "'Yandex.Disk Backup') } catch { } }"
        )
    if kind == "portable":
        restart_lines = ["Start-Process -FilePath " + _ps_quote(exe)] if restart else []
    else:
        # Запускаем не старый путь, а свежепоставленную копию: если программа
        # была установлена «для всех» в Program Files, новая окажется в папке
        # пользователя, и старый exe запускать нельзя.
        uninstall_key = ("HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion"
                         "\\Uninstall\\" + APP_UNINSTALL_KEY)
        restart_lines = [
            "$loc = (Get-ItemProperty " + _ps_quote(uninstall_key) +
            " -ErrorAction SilentlyContinue).InstallLocation",
            "if ($loc) { $target = Join-Path $loc " + _ps_quote(Path(exe).name) +
            " } else { $target = " + _ps_quote(exe) + " }",
            "if (Test-Path $target) { Start-Process -FilePath $target } "
            "else { Start-Process -FilePath " + _ps_quote(exe) + " }",
        ] if restart else []
    lines = [
        "$ErrorActionPreference = 'Continue'",
        # След в логе: если помощник вообще не запустился, файла не будет —
        # это первое, что надо смотреть при «обновление не сработало».
        "('помощник запущен ' + (Get-Date -Format s)) | "
        "Out-File -Append -Encoding utf8 " + _ps_quote(log_file),
        "try { Wait-Process -Id " + str(pid) + " -Timeout 180 -ErrorAction Stop } catch { }",
        "Start-Sleep -Seconds 2",
        action,
        *restart_lines,
        "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force "
        "-ErrorAction SilentlyContinue",
    ]
    script = chr(10).join(line for line in lines if line) + chr(10)
    helper.write_text(script, encoding="utf-8-sig")
    return helper


def _free_gb(paths: list[str]) -> Optional[float]:
    """Минимум свободного места по указанным папкам (по одному разу на диск)."""
    frees: list[float] = []
    seen: set[str] = set()
    for p in paths:
        try:
            if not p or not os.path.exists(p):
                continue
            drive = os.path.splitdrive(os.path.abspath(p))[0].lower() or p
            if drive in seen:
                continue
            seen.add(drive)
            frees.append(shutil.disk_usage(p).free / (1024 ** 3))
        except Exception:
            pass
    return min(frees) if frees else None


class DiskWatchdog:
    """Сторож свободного места: останавливает выгрузку при падении ниже порога."""

    INTERVAL = 30.0

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop = False
        self.state = "idle"  # idle | watching | triggered | stopped
        self.threshold_gb: float = 50.0
        self.free_gb: Optional[float] = None
        self.message: str = ""
        self.started_at: Optional[float] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, threshold_gb: float) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return False, "Сторож уже включён"
            self.threshold_gb = max(1.0, float(threshold_gb))
            self._stop = False
            self.state = "watching"
            self.message = ""
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, f"Сторож включён (порог {self.threshold_gb:g} ГБ)"

    def stop(self) -> tuple[bool, str]:
        if not self.is_running():
            self.state = "idle"
            return False, "Сторож не работает"
        self._stop = True
        return True, "Сторож выключен"

    def _watched_paths(self) -> list[str]:
        cfg = get_config()
        return [cfg.get("unlim_dir") or "", cfg.get("disk_dir") or cfg.get("download_dir") or ""]

    def _run(self):
        while not self._stop:
            free = _free_gb(self._watched_paths())
            self.free_gb = round(free, 1) if free is not None else None
            if free is not None and free < self.threshold_gb:
                self._stop_everything(free)
                self.state = "triggered"
                self.message = (
                    f"Свободно {free:.1f} ГБ — меньше порога {self.threshold_gb:g} ГБ. "
                    f"Выгрузка остановлена."
                )
                return
            for _ in range(int(self.INTERVAL)):
                if self._stop:
                    break
                time.sleep(1)
        self.state = "stopped"

    def _stop_everything(self, free: float):
        full_cycle.stop()
        pipeline.stop()
        tm.stop_all()
        try:
            with ACTION_LOG.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  watchdog  "
                        f"free={free:.2f}GB threshold={self.threshold_gb:g}GB stop_all\n")
        except Exception:
            pass

    def status(self) -> dict:
        return {
            "state": self.state,
            "running": self.is_running(),
            "threshold_gb": self.threshold_gb,
            "free_gb": self.free_gb,
            "message": self.message,
            "started_at": self.started_at,
        }


watchdog = DiskWatchdog()


# ── Чтение состояния ────────────────────────────────────────────────────────


def db_stats(db_file: Path, table: str = "files",
             id_col: str = "remote_path") -> dict:
    if not db_file.exists():
        return {"total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
                "failed": 0, "in_progress": 0,
                "bytes_total": 0, "bytes_done": 0, "recent": []}
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        out = {"total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
               "failed": 0, "in_progress": 0,
               "bytes_total": 0, "bytes_done": 0}
        for r in c.execute(
            f"SELECT status, COUNT(*) AS n, COALESCE(SUM(size),0) AS b "
            f"FROM {table} GROUP BY status"
        ):
            st = r["status"]
            if st not in out:
                out[st] = 0
            out[st] = r["n"]
            out["total"] += r["n"]
            out["bytes_total"] += r["b"]
            if st in ("downloaded", "skipped"):
                out["bytes_done"] += r["b"]
        recent = list(c.execute(
            f"SELECT {id_col} AS path, status, size FROM {table} "
            f"WHERE downloaded_at IS NOT NULL "
            f"ORDER BY downloaded_at DESC LIMIT 10"
        ))
        out["recent"] = [{"path": r["path"], "status": r["status"],
                          "size": r["size"]} for r in recent]
        return out
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        if c is not None:
            c.close()


# Кэш для тяжёлых операций (polling /api/status каждые 1.5 сек)
_local_size_cache: dict[str, dict] = {}
# 60 сек, а не 10: обход папки — это диск, а по нему в это же время идёт
# скачивание. /api/status опрашивается каждые 1.5 сек, так что дёшево тут важно.
_LOCAL_SIZE_TTL = 60  # секунд


def local_size(path: str) -> dict:
    r"""Размер папки. С TTL-кэшем 10 сек.
    Защита от корня диска (C:\) — не обходим, иначе зависнет на десятках ГБ.
    """
    if not os.path.isdir(path):
        return {"files": 0, "bytes": 0}

    # Защита: если path это корень диска (C:\, D:\) — не считаем (займёт минуты)
    abs_path = os.path.abspath(path)
    if len(abs_path) <= 3 and abs_path.endswith(":\\"):
        return {"files": 0, "bytes": 0, "skipped": "корень диска"}

    cached = _local_size_cache.get(path)
    now = time.time()
    if cached and (now - cached["at"] < _LOCAL_SIZE_TTL):
        return cached["data"]

    n = 0; b = 0
    partial = False
    deadline = now + 2.0  # макс 2 секунды на обход
    try:
        for dp, _, fns in os.walk(path):
            if time.time() > deadline:
                partial = True  # не досчитали — честно скажем UI
                break
            for fn in fns:
                try:
                    b += os.path.getsize(os.path.join(dp, fn))
                    n += 1
                except OSError:
                    pass
    except OSError:
        pass

    data = {"files": n, "bytes": b, "partial": partial}
    # Ограничиваем размер кэша — храним максимум 4 пути
    if len(_local_size_cache) >= 4 and path not in _local_size_cache:
        # удалить самый старый
        oldest = min(_local_size_cache.items(), key=lambda x: x[1]["at"])[0]
        _local_size_cache.pop(oldest, None)
    _local_size_cache[path] = {"at": now, "data": data}
    return data


# Кэш для check_token: хранит только ОДИН токен (последний проверенный).
# Так не накапливаются старые токены в памяти.
_token_check_cache: dict = {"token": None, "at": 0, "data": None}
_TOKEN_TTL = 30  # секунд


def check_token(token: str) -> dict:
    if not token or token.startswith("AQAAA_paste"):
        return {"ok": False, "msg": "Токен не задан"}
    # Кэш — не дёргать Яндекс каждые 1.5 сек!
    now = time.time()
    if (_token_check_cache["token"] == token
            and now - _token_check_cache["at"] < _TOKEN_TTL):
        return _token_check_cache["data"]
    try:
        import yadisk
        cli = yadisk.Client(token=token)
        if not cli.check_token():
            cli.close()
            return {"ok": False, "msg": "Токен невалиден"}
        info = cli.get_disk_info()
        used = getattr(info, "used_space", 0) or 0
        total = getattr(info, "total_space", 0) or 0
        photo_unlim = getattr(info, "photounlim_size", 0) or 0
        login = getattr(info.user, "login", "?") if getattr(info, "user", None) else "?"
        cli.close()
        data = {"ok": True, "msg": "OK", "used": used, "total": total,
                "photo_unlim": photo_unlim, "login": login}
        _token_check_cache.update({"token": token, "at": now, "data": data})
        return data
    except Exception as e:
        data = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
        _token_check_cache.update({"token": token, "at": now, "data": data})
        return data


def check_cookies() -> dict:
    if not COOKIES_FILE.exists():
        return {"ok": False, "msg": "cookies.json не загружен", "count": 0}
    try:
        raw = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return {"ok": False, "msg": "Формат не Cookie-Editor", "count": 0}
        names = {c.get("name") for c in raw if isinstance(c, dict)}
        important = ["Session_id", "yandexuid", "sessionid2"]
        missing = [n for n in important if n not in names]
        if missing:
            return {"ok": False,
                    "msg": f"Не хватает: {', '.join(missing)}. "
                           f"Залогинься в браузере и заэкспортируй заново.",
                    "count": len(raw)}
        return {"ok": True, "msg": f"Куки OK ({len(raw)} шт)", "count": len(raw)}
    except Exception as e:
        return {"ok": False, "msg": f"Ошибка: {e}", "count": 0}


def cookies_fetch_status() -> dict:
    """Статус полуавтоматического обновления кук (fetch_cookies.py)."""
    data: dict = {"state": "idle", "msg": ""}
    if COOKIES_FETCH_STATUS.exists():
        try:
            data = json.loads(COOKIES_FETCH_STATUS.read_text(encoding="utf-8"))
        except Exception:
            return {"state": "unknown", "msg": "Ошибка чтения статуса"}
    # Если файл говорит «running», но сам процесс уже не живой — он оборвался
    # (отмена/крах), иначе UI висел бы в ожидании входа бесконечно.
    if data.get("state") == "running":
        task = tm.status().get("cookies_fetch")
        if task is not None and not task.get("running"):
            data = {"state": "stopped", "msg": "Процесс завершён",
                    "updated_at": data.get("updated_at")}
    return data


# ── Endpoints ───────────────────────────────────────────────────────────────


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    html_path = TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>UI не установлен</h1>")
    # no-store: иначе браузер может выполнять закэшированный старый JS
    # и слать не те запросы, которые ожидает пользователь.
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def unlim_db_summary() -> dict:
    db_file = UNLIM_DB_FILE
    if not db_file.exists() or db_file.stat().st_size == 0:
        return {"exists": False, "total_files": 0, "clusters_total": 0, "clusters_unfetched": 0, "is_ready": False, "size_mb": 0}
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=5)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        total_files = 0
        clusters_total = 0
        clusters_unfetched = 0
        if "unlim_files" in tables:
            total_files = c.execute("SELECT count(*) FROM unlim_files").fetchone()[0]
        if "unlim_clusters" in tables:
            clusters_total = c.execute("SELECT count(*) FROM unlim_clusters").fetchone()[0]
            clusters_unfetched = c.execute("SELECT count(*) FROM unlim_clusters WHERE fetched = 0").fetchone()[0]
        c.close()
        is_ready = (total_files > 0 and (clusters_total == 0 or clusters_unfetched == 0))
        return {
            "exists": True,
            "total_files": total_files,
            "clusters_total": clusters_total,
            "clusters_unfetched": clusters_unfetched,
            "is_ready": is_ready,
            "size_mb": round(db_file.stat().st_size / (1024 * 1024), 2),
        }
    except Exception as e:
        return {"exists": True, "error": str(e), "total_files": 0, "is_ready": False}


def get_last_unlim_log(max_lines: int = 300) -> list[str]:
    log_file = paths.UNLIM_LOG
    if not log_file.exists():
        return []
    try:
        # Читаем только хвост файла (последние 256 КБ), а не весь лог целиком:
        # иначе /api/status (опрашивается каждые 1.5 сек) виснет на сотнях МБ.
        with log_file.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 256 * 1024)
            f.seek(size - read_size)
            tail = f.read(read_size).decode("utf-8", errors="replace")
        lines = tail.splitlines()
        return [l.strip() for l in lines if l.strip()][-max_lines:]
    except Exception:
        return []


@app.get("/api/status")
def api_status():
    cfg = get_config()
    status_file = paths.UNLIM_STATUS_FILE
    download_status = {}
    download_error = None
    if status_file.exists():
        try:
            st = json.loads(status_file.read_text(encoding="utf-8"))
            download_error = st.get("error")
            # Отдаём целиком, без фильтра свежести: UI сам решает по состоянию задачи,
            # чтобы даже при зависшей сессии был виден последний известный счётчик.
            download_status = st
        except Exception:
            pass

    # Пер-сессионный счётчик обычного диска (скачивание через main.py/downloader.py)
    disk_status = {}
    disk_status_file = paths.DISK_STATUS_FILE
    if disk_status_file.exists():
        try:
            disk_status = json.loads(disk_status_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "config": public_config(cfg),
        "token": check_token(cfg["token"]),
        "cookies": check_cookies(),
        "disk": {
            "db": db_stats(DB_FILE, "files", "remote_path"),
            "local": local_size(cfg.get("disk_dir") or cfg.get("download_dir") or str(paths.DEFAULT_DISK_DIR)),
        },
        "unlim": {
            "db": db_stats(UNLIM_DB_FILE, "unlim_files", "file_id"),
            "local": local_size(cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR)),
            "summary": unlim_db_summary(),
        },
        "ram": ram_info(),
        "full_cycle": full_cycle.status(),
        "update": update_manager.status(),
        "legacy_install": legacy_install_status(),
        "watchdog": watchdog.status(),
        "tasks": tm.status(),
        "pipeline": pipeline.status(),
        "speed": download_status,
        "disk_speed": disk_status,
        "download_error": download_error,
        "cookies_fetch": cookies_fetch_status(),
        "last_unlim_log": get_last_unlim_log(),
    }


class StartPayload(BaseModel):
    workers: int = Field(default=4, ge=1, le=6)
    do_disk: bool = True
    do_unlim: bool = True


@app.post("/api/start")
def api_start(payload: StartPayload):
    ok, msg = pipeline.start(workers=payload.workers,
                              do_disk=payload.do_disk,
                              do_unlim=payload.do_unlim)
    return {"ok": ok, "msg": msg}


@app.post("/api/start/stop")
def api_start_stop():
    ok, msg = pipeline.stop()
    return {"ok": ok, "msg": msg}


class DownloadControlPayload(BaseModel):
    mode: str = "unlim"  # "unlim" or "disk"
    media: str = "photos"  # "photos", "videos", "all"
    target: str = "pending"  # "pending", "skipped"
    workers: int = 10
    batch_size: int = 1000
    batches: int = 0
    timeout: float = 7.0
    from_end: bool = True
    sort_after: bool = False  # после скачивания Безлимита сразу разложить по датам


@app.post("/api/download/start")
def api_download_start(payload: DownloadControlPayload):
    log_action("/api/download/start", payload)
    cfg = get_config()
    if payload.mode == "unlim":
        out_dir = cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR)
        args = [
            "--out-dir", out_dir,
            "--media", payload.media,
            "--target", payload.target,
            "--workers", str(payload.workers),
            "--timeout", str(payload.timeout),
            "--batch-size", str(payload.batch_size),
            "--batches", str(payload.batches),
        ]
        if payload.from_end:
            args.append("--from-end")
        if payload.sort_after:
            args.append("--sort-after")
        ok, msg = tm.start("unlim", "unlim_backup.py", args)
        return {"ok": ok, "msg": msg, "mode": "unlim"}
    else:
        out_dir = cfg.get("disk_dir") or cfg.get("download_dir") or str(paths.DEFAULT_DISK_DIR)
        args = ["download", "--workers", str(auto_workers())]
        if DB_FILE.exists():
            args.append("--no-scan")
        ok, msg = tm.start("disk", "main.py", args)
        return {"ok": ok, "msg": msg, "mode": "disk"}


class DownloadAllPayload(BaseModel):
    workers: int = 10
    passes: int = 3  # сколько проходов по сбойным делать за запуск (1–5)
    sort_after: bool = False  # после скачивания Безлимита сразу разложить по датам


@app.post("/api/download/all")
def api_download_all(payload: DownloadAllPayload):
    """Полный цикл: проверить облако, скачать новые и докачать сбойные."""
    log_action("/api/download/all", payload)
    ok, msg = full_cycle.start(payload.workers, payload.passes, payload.sort_after)
    return {"ok": ok, "msg": msg, "state": full_cycle.state}


class WatchdogPayload(BaseModel):
    threshold_gb: float = 50.0


@app.post("/api/watchdog/start")
def api_watchdog_start(payload: WatchdogPayload):
    log_action("/api/watchdog/start", payload)
    ok, msg = watchdog.start(payload.threshold_gb)
    return {"ok": ok, "msg": msg}


@app.post("/api/watchdog/stop")
def api_watchdog_stop():
    ok, msg = watchdog.stop()
    return {"ok": ok, "msg": msg}


# ── Сброс статусов обратно в очередь ────────────────────────────────────────
def _any_task_running(prefixes: tuple[str, ...]) -> bool:
    try:
        for slot, info in tm.status().items():
            if isinstance(info, dict) and info.get("running") and slot.startswith(prefixes):
                return True
    except Exception:
        pass
    return False


class ResetPayload(BaseModel):
    what: str  # disk_failed | unlim_skipped | unlim_stuck


@app.post("/api/reset")
def api_reset(payload: ResetPayload):
    """Возвращает файлы из failed/skipped/in_progress обратно в pending.

    Меняются только статусы в БД — уже скачанные файлы на диске не трогаются.
    Пока идёт соответствующая выгрузка, сброс запрещён: иначе воркеры могут
    взять один и тот же файл дважды.
    """
    log_action("/api/reset", payload)
    what = payload.what

    if what == "disk_failed":
        if _any_task_running(("disk",)):
            return {"ok": False, "msg": "Сначала остановите выгрузку Диска"}
        db_file, sql = DB_FILE, (
            "UPDATE files SET status='pending', error=NULL WHERE status='failed'"
        )
        done = "ошибочных файлов Диска возвращено в очередь"
    elif what == "unlim_skipped":
        if _any_task_running(("unlim",)):
            return {"ok": False, "msg": "Сначала остановите выгрузку Безлимита"}
        db_file, sql = UNLIM_DB_FILE, (
            "UPDATE unlim_files SET status='pending', attempts=0, error=NULL "
            "WHERE status='skipped'"
        )
        done = "пропущенных файлов Безлимита возвращено в очередь"
    elif what == "unlim_stuck":
        if _any_task_running(("unlim",)):
            return {"ok": False, "msg": "Сначала остановите выгрузку Безлимита"}
        db_file, sql = UNLIM_DB_FILE, (
            "UPDATE unlim_files SET status='pending' WHERE status='in_progress'"
        )
        done = "зависших файлов Безлимита возвращено в очередь"
    else:
        return {"ok": False, "msg": f"Неизвестное действие: {what}"}

    if not Path(db_file).exists():
        return {"ok": False, "msg": "База данных не найдена"}

    con = None
    try:
        con = sqlite3.connect(str(db_file), timeout=30)
        with con:
            count = con.execute(sql).rowcount
        return {"ok": True, "count": count, "msg": f"Сброшено {count}: {done}"}
    except sqlite3.Error as e:
        return {"ok": False, "msg": f"Ошибка БД: {e}"}
    finally:
        if con is not None:
            con.close()


@app.post("/api/download/stop")
def api_download_stop():
    full_cycle.stop()
    n = tm.stop_all()
    pipeline.stop()
    return {"ok": True, "stopped": n}


class TokenPayload(BaseModel):
    token: str


@app.post("/api/config/token/check")
def api_check_token(payload: TokenPayload):
    """Проверка без сохранения."""
    return check_token((payload.token or "").strip())


@app.post("/api/config/token")
def api_set_token(payload: TokenPayload):
    token = (payload.token or "").strip()
    if not token:
        raise HTTPException(400, "Пустой токен")
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE), "YA_DISK_TOKEN", token, quote_mode="never")
    load_dotenv(ENV_FILE, override=True)
    return {"ok": True, "check": check_token(token)}


class DownloadDirPayload(BaseModel):
    download_dir: str


@app.post("/api/config/download_dir")
def api_set_dir(payload: DownloadDirPayload):
    d = (payload.download_dir or "").strip()
    if not d:
        raise HTTPException(400, "Пустой путь")
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE), "DOWNLOAD_DIR", d, quote_mode="never")
    set_key(str(ENV_FILE), "DISK_DIR", d, quote_mode="never")
    load_dotenv(ENV_FILE, override=True)
    return {"ok": True, "download_dir": d}


class DirsPayload(BaseModel):
    unlim_dir: str = ""
    disk_dir: str = ""


@app.post("/api/config/dirs")
def api_set_dirs(payload: DirsPayload):
    u = (payload.unlim_dir or "").strip()
    d = (payload.disk_dir or "").strip()
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    if u:
        set_key(str(ENV_FILE), "UNLIM_DIR", u, quote_mode="never")
    if d:
        set_key(str(ENV_FILE), "DISK_DIR", d, quote_mode="never")
        set_key(str(ENV_FILE), "DOWNLOAD_DIR", d, quote_mode="never")
    load_dotenv(ENV_FILE, override=True)
    return {"ok": True, "unlim_dir": u, "disk_dir": d}


class BrowseDirPayload(BaseModel):
    initial: str = ""


def pick_folder_os(initial_dir: str = "") -> str | None:
    # 1. Linux: kdialog
    if shutil.which("kdialog"):
        try:
            cmd = ["kdialog", "--getexistingdirectory"]
            if initial_dir and os.path.exists(initial_dir):
                cmd.append(initial_dir)
            cmd.extend(["--title", "Выберите папку для сохранения"])
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            p = r.stdout.strip()
            if p:
                return p
        except Exception:
            pass

    # 2. Linux: zenity
    if shutil.which("zenity"):
        try:
            cmd = ["zenity", "--file-selection", "--directory", "--title=Выберите папку для сохранения"]
            if initial_dir and os.path.exists(initial_dir):
                cmd.append(f"--filename={initial_dir}/")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            p = r.stdout.strip()
            if p:
                return p
        except Exception:
            pass

    # 3. macOS: osascript
    if sys.platform == "darwin":
        try:
            cmd = ["osascript", "-e", 'POSIX path of (choose folder with prompt "Выберите папку для сохранения:")']
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            p = r.stdout.strip()
            if p:
                return p
        except Exception:
            pass

    # 4. Windows: PowerShell FolderBrowserDialog
    if sys.platform.startswith("win"):
        try:
            # Показываем диалог поверх всех окон: невидимая TopMost-форма-владелец,
            # иначе FolderBrowserDialog открывается позади браузера.
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$o=New-Object System.Windows.Forms.Form;"
                "$o.TopMost=$true;"
                "$o.ShowInTaskbar=$false;"
                "$o.FormBorderStyle='None';"
                "$o.StartPosition='CenterScreen';"
                "$o.Width=1;"
                "$o.Height=1;"
                "$o.Show();"
                "$o.Activate();"
                "[System.Windows.Forms.Application]::DoEvents();"
                "Start-Sleep -Milliseconds 150;"
                "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
                "$f.Description='Выберите папку для сохранения';"
                "$f.ShowNewFolderButton=$true;"
                + (f"$f.SelectedPath='{initial_dir}';" if initial_dir else "") +
                "if($f.ShowDialog($o) -eq 'OK'){$f.SelectedPath};"
                "$o.Close();"
            )
            cmd = ["powershell", "-NoProfile", "-Command", ps_script]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            p = r.stdout.strip()
            if p:
                return p
        except Exception:
            pass

    return None


@app.post("/api/browse_dir")
def api_browse_dir(payload: BrowseDirPayload):
    chosen = pick_folder_os(payload.initial)
    if chosen:
        return {"ok": True, "path": chosen}
    return {"ok": False, "msg": "Выбор отменён"}


class CookiesPayload(BaseModel):
    cookies: list


def _validate_cookies(arr: list) -> dict:
    if not isinstance(arr, list):
        return {"ok": False, "msg": "Должен быть массив"}
    names = {c.get("name") for c in arr if isinstance(c, dict)}
    important = ["Session_id", "yandexuid", "sessionid2"]
    missing = [n for n in important if n not in names]
    if missing:
        return {"ok": False,
                "msg": f"Не хватает кук: {', '.join(missing)}. "
                       f"Залогинься в браузере на disk.yandex.ru и заэкспортируй заново."}
    return {"ok": True, "msg": f"Куки готовы ({len(arr)} шт)"}


@app.post("/api/cookies/check")
def api_check_cookies(payload: CookiesPayload):
    """Проверка без сохранения."""
    return _validate_cookies(payload.cookies)


@app.post("/api/cookies")
def api_set_cookies(payload: CookiesPayload):
    """Сохраняет cookies.json атомарно через tmp+replace."""
    arr = payload.cookies
    tmp = COOKIES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, COOKIES_FILE)
    return {"ok": True, "check": check_cookies(), "saved_count": len(arr)}


@app.post("/api/cookies/clear")
def api_clear_cookies():
    if COOKIES_FILE.exists():
        try:
            COOKIES_FILE.unlink()
        except OSError as e:
            return {"ok": False, "msg": str(e)}
    return {"ok": True}


@app.post("/api/cookies/fetch")
def api_cookies_fetch():
    """Запускает полуавтоматическое обновление кук через Playwright."""
    st = tm.status().get("cookies_fetch")
    if st and st.get("running"):
        return {"ok": False, "msg": "Вход уже идёт"}
    ok, msg = tm.start("cookies_fetch", "fetch_cookies.py", [])
    return {"ok": ok, "msg": msg}


@app.post("/api/cookies/fetch/cancel")
def api_cookies_fetch_cancel():
    ok, msg = tm.stop("cookies_fetch")
    # Явно помечаем статус, чтобы UI сразу показал «отменено»
    try:
        COOKIES_FETCH_STATUS.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_FETCH_STATUS.write_text(
            json.dumps({"state": "cancelled", "msg": "Отменено пользователем",
                        "updated_at": time.time()}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    return {"ok": ok, "msg": msg}


@app.post("/api/config/token/clear")
def api_clear_token():
    if ENV_FILE.exists():
        set_key(str(ENV_FILE), "YA_DISK_TOKEN", "", quote_mode="never")
        load_dotenv(ENV_FILE, override=True)
    return {"ok": True}


def preview_db(db_file: Path, table: str, id_col: str) -> dict:
    """Группирует файлы в БД по категориям."""
    if not db_file.exists():
        return {"empty": True, "categories": {}}
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        summary: dict[str, dict] = {
            k: {"count": 0, "bytes": 0, "label": CATEGORY_LABELS[k]}
            for k in list(CATEGORY_LABELS.keys())
        }
        total = 0
        for row in c.execute(
            f"SELECT {id_col}, COALESCE(size, 0) FROM {table} "
            f"WHERE status IN ('pending','downloaded','skipped','in_progress','failed')"
        ):
            cat = categorize(row[0])
            summary[cat]["count"] += 1
            summary[cat]["bytes"] += row[1]
            total += 1
        return {"empty": total == 0, "total": total, "categories": summary}
    except sqlite3.Error as e:
        return {"empty": True, "error": str(e)}
    finally:
        if c is not None:
            c.close()


def filter_to_categories(db_file: Path, table: str, id_col: str,
                          selected: list[str]) -> tuple[int, int]:
    """Помечает файлы вне выбранных категорий как 'skipped' (одним проходом).
    Уже скачанные/skipped/in_progress не трогаем.
    Возвращает (skipped_count, kept_pending).
    """
    if not db_file.exists():
        return (0, 0)
    sel = set(selected)
    c = sqlite3.connect(db_file, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        rows = c.execute(
            f"SELECT {id_col} FROM {table} WHERE status='pending'"
        ).fetchall()
        to_skip = []
        kept = 0
        for (path,) in rows:
            if categorize(path) in sel:
                kept += 1
            else:
                to_skip.append(path)
        if to_skip:
            c.execute("BEGIN")
            try:
                c.executemany(
                    f"UPDATE {table} SET status='skipped', "
                    f"error='not in selected categories' WHERE {id_col}=?",
                    [(p,) for p in to_skip],
                )
                c.execute("COMMIT")
            except Exception:
                try: c.execute("ROLLBACK")
                except sqlite3.Error: pass
                raise
        return (len(to_skip), kept)
    finally:
        c.close()


def unskip_category_marker(db_file: Path, table: str) -> int:
    """Сбрасывает skipped-with-marker обратно в pending — на случай повторного выбора."""
    if not db_file.exists():
        return 0
    c = sqlite3.connect(db_file, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.execute("BEGIN")
        try:
            n = c.execute(
                f"UPDATE {table} SET status='pending', error=NULL "
                f"WHERE status='skipped' AND error='not in selected categories'"
            ).rowcount
            c.execute("COMMIT")
            return n
        except Exception:
            try: c.execute("ROLLBACK")
            except sqlite3.Error: pass
            raise
    finally:
        c.close()


@app.get("/api/disk/preview")
def disk_preview():
    return preview_db(DB_FILE, "files", "remote_path")


@app.get("/api/unlim/preview")
def unlim_preview():
    return preview_db(UNLIM_DB_FILE, "unlim_files", "file_id")


class SelectivePayload(BaseModel):
    categories: list[str]
    workers: int = Field(default=4, ge=1, le=6)


@app.post("/api/disk/download_selected")
def disk_download_selected(payload: SelectivePayload):
    # Сначала вернуть всё что было ранее помечено как "не выбрано" в pending
    unskip_category_marker(DB_FILE, "files")
    # Потом отфильтровать заново
    filter_to_categories(DB_FILE, "files", "remote_path", payload.categories)
    # И запустить download
    workers = auto_workers()
    ok, msg = tm.start("disk", "main.py",
                       ["download", "--workers", str(workers), "--no-scan"])
    return {"ok": ok, "msg": msg}


@app.post("/api/unlim/download_selected")
def unlim_download_selected(payload: SelectivePayload):
    unskip_category_marker(UNLIM_DB_FILE, "unlim_files")
    filter_to_categories(UNLIM_DB_FILE, "unlim_files", "file_id",
                         payload.categories)
    n = max(1, min(payload.workers, 6))
    started = []
    failed = []
    for i in range(1, n + 1):
        ok, msg = tm.start(f"unlim_w{i}", "unlim_worker.py", ["--id", str(i)])
        if ok:
            started.append(i)
        else:
            failed.append({"id": i, "msg": msg})
    return {"ok": True, "started": started, "failed": failed}


# Disk actions
@app.post("/api/disk/scan")
def disk_scan():
    return _resp(tm.start("disk", "main.py", ["scan"]))


@app.post("/api/unlim/scan")
def api_unlim_scan(force: bool = False):
    args = ["--scan-only"]
    if force:
        args.append("--force")
    return _resp(tm.start("unlim", "unlim_backup.py", args))


class ScanPayload(BaseModel):
    mode: str = "unlim"
    force: bool = False


@app.post("/api/scan")
def api_scan(payload: ScanPayload):
    if payload.mode == "unlim":
        args = ["--scan-only"]
        if payload.force:
            args.append("--force")
        return _resp(tm.start("unlim", "unlim_backup.py", args))
    else:
        return _resp(tm.start("disk", "main.py", ["scan"]))


class DownloadOpts(BaseModel):
    workers: int = Field(default=4, ge=1, le=5)


@app.post("/api/disk/download")
def disk_download(opts: DownloadOpts):
    return _resp(tm.start("disk", "main.py",
                          ["download", "--workers", str(auto_workers()), "--no-scan"]))


@app.post("/api/disk/retry")
def disk_retry():
    return _resp(tm.start("disk", "main.py", ["retry-failed"]))


@app.post("/api/disk/verify")
def disk_verify():
    return _resp(tm.start("disk", "main.py", ["verify"]))


@app.post("/api/disk/stop")
def disk_stop():
    return _resp(tm.stop("disk"))


@app.post("/api/disk/clear")
def disk_clear():
    """Полная очистка БД сканирования диска + остановка задач."""
    tm.stop("disk")
    try:
        for suffix in ("", "-wal", "-shm"):
            f = DB_FILE.with_suffix(DB_FILE.suffix + suffix) if suffix else DB_FILE
            if f.exists():
                f.unlink()
        return {"ok": True, "msg": "БД диска очищена"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


@app.post("/api/unlim/clear")
def unlim_clear():
    """Полная очистка БД безлимита + остановка воркеров."""
    with tm._lock:
        for slot, t in list(tm._tasks.items()):
            if slot.startswith("unlim") and t.alive():
                t.stop()
    try:
        for suffix in ("", "-wal", "-shm"):
            f = UNLIM_DB_FILE.with_suffix(UNLIM_DB_FILE.suffix + suffix) if suffix else UNLIM_DB_FILE
            if f.exists():
                f.unlink()
        return {"ok": True, "msg": "БД безлимита очищена"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


class RebuildPayload(BaseModel):
    confirm: str = ""


@app.post("/api/unlim/rebuild")
def unlim_rebuild(payload: RebuildPayload):
    """Полное пересоздание базы Безлимита (аналог --scan-only --force).

    Вызывается только из «Диагностики»: статусы скачанных файлов теряются,
    поэтому перед удалением таблиц сохраняем согласованную копию базы через
    sqlite backup API (копирование файла при WAL может дать битый снимок).
    """
    if payload.confirm.strip().upper() != "ПЕРЕСОЗДАТЬ":
        return {"ok": False, "msg": "Введите слово ПЕРЕСОЗДАТЬ для подтверждения"}
    with tm._lock:
        for slot, t in list(tm._tasks.items()):
            if slot.startswith("unlim") and t.alive():
                return {"ok": False, "msg": "Сначала остановите скачивание Безлимита"}
    if not UNLIM_DB_FILE.exists():
        return {"ok": False,
                "msg": "Базы Безлимита ещё нет — сначала нажмите «Обновить список файлов»"}
    backup = UNLIM_DB_FILE.with_name(
        f"{UNLIM_DB_FILE.stem}.bak-{datetime.now():%Y%m%d-%H%M%S}{UNLIM_DB_FILE.suffix}"
    )
    try:
        src = sqlite3.connect(UNLIM_DB_FILE, timeout=30)
        try:
            dst = sqlite3.connect(backup)
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as e:
        return {"ok": False, "msg": f"Не удалось сохранить копию базы: {e}"}
    ok, msg = tm.start("unlim", "unlim_backup.py", ["--scan-only", "--force"])
    if not ok:
        return {"ok": False, "msg": msg}
    return {"ok": True, "msg": "База пересоздаётся", "backup": str(backup)}


# Резервные копии баз (только статусы, сами файлы не копируются)
def _backup_sources() -> dict[str, Path]:
    return {"disk": DB_FILE, "unlim": UNLIM_DB_FILE}


def _tasks_busy() -> bool:
    with tm._lock:
        return any(t.alive() for t in tm._tasks.values())


class BackupCreatePayload(BaseModel):
    scope: str = "all"


class BackupRestorePayload(BaseModel):
    id: str
    scopes: list[str] = []
    confirm: str = ""


@app.get("/api/backups")
def api_backups():
    items = backups.list_backups(paths.DATA_DIR)
    sources = _backup_sources()
    return {
        "items": items,
        "total_bytes": sum(i["size_bytes"] for i in items),
        "busy": _tasks_busy(),
        "available": {k: Path(v).exists() for k, v in sources.items()},
        "auto_keep": backups.AUTO_KEEP,
    }


@app.post("/api/backups/create")
def api_backups_create(payload: BackupCreatePayload):
    try:
        item = backups.create_backup(
            paths.DATA_DIR,
            payload.scope,
            _backup_sources(),
            version.APP_VERSION,
        )
    except (backups.BackupError, OSError, sqlite3.Error) as e:
        return {"ok": False, "msg": str(e)}
    return {"ok": True, "item": item}


@app.post("/api/backups/restore")
def api_backups_restore(payload: BackupRestorePayload):
    if payload.confirm.strip().upper() != "ВОССТАНОВИТЬ":
        return {"ok": False, "msg": "Введите слово ВОССТАНОВИТЬ для подтверждения"}
    if _tasks_busy():
        return {"ok": False,
                "msg": "Сначала остановите скачивание и сортировку, затем повторите"}
    try:
        result = backups.restore_backup(
            paths.DATA_DIR,
            payload.id,
            payload.scopes or list(backups.SCOPES),
            _backup_sources(),
            version.APP_VERSION,
        )
    except (backups.BackupError, OSError, sqlite3.Error) as e:
        return {"ok": False, "msg": str(e)}
    return {"ok": True, **result}


@app.delete("/api/backups/{backup_id}")
def api_backups_delete(backup_id: str):
    try:
        backups.delete_backup(paths.DATA_DIR, backup_id)
    except (backups.BackupError, OSError) as e:
        return {"ok": False, "msg": str(e)}
    return {"ok": True, "msg": "Копия удалена"}


@app.post("/api/backups/open")
def api_backups_open():
    try:
        folder = backups.backups_dir(paths.DATA_DIR)
        if os.name == "nt":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return {"ok": True, "path": str(folder)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Photoslice (безлимит)
@app.post("/api/unlim/collect")
def unlim_collect():
    return _resp(tm.start("unlim_collect", "unlim_download.py", ["--collect"]))


class WorkersPayload(BaseModel):
    workers: int = 4


@app.post("/api/unlim/start_workers")
def unlim_start_workers(payload: WorkersPayload):
    n = max(1, min(payload.workers, 6))
    started = []
    failed = []
    for i in range(1, n + 1):
        ok, msg = tm.start(f"unlim_w{i}", "unlim_worker.py", ["--id", str(i)])
        if ok:
            started.append(i)
        else:
            failed.append({"id": i, "msg": msg})
    return {"ok": True, "started": started, "failed": failed}


@app.post("/api/unlim/stop")
def unlim_stop():
    n = 0
    with tm._lock:
        for slot, t in tm._tasks.items():
            if slot.startswith("unlim") and t.alive():
                t.stop()
                n += 1
    return {"ok": True, "stopped": n}


# Organize / dedupe
class OrganizePayload(BaseModel):
    input: str
    action: str = "report-only"  # report-only | move
    scheme: str = "type-year-month"  # type-year-month | year-month | year
    quick: bool = False  # быстрый отчёт: без поиска дубликатов (MD5)


# Запрещённые пути для organize (нельзя бесконтрольно сортировать систему)
_FORBIDDEN_ORGANIZE_PREFIXES = [
    "c:\\windows", "c:\\program files", "c:\\programdata",
    "c:\\users\\all users", "c:\\$recycle.bin",
    "/", "/etc", "/usr", "/bin", "/sbin", "/system", "/library",
]


def _is_safe_organize_path(path: str) -> tuple[bool, str]:
    """Запрещаем сортировку системных папок и корней дисков."""
    try:
        abs_path = os.path.abspath(path)
    except Exception:
        return False, "Невалидный путь"
    norm = abs_path.lower().replace("/", "\\")
    raw_norm = path.strip().lower().replace("/", "\\")
    # Корень диска (типа C:\ или D:\) — слишком опасно
    if (len(abs_path) <= 3 and abs_path.endswith(":\\")) or (len(raw_norm) <= 3 and raw_norm.endswith(":\\")):
        return False, "Нельзя сортировать корень диска целиком"
    for bad in _FORBIDDEN_ORGANIZE_PREFIXES:
        bad_norm = bad.replace("/", "\\")
        if (norm == bad_norm or norm.startswith(bad_norm + "\\")
                or raw_norm == bad_norm or raw_norm.startswith(bad_norm + "\\")):
            return False, f"Системная папка запрещена: {bad}"
    if not os.path.isdir(abs_path):
        return False, "Папка не существует"
    return True, ""


@app.post("/api/organize")
def organize(payload: OrganizePayload):
    ok, msg = _is_safe_organize_path(payload.input)
    if not ok:
        return {"ok": False, "msg": msg}
    if payload.action not in ("report-only", "move"):
        return {"ok": False, "msg": "Неизвестное действие"}
    if payload.scheme not in ("type-year-month", "year-month", "year"):
        return {"ok": False, "msg": "Неизвестная схема раскладки"}
    args = ["--input", payload.input,
            "--action", payload.action,
            "--scheme", payload.scheme]
    if payload.quick:
        args.append("--quick")
    return _resp(tm.start("organize", "organize.py", args))


@app.post("/api/organize/stop")
def organize_stop():
    return _resp(tm.stop("organize"))


@app.post("/api/stop_all")
def stop_all():
    n = tm.stop_all()
    return {"ok": True, "stopped": n}


@app.post("/api/task/{slot}/clear_log")
def clear_task_log(slot: str):
    """Очищает буфер логов задачи (или всех slot с префиксом)."""
    cleared = 0
    with tm._lock:
        for s, t in tm._tasks.items():
            if s == slot or (slot.endswith("*") and s.startswith(slot[:-1])):
                t.log.clear()
                cleared += 1
    return {"ok": True, "cleared": cleared}


# ── Первая настройка и диагностика ──────────────────────────────────────────


class SetupPayload(BaseModel):
    token: str = ""
    disk_dir: str = ""
    unlim_dir: str = ""
    report_key: str = ""


def _setup_status() -> dict:
    cfg = get_config()
    has_token = bool(cfg.get("token"))
    has_cookies = paths.COOKIES_FILE.exists()
    done = paths.SETUP_DONE_FILE.exists()
    return {
        "needs_setup": not done and not has_token and not has_cookies,
        "setup_done": done,
        "has_token": has_token,
        "has_cookies": has_cookies,
        "disk_dir": cfg.get("disk_dir", ""),
        "unlim_dir": cfg.get("unlim_dir", ""),
        "report_key_configured": diagnostics.report_key_configured(),
        "version": version.version_info(),
    }


def _write_setup_dirs(payload: SetupPayload) -> tuple[str, str]:
    disk_dir = (payload.disk_dir or "").strip()
    unlim_dir = (payload.unlim_dir or "").strip()
    for value in (disk_dir, unlim_dir):
        if not value:
            continue
        try:
            Path(value).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"Не удалось создать папку {value}: {e}")
    if disk_dir or unlim_dir:
        if not ENV_FILE.exists():
            ENV_FILE.write_text("", encoding="utf-8")
        if disk_dir:
            set_key(str(ENV_FILE), "DISK_DIR", disk_dir, quote_mode="never")
            set_key(str(ENV_FILE), "DOWNLOAD_DIR", disk_dir, quote_mode="never")
        if unlim_dir:
            set_key(str(ENV_FILE), "UNLIM_DIR", unlim_dir, quote_mode="never")
        load_dotenv(ENV_FILE, override=True)
    return disk_dir, unlim_dir


@app.get("/api/version")
def api_version():
    return version.version_info()


@app.get("/api/setup/status")
def api_setup_status():
    return _setup_status()


@app.post("/api/setup")
def api_setup(payload: SetupPayload):
    """Завершает первичную настройку: токен, папки, ключ отчётов."""
    # Пароль/ключ не журналируем: в лог попадает только сам факт настройки.
    log_action("setup/finish")
    token = (payload.token or "").strip()
    token_check = None
    if token:
        token_check = check_token(token)
        if not token_check.get("ok"):
            return JSONResponse(
                {"ok": False,
                 "error": f"Токен не принят Яндексом: {token_check.get('msg', '')}",
                 "token_check": token_check},
                status_code=400,
            )
        if not ENV_FILE.exists():
            ENV_FILE.write_text("", encoding="utf-8")
        set_key(str(ENV_FILE), "YA_DISK_TOKEN", token, quote_mode="never")
        load_dotenv(ENV_FILE, override=True)
    disk_dir, unlim_dir = _write_setup_dirs(payload)
    if (payload.report_key or "").strip():
        diagnostics.save_report_key(payload.report_key)
    paths.SETUP_DONE_FILE.write_text(
        time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8"
    )
    return {
        "ok": True,
        "token_check": token_check,
        "disk_dir": disk_dir,
        "unlim_dir": unlim_dir,
        "status": _setup_status(),
    }


@app.post("/api/setup/skip")
def api_setup_skip():
    log_action("setup/skip")
    paths.SETUP_DONE_FILE.write_text(
        time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8"
    )
    return {"ok": True, "status": _setup_status()}


@app.post("/api/diagnostics")
def api_diagnostics():
    """Собрать отчёт и сохранить ZIP. Ничего никуда не отправляет."""
    log_action("diagnostics/build")
    try:
        report = diagnostics.build_report(tm.status(), full_cycle.status())
        path = diagnostics.write_report(report)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": diagnostics.redact_text(f"{type(e).__name__}: {e}")},
            status_code=500,
        )
    return {
        "ok": True,
        "report_id": report.get("report_id"),
        "path": str(path),
        "size_mb": round(diagnostics._size_h(path) / (1024 * 1024), 2),
        "summary": diagnostics.render_summary(report),
        "reports": diagnostics.latest_reports(),
    }


@app.post("/api/diagnostics/send")
def api_diagnostics_send():
    """Собрать отчёт и отправить через Cloudflare Worker в Telegram."""
    log_action("diagnostics/send")
    try:
        report = diagnostics.build_report(tm.status(), full_cycle.status())
        path = diagnostics.write_report(report)
        result = diagnostics.send_report(path, report)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": diagnostics.redact_text(f"{type(e).__name__}: {e}")},
            status_code=500,
        )
    result["summary"] = diagnostics.render_summary(report)
    result["reports"] = diagnostics.latest_reports()
    return result


class ReportKeyPayload(BaseModel):
    key: str = ""


@app.post("/api/diagnostics/key")
def api_diagnostics_key(payload: ReportKeyPayload):
    """Сохраняет ключ отправки. В журнал и отчёт значение не попадает."""
    log_action("diagnostics/key")
    diagnostics.save_report_key(payload.key)
    return {"ok": True,
            "configured": diagnostics.report_key_configured()}


@app.get("/api/diagnostics/reports")
def api_diagnostics_reports():
    return {"ok": True, "reports": diagnostics.latest_reports()}


class UpdateOpenPayload(BaseModel):
    url: str = ""


@app.get("/api/updates/check")
def api_updates_check():
    """Сравнивает версию с последним GitHub Release. Сеть может быть недоступна."""
    log_action("updates/check")
    return updater.check_for_updates()


@app.post("/api/updates/open")
def api_updates_open(payload: UpdateOpenPayload):
    """Открывает страницу релиза в системном браузере (только наш репозиторий)."""
    if not updater.release_url_allowed(payload.url):
        return JSONResponse(
            {"ok": False, "error": "Ссылка не относится к релизам проекта."},
            status_code=400,
        )
    try:
        import webbrowser

        webbrowser.open(payload.url)
    except Exception as e:
        return {"ok": False, "error": diagnostics.redact_text(f"{type(e).__name__}: {e}")}
    return {"ok": True}


class UpdateInstallPayload(BaseModel):
    restart: bool = True


@app.post("/api/updates/download")
def api_updates_download():
    """Загружает установщик последнего релиза с проверкой SHA-256."""
    log_action("updates/download")
    ok, msg = update_manager.start()
    return {"ok": ok, "msg": msg, "state": update_manager.state}


@app.post("/api/updates/cancel")
def api_updates_cancel():
    ok, msg = update_manager.cancel()
    return {"ok": ok, "msg": msg}


@app.post("/api/updates/install")
def api_updates_install(payload: UpdateInstallPayload):
    """Запускает помощника, который поставит обновление после закрытия программы."""
    log_action("updates/install")
    if os.name != "nt":
        return {"ok": False, "msg": "Установка обновлений поддерживается только в Windows."}
    if not paths.IS_FROZEN:
        return {"ok": False, "msg": "Запущено из исходников — обновитесь через git."}
    if update_manager.state != "ready" or not update_manager.path:
        return {"ok": False, "msg": "Обновление ещё не загружено."}
    payload_path = Path(update_manager.path)
    if not payload_path.is_file():
        return {"ok": False, "msg": "Файл обновления пропал из временной папки."}
    with tm._lock:
        busy = [slot for slot, task in tm._tasks.items() if task.alive()]
    if busy or pipeline.is_running() or full_cycle.is_running():
        return {"ok": False, "msg": "Сначала остановите скачивание."}
    try:
        helper = write_update_helper(payload_path, update_manager.kind, payload.restart)
        # DETACHED_PROCESS здесь использовать нельзя: powershell.exe без консоли
        # стартует и молча завершается с кодом 0, не выполнив скрипт — программа
        # закрывалась, а обновление не ставилось. CREATE_NO_WINDOW даёт скрытое
        # окно без консоли, и потомок переживает выход родителя (проверено на
        # Windows 10: с DETACHED_PROCESS install_update.log не появлялся вообще).
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", str(helper)],
            cwd=str(helper.parent), close_fds=True, creationflags=flags,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"Не удалось запустить установку: {e}"}
    update_manager.state = "installing"
    threading.Timer(1.5, _quit_application).start()
    version_text = update_manager.version or "новую версию"
    return {"ok": True, "msg": f"Программа закроется и установит {version_text}."}


@app.post("/api/reports/open")
def api_open_reports():
    """Открывает папку с отчётами в проводнике."""
    try:
        paths.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(paths.REPORT_DIR))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(paths.REPORT_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(paths.REPORT_DIR)])
        return {"ok": True, "path": str(paths.REPORT_DIR)}
    except Exception as e:
        return {"ok": False, "error": str(e), "path": str(paths.REPORT_DIR)}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _resp(t: tuple[bool, str]):
    return {"ok": t[0], "msg": t[1]}


if (TEMPLATES_DIR / "static").exists():
    app.mount("/static",
              StaticFiles(directory=str(TEMPLATES_DIR / "static")),
              name="static")


def _browser_host(host: str) -> str:
    """0.0.0.0/:: — адреса прослушивания, а не для входа в браузере.
    Используем 127.0.0.1 явно: на Windows localhost резолвится в IPv6 ::1,
    а сервер слушает только IPv4, поэтому localhost может не открываться."""
    if host in ("0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return host


def _open_browser_when_ready(host: str, port: int):
    """Открывает браузер только когда сервер начал отвечать."""
    import http.client
    import webbrowser
    browser_host = _browser_host(host)
    url = f"http://{browser_host}:{port}"
    for _ in range(60):  # ждём до ~30 сек
        time.sleep(0.5)
        try:
            conn = http.client.HTTPConnection(browser_host, port, timeout=1)
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            if resp.status == 200:
                conn.close()
                break
            conn.close()
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


# Скрипты, которые WebUI запускает дочерними процессами. В собранном exe
# argv[1] содержит имя скрипта: один и тот же exe работает как GUI и как
# воркер (multi-call binary). Здесь должны быть ВСЕ такие скрипты, иначе
# frozen-сборка вместо воркера снова поднимет окно приложения.
WORKER_SCRIPTS = {
    "main.py": "main",
    "unlim_backup.py": "unlim_backup",
    "unlim_download.py": "unlim_download",
    "unlim_worker.py": "unlim_worker",
    "organize.py": "organize",
    "dedupe.py": "dedupe",
    "fetch_cookies.py": "fetch_cookies",
    "extract_cookies.py": "extract_cookies",
    "report_status.py": "report_status",
    "watch.py": "watch",
}


def dispatch_worker(argv: list[str] | None = None) -> bool:
    """Выполняет воркер, если argv начинается с имени скрипта.

    Возвращает False для обычного запуска GUI. Для воркера процесс
    завершается через SystemExit — код возврата скрипта.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in WORKER_SCRIPTS:
        return False
    sub_cmd = args[0]
    sys.argv = [sub_cmd] + args[1:]
    module = importlib.import_module(WORKER_SCRIPTS[sub_cmd])
    result = module.main()
    raise SystemExit(result or 0)


def main():
    # Диспетчер для режима единого скомпилированного EXE (multi-call binary)
    dispatch_worker()

    import uvicorn
    host = os.getenv("WEBUI_HOST", "0.0.0.0")
    port = int(os.getenv("WEBUI_PORT", "8000"))
    url = f"http://{_browser_host(host)}:{port}"
    print("=" * 64)
    print("  Yandex.Disk Backup by Sergey Pukhir (ex3544, vovapuh's son)")
    print(f"  Дашборд: {url}")
    print("=" * 64)
    print("  Быстрые ссылки для настройки:")
    print("  * Получить OAuth-токен:  https://yandex.ru/dev/disk/poligon/")
    print("  * Скачать Cookie-Editor: https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm")
    print("  * Раздел фото (безлимит): https://disk.yandex.ru/client/photo")
    print("=" * 64)
    print()
    print("  Сервер запускается, браузер откроется автоматически.")
    print("  Чтобы остановить — закройте это окно или нажмите Ctrl+C.")
    print()

    # Открыть браузер в фоне когда сервер готов
    threading.Thread(
        target=_open_browser_when_ready,
        args=(host, port),
        daemon=True,
    ).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
