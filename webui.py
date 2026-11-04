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
import cloud_audit as cloud_audit_mod
import diagnostics
import exclusions
import guide
import monitor
import paths
import previews
import profiles
import relocate as relocate_mod
import selection
import unlim_dates
import updater
import verify as verify_mod
import version
from utils import (
    auto_workers,
    check_target_dir,
    check_target_path,
    normalize_scan_roots,
    normalize_unlim_media,
    ram_info,
)

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
TOKEN_FETCH_STATUS = paths.TOKEN_FETCH_STATUS
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


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "нет")


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name, "") or "").strip().replace(",", ".")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def get_config() -> dict:
    """Перечитывает .env заново."""
    load_dotenv(ENV_FILE, override=True)
    return {
        "token": (os.getenv("YA_DISK_TOKEN", "") or "").strip(),
        "download_dir": (os.getenv("DOWNLOAD_DIR", str(paths.DEFAULT_DISK_DIR)) or "").strip(),
        "unlim_dir": (os.getenv("UNLIM_DIR", str(paths.DEFAULT_UNLIM_DIR)) or "").strip(),
        "disk_dir": (os.getenv("DISK_DIR", os.getenv("DOWNLOAD_DIR", str(paths.DEFAULT_DISK_DIR))) or "").strip(),
        "max_workers": int(os.getenv("MAX_WORKERS", "4") or "4"),
        "previews": _env_flag("PREVIEWS", True),
        # Папка превью по умолчанию — на диске C рядом с базами: превью
        # занимают копейки, а папки выгрузки (внешние диски) меняются.
        "previews_dir": ((os.getenv("PREVIEWS_DIR", "") or "").strip()
                         or previews.preview_root()),
        # Сторож места включён «из коробки» с порогом 20 ГБ; человек может
        # выключить, и выключение запоминается.
        "watchdog": _env_flag("WATCHDOG", True),
        "watchdog_gb": _env_float("WATCHDOG_GB", 20.0),
        # C2: какие папки облака обходить и что брать из Безлимита.
        "scan_roots": normalize_scan_roots(os.getenv("DISK_SCAN_ROOTS", "/")),
        "unlim_media": normalize_unlim_media(os.getenv("UNLIM_MEDIA", "all"), "all"),
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


app = FastAPI(title="Yad.backup UI")


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


def reset_session_counters(kind: str) -> None:
    """Убирает счётчики прошлой сессии перед новым запуском выгрузки.

    Файл именно удаляется, а не обнуляется: пока дочерний процесс не напишет
    свой первый статус, интерфейс покажет «подготовка», а не чужие числа из
    прошлой сессии (именно так на старте появлялось «136 файлов загружено»).
    """
    path = paths.UNLIM_STATUS_FILE if kind == "unlim" else paths.DISK_STATUS_FILE
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass



def task_kind(script: str, args: list[str]) -> str:
    """Что делает задача: выгрузка, сканирование или служебное.

    От этого зависит, считается ли она новой сессией для плашек. Сканирование
    и проверка используют те же слоты `disk`/`unlim`, но файлов не качают:
    раньше их нулевой статус затирал итог последней сессии.
    """
    name = os.path.basename(str(script or "")).lower()
    argv = [str(a) for a in (args or [])]
    if name.startswith("unlim_backup"):
        return "scan" if "--scan-only" in argv else "download"
    if name.startswith("main"):
        cmd = next((a for a in argv if not a.startswith("-")), "")
        # retry-failed сбрасывает сбойные и сразу качает их тем же проходом.
        return "download" if cmd in ("download", "retry-failed") else "other"
    if name.startswith("unlim_download"):
        return "scan"  # сбор списка Безлимита
    return "other"


class Task:
    def __init__(self, name: str, proc: subprocess.Popen, kind: str = "download"):
        self.name = name
        self.proc = proc
        self.kind = kind
        self.started_at = time.time()
        self.log: deque[str] = deque(maxlen=2000)
        # Процесс закончил писать (EOF в stdout). Нужен, потому что poll() иногда
        # продолжает возвращать None, и тогда полный цикл ждёт «живую» задачу,
        # которая на самом деле уже упала, до ручной остановки.
        self.finished = False
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
            self.finished = True

    def alive(self) -> bool:
        # EOF в stdout означает, что процесс дописал последнее: считаем его
        # завершённым, даже если poll() ещё не отдал код возврата.
        return not self.finished and self.proc.poll() is None

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

    def start(
        self,
        slot: str,
        script: str,
        args: list[str],
        env_extra: Optional[dict[str, str]] = None,
    ) -> tuple[bool, str]:
        """slot — логическое имя ('disk', 'photoslice_collect', 'worker-1', ...).
        Можно одновременно несколько задач в разных слотах.

        `env_extra` добавляется к окружению дочернего процесса. «Зверь» так
        включает пробы мёртвых серверов хранения.
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
            if env_extra:
                env.update({str(k): str(v) for k, v in env_extra.items()})
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
            self._tasks[slot] = Task(slot, proc, task_kind(script, args))
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
                    "kind": t.kind,
                    "started_at": t.started_at,
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

    Линии идут параллельно. В каждой — ровно ``passes`` проходов (Сергей:
    «поставил 5 — значит 5, поставил 10 — значит 10»); между проходами пауза
    PAUSE_AFTER_PASS. Отсутствие прогресса пишется в журнал, но цикл не
    останавливает: сбойные могут не уменьшиться, хотя файлы качаются.
    Итог по каждой линии считается по базе: сколько добавилось скачанных
    и сколько сбойных осталось.
    """

    PAUSE_AFTER_PASS = 60.0
    MAX_PASSES = 10

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

    def start(self, workers: int = 10, passes: int = 3) -> tuple[bool, str]:
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
                # Даём процессу пару секунд, чтобы poll() отдал код возврата:
                # иначе упавшая задача выглядела бы как успешная.
                for _ in range(50):
                    code = t.proc.poll()
                    if code is not None:
                        return code
                    time.sleep(0.1)
                return 1
            time.sleep(1.0)
        return -1

    def _pause(self, seconds: float):
        end = time.time() + seconds
        while not self._stop and time.time() < end:
            time.sleep(1.0)

    @staticmethod
    def _read_status(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — статуса может не быть
            return {}

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

    @staticmethod
    def _progress_note(name: str, gained: int, before: int, after: int) -> str:
        """Строка журнала после прохода.

        Только сообщает цифры — остановить цикл она не может: сколько проходов
        задано, столько и будет.
        """
        if gained > 0 or after < before:
            return f"   {name}: +{gained} скачано, осталось сбойных {after}"
        if after > before:
            return (f"   {name}: +{gained} скачано, сбойных стало больше "
                    f"({before} → {after}) — продолжаю заданное число проходов")
        return (f"   {name}: новых файлов нет, сбойных {after} — "
                f"продолжаю заданное число проходов")

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
        cfg = get_config()
        problem = check_target_dir(cfg.get("disk_dir") or cfg.get("download_dir")
                                   or str(paths.DEFAULT_DISK_DIR))
        if problem:
            raise RuntimeError(problem)
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
            if before["errors"]:
                # Прозрачность: `--retry` вернёт эти файлы в очередь, и это
                # видно в журнале, а не только по косвенным цифрам.
                self._log(f"   Диск: сброшено в очередь: {before['errors']} сбойных файлов")
            reset_session_counters("disk")
            disk_args = ["download", "--workers", str(disk_workers),
                         "--no-scan", "--retry"]
            if p > 1:
                # Сгоревшие возвращаются один раз за запуск, а не в каждом
                # проходе полного цикла.
                disk_args.append("--no-return-lost")
            self._own_start("disk", "main.py", disk_args)
            code = self._wait_task("disk")
            if self._stop:
                lane["state"] = "остановлено"
                return
            if code not in (0, 1):
                # 0 — всё скачано, 1 — часть файлов упала (это норма), остальное
                # (нет токена и т.п.) — смысла повторять проходы нет.
                raise RuntimeError(f"скачивание завершилось с кодом {code}")
            after = self._db_snapshot(DB_FILE, "files", ("failed",))
            gained = max(0, after["downloaded"] - before["downloaded"])
            lane["downloaded"] += gained
            lane["bytes"] += max(0, after["bytes"] - before["bytes"])
            lane["errors"] = after["errors"]
            self._log(self._progress_note("Диск", gained, before["errors"],
                                          after["errors"]))
            # Проходов ровно столько, сколько задано: досрочно не выходим.
            # Сбойные могут не уменьшиться, хотя файлы качаются (новые падают,
            # старые спасаются) — раньше это обрывало цикл после первого прохода.
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
        problem = check_target_dir(out_dir)
        if problem:
            raise RuntimeError(problem)

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
            if before["errors"]:
                self._log(f"   Безлимит: в повтор пойдут: {before['errors']} пропущенных файлов")
            reset_session_counters("unlim")
            unlim_args = [
                "--out-dir", out_dir,
                "--media", cfg.get("unlim_media") or "all",
                "--target", "all",
                "--workers", str(self.workers),
            ]
            if not cfg.get("previews", True):
                unlim_args.append("--no-previews")
            if p > 1:
                # Сгоревшие возвращаются один раз за запуск, а не в каждом
                # проходе полного цикла.
                unlim_args.append("--no-return-lost")
            self._own_start("unlim", "unlim_backup.py", unlim_args)
            code = self._wait_task("unlim")
            if self._stop:
                lane["state"] = "остановлено"
                return
            if code not in (0, 1):
                raise RuntimeError(f"скачивание завершилось с кодом {code}")
            fatal = self._read_status(paths.UNLIM_STATUS_FILE).get("error")
            if fatal in ("cookies_expired", "disk_full"):
                # Процесс отработал «штатно», но по факту ничего не смог:
                # повторять проходы бессмысленно, нужна реакция пользователя.
                reason = ("сессия Яндекса истекла — обновите cookies"
                          if fatal == "cookies_expired"
                          else "недостаточно места на диске")
                raise RuntimeError(reason)
            after = self._db_snapshot(UNLIM_DB_FILE, "unlim_files", ("skipped",))
            gained = max(0, after["downloaded"] - before["downloaded"])
            lane["downloaded"] += gained
            lane["bytes"] += max(0, after["bytes"] - before["bytes"])
            lane["errors"] = after["errors"]
            self._log(self._progress_note("Безлимит", gained, before["errors"],
                                          after["errors"]))
            if p < self.passes:
                self._pause(self.PAUSE_AFTER_PASS)

        lane["state"] = "готово"

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


# ── Режим «Зверь» ────────────────────────────────────────────────────────────
#
# Одна кнопка вместо ручного «Скачать всё». Пока файлы
# качаются, режим ничего не трогает. Как только прогресс встал (нет новых
# файлов и нет байт 90 секунд), он меняет тактику: возвращает отложенные,
# обнуляет счётчики, меняет число потоков/таймаут/порядок. Так до тех пор,
# пока в базах не останется ни одного нескачанного файла.
#
# Почему это работает: отложенный файл после трёх ответов 5xx ждёт 30 минут, а
# автовозврат не обнуляет `server_fails` — первый же новый 500 снова сжигает
# файл. «Попробовать» обнуляет счётчики, поэтому ручное чередование кнопок и
# давало +15…35 файлов. Здесь то же самое делает каждая смена тактики.

class Beast:
    TICK = 2.0
    # Нет ни новых файлов, ни байт (heartbeat) — считаем, что встало.
    STALL_SEC = 90.0
    # Сколько ждать первого файла после старта тактики (авторизация, сборка
    # списка). Сканирование облака может идти минутами — для него свой запас.
    GRACE_SEC = 120.0
    SCAN_GRACE_SEC = 900.0
    # Паузы между полными кругами без единого скачанного файла.
    EMPTY_PAUSES = (60.0, 300.0, 900.0)
    MAX_LOG = 400

    STRATEGIES = (
        {"key": "reset", "title": "свежие попытки", "disk_workers": 0,
         "workers": 10, "timeout": 7.0, "from_end": False, "scan": False},
        {"key": "few", "title": "меньше потоков", "disk_workers": 3,
         "workers": 4, "timeout": 15.0, "from_end": False, "scan": False},
        {"key": "many", "title": "больше потоков", "disk_workers": 5,
         "workers": 14, "timeout": 7.0, "from_end": False, "scan": False},
        {"key": "end", "title": "с конца списка", "disk_workers": 4,
         "workers": 10, "timeout": 7.0, "from_end": True, "scan": False},
        {"key": "long", "title": "долгий таймаут", "disk_workers": 4,
         "workers": 6, "timeout": 30.0, "from_end": False, "scan": False},
        {"key": "short", "title": "короткий таймаут", "disk_workers": 4,
         "workers": 6, "timeout": 4.0, "from_end": False, "scan": False},
        {"key": "refresh", "title": "пересобрать список", "disk_workers": 5,
         "workers": 10, "timeout": 7.0, "from_end": False, "scan": True},
    )

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop = False
        self.state = "idle"  # idle | running | done | stopped | failed
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.round = 0
        self.strategy_index = 0
        self.strategy = ""
        self.gained = 0
        self.gained_round = 0
        # Накопительный итог по линиям за всю сессию «Зверя». `gained` —
        # общий, а плашкам Диска и Безлимита нужен свой счётчик: так он не
        # сбрасывается при смене тактики и не смешивает линии.
        self.gained_by_kind = {"disk": 0, "unlim": 0}
        self.message = ""
        self.log: deque[str] = deque(maxlen=self.MAX_LOG)
        self.remaining = {"disk": 0, "unlim": 0}

    def _log(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        elapsed = None
        if self.started_at:
            elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "state": self.state,
            "running": self.is_running(),
            "round": self.round,
            "strategy": self.strategy,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": elapsed,
            "gained": self.gained,
            "gained_round": self.gained_round,
            "gained_by_kind": dict(self.gained_by_kind),
            "message": self.message,
            "remaining": dict(self.remaining),
            "log": list(self.log)[-200:],
        }

    # ── запуск и остановка ──────────────────────────────────────────────

    @staticmethod
    def _slot_running(slot: str) -> bool:
        with tm._lock:
            task = tm._tasks.get(slot)
            return bool(task and task.alive())

    def _stop_slots(self, slots: tuple[str, ...], wait: float = 12.0) -> None:
        for slot in slots:
            try:
                tm.stop(slot)
            except Exception:  # noqa: BLE001 — остановка не должна мешать запуску
                pass
        end = time.time() + wait
        while time.time() < end:
            if not any(self._slot_running(s) for s in slots):
                return
            time.sleep(0.2)

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return False, "Зверь уже работает"
            busy = []
            with tm._lock:
                for slot, task in tm._tasks.items():
                    if task.alive() and slot not in (
                        "disk", "unlim", "unlim_collect"
                    ):
                        busy.append(slot)
            if busy:
                return False, f"Сначала дождитесь: {', '.join(sorted(busy))}"

            cfg = get_config()
            for folder in (
                cfg.get("disk_dir") or cfg.get("download_dir") or str(paths.DEFAULT_DISK_DIR),
                cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR),
            ):
                problem = check_target_dir(folder)
                if problem:
                    return False, problem
            cookies = check_cookies()
            if not cookies.get("ok"):
                return False, (f"Куки: {cookies.get('msg')}. Обновите cookies "
                               f"и включите «Зверя» снова.")
            if not (cfg.get("token") or "").strip():
                return False, "Нет OAuth-токена для обычного Диска."

            # Кнопка забирает управление: останавливаем то, что уже идёт.
            try:
                full_cycle.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                pipeline.stop()
            except Exception:  # noqa: BLE001
                pass
            self._stop_slots(("disk", "unlim", "unlim_collect"))

            self._stop = False
            self.state = "running"
            self.started_at = time.time()
            self.finished_at = None
            self.round = 0
            self.strategy_index = 0
            self.strategy = ""
            self.gained = 0
            self.gained_round = 0
            self.gained_by_kind = {"disk": 0, "unlim": 0}
            self.message = ""
            self.log.clear()
            self.remaining = {"disk": self._remaining("disk"),
                              "unlim": self._remaining("unlim")}
            self._log("🐾 Зверь включён. Пока файлы качаются — не трогаю; "
                      "встало — меняю тактику.")
            thread = threading.Thread(target=self._run, daemon=True)
            self._thread = thread
        thread.start()
        return True, "Зверь запущен"

    def stop(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "Зверь не работает"
        self._stop = True
        self._log("⏹ остановка…")
        self._stop_slots(("disk", "unlim", "unlim_collect"), wait=8.0)
        return True, "Зверь остановлен"

    # ── база ────────────────────────────────────────────────────────────

    @staticmethod
    def _db_pair(kind: str) -> tuple[Path, str]:
        return (DB_FILE, "files") if kind == "disk" else (UNLIM_DB_FILE, "unlim_files")

    def _remaining(self, kind: str) -> int:
        db_file, table = self._db_pair(kind)
        if not Path(db_file).exists():
            return 0
        statuses = ("pending", "in_progress", "lost", "failed") \
            if kind == "disk" else ("pending", "in_progress", "lost", "skipped")
        marks = ",".join("?" for _ in statuses)
        try:
            con = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=15)
            try:
                row = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE status IN ({marks})",
                    statuses,
                ).fetchone()
                return int(row[0] or 0)
            finally:
                con.close()
        except sqlite3.Error:
            return 0

    def _refresh_remaining(self) -> None:
        self.remaining = {"disk": self._remaining("disk"),
                          "unlim": self._remaining("unlim")}

    def _remaining_total(self) -> int:
        return int(self.remaining.get("disk") or 0) + int(self.remaining.get("unlim") or 0)

    def _progress(self, kind: str) -> tuple[int, int]:
        """Сколько файлов скачано и сколько байт — для детектора прогресса."""
        db_file, table = self._db_pair(kind)
        if not Path(db_file).exists():
            return 0, 0
        try:
            con = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=15)
            try:
                row = con.execute(
                    f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM {table} "
                    f"WHERE status='downloaded'"
                ).fetchone()
                return int(row[0] or 0), int(row[1] or 0)
            finally:
                con.close()
        except sqlite3.Error:
            return 0, 0

    @staticmethod
    def _heartbeat_mtime(kind: str) -> float:
        path = paths.DISK_HEARTBEAT_FILE if kind == "disk" else paths.UNLIM_HEARTBEAT_FILE
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    @staticmethod
    def _clear_heartbeat(kind: str) -> None:
        path = paths.DISK_HEARTBEAT_FILE if kind == "disk" else paths.UNLIM_HEARTBEAT_FILE
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _mark_unavailable(self) -> None:
        """Снимает с очереди файлы, которые Яндекс не отдаст.

        404/410 — файла в облаке нет: статус deleted, скан его не оживит.
        403 — доступ закрыт, но это может быть временным. Такой файл получает
        статус denied: «Зверь» его не ждёт и сам не повторяет, зато вручную
        его можно вернуть кнопкой «Отложенные сервером».
        """
        deleted = denied = 0
        jobs = (
            (DB_FILE, "files", (
                ("status='failed' AND (error LIKE '%PathNotFound%' "
                 "OR error LIKE '%404%' OR error LIKE '%410%')", "deleted"),
                ("status='failed' AND error LIKE '%403%'", "denied"),
            )),
            (UNLIM_DB_FILE, "unlim_files", (
                ("status IN ('skipped','failed') AND (error LIKE '%404%' "
                 "OR error LIKE '%410%' "
                 "OR error LIKE '%Ссылка не найдена%')", "deleted"),
                ("status IN ('skipped','failed') AND error LIKE '%403%'", "denied"),
            )),
        )
        for db_file, table, rules in jobs:
            if not Path(db_file).exists():
                continue
            try:
                con = sqlite3.connect(str(db_file), timeout=30)
                try:
                    with con:
                        for cond, status in rules:
                            n = con.execute(
                                f"UPDATE {table} SET status='{status}' "
                                f"WHERE {cond} AND COALESCE(attempts, 0) >= 3"
                            ).rowcount
                            if status == "deleted":
                                deleted += n
                            else:
                                denied += n
                finally:
                    con.close()
            except sqlite3.Error:
                pass
        if deleted:
            self._log(f"   {deleted} файлов нет в облаке (404) — больше не считаю их остатком")
        if denied:
            self._log(f"   {denied} файлов ответили 403 (нет доступа) — «Зверь» их "
                      f"не ждёт; вернуть вручную можно в «Сброс очереди»")

    def _reset_lost(self) -> int:
        """Возвращает всё нескачанное в очередь и обнуляет счётчики 5xx."""
        total = 0
        jobs = (
            (DB_FILE, (
                ("UPDATE files SET status='pending', error=NULL, "
                 "server_fails=0, retry_after=NULL WHERE status='lost'", True),
                # Зависшие после закрытия процесса — тоже в очередь.
                ("UPDATE files SET status='pending' "
                 "WHERE status='in_progress'", True),
                # Файлы «ждут Яндекса»: паузу снимаем, попытки не тратим.
                ("UPDATE files SET retry_after=NULL WHERE status='pending'", False),
            )),
            (UNLIM_DB_FILE, (
                ("UPDATE unlim_files SET status='pending', error=NULL, "
                 "server_fails=0, retry_after=NULL, assigned_at=NULL "
                 "WHERE status='lost'", True),
                ("UPDATE unlim_files SET status='pending', assigned_at=NULL "
                 "WHERE status='in_progress'", True),
                ("UPDATE unlim_files SET retry_after=NULL WHERE status='pending'",
                 False),
                # Пропущенные Безлимита повторяем в каждом круге, но счётчик
                # попыток не обнуляем: по нему видно, какие файлы уже точно
                # недоступны.
                ("UPDATE unlim_files SET status='pending', error=NULL, "
                 "retry_after=NULL, assigned_at=NULL WHERE status='skipped'", True),
            )),
        )
        for db_file, queries in jobs:
            if not Path(db_file).exists():
                continue
            try:
                con = sqlite3.connect(str(db_file), timeout=30)
                try:
                    with con:
                        for sql, counted in queries:
                            n = con.execute(sql).rowcount
                            if counted:
                                total += n
                finally:
                    con.close()
            except sqlite3.Error as e:
                self._log(f"   сброс базы {Path(db_file).name}: {e}")
        return total

    # ── тактики ─────────────────────────────────────────────────────────

    def _scan_all(self) -> None:
        """Пересобирает списки Диска и Безлимита перед тактикой «refresh»."""
        self._log("🔍 обновляю списки облака…")
        self._clear_heartbeat("disk")
        ok, msg = tm.start("disk", "main.py", ["scan"])
        if ok:
            self._wait_slot("disk", self.SCAN_GRACE_SEC)
        else:
            self._log(f"   Диск: скан не запущен: {msg}")
        ok, msg = tm.start("unlim_collect", "unlim_download.py", ["--collect"])
        if ok:
            self._wait_slot("unlim_collect", self.SCAN_GRACE_SEC)
        else:
            self._log(f"   Безлимит: сбор списка не запущен: {msg}")

    def _wait_slot(self, slot: str, limit: float) -> None:
        end = time.time() + limit
        while not self._stop and time.time() < end:
            if not self._slot_running(slot):
                return
            time.sleep(1.0)
        if self._stop:
            return
        self._log(f"   {slot}: дольше {int(limit)} с — останавливаю")
        tm.stop(slot)

    def _start_lane(self, kind: str, strategy: dict) -> tuple[bool, str]:
        if self._stop:
            return False, "Зверь остановлен"
        cfg = get_config()
        if kind == "disk":
            workers = int(strategy.get("disk_workers") or 0) or auto_workers()
            workers = max(1, min(workers, 5))
            args = ["download", "--workers", str(workers),
                    "--no-scan", "--retry"]
            script = "main.py"
        else:
            workers = int(strategy.get("workers") or 10)
            timeout = float(strategy.get("timeout") or 7.0)
            out_dir = cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR)
            args = [
                "--out-dir", out_dir,
                "--media", cfg.get("unlim_media") or "all",
                "--target", "all",
                "--workers", str(workers),
                "--timeout", f"{timeout:g}",
            ]
            if strategy.get("from_end"):
                args.append("--from-end")
            if not cfg.get("previews", True):
                args.append("--no-previews")
            script = "unlim_backup.py"
        self._clear_heartbeat(kind)
        reset_session_counters(kind)
        # Помеченный мёртвым сервер хранения получает несколько настоящих
        # запросов за круг: если Яндекс его уже вернул в строй, файлы поедут
        # сразу, а не через полчаса TTL.
        return tm.start(kind, script, args, env_extra={"YADISK_DEAD_HOST_PROBES": "3"})

    def _run_round(self, strategy: dict) -> tuple[int, bool, str]:
        """Один круг: сброс счётчиков, запуск линий, наблюдение.

        Возвращает (сколько скачано, была ли остановка по застою, фатальная
        ошибка или пустая строка).
        """
        if self._stop:
            return 0, False, ""
        self._log("   возвращаю отложенные и обнуляю счётчики…")
        returned = self._reset_lost()
        if returned:
            self._log(f"   в очередь возвращено: {returned}")
        self._refresh_remaining()
        if self._remaining_total() == 0:
            return 0, False, ""

        if strategy.get("scan"):
            self._scan_all()
            if self._stop:
                return 0, False, ""
            self._mark_unavailable()
            self._reset_lost()
            self._refresh_remaining()
            if self._remaining_total() == 0:
                return 0, False, ""

        before = {k: self._progress(k) for k in ("disk", "unlim")}
        started_kinds = []
        for kind in ("disk", "unlim"):
            if self._remaining(kind) <= 0:
                continue
            ok, msg = self._start_lane(kind, strategy)
            if ok:
                started_kinds.append(kind)
            else:
                self._log(f"   {self._lane_title(kind)}: не запущено — {msg}")
        if not started_kinds:
            return 0, False, ""

        healthy = self._watch(started_kinds, strategy)
        after = {k: self._progress(k) for k in ("disk", "unlim")}
        for kind in ("disk", "unlim"):
            self.gained_by_kind[kind] += max(0, after[kind][0] - before[kind][0])
        gained = sum(max(0, after[k][0] - before[k][0]) for k in ("disk", "unlim"))
        self.gained_round = gained
        self._refresh_remaining()

        fatal = self._fatal_reason()
        return gained, not healthy, fatal

    @staticmethod
    def _lane_title(kind: str) -> str:
        return "Диск" if kind == "disk" else "Безлимит"

    def _watch(self, kinds: list[str], strategy: dict) -> bool:
        """Ждёт завершения линий. False — если пришлось остановить по застою."""
        grace = self.SCAN_GRACE_SEC if strategy.get("scan") else self.GRACE_SEC
        started = time.time()
        last_gain = started
        prev = {k: self._progress(k) for k in kinds}
        last_hb = {k: self._heartbeat_mtime(k) for k in kinds}
        while not self._stop:
            time.sleep(self.TICK)
            now = time.time()
            for kind in kinds:
                cur = self._progress(kind)
                if cur > prev[kind]:
                    last_gain = now
                prev[kind] = cur
                hb = self._heartbeat_mtime(kind)
                if hb > last_hb[kind]:
                    last_gain = now
                    last_hb[kind] = hb
            running = [k for k in kinds if self._slot_running(k)]
            if not running:
                return True
            if now - started > grace and now - last_gain > self.STALL_SEC:
                self._log(
                    f"   нет прогресса {int(now - last_gain)} с — останавливаю "
                    f"линии и меняю тактику"
                )
                for kind in running:
                    tm.stop(kind)
                # Даём процессам выйти: иначе следующий круг начнётся, пока
                # прошлый ещё держит файлы в in_progress и пишет статус.
                end = time.time() + 8.0
                while time.time() < end and any(
                    self._slot_running(k) for k in running
                ):
                    time.sleep(0.2)
                return False
        return True

    def _fatal_reason(self) -> str:
        """Причина, по которой крутить дальше бессмысленно."""
        try:
            unlim_st = json.loads(paths.UNLIM_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            unlim_st = {}
        err = unlim_st.get("error")
        if err == "cookies_expired":
            return "Сессия Яндекса истекла — обновите cookies"
        if err == "disk_full":
            return "Недостаточно места на диске Безлимита"
        try:
            disk_st = json.loads(paths.DISK_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            disk_st = {}
        if disk_st.get("state") == "disk_full":
            return "Недостаточно места на диске Диска"
        if disk_st.get("state") == "disk_offline":
            return "Диск отключён — подключите его и включите «Зверя» снова"
        # Код 2 у main.py — токен не принят: смена тактики не поможет.
        with tm._lock:
            disk_task = tm._tasks.get("disk")
            code = disk_task.proc.poll() if disk_task else None
            tail = list(disk_task.log)[-30:] if disk_task else []
        if code == 2:
            return "Обычный Диск: токен не принят — получите новый токен"
        if code not in (None, 0, 1, -15):
            return f"Обычный Диск остановился с кодом {code}"
        # Безлимит без статуса и с ошибкой в журнале — обычно куки/сессия.
        if not unlim_st and any(
            ("куки" in line.lower() or "cookies" in line.lower()
             or "сессия" in line.lower())
            for line in tail
        ):
            return "Безлимит: проверьте cookies (сессия Яндекса)"
        return ""

    def _pause(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self._stop and time.time() < end:
            time.sleep(0.5)

    # ── основной цикл ───────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._log("💾 сохраняю копию баз перед работой…")
            ok, item, err = _auto_backup("beast", ("disk", "unlim"))
            if ok and item:
                self._log(f"   копия баз: {item.get('id') or item.get('name') or 'готова'}")
            elif not ok:
                self._log(f"   копию баз сделать не удалось: {err}")
            empty_streak = 0
            while not self._stop:
                self._mark_unavailable()
                self._refresh_remaining()
                if self._remaining_total() == 0:
                    self._finish("done", f"Зверь: всё скачано, {self.gained} файлов")
                    return
                strategy = self.STRATEGIES[self.strategy_index % len(self.STRATEGIES)]
                self.strategy = strategy["title"]
                self.round += 1
                self._log(f"🐾 круг {self.round}, тактика «{self.strategy}»")
                gained, stalled, fatal = self._run_round(strategy)
                self.gained += gained
                if self._stop:
                    self.state = "stopped"
                    self.message = "Зверь остановлен"
                    self._log("⏹ остановлено пользователем")
                    return
                if fatal:
                    self._finish("failed", fatal)
                    return
                if self._remaining_total() == 0:
                    self._finish("done", f"Зверь: всё скачано, {self.gained} файлов")
                    return
                if gained > 0:
                    empty_streak = 0
                    self._log(f"   +{gained} файлов — тактика работает, "
                              f"остаюсь на ней; осталось {self._remaining_total()}")
                    self._pause(3.0)
                    continue
                empty_streak += 1
                self.strategy_index += 1
                nxt = self.STRATEGIES[self.strategy_index % len(self.STRATEGIES)]
                self._log(f"   новых файлов нет — следующая тактика: «{nxt['title']}»")
                if empty_streak >= len(self.STRATEGIES):
                    idx = min(empty_streak - len(self.STRATEGIES),
                              len(self.EMPTY_PAUSES) - 1)
                    pause = self.EMPTY_PAUSES[idx]
                    self._log(f"   полный круг без файлов — пауза {int(pause / 60)} мин")
                    self._pause(pause)
                else:
                    self._pause(3.0)
            self.state = "stopped"
            self.message = "Зверь остановлен"
        except Exception as e:  # noqa: BLE001 — режим не должен ронять сервер
            self.state = "failed"
            self.message = f"Зверь: ошибка — {e}"
            self._log(f"❌ {e}")
        finally:
            self.finished_at = time.time()

    def _finish(self, state: str, message: str) -> None:
        self.state = state
        self.message = message
        self._log(("🎉 " if state == "done" else "❌ ") + message)
        for slot in ("disk", "unlim", "unlim_collect"):
            try:
                tm.stop(slot)
            except Exception:  # noqa: BLE001
                pass


beast = Beast()


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
            updater.record_check(release)
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

# Уведомления Windows. Браузерный `Notification` в окне WebView2 запрещён
# навсегда (`Notification.permission === 'denied'`, и `requestPermission()`
# ничего не спрашивает), поэтому показываем системный балун из иконки в трее.
_notifier = None


def register_shutdown_hook(func) -> None:
    """launcher передаёт сюда свой quit_app, чтобы установка могла закрыть программу."""
    global _shutdown_hook
    _shutdown_hook = func


def set_notifier(func) -> None:
    """launcher передаёт сюда показ системного уведомления из трея."""
    global _notifier
    _notifier = func


def native_notify(title: str, body: str = "") -> bool:
    """Показать уведомление средствами Windows. False — канала нет."""
    func = _notifier
    if func is None:
        return False
    try:
        func(title or "Yad.backup", body or "")
    except Exception:  # noqa: BLE001 — уведомление не должно ломать работу
        return False
    return True


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
            "'Yad.backup') } catch { } }"
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
        # Ждём выхода приложения без ограничения по времени. Раньше стоял
        # -Timeout 180: если приложение жило дольше (шла выгрузка), установщик
        # ставился поверх работающего — у тестера 28.10 так пропал
        # `_internal\certifi\cacert.pem`, и 866 файлов упали с OSError.
        # Лучше не установить обновление вовсе, чем сломать живую программу.
        "try { Wait-Process -Id " + str(pid) + " -ErrorAction Stop } catch { }",
        "Start-Sleep -Seconds 2",
        action,
        *restart_lines,
        "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force "
        "-ErrorAction SilentlyContinue",
    ]
    script = chr(10).join(line for line in lines if line) + chr(10)
    helper.write_text(script, encoding="utf-8-sig")
    return helper


def write_profile_restart_helper(target_dir: Path) -> Path:
    """Помощник, который дождётся выхода программы и запустит её с профилем.

    Пути данных фиксируются при импорте `paths`, поэтому сменить профиль в
    работающем процессе нельзя — только перезапуск с другим `YADISK_DATA_DIR`.
    """
    work = paths.LOG_DIR
    work.mkdir(parents=True, exist_ok=True)
    helper = work / "switch_profile.ps1"
    log_file = work / "switch_profile.log"
    lines = [
        "$ErrorActionPreference = 'Continue'",
        "('переключение профиля ' + (Get-Date -Format s)) | "
        "Out-File -Append -Encoding utf8 " + _ps_quote(log_file),
        # Ждём штатного выхода до 10 секунд: принудительное убийство может
        # оставить живыми дочерние воркеры и оборвать запись в базу. Только если
        # процесс завис — завершаем его сами.
        "$sw = Get-Process -Id " + str(os.getpid()) + " -ErrorAction SilentlyContinue",
        "if ($sw) { try { $sw.WaitForExit(10000) | Out-Null } catch { } }",
        "$left = Get-Process -Id " + str(os.getpid()) + " -ErrorAction SilentlyContinue",
        "if ($left) { Stop-Process -Id " + str(os.getpid()) + " -Force "
        "-ErrorAction SilentlyContinue }",
        "Start-Sleep -Milliseconds 700",
        # Дочерний процесс наследует переменную — так paths выбирает новый каталог.
        "$env:YADISK_DATA_DIR = " + _ps_quote(str(target_dir)),
        "Start-Process -FilePath " + _ps_quote(sys.executable),
        "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force "
        "-ErrorActionSilentlyContinue",
    ]
    helper.write_text(chr(10).join(lines) + chr(10), encoding="utf-8-sig")
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
    # Порог по умолчанию: сторож включён «из коробки» и останавливает выгрузку,
    # когда свободного места остаётся меньше 20 ГБ.
    DEFAULT_THRESHOLD_GB = 20.0

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop = False
        self.state = "idle"  # idle | watching | triggered | stopped
        self.threshold_gb: float = self.DEFAULT_THRESHOLD_GB
        self.free_gb: Optional[float] = None
        self.message: str = ""
        self.started_at: Optional[float] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, threshold_gb: float) -> tuple[bool, str]:
        with self._lock:
            self.threshold_gb = max(1.0, float(threshold_gb))
            if self.is_running():
                # Поток перечитывает порог на каждом круге, поэтому менять его
                # у работающего сторожа можно без перезапуска — раньше выбор
                # в списке молча пропадал.
                return True, f"Порог сторожа изменён: {self.threshold_gb:g} ГБ"
            self._stop = False
            self.state = "watching"
            self.message = ""
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, f"Сторож включён (порог {self.threshold_gb:g} ГБ)"

    def set_threshold(self, threshold_gb: float) -> tuple[bool, str]:
        """Запоминает порог даже у выключенного сторожа.

        Настройка должна сохраняться сразу: человек выбирает 10 ГБ, выключает
        сторожа, а при следующем запуске видит снова 20 — так было до 35.8.5.
        """
        with self._lock:
            self.threshold_gb = max(1.0, float(threshold_gb))
        return True, f"Порог сторожа: {self.threshold_gb:g} ГБ"

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
        # «Зверь» останавливаем первым: иначе он тут же запускает новый круг,
        # и диск продолжает заполняться уже после срабатывания сторожа.
        try:
            beast.stop()
        except Exception:  # noqa: BLE001 — остановка не должна мешать остальным
            pass
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


def _storage_host(error: object) -> str:
    """«storage_down | storage.yandex.net» → «storage.yandex.net»."""
    if not isinstance(error, str) or "|" not in error:
        return ""
    return error.split("|", 1)[1].strip()


def db_stats(db_file: Path, table: str = "files",
             id_col: str = "remote_path") -> dict:
    if not db_file.exists():
        return {"total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
                "failed": 0, "in_progress": 0, "waiting": 0, "waiting_host": "",
                "lost": 0, "denied": 0, "deleted": 0, "excluded": 0,
                "bytes_total": 0, "bytes_done": 0, "bytes_by_status": {}, "recent": []}
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        out = {"total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
               "failed": 0, "in_progress": 0, "waiting": 0, "waiting_host": "",
               "lost": 0, "denied": 0, "deleted": 0, "excluded": 0,
               "bytes_total": 0, "bytes_done": 0, "bytes_by_status": {}}
        for r in c.execute(
            f"SELECT status, COUNT(*) AS n, COALESCE(SUM(size),0) AS b "
            f"FROM {table} GROUP BY status"
        ):
            st = r["status"]
            if st not in out:
                out[st] = 0
            out[st] = r["n"]
            # «Удалён из облака» — это история, а не очередь: в общий объём
            # такие файлы не входят, иначе полоса готовности откатывалась бы
            # назад после «Пометить пропавшие».
            if st == "deleted":
                continue
            out["total"] += r["n"]
            out["bytes_total"] += r["b"]
            # Объём по каждому статусу: по нему считается остаток в гигабайтах
            # для плашек и таймера.
            out["bytes_by_status"][st] = r["b"]
            if st in ("downloaded", "skipped"):
                out["bytes_done"] += r["b"]
        # «Ждут Яндекса»: файлы, которые не сбойные, а отложены до ответа
        # сервера хранения. Считаем отдельно от failed — иначе плашка пугает.
        # status IN ('pending','in_progress'), иначе цифра прыгает: пока батч
        # в работе, take_batch держит файлы в in_progress, и они «исчезают».
        out["waiting"] = 0
        out["waiting_host"] = ""
        try:
            row_w = c.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE status IN ('pending','in_progress') "
                f"AND error LIKE 'storage_down%'"
            ).fetchone()
            out["waiting"] = int(row_w[0] if row_w else 0)
            row_h = c.execute(
                f"SELECT error, COUNT(*) AS n FROM {table} "
                f"WHERE status IN ('pending','in_progress') "
                f"AND error LIKE 'storage_down%' "
                f"GROUP BY error ORDER BY n DESC LIMIT 1"
            ).fetchone()
            if row_h is not None:
                out["waiting_host"] = _storage_host(row_h[0])
        except sqlite3.Error:
            out["waiting"] = 0
            out["waiting_host"] = ""
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


# Какой файл статуса токена мы уже подхватили в os.environ. fetch_token.py
# пишет токен в .env отдельным процессом, поэтому без перечитывания UI видел
# бы старый токен до перезапуска программы.
_token_env_stamp: float | None = None


def token_fetch_status() -> dict:
    """Статус полуавтоматического получения токена (fetch_token.py)."""
    global _token_env_stamp
    data: dict = {"state": "idle", "msg": ""}
    if TOKEN_FETCH_STATUS.exists():
        try:
            data = json.loads(TOKEN_FETCH_STATUS.read_text(encoding="utf-8"))
        except Exception:
            return {"state": "unknown", "msg": "Ошибка чтения статуса"}
    if data.get("state") == "running":
        task = tm.status().get("token_fetch")
        if task is not None and not task.get("running"):
            data = {"state": "stopped", "msg": "Процесс завершён",
                    "updated_at": data.get("updated_at")}
    if data.get("state") == "done":
        try:
            stamp = TOKEN_FETCH_STATUS.stat().st_mtime
        except OSError:
            stamp = None
        if stamp is not None and stamp != _token_env_stamp:
            try:
                load_dotenv(ENV_FILE, override=True)
            except Exception:  # noqa: BLE001 - статус важнее перечитывания
                pass
            _token_env_stamp = stamp
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
            # Файлы, помеченные «удалён из облака», остаются в базе как
            # история, но в счётчики и оценку готовности не входят.
            total_files = c.execute(
                "SELECT count(*) FROM unlim_files WHERE status <> 'deleted'"
            ).fetchone()[0]
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


# ── Сессия скачивания: итоги за все проходы ─────────────────────────────────
#
# Плашки в шапке показывают итог текущей сессии: сколько скачано за все проходы
# и сколько файлов сейчас сбойные. Сессию ведёт либо отдельная задача (файл
# статуса дочернего процесса), либо полный цикл (линии, копящие итог по
# проходам). Владелец — тот, кто начался позже.

# Какие ключи файла статуса отвечают за «скачано» и «сбойные» по разделу.
_SESSION_KEYS = {"disk": ("downloaded", "failed"), "unlim": ("success", "skipped")}

# Повторяет ли запущенная вручную сессия сбойные файлы (нужно для остатка/таймера).
_session_retry: dict[str, bool] = {"disk": False, "unlim": False}

# Сколько секунд статус может оставаться «активным» без идущей задачи, прежде
# чем UI перестанет показывать фантомную скорость. Процесс мог быть закрыт
# аварийно и не успеть записать active=False.
STALE_ACTIVE_SEC = 90.0


def _lane_tasks_running(tasks: dict, prefix: str) -> bool:
    return any(
        name.startswith(prefix) and bool((info or {}).get("running"))
        for name, info in (tasks or {}).items()
    )


def _mark_stale_inactive(st: dict, lane_running: bool, now: float) -> dict:
    """Гасит «active» у файла статуса, оставшегося от аварийно закрытой задачи."""
    if not st or not st.get("active") or lane_running:
        return st
    updated = float(st.get("updated_at") or 0)
    if updated and now - updated > STALE_ACTIVE_SEC:
        out = dict(st)
        out["active"] = False
        out["speed_mb"] = 0
        out["speed_mbit"] = 0
        return out
    return st


# ── Итог последней сессии после перезапуска ─────────────────────────────────
#
# Плашки в шапке показывают, сколько файлов скачалось за последнюю сессию.
# После перезапуска приложения задач уже нет, и сводка строилась бы из нулей.
# Поэтому итог пишется в logs/last_session.json и показывается до тех пор,
# пока не начнётся новая сессия.

def _load_last_sessions() -> dict:
    try:
        data = json.loads(paths.LAST_SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — файла может не быть
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if k in ("disk", "unlim") and isinstance(v, dict)}


def _save_last_sessions(data: dict) -> None:
    try:
        paths.LAST_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = paths.LAST_SESSION_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, paths.LAST_SESSION_FILE)
    except OSError:
        pass


def remember_sessions(session: dict) -> None:
    """Запоминает итог живой сессии, чтобы он пережил перезапуск."""
    data = _load_last_sessions()
    changed = False
    for kind in ("disk", "unlim"):
        s = session.get(kind)
        if not s or s.get("restored") or not s.get("started_at"):
            continue
        rec = {
            "started_at": s.get("started_at"),
            "success": 0 if s.get("preparing") else int(s.get("success") or 0),
            "pass": int(s.get("pass") or 0),
            "passes": int(s.get("passes") or 0),
            "mode": s.get("mode") or "all",
            "total": int(s.get("total") or 0),
        }
        if data.get(kind) != rec:
            data[kind] = rec
            changed = True
    if changed:
        _save_last_sessions(data)


def restore_sessions(session: dict, disk_db: dict, unlim_db: dict) -> dict:
    """Дополняет сводку итогом прошлой сессии, если текущей ещё нет."""
    saved = _load_last_sessions()
    for kind in ("disk", "unlim"):
        if session.get(kind):
            continue
        rec = saved.get(kind)
        if not rec or not rec.get("started_at"):
            continue
        db = unlim_db if kind == "unlim" else disk_db
        failed_key = _SESSION_KEYS[kind][1]
        out = {
            "started_at": rec.get("started_at"),
            "running": False,
            "preparing": False,
            "restored": True,
            "success": int(rec.get("success") or 0),
            "pass": int(rec.get("pass") or 0),
            "passes": int(rec.get("passes") or 0),
            "mode": str(rec.get("mode") or "all"),
            "total": int(rec.get("total") or 0),
        }
        out["failed"] = int(db.get(failed_key) or 0)
        out["waiting"] = int(db.get("waiting") or 0)
        out["lost"] = int(db.get("lost") or 0)
        out["remaining"] = int(db.get("pending") or 0) + int(db.get("in_progress") or 0)
        by_status = db.get("bytes_by_status") or {}
        out["remaining_bytes"] = sum(
            int(by_status.get(k) or 0)
            for k in ("pending", "in_progress")
        )
        session[kind] = out
    return session


def _status_fresh(st: dict, started_at: float | None) -> bool:
    """Статус относится к текущей задаче, а не остался от прошлой сессии."""
    updated = float(st.get("updated_at") or 0)
    if not updated:
        return False
    # Старт задачи точнее, чем started_at внутри файла: старый файл мог быть
    # записан давно и сам себе «свежий».
    started = float(started_at or st.get("started_at") or 0)
    return updated >= started - 5


def build_session(fc: dict, tasks: dict, unlim_st: dict, disk_st: dict,
                  disk_db: dict, unlim_db: dict,
                  retry: dict[str, bool] | None = None,
                  beast_st: dict | None = None) -> dict:
    """Сводка сессии для плашек и таймера: {disk: {...}|None, unlim: {...}|None}."""
    retry = retry or {}
    beast_st = beast_st or {}
    out: dict = {}
    cycle_started = fc.get("started_at")
    beast_started = beast_st.get("started_at")
    beast_finished = beast_st.get("finished_at")
    beast_running = bool(beast_st.get("running"))
    gained_by_kind = beast_st.get("gained_by_kind") or {}
    for kind in ("disk", "unlim"):
        task = tasks.get(kind) or {}
        # Сканирование и проверка живут в тех же слотах, что и выгрузка, но
        # сессией не являются: их нулевой статус не должен затирать итог.
        task_is_download = str(task.get("kind") or "download") == "download"
        st = unlim_st if kind == "unlim" else disk_st
        db = unlim_db if kind == "unlim" else disk_db
        failed_key = _SESSION_KEYS[kind][1]
        task_started = task.get("started_at") if task_is_download else None
        # Отдельная выгрузка, запущенная после завершения цикла, — новая сессия:
        # старые линии к ней уже не относятся.
        takeover = bool(cycle_started) and not fc.get("running") and task_started \
            and task_started > (fc.get("finished_at") or cycle_started)
        use_cycle = bool(cycle_started) and not takeover
        # «Зверь» владеет сессией, если стартовал позже цикла. После остановки
        # итог остаётся, пока не начнётся ручная выгрузка: её задача стартует
        # позже finished_at. Задачи-линии «Зверя» стартуют уже внутри сессии и
        # владельца не меняют.
        beast_owns = bool(beast_started) and (
            not use_cycle or beast_started > (cycle_started or 0))
        if beast_owns and not beast_running and task_started \
                and task_started > (beast_finished or beast_started or 0):
            beast_owns = False
        session = None
        if use_cycle and not beast_owns:
            lane = (fc.get("lanes") or {}).get(kind) or {}
            started = fc.get("running") or lane.get("state") not in (None, "ожидание")
            if started:
                running = bool(fc.get("running"))
                # Пока дочерний процесс прохода жив, к итогу линии прибавляем его
                # живые цифры: иначе плашка «замирает» между проходами.
                success = int(lane.get("downloaded") or 0)
                if task_is_download and task.get("running") \
                        and _status_fresh(st, task_started):
                    success += int(st.get(_SESSION_KEYS[kind][0]) or 0)
                session = {
                    "started_at": cycle_started,
                    "running": running,
                    "preparing": running and int(lane.get("pass") or 0) == 0,
                    "success": success,
                    "pass": int(lane.get("pass") or 0),
                    "passes": int(lane.get("passes") or 0),
                    "retry_failed": True,
                }
        elif beast_owns:
            running = beast_running
            success = int(gained_by_kind.get(kind) or 0)
            # Прошлые круги уже в накопительном счётчике, а скачанное текущей
            # линией лежит в её файле статуса, пока процесс идёт.
            live = task_is_download and bool(task.get("running")) \
                and _status_fresh(st, task_started)
            if live:
                success += int(st.get(_SESSION_KEYS[kind][0]) or 0)
            round_no = int(beast_st.get("round") or 0)
            session = {
                "started_at": beast_started,
                "running": running,
                "preparing": running and not live and round_no <= 0,
                "success": success,
                "pass": round_no,
                "passes": 0,
                "beast": True,
                "round": round_no,
                "strategy": str(beast_st.get("strategy") or ""),
                "retry_failed": True,
            }
        elif task_is_download and (
                task.get("running") or task.get("exit_code") is not None):
            running = bool(task.get("running"))
            fresh = _status_fresh(st, task_started)
            session = {
                "started_at": task_started,
                "running": running,
                "preparing": running and not fresh,
                "success": int(st.get(_SESSION_KEYS[kind][0]) or 0) if fresh else 0,
                "pass": 0,
                "passes": 0,
                "retry_failed": bool(retry.get(kind)),
            }
        if not session:
            out[kind] = None
            continue
        session["failed"] = int(db.get(failed_key) or 0)
        # «Ждут Яндекса» и «отложенные» — по базе, как и сбойные: каждый файл
        # считается один раз, цифра уменьшается, когда файл докачался.
        session["waiting"] = int(db.get("waiting") or 0)
        session["lost"] = int(db.get("lost") or 0)
        # Режим прохода нужен панели: «Выбрано N, скачано X из N» — только для
        # выгрузки по выбору, у неё total_files равен числу отмеченных файлов.
        session["mode"] = str(st.get("mode") or "all")
        session["total"] = int(st.get("total_files") or 0)
        retry_failed = session.pop("retry_failed")
        session["remaining"] = int(db.get("pending") or 0) + (
            session["failed"] if retry_failed else 0)
        # Остаток в байтах — для оценки времени по объёму, а не по числу файлов
        # (у Диска файлы разного размера, и счёт по штукам скачет в разы).
        by_status = db.get("bytes_by_status") or {}
        left_bytes = int(by_status.get("pending") or 0)
        if retry_failed:
            left_bytes += int(by_status.get(failed_key) or 0)
        session["remaining_bytes"] = left_bytes
        out[kind] = session
    return out


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

    disk_db = db_stats(DB_FILE, "files", "remote_path")
    unlim_db = db_stats(UNLIM_DB_FILE, "unlim_files", "file_id")
    tasks = tm.status()
    cycle = full_cycle.status()
    beast_info = beast.status()

    # Файл статуса мог остаться «активным» после аварийного закрытия задачи.
    # Показывать «11 МБ/с» при мёртвом процессе нельзя.
    now = time.time()
    download_status = _mark_stale_inactive(
        download_status, _lane_tasks_running(tasks, "unlim"), now)
    disk_status = _mark_stale_inactive(
        disk_status, _lane_tasks_running(tasks, "disk"), now)

    session = build_session(cycle, tasks, download_status, disk_status,
                            disk_db, unlim_db, _session_retry, beast_info)
    # После перезапуска задач нет — показываем итог последней сессии.
    session = restore_sessions(session, disk_db, unlim_db)
    remember_sessions(session)

    return {
        "config": public_config(cfg),
        # Есть ли канал системных уведомлений (иконка в трее). В браузере
        # канала нет, и окно честно показывает это в настройках.
        "notify_native": _notifier is not None,
        # Имя текущего профиля — для пилюли в шапке. Список профилей отдельно,
        # в /api/profiles: он меняется только по действию человека.
        "profile": profiles.current_profile(),
        # Свободное место на дисках выгрузки — для оценки «влезет ли очередь».
        # Считается даже при выключенном стороже: shutil.disk_usage дешёвый.
        "free_gb": _free_gb([
            cfg.get("disk_dir") or str(paths.DEFAULT_DISK_DIR),
            cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR),
        ]),
        "token": check_token(cfg["token"]),
        "cookies": check_cookies(),
        "disk": {
            "db": disk_db,
            "local": local_size(cfg.get("disk_dir") or cfg.get("download_dir") or str(paths.DEFAULT_DISK_DIR)),
        },
        "unlim": {
            "db": unlim_db,
            "local": local_size(cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR)),
            "summary": unlim_db_summary(),
        },
        "session": session,
        "ram": ram_info(),
        "full_cycle": cycle,
        # Режим «Зверь»: кнопка в шапке запуска и журнал тактик.
        "beast": beast_info,
        "update": update_manager.status(),
        # Сохранённая последняя проверка: плашка в шапке должна знать о новой
        # версии сразу, не дожидаясь ответа GitHub и не создавая второго запроса.
        "update_check": updater.last_check_summary(),
        "legacy_install": legacy_install_status(),
        "watchdog": watchdog.status(),
        "tasks": tasks,
        "verify": _verify_status(verify_mod.read_status(), tasks),
        "cloud_audit": _cloud_audit_status(tasks),
        "pipeline": pipeline.status(),
        "speed": download_status,
        "disk_speed": disk_status,
        # Финальное состояние прохода Диска: done / stopped / disk_full /
        # disk_offline. По нему UI показывает, что диск отключили (B0).
        "disk_state": disk_status.get("state"),
        "download_error": download_error,
        "cookies_fetch": cookies_fetch_status(),
        "token_fetch": token_fetch_status(),
        "last_unlim_log": get_last_unlim_log(),
        "monitor": monitor.monitor_status(),
    }


def _monitor_snapshot() -> dict:
    """Урезанный статус для heartbeat: без сетевых проверок и тяжёлых списков."""
    cfg = get_config()
    return {
        "config": public_config(cfg),
        "disk": {"db": db_stats(DB_FILE, "files", "remote_path")},
        "unlim": {"db": db_stats(UNLIM_DB_FILE, "unlim_files", "file_id")},
        "tasks": tm.status(),
        "full_cycle": full_cycle.status(),
        "cookies": check_cookies(),
    }


def start_background_services() -> None:
    """Хук исключений и мониторинг. Общая точка для webui.main() и launcher."""
    try:
        diagnostics.install_crash_hook()
    except Exception:  # noqa: BLE001 - без хука приложение должно работать
        pass
    try:
        monitor.start_monitor(_monitor_snapshot)
    except Exception:  # noqa: BLE001 - мониторинг необязателен
        pass
    # Сторож места включён по умолчанию (порог 20 ГБ). Если человек его
    # выключил, в .env лежит WATCHDOG=0 — тогда не навязываемся.
    try:
        cfg = get_config()
        # Порог помним и у выключенного сторожа: иначе выбор в списке снова
        # показывал бы 20 ГБ до первого включения.
        if cfg.get("watchdog_gb"):
            watchdog.set_threshold(float(cfg["watchdog_gb"]))
        if cfg.get("watchdog") and not watchdog.is_running():
            watchdog.start(float(cfg.get("watchdog_gb")
                                 or DiskWatchdog.DEFAULT_THRESHOLD_GB))
    except Exception:  # noqa: BLE001 - без сторожа приложение работает
        pass
    # Старые превью лежали внутри папки выгрузки — переносим их на диск C,
    # иначе они пропадут при смене внешнего диска.
    try:
        migrate_legacy_previews()
    except Exception:  # noqa: BLE001 - превью не должны мешать запуску
        pass
    # Старые «снимки баз» с 35.8.0 живут в общем списке резервных копий.
    try:
        backups.migrate_snapshots(paths.DATA_DIR)
    except Exception:  # noqa: BLE001 - перенос копий не должен мешать запуску
        pass


class StartPayload(BaseModel):
    workers: int = Field(default=4, ge=1, le=6)
    do_disk: bool = True
    do_unlim: bool = True


@app.post("/api/start")
def api_start(payload: StartPayload):
    if beast.is_running():
        return {"ok": False, "msg": "Включён режим «Зверь» — остановите его, чтобы запускать вручную"}
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


@app.post("/api/download/start")
def api_download_start(payload: DownloadControlPayload):
    log_action("/api/download/start", payload)
    if beast.is_running():
        return {"ok": False, "mode": payload.mode,
                "msg": "Включён режим «Зверь» — остановите его, чтобы запускать вручную"}
    cfg = get_config()
    problem = check_target_dir(
        (cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR))
        if payload.mode == "unlim"
        else (cfg.get("disk_dir") or cfg.get("download_dir")
              or str(paths.DEFAULT_DISK_DIR))
    )
    if problem:
        # Отключённый диск ловим до запуска процессов: иначе дочерний процесс
        # падал с окном «Unhandled exception in script».
        return {"ok": False, "msg": problem, "mode": payload.mode}
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
        if not cfg.get("previews", True):
            args.append("--no-previews")
        # «all» и «skipped» — повтор сбойных, значит они тоже входят в остаток.
        _session_retry["unlim"] = payload.target in ("all", "skipped")
        reset_session_counters("unlim")
        ok, msg = tm.start("unlim", "unlim_backup.py", args)
        return {"ok": ok, "msg": msg, "mode": "unlim"}
    else:
        out_dir = cfg.get("disk_dir") or cfg.get("download_dir") or str(paths.DEFAULT_DISK_DIR)
        args = ["download", "--workers", str(auto_workers())]
        if DB_FILE.exists():
            args.append("--no-scan")
        _session_retry["disk"] = False
        reset_session_counters("disk")
        ok, msg = tm.start("disk", "main.py", args)
        return {"ok": ok, "msg": msg, "mode": "disk"}


class DiskResumePayload(BaseModel):
    scope: str = "auto"


@app.post("/api/disk/resume")
def api_disk_resume(payload: DiskResumePayload):
    """«Продолжить» после заполнения диска или его отключения.

    Запуск разрешён только когда места действительно хватает: иначе первый же
    крупный файл снова упрётся в ноль. Порог берём у сторожа плюс 1 ГБ запаса.
    """
    log_action("/api/disk/resume", payload)
    if beast.is_running():
        return {"ok": False, "msg": "Включён режим «Зверь» — он остановлен сторожем. "
                                    "Освободите место и включите его снова"}
    cfg = get_config()
    scope = (payload.scope or "auto").strip().lower()
    if scope not in ("disk", "unlim"):
        # Авто: заполненный диск Безлимита пишет download_error, обычный —
        # disk_state. Если оба молчат, продолжаем Безлимит как более частый.
        try:
            disk_state = json.loads(paths.DISK_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            disk_state = {}
        scope = "disk" if str(disk_state.get("state") or "") in (
            "disk_full", "disk_offline"
        ) else "unlim"
    folder = (cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR)) if scope == "unlim" \
        else (cfg.get("disk_dir") or cfg.get("download_dir") or str(paths.DEFAULT_DISK_DIR))
    problem = check_target_dir(folder)
    if problem:
        return {"ok": False, "msg": problem, "scope": scope}
    free = _free_gb([folder])
    need = float(watchdog.threshold_gb) + 1.0
    if free is not None and free < need:
        return {"ok": False, "scope": scope,
                "msg": f"Свободно {free:.1f} ГБ, нужно хотя бы {need:.1f} ГБ. "
                       f"Освободите место и нажмите «Продолжить» ещё раз."}
    if scope == "unlim":
        return api_download_start(DownloadControlPayload(
            mode="unlim", media=cfg.get("unlim_media") or "all",
            target="all", workers=10, from_end=True,
        ))
    return api_download_start(DownloadControlPayload(mode="disk"))


class DownloadAllPayload(BaseModel):
    workers: int = 10
    # Сколько проходов по сбойным делать за запуск. Ровно столько и будет.
    passes: int = Field(default=3, ge=1, le=10)


@app.post("/api/download/all")
def api_download_all(payload: DownloadAllPayload):
    """Полный цикл: проверить облако, скачать новые и докачать сбойные."""
    log_action("/api/download/all", payload)
    if beast.is_running():
        return {"ok": False, "state": full_cycle.state,
                "msg": "Включён режим «Зверь» — он уже качает; нажмите «зверь», чтобы остановить"}
    ok, msg = full_cycle.start(payload.workers, payload.passes)
    return {"ok": ok, "msg": msg, "state": full_cycle.state}


@app.post("/api/beast/start")
def api_beast_start():
    log_action("/api/beast/start")
    ok, msg = beast.start()
    return {"ok": ok, "msg": msg, "beast": beast.status()}


@app.post("/api/beast/stop")
def api_beast_stop():
    log_action("/api/beast/stop")
    ok, msg = beast.stop()
    return {"ok": ok, "msg": msg, "beast": beast.status()}


class WatchdogPayload(BaseModel):
    threshold_gb: float = 20.0


def _remember_watchdog(enabled: bool, threshold_gb: Optional[float] = None) -> None:
    """Запоминает выбор человека в .env: сторож включён по умолчанию."""
    try:
        if not ENV_FILE.exists():
            ENV_FILE.write_text("", encoding="utf-8")
        set_key(str(ENV_FILE), "WATCHDOG", "1" if enabled else "0",
                quote_mode="never")
        if threshold_gb is not None:
            set_key(str(ENV_FILE), "WATCHDOG_GB", f"{float(threshold_gb):g}",
                    quote_mode="never")
        load_dotenv(ENV_FILE, override=True)
    except Exception:  # noqa: BLE001 — память о настройке не должна мешать
        pass


@app.post("/api/watchdog/start")
def api_watchdog_start(payload: WatchdogPayload):
    log_action("/api/watchdog/start", payload)
    ok, msg = watchdog.start(payload.threshold_gb)
    if ok:
        _remember_watchdog(True, payload.threshold_gb)
    return {"ok": ok, "msg": msg}


@app.post("/api/watchdog/threshold")
def api_watchdog_threshold(payload: WatchdogPayload):
    """Сохраняет порог, не включая и не выключая сторожа."""
    log_action("/api/watchdog/threshold", payload)
    ok, msg = watchdog.set_threshold(payload.threshold_gb)
    if ok:
        # Текущее состояние сторожа не меняем: только запоминаем порог.
        _remember_watchdog(watchdog.is_running(), payload.threshold_gb)
    return {"ok": ok, "msg": msg}


@app.post("/api/watchdog/stop")
def api_watchdog_stop():
    ok, msg = watchdog.stop()
    _remember_watchdog(False)
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
    what: str  # disk_failed | unlim_skipped | unlim_stuck | lost


@app.post("/api/reset")
def api_reset(payload: ResetPayload):
    """Возвращает файлы из failed/skipped/in_progress обратно в pending.

    Меняются только статусы в БД — уже скачанные файлы на диске не трогаются.
    Пока идёт соответствующая выгрузка, сброс запрещён: иначе воркеры могут
    взять один и тот же файл дважды.
    """
    log_action("/api/reset", payload)
    what = payload.what

    if what == "lost":
        # «Отложенные сервером»: Яндекс отвечал 500 несколько проходов подряд
        # (lost) или закрыл доступ 403 (denied). Это явное действие
        # пользователя («Попробовать»), поэтому счётчики обнуляем — у файла
        # снова три попытки. Файлы, которых нет в облаке (404), не трогаем:
        # статус deleted остаётся.
        if _any_task_running(("disk", "unlim")):
            return {"ok": False, "msg": "Сначала остановите выгрузку"}
        ok, item, err = _auto_backup("before-reset", ("disk", "unlim"))
        if not ok:
            return {"ok": False,
                    "msg": f"Не удалось сохранить копию баз: {err}"}
        total = 0
        for db_file, table in ((DB_FILE, "files"), (UNLIM_DB_FILE, "unlim_files")):
            if not Path(db_file).exists():
                continue
            con = None
            try:
                con = sqlite3.connect(str(db_file), timeout=30)
                with con:
                    total += con.execute(
                        f"UPDATE {table} SET status='pending', error=NULL, "
                        f"server_fails=0, retry_after=NULL, attempts=0 "
                        f"WHERE status IN ('lost','denied')"
                    ).rowcount
            except sqlite3.Error as e:
                return {"ok": False, "msg": f"Ошибка БД: {e}"}
            finally:
                if con is not None:
                    con.close()
        return {"ok": True, "count": total,
                "backup": item["id"] if item else "",
                "msg": f"Отложенных файлов возвращено в очередь: {total}"}

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

    scope = "disk" if what == "disk_failed" else "unlim"
    ok, item, err = _auto_backup("before-reset", (scope,))
    if not ok:
        return {"ok": False,
                "msg": f"Не удалось сохранить копию базы: {err}"}

    con = None
    try:
        con = sqlite3.connect(str(db_file), timeout=30)
        with con:
            count = con.execute(sql).rowcount
        return {"ok": True, "count": count,
                "backup": item["id"] if item else "",
                "msg": f"Сброшено {count}: {done}"}
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


class NotifyPayload(BaseModel):
    title: str = ""
    body: str = ""


@app.post("/api/notify")
def api_notify(payload: NotifyPayload):
    """Системное уведомление Windows из окна.

    Окно WebView2 не умеет браузерные `Notification` (разрешение зашито как
    `denied`), поэтому уведомление показывает иконка в трее.
    """
    ok = native_notify(payload.title, payload.body)
    return {"ok": ok, "native": ok}


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


class DirCheckPayload(BaseModel):
    path: str = ""


@app.post("/api/check_dir")
def api_check_dir(payload: DirCheckPayload):
    """Проверка папки БЕЗ её создания — кнопка «Проверить папку» в настройках.

    Отвечает теми же словами, что увидит человек: «✅ папка доступна, свободно
    240 ГБ» или «❌ Диска G: сейчас нет». Папку создаёт уже сама выгрузка.
    """
    info = check_target_path(payload.path)
    log_action("/api/check_dir", {"ok": info.get("ok"), "path": info.get("path", "")[:300]})
    return info


class PreviewsPayload(BaseModel):
    enabled: bool = True


@app.post("/api/config/previews")
def api_set_previews(payload: PreviewsPayload):
    """Тумблер «сохранять превью отложенных фото»."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE), "PREVIEWS", "1" if payload.enabled else "0", quote_mode="never")
    load_dotenv(ENV_FILE, override=True)
    return {"ok": True, "previews": payload.enabled}


class PreviewsDirPayload(BaseModel):
    dir: str = ""


def migrate_previews_dir(old_root: str, new_root: str) -> dict:
    """Переносит превью в новую папку и переписывает пути в обеих базах.

    Возвращает счётчики для сообщения человеку: сколько файлов перенесено и
    сколько записей в базах поправлено. Ошибки не бросает — превью не должны
    ломать настройку.
    """
    moved = 0
    old_abs = os.path.abspath(old_root) if old_root else ""
    new_abs = os.path.abspath(new_root) if new_root else ""
    if not old_abs or not new_abs or old_abs == new_abs:
        return {"moved": 0, "updated": 0}
    if os.path.isdir(old_abs):
        for dirpath, _dirnames, filenames in os.walk(old_abs):
            rel = os.path.relpath(dirpath, old_abs)
            target_dir = new_abs if rel in (".", "") else os.path.join(new_abs, rel)
            for name in filenames:
                src = os.path.join(dirpath, name)
                dst = os.path.join(target_dir, name)
                try:
                    if os.path.exists(dst):
                        os.remove(src)  # уже перенесён раньше
                    else:
                        os.makedirs(target_dir, exist_ok=True)
                        shutil.move(src, dst)
                    moved += 1
                except OSError:
                    pass

    updated = 0
    for db_file, table in ((DB_FILE, "files"), (UNLIM_DB_FILE, "unlim_files")):
        if not Path(db_file).exists():
            continue
        conn = None
        try:
            conn = sqlite3.connect(str(db_file), timeout=30)
            cur = conn.execute(
                f"UPDATE {table} SET preview_path = ? || substr(preview_path, ?) "
                f"WHERE substr(preview_path, 1, ?) = ?",
                (new_abs, len(old_abs) + 1, len(old_abs), old_abs),
            )
            updated += cur.rowcount
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()
    return {"moved": moved, "updated": updated}


def migrate_legacy_previews() -> dict:
    """Переносит превью из старых версий: они лежали в `<папка выгрузки>/_превью`.

    Без переноса они терялись при смене внешнего диска. Пустые папки-остатки
    убираем, чтобы не путали.
    """
    cfg = get_config()
    total = {"moved": 0, "updated": 0}
    # (папка выгрузки, куда класть): старые `_превью` раскладываем по видам.
    bases = (
        (cfg.get("unlim_dir"), previews.preview_kind_dir("unlim")),
        (cfg.get("disk_dir") or cfg.get("download_dir"),
         previews.preview_kind_dir("disk")),
    )
    for base, new_root in bases:
        if not base:
            continue
        old = os.path.join(str(base), previews.LEGACY_PREVIEW_DIR_NAME)
        if not os.path.isdir(old):
            continue
        if os.path.abspath(old) == os.path.abspath(new_root):
            continue
        res = migrate_previews_dir(old, new_root)
        total["moved"] += res["moved"]
        total["updated"] += res["updated"]
        # Убираем только опустевшие папки — то, что не перенеслось, не трогаем.
        try:
            for dirpath, _dirnames, _filenames in os.walk(old, topdown=False):
                os.rmdir(dirpath)
        except OSError:
            pass
    return total


@app.post("/api/config/previews_dir")
def api_set_previews_dir(payload: PreviewsDirPayload):
    """Папка превью. По умолчанию — на диске C рядом с базами."""
    new_dir = (payload.dir or "").strip()
    old_dir = previews.preview_root()
    if new_dir:
        new_dir = os.path.abspath(os.path.expanduser(new_dir))
        try:
            os.makedirs(new_dir, exist_ok=True)
        except OSError as e:
            return {"ok": False, "msg": f"Не удалось создать папку: {e}"}
        if not os.access(new_dir, os.W_OK):
            return {"ok": False, "msg": "В эту папку нельзя писать"}
    else:
        # Пустое поле — вернуть папку по умолчанию (диск C).
        new_dir = str(paths.DATA_DIR / previews.PREVIEW_DIR_NAME)

    res = migrate_previews_dir(old_dir, new_dir)
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    # Пустое значение в .env означает «папка по умолчанию».
    set_key(str(ENV_FILE), "PREVIEWS_DIR",
            new_dir if new_dir != str(paths.DATA_DIR / previews.PREVIEW_DIR_NAME) else "",
            quote_mode="never")
    load_dotenv(ENV_FILE, override=True)
    final_dir = previews.preview_root()
    log_action("/api/config/previews_dir",
               {"dir": final_dir, "moved": res["moved"], "updated": res["updated"]})
    msg = f"Превью теперь в {final_dir}"
    if res["moved"]:
        msg += f" · перенесено файлов: {res['moved']}"
    return {"ok": True, "msg": msg, "dir": final_dir,
            "moved": res["moved"], "updated": res["updated"]}


def open_in_file_manager(folder) -> None:
    """Открывает папку в системном файловом менеджере.

    Отдельной функцией — чтобы тесты подменяли её целиком: на Linux
    os.startfile нет, и проверять «открылось ли» по факту нельзя.
    """
    if os.name == "nt":
        os.startfile(str(folder))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


@app.post("/api/previews/open")
def api_previews_open():
    """Открывает папку превью в проводнике; пустую создаёт заранее."""
    try:
        folder = Path(previews.preview_root())
        folder.mkdir(parents=True, exist_ok=True)
        open_in_file_manager(folder)
        return {"ok": True, "path": str(folder)}
    except Exception as e:  # noqa: BLE001 — причину показываем в окне
        return {"ok": False,
                "error": diagnostics.redact_text(f"{type(e).__name__}: {e}")}


# ── Облачные папки и типы файлов (C2) ───────────────────────────────────────

MEDIA_LABELS = {"photos": "только фото", "videos": "только видео", "all": "фото и видео"}


class ScanRootsPayload(BaseModel):
    roots: list[str] = []


@app.post("/api/config/scan_roots")
def api_set_scan_roots(payload: ScanRootsPayload):
    """Запоминает, какие папки облачного Диска обходить."""
    roots = normalize_scan_roots(payload.roots)
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE), "DISK_SCAN_ROOTS", ",".join(roots), quote_mode="never")
    load_dotenv(ENV_FILE, override=True)
    log_action("/api/config/scan_roots", {"roots": roots})
    if roots == ["/"]:
        msg = "Сохранено: обходить весь Диск"
    else:
        msg = "Сохранено папок: " + str(len(roots))
    return {"ok": True, "msg": msg, "scan_roots": roots}


class UnlimMediaPayload(BaseModel):
    media: str = "all"


@app.post("/api/config/unlim_media")
def api_set_unlim_media(payload: UnlimMediaPayload):
    """Запоминает тип контента Безлимита для полного цикла."""
    media = normalize_unlim_media(payload.media, "")
    if not media:
        return {"ok": False, "msg": "Неизвестный тип контента"}
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE), "UNLIM_MEDIA", media, quote_mode="never")
    load_dotenv(ENV_FILE, override=True)
    log_action("/api/config/unlim_media", {"media": media})
    return {"ok": True, "msg": "Сохранено: " + MEDIA_LABELS[media], "media": media}


# ── Исключения из выгрузки Диска (35.9.0) ──────────────────────────────────

class ExclusionsPayload(BaseModel):
    items: list[str] = []


@app.get("/api/config/exclusions")
def api_get_exclusions():
    """Список облачных путей, которые не нужно скачивать."""
    items = exclusions.load()
    return {"ok": True, "items": items, "count": len(items)}


@app.post("/api/config/exclusions")
def api_set_exclusions(payload: ExclusionsPayload):
    """Сохраняет исключения и сразу приводит статусы строк Диска.

    Уже скачанные файлы не трогаются. Снятое исключение возвращает строки
    в очередь. Перед изменением базы сохраняется копия.
    """
    if _any_task_running(("disk", "unlim", "verify", "organize", "audit")):
        return {"ok": False,
                "msg": "Сначала остановите выгрузку и проверки — иначе статусы "
                       "файлов могут разойтись"}
    items = exclusions.normalize_items(payload.items)
    ok, item, err = _auto_backup("before-exclusions", ("disk",))
    if not ok:
        return {"ok": False,
                "msg": f"Не удалось сохранить копию базы: {err}"}
    try:
        changed = exclusions.sync_db(items)
        saved = exclusions.save(items)
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "msg": f"Не удалось сохранить исключения: {exc}"}
    log_action("/api/config/exclusions", {"count": len(saved)})
    parts = [f"Исключено из очереди: {changed['excluded']}"]
    if changed["returned"]:
        parts.append(f"возвращено в очередь: {changed['returned']}")
    return {
        "ok": True,
        "msg": "Сохранено. " + " · ".join(parts),
        "items": saved,
        "count": len(saved),
        "backup": item["id"] if item else "",
        **changed,
    }


def cloud_folders_from(items) -> list[dict]:
    """Только папки из ответа Яндекс.Диска, по алфавиту."""
    folders = []
    for item in items:
        if getattr(item, "type", "") != "dir":
            continue
        item_path = (getattr(item, "path", "") or "").strip()
        if not item_path:
            continue
        name = (getattr(item, "name", "") or "").strip() or item_path.rsplit("/", 1)[-1]
        folders.append({"name": name, "path": item_path})
    folders.sort(key=lambda f: f["name"].lower())
    return folders


def db_top_folders(db_file: Path) -> list[dict]:
    """Верхние папки Диска, известные из базы, — когда облако недоступно."""
    if not db_file.exists():
        return []
    try:
        con = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        try:
            rows = con.execute("SELECT remote_path FROM files")
            seen: dict[str, dict] = {}
            for (remote,) in rows:
                text = str(remote or "")
                if text.startswith("disk:"):
                    text = text[len("disk:"):]
                parts = [p for p in text.split("/") if p]
                if len(parts) > 1 and parts[0] not in seen:
                    seen[parts[0]] = {"name": parts[0], "path": "/" + parts[0]}
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return sorted(seen.values(), key=lambda f: f["name"].lower())


@app.get("/api/disk/folders")
def api_disk_folders(path: str = "/"):
    """Папки облачного Диска — чтобы выбрать корни обхода мышкой."""
    cfg = get_config()
    if not cfg.get("token"):
        return {"ok": False, "msg": "Токен не задан", "folders": []}
    path = normalize_scan_roots([path])[0]
    try:
        import yadisk

        cli = yadisk.Client(token=cfg["token"])
        try:
            items = list(cli.listdir(path))
        finally:
            cli.close()
    except Exception as e:  # noqa: BLE001 — в окне нужна причина, а не трейсбек
        # В текст ошибки не попадают ни токен, ни ссылки: только тип исключения.
        # Облако недоступно — показываем папки, которые уже знаем из базы.
        known = db_top_folders(DB_FILE)
        if known:
            return {"ok": True, "path": path, "folders": known, "from_db": True,
                    "msg": f"Облако сейчас недоступно ({type(e).__name__}) — "
                           "показываю папки из базы."}
        return {"ok": False, "msg": f"Не удалось получить список: {type(e).__name__}",
                "folders": []}
    folders = cloud_folders_from(items)
    return {"ok": True, "path": path, "folders": folders}


# ── выбор того, что качать ──────────────────────────────────────────────────

def _disk_statuses(paths_list: list[str]) -> dict[str, str]:
    """Статусы файлов из базы Диска: по ним окно скрывает уже скачанное."""
    if not DB_FILE.exists() or not paths_list:
        return {}
    out: dict[str, str] = {}
    try:
        con = sqlite3.connect(sqlite_ro_uri(DB_FILE), uri=True, timeout=30)
        try:
            for start in range(0, len(paths_list), 400):
                chunk = paths_list[start:start + 400]
                marks = ",".join("?" * len(chunk))
                rows = con.execute(
                    f"SELECT remote_path, status FROM files WHERE remote_path IN ({marks})",
                    chunk,
                )
                for remote, status in rows:
                    out[remote] = status
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return out


def _cloud_parent(path: str) -> str:
    body = path[len("disk:"):] if path.startswith("disk:") else path
    parts = [p for p in body.split("/") if p]
    if not parts:
        return "disk:/"
    return "disk:/" + "/".join(parts[:-1]) if len(parts) > 1 else "disk:/"


@app.get("/api/cloud/list")
def api_cloud_list(path: str = "/"):
    """Содержимое папки облака для проводника «Что качать».

    Отдаёт папки и файлы; у файлов — размер, дата изменения и статус из базы,
    чтобы уже скачанное нельзя было отметить повторно.
    """
    cfg = get_config()
    if not cfg.get("token"):
        return {"ok": False, "msg": "Токен не задан", "path": path,
                "folders": [], "files": []}
    try:
        folder = selection.normalize_remote(path) or "disk:/"
    except ValueError:
        return {"ok": False, "msg": "Недопустимый путь", "path": path,
                "folders": [], "files": []}
    try:
        import yadisk

        cli = yadisk.Client(token=cfg["token"])
        try:
            items = list(cli.listdir(folder, max_items=None))
        finally:
            cli.close()
    except Exception as e:  # noqa: BLE001 — в окне нужна причина, а не трейсбек
        return {"ok": False, "path": folder, "folders": [], "files": [],
                "msg": f"Облако недоступно ({type(e).__name__})"}

    folders: list[dict] = []
    files: list[dict] = []
    for item in items:
        item_path = getattr(item, "path", "") or ""
        if not item_path:
            continue
        name = (getattr(item, "name", "") or "").strip() or item_path.rsplit("/", 1)[-1]
        if getattr(item, "type", "") == "dir":
            folders.append({"name": name, "path": item_path})
        else:
            files.append({
                "name": name,
                "path": item_path,
                "size": int(item.size) if getattr(item, "size", None) else None,
                "modified": str(getattr(item, "modified", "") or ""),
            })
    folders.sort(key=lambda f: f["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    statuses = _disk_statuses([f["path"] for f in files])
    for item in files:
        item["status"] = statuses.get(item["path"], "")
    return {"ok": True, "path": folder, "parent": _cloud_parent(folder),
            "folders": folders, "files": files}


class SelectionDiskPayload(BaseModel):
    folders: list = Field(default_factory=list)
    files: list = Field(default_factory=list)


@app.get("/api/selection/disk")
def api_selection_get():
    data = selection.disk_selection()
    return {"ok": True, **data, "summary": selection.summary(data)}


@app.post("/api/selection/disk")
def api_selection_save(payload: SelectionDiskPayload):
    try:
        data = selection.save_disk(payload.folders, payload.files)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    log_action("/api/selection/disk", selection.summary(data["disk"]))
    return {"ok": True, **data["disk"], "summary": selection.summary(data["disk"])}


@app.post("/api/selection/disk/clear")
def api_selection_clear():
    data = selection.clear_disk()
    log_action("/api/selection/disk/clear")
    return {"ok": True, "msg": "Выбор очищен", **data["disk"]}


@app.post("/api/disk/download_selection")
def api_download_selection():
    """Скачивает только отмеченное. Статусы базы не меняются."""
    if selection.is_empty():
        return {"ok": False, "msg": "Сначала отметьте папки или файлы"}
    cfg = get_config()
    out_dir = cfg.get("disk_dir") or cfg.get("download_dir") or str(paths.DEFAULT_DISK_DIR)
    problem = check_target_dir(out_dir)
    if problem:
        return {"ok": False, "msg": problem}
    args = ["download", "--no-scan", "--selection", "--workers", str(auto_workers())]
    _session_retry["disk"] = False
    reset_session_counters("disk")
    ok, msg = tm.start("disk", "main.py", args)
    return {"ok": ok, "msg": msg, "mode": "disk"}


# ── выбор дней Безлимита ───────────────────────────────────────────────────

class UnlimSelectionPayload(BaseModel):
    dates: list = Field(default_factory=list)


def _unlim_selection_payload() -> dict:
    sel = selection.unlim_selection()
    stats = unlim_dates.selection_stats(UNLIM_DB_FILE, sel["ranges"])
    return {"ok": True, **sel, "stats": stats}


@app.get("/api/unlim/calendar")
def api_unlim_calendar(month: str = ""):
    """Календарь Безлимита: по дням сколько файлов, сколько скачано и осталось."""
    data = unlim_dates.calendar(UNLIM_DB_FILE, month)
    return {"ok": True, **data, "selection": selection.unlim_selection()}


@app.get("/api/selection/unlim")
def api_unlim_selection_get():
    return _unlim_selection_payload()


@app.post("/api/selection/unlim")
def api_unlim_selection_save(payload: UnlimSelectionPayload):
    selection.save_unlim_dates(payload.dates)
    log_action("/api/selection/unlim", {"days": len(payload.dates or [])})
    return _unlim_selection_payload()


@app.post("/api/selection/unlim/clear")
def api_unlim_selection_clear():
    selection.clear_unlim()
    log_action("/api/selection/unlim/clear")
    return {**_unlim_selection_payload(), "msg": "Даты сняты"}


@app.post("/api/unlim/download_selection")
def api_unlim_download_selection():
    """Качает только отмеченные дни. Статусы остальной очереди не меняются."""
    ranges = selection.unlim_ranges()
    if not ranges:
        return {"ok": False, "msg": "Сначала отметьте дни в календаре"}
    stats = unlim_dates.selection_stats(UNLIM_DB_FILE, ranges)
    if stats["left"] <= 0:
        return {"ok": False, "msg": "В выбранных днях всё уже скачано"}
    cfg = get_config()
    out_dir = cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR)
    problem = check_target_dir(out_dir)
    if problem:
        return {"ok": False, "msg": problem}
    args = [
        "--selection",
        "--target", "all",
        "--media", "all",
        "--out-dir", out_dir,
        "--workers", str(auto_workers()),
    ]
    if not cfg.get("previews", True):
        args.append("--no-previews")
    _session_retry["unlim"] = True
    reset_session_counters("unlim")
    ok, msg = tm.start("unlim", "unlim_backup.py", args)
    return {"ok": ok, "msg": msg, "mode": "unlim"}


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


@app.post("/api/token/fetch")
def api_token_fetch():
    """Запускает полуавтоматическое получение OAuth-токена через Playwright."""
    st = tm.status().get("token_fetch")
    if st and st.get("running"):
        return {"ok": False, "msg": "Получение токена уже идёт"}
    ok, msg = tm.start("token_fetch", "fetch_token.py", [])
    return {"ok": ok, "msg": msg}


@app.post("/api/token/fetch/cancel")
def api_token_fetch_cancel():
    ok, msg = tm.stop("token_fetch")
    try:
        TOKEN_FETCH_STATUS.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FETCH_STATUS.write_text(
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


class DbRebuildPayload(BaseModel):
    scope: str = "unlim"
    confirm: str = ""


class DbRelocatePayload(BaseModel):
    scope: str = ""
    new_dir: str = ""
    # «Старая папка»: обрабатывать только строки, чей путь лежит внутри неё.
    old_dir: str = ""
    preview: bool = False


class DbRestorePayload(BaseModel):
    scope: str = "disk"
    preview: bool = True


class DbRedownloadPayload(BaseModel):
    scope: str = ""
    new_dir: str = ""
    confirm: str = ""


DB_TABLES = {"disk": "files", "unlim": "unlim_files"}
DB_REBUILD_WORD = "ПЕРЕСОЗДАТЬ"


def _db_files() -> dict[str, Path]:
    return {"disk": DB_FILE, "unlim": UNLIM_DB_FILE}


def _db_scope(scope: str):
    """Путь к базе и имя таблицы по короткому имени, либо None."""
    scope = (scope or "").strip().lower()
    if scope not in DB_TABLES:
        return None
    return _db_files()[scope], DB_TABLES[scope], scope


def _db_running(scope: str) -> bool:
    """Занята ли база какой-нибудь живой задачей."""
    with tm._lock:
        for slot, task in tm._tasks.items():
            if not task.alive():
                continue
            if scope == "disk" and slot == "disk":
                return True
            if scope == "unlim" and slot.startswith("unlim"):
                return True
    return False


def _copy_db(db_file: Path) -> Path:
    """Согласованная копия базы.

    Копировать файл при WAL нельзя — получится битый снимок, поэтому через
    sqlite backup API.
    """
    backup = db_file.with_name(
        f"{db_file.stem}.bak-{datetime.now():%Y%m%d-%H%M%S}{db_file.suffix}"
    )
    src = sqlite3.connect(db_file, timeout=30)
    try:
        dst = sqlite3.connect(backup)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup


def _scope_dir_setting(scope: str) -> str:
    cfg = get_config()
    if scope == "unlim":
        return cfg.get("unlim_dir") or ""
    return cfg.get("disk_dir") or cfg.get("download_dir") or ""


def _write_dir_setting(scope: str, new_dir: str) -> None:
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    if scope == "disk":
        set_key(str(ENV_FILE), "DISK_DIR", new_dir, quote_mode="never")
        set_key(str(ENV_FILE), "DOWNLOAD_DIR", new_dir, quote_mode="never")
    else:
        set_key(str(ENV_FILE), "UNLIM_DIR", new_dir, quote_mode="never")
    load_dotenv(ENV_FILE, override=True)


def _relative_under(path: str, root: str) -> Optional[str]:
    """Путь относительно root или None, если path лежит вне root.

    os.path.relpath на Windows сравнивает без учёта регистра, но возвращает
    исходное написание — то, что нужно для local_path.
    """
    if not path or not root:
        return None
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return None
    if rel == os.curdir:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel


@app.post("/api/db/rebuild")
def db_rebuild(payload: DbRebuildPayload):
    """Пересоздание базы Диска или Безлимита одним путём.

    Статусы скачанного теряются, поэтому сначала сохраняем копию. Диск
    пересобирается `main.py scan --force`, Безлимит — `unlim_backup.py
    --scan-only --force`.
    """
    resolved = _db_scope(payload.scope)
    if not resolved:
        return {"ok": False, "msg": "Неизвестная база: ожидается disk или unlim"}
    db_file, _table, scope = resolved
    if payload.confirm.strip().upper() != DB_REBUILD_WORD:
        return {"ok": False,
                "msg": f"Введите слово {DB_REBUILD_WORD} для подтверждения"}
    if _db_running(scope):
        return {"ok": False, "msg": "Сначала остановите выгрузку этой базы"}
    if not db_file.exists():
        return {"ok": False,
                "msg": "Базы ещё нет — сначала нажмите «Обновить список файлов»"}
    ok, item, err = _auto_backup("before-rebuild", (scope,))
    if not ok:
        return {"ok": False,
                "msg": f"Не удалось сохранить копию базы: {err}"}
    backup = item["id"] if item else ""
    log_action(f"db/rebuild/{scope}")
    if scope == "disk":
        started, msg = tm.start("disk", "main.py", ["scan", "--force"])
        what = "Диска"
    else:
        started, msg = tm.start("unlim", "unlim_backup.py", ["--scan-only", "--force"])
        what = "Безлимита"
    if not started:
        return {"ok": False, "msg": msg, "backup": backup}
    return {"ok": True, "msg": f"База {what} пересоздаётся", "backup": backup}


@app.post("/api/unlim/rebuild")
def unlim_rebuild(payload: RebuildPayload):
    """Старый адрес — та же пересборка базы Безлимита."""
    return db_rebuild(DbRebuildPayload(scope="unlim", confirm=payload.confirm))


@app.post("/api/db/relocate")
def db_relocate(payload: DbRelocatePayload):
    """Ищет файлы в новой папке и перепривязывает базу, ничего не скачивая.

    `preview=true` — только показать, что найдено, и ничего не менять.
    Ищет не только по старому хвосту пути, но и по имени/размеру в подпапках:
    так находятся файлы после сортировки или ручного переноса.
    """
    scope = (payload.scope or "").strip().lower()
    scopes = ["disk", "unlim"] if scope == "both" else [scope]
    if any(s not in ("disk", "unlim") for s in scopes):
        return {"ok": False, "msg": "Неизвестная база: ожидается disk, unlim или both"}
    new_dir = (payload.new_dir or "").strip()
    if not new_dir:
        return {"ok": False, "msg": "Укажите новую папку с файлами"}
    if any(_db_running(s) for s in scopes):
        return {"ok": False, "msg": "Сначала остановите выгрузку этой базы"}
    if not os.path.isdir(new_dir):
        return {"ok": False, "msg": f"Папка не найдена или диск не подключён: {new_dir}"}

    plans = []
    for one in scopes:
        try:
            plans.append(relocate_mod.plan_relocate(
                one, new_dir, _scope_dir_setting(one), payload.old_dir
            ))
        except relocate_mod.RelocateError as exc:
            return {"ok": False, "msg": str(exc)}
        except sqlite3.Error as exc:
            return {"ok": False, "msg": f"Ошибка базы {one}: {exc}"}

    result = {p.scope: p.as_dict() for p in plans}
    if payload.preview:
        log_action(f"db/relocate/preview/{scope}")
        return {"ok": True, "preview": True, "plans": result,
                "msg": "Предпросмотр готов — файлы не менялись"}

    saved = {}
    for plan in plans:
        if not plan.updates:
            continue
        ok, item, err = _auto_backup("before-relocate", (plan.scope,))
        if not ok:
            return {"ok": False,
                    "msg": f"Не удалось сохранить копию базы: {err}"}
        if item:
            saved[plan.scope] = item["id"]

    moved = 0
    for plan in plans:
        try:
            moved += relocate_mod.apply_relocate(plan)
        except sqlite3.Error as exc:
            return {"ok": False, "msg": f"Ошибка базы {plan.scope}: {exc}",
                    "backup": saved}
        # Папку в настройках меняем только когда выбрана одна база: при «обеих»
        # одна общая папка затёрла бы отдельные папки Диска и Безлимита.
        if scope != "both":
            _write_dir_setting(plan.scope, new_dir)
    log_action(f"db/relocate/{scope}")
    return {
        "ok": True,
        "preview": False,
        "plans": result,
        "moved": moved,
        "backup": saved,
        "msg": f"Перепривязано {moved} файлов",
    }


@app.post("/api/db/restore_cloud")
def db_restore_cloud(payload: DbRestorePayload):
    """Возвращает файлы Диска в папки, как они называются в облаке."""
    scope = (payload.scope or "disk").strip().lower()
    if scope != "disk":
        return {"ok": False, "msg": "Вернуть структуру облака можно только для Диска"}
    if _db_running("disk"):
        return {"ok": False, "msg": "Сначала остановите выгрузку Диска"}
    try:
        plan = relocate_mod.plan_restore_cloud(
            "disk", _scope_dir_setting("disk")
        )
    except relocate_mod.RelocateError as exc:
        return {"ok": False, "msg": str(exc)}
    except sqlite3.Error as exc:
        return {"ok": False, "msg": f"Ошибка базы: {exc}"}
    if payload.preview:
        log_action("db/restore_cloud/preview")
        return {"ok": True, "preview": True, "plan": plan.as_dict(),
                "msg": "Предпросмотр готов — файлы не менялись"}
    ok, item, err = _auto_backup("before-restore-cloud", ("disk",))
    if not ok:
        return {"ok": False,
                "msg": f"Не удалось сохранить копию базы: {err}"}
    backup = item["id"] if item else ""
    done, errors = relocate_mod.apply_restore_cloud(plan)
    log_action("db/restore_cloud")
    return {
        "ok": True,
        "preview": False,
        "moved": done,
        "errors": errors[:20],
        "backup": str(backup),
        "msg": f"Возвращено в структуру облака: {done}",
    }


@app.post("/api/db/redownload")
def db_redownload(payload: DbRedownloadPayload):
    """Скачать всё заново в новую папку: копия базы → статусы в pending."""
    resolved = _db_scope(payload.scope)
    if not resolved:
        return {"ok": False, "msg": "Неизвестная база: ожидается disk или unlim"}
    db_file, table, scope = resolved
    if payload.confirm.strip().upper() != DB_REBUILD_WORD:
        return {"ok": False,
                "msg": f"Введите слово {DB_REBUILD_WORD} для подтверждения"}
    new_dir = (payload.new_dir or "").strip()
    if not new_dir:
        return {"ok": False, "msg": "Укажите новую папку выгрузки"}
    if _db_running(scope):
        return {"ok": False, "msg": "Сначала остановите выгрузку этой базы"}
    if not db_file.exists():
        return {"ok": False, "msg": "Базы ещё нет"}
    ok, item, err = _auto_backup("before-redownload", (scope,))
    if not ok:
        return {"ok": False,
                "msg": f"Не удалось сохранить копию базы: {err}"}
    backup = item["id"] if item else ""
    # У Диска есть downloaded_at, у Безлимита — attempts; обнуляем своё.
    extra = "downloaded_at = NULL" if scope == "disk" else "attempts = 0"
    try:
        con = sqlite3.connect(db_file, timeout=30)
        try:
            with con:
                con.execute(
                    f"UPDATE {table} SET status = 'pending', local_path = NULL, "
                    f"error = NULL, {extra}"
                )
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"ok": False, "msg": f"Ошибка базы: {e}", "backup": str(backup)}
    _write_dir_setting(scope, new_dir)
    log_action(f"db/redownload/{scope}")
    reset_session_counters(scope)
    if scope == "disk":
        ok, msg = tm.start("disk", "main.py",
                           ["download", "--workers", str(auto_workers())])
    else:
        _session_retry["unlim"] = True
        ok, msg = tm.start(
            "unlim", "unlim_backup.py",
            ["--target", "all", "--media", "all",
             "--workers", str(auto_workers())],
        )
    if not ok:
        return {"ok": False, "msg": msg, "backup": str(backup)}
    return {"ok": True, "msg": "Скачивание начато в новую папку",
            "backup": str(backup)}


# Резервные копии баз (только статусы, сами файлы не копируются)
def _backup_sources() -> dict[str, Path]:
    return {"disk": DB_FILE, "unlim": UNLIM_DB_FILE}


def _auto_backup(
    reason: str, scopes: tuple[str, ...] | list[str]
) -> tuple[bool, dict | None, str]:
    """Копия перед рискованным действием: (получилось, копия, ошибка).

    Если баз ещё нет, это не ошибка: делать копию нечего, действие можно
    продолжать.
    """
    try:
        item = backups.auto_backup(
            paths.DATA_DIR,
            list(scopes),
            _backup_sources(),
            version.APP_VERSION,
            reason=reason,
        )
    except (backups.BackupError, OSError, sqlite3.Error) as e:
        return False, None, str(e)
    return True, item, ""


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
    # На случай, если перенос старых снимков не успел при запуске.
    try:
        backups.migrate_snapshots(paths.DATA_DIR)
    except OSError:
        pass
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
        open_in_file_manager(folder)
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
    update_db: bool = True  # обновить пути в базе после перемещения
    confirmed: bool = False  # человек согласился сортировать не папку Безлимита


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

    cfg = get_config()
    unlim_dir = cfg.get("unlim_dir") or str(paths.DEFAULT_UNLIM_DIR)
    disk_dir = cfg.get("disk_dir") or cfg.get("download_dir") or str(paths.DEFAULT_DISK_DIR)
    root = os.path.normcase(os.path.abspath(payload.input))
    is_unlim = root == os.path.normcase(os.path.abspath(unlim_dir))
    is_disk = root == os.path.normcase(os.path.abspath(disk_dir))
    if not is_unlim and not payload.confirmed:
        where = "папка Диска" if is_disk else "не папка Безлимита"
        return {
            "ok": False,
            "msg": f"Это {where}. Сортировка перемешает файлы и сломает "
                   f"структуру облака. Подтвердите действие в окне.",
        }

    # Сортировка с обновлением базы меняет пути у скачанных файлов — копия
    # базы перед этим действием сохраняется автоматически.
    backup_id = ""
    if payload.action == "move" and payload.update_db:
        sort_scope = "unlim" if is_unlim else ("disk" if is_disk else "")
        if sort_scope:
            ok, item, err = _auto_backup("before-sort", (sort_scope,))
            if not ok:
                return {"ok": False,
                        "msg": f"Не удалось сохранить копию базы: {err}"}
            backup_id = item["id"] if item else ""

    for old in (paths.ORGANIZE_REPORT_FILE, paths.ORGANIZE_PLAN_FILE):
        try:
            old.unlink()
        except OSError:
            pass

    args = ["--input", payload.input,
            "--action", payload.action,
            "--scheme", payload.scheme,
            "--report-file", str(paths.ORGANIZE_REPORT_FILE),
            "--plan-file", str(paths.ORGANIZE_PLAN_FILE)]
    if payload.quick:
        args.append("--quick")
    if payload.update_db and payload.action == "move":
        if is_unlim:
            args += ["--db", str(UNLIM_DB_FILE), "--table", "unlim_files"]
        elif is_disk:
            args += ["--db", str(DB_FILE), "--table", "files"]
    out = _resp(tm.start("organize", "organize.py", args))
    out["backup"] = backup_id
    return out


@app.get("/api/organize/report")
def organize_report():
    """Отчёт последней сортировки: куда что ляжет и полный список."""
    try:
        text = paths.ORGANIZE_REPORT_FILE.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return {
        "ok": True,
        "report": text,
        "plan_file": str(paths.ORGANIZE_PLAN_FILE)
        if paths.ORGANIZE_PLAN_FILE.exists() else "",
    }


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
    cookies: str = ""   # JSON-массив от Cookie-Editor, необязательно


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
        "report_url": diagnostics.report_url(),
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
    """Завершает первичную настройку: токен, папки, куки, ключ отчётов."""
    # Пароль/ключ не журналируем: в лог попадает только сам факт настройки.
    log_action("setup/finish")
    token = (payload.token or "").strip()
    cookies_saved = 0
    raw_cookies = (payload.cookies or "").strip()
    if raw_cookies:
        try:
            arr = json.loads(raw_cookies)
        except ValueError:
            return JSONResponse(
                {"ok": False,
                 "error": "Куки: это не JSON. Скопируйте массив целиком, "
                          "как его отдаёт Export → Export as JSON."},
                status_code=400,
            )
        check = _validate_cookies(arr)
        if not check.get("ok"):
            return JSONResponse({"ok": False, "error": check.get("msg")},
                                status_code=400)
        tmp = COOKIES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(arr, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, COOKIES_FILE)
        cookies_saved = len(arr)
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
        "cookies_saved": cookies_saved,
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
        report = diagnostics.build_report(
            tm.status(), full_cycle.status(),
            # Проверка обновлений: свежая (не старше 6 часов) берётся из
            # logs/updates_status.json, иначе — короткий запрос на GitHub.
            updates=updater.updates_snapshot(update_manager.status()),
        )
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
        report = diagnostics.build_report(
            tm.status(), full_cycle.status(),
            # Проверка обновлений: свежая (не старше 6 часов) берётся из
            # logs/updates_status.json, иначе — короткий запрос на GitHub.
            updates=updater.updates_snapshot(update_manager.status()),
        )
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


class FeedbackPayload(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    message: str = ""
    consent: bool = False
    attach_report: bool = True


@app.post("/api/diagnostics/feedback")
def api_diagnostics_feedback(payload: FeedbackPayload):
    """Отправляет сообщение разработчику через Cloudflare Worker в Telegram."""
    log_action("diagnostics/feedback", {"attach_report": payload.attach_report})
    name = (payload.name or "").strip()
    email = (payload.email or "").strip()
    phone = (payload.phone or "").strip()
    message = (payload.message or "").strip()
    if not name:
        return {"ok": False, "error": "Укажите имя."}
    if len(name) > 80:
        return {"ok": False, "error": "Имя слишком длинное (до 80 символов)."}
    if not email or "@" not in email or "." not in email.rsplit("@", 1)[-1] or " " in email:
        return {"ok": False, "error": "Укажите почту — по ней придёт ответ."}
    if len(email) > 120:
        return {"ok": False, "error": "Почта слишком длинная (до 120 символов)."}
    if len(phone) > 40:
        return {"ok": False, "error": "Телефон слишком длинный (до 40 символов)."}
    if not message:
        return {"ok": False, "error": "Опишите проблему — без текста отправить нечего."}
    if len(message) > 4000:
        return {"ok": False, "error": "Сообщение слишком длинное (до 4000 символов)."}
    if not payload.consent:
        return {"ok": False, "error": "Нужно согласие на отправку сообщения."}
    report_path = None
    try:
        if payload.attach_report:
            report = diagnostics.build_report(
                tm.status(), full_cycle.status(),
                updates=updater.updates_snapshot(update_manager.status()),
            )
            report_path = diagnostics.write_report(report)
        result = diagnostics.send_feedback(name, email, phone, message, report_path)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": diagnostics.redact_text(f"{type(e).__name__}: {e}")},
            status_code=500,
        )
    return result


@app.get("/api/diagnostics/reports")
def api_diagnostics_reports():
    return {"ok": True, "reports": diagnostics.latest_reports()}


class ProfileCreatePayload(BaseModel):
    name: str = ""
    disk_dir: str = ""
    unlim_dir: str = ""


class ProfileRenamePayload(BaseModel):
    id: str = ""
    name: str = ""


class ProfileIdPayload(BaseModel):
    id: str = ""


class ProfileAvatarPayload(BaseModel):
    id: str = ""
    avatar: str = ""


@app.get("/api/profiles")
def api_profiles():
    """Профили пользователей: у каждого свой каталог данных."""
    return {
        "ok": True,
        "profiles": profiles.list_profiles(),
        "current": profiles.current_profile(),
        "avatars": list(profiles.AVATARS),
        "data_dir": str(paths.DATA_DIR),
    }


@app.post("/api/profiles/create")
def api_profiles_create(payload: ProfileCreatePayload):
    log_action("/api/profiles/create", {"name": payload.name})
    try:
        created = profiles.create_profile(
            payload.name, payload.disk_dir, payload.unlim_dir)
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "profile": created}


@app.post("/api/profiles/rename")
def api_profiles_rename(payload: ProfileRenamePayload):
    log_action("/api/profiles/rename", {"id": payload.id})
    try:
        item = profiles.rename_profile(payload.id, payload.name)
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "profile": item}


@app.post("/api/profiles/avatar")
def api_profiles_avatar(payload: ProfileAvatarPayload):
    """Запоминает выбранную аватарку профиля."""
    log_action("/api/profiles/avatar", {"id": payload.id})
    try:
        item = profiles.set_avatar(payload.id, payload.avatar)
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "profile": item, "avatars": list(profiles.AVATARS)}


@app.post("/api/profiles/delete")
def api_profiles_delete(payload: ProfileIdPayload):
    log_action("/api/profiles/delete", {"id": payload.id})
    try:
        item = profiles.delete_profile(payload.id)
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "deleted": item}


@app.post("/api/profiles/switch")
def api_profiles_switch(payload: ProfileIdPayload):
    """Перезапускает программу с данными другого профиля."""
    log_action("/api/profiles/switch", {"id": payload.id})
    try:
        target = profiles.profile_dir(payload.id)
    except (ValueError, OSError) as e:
        return {"ok": False, "msg": str(e)}
    if not IS_FROZEN:
        return {"ok": False,
                "msg": "Запущено из исходников: закройте программу и запустите "
                       "с YADISK_DATA_DIR, указывающим на папку профиля."}
    with tm._lock:
        busy = [slot for slot, task in tm._tasks.items() if task.alive()]
    if busy or pipeline.is_running() or full_cycle.is_running():
        return {"ok": False, "msg": "Сначала остановите скачивание."}
    try:
        helper = write_profile_restart_helper(target)
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", str(helper)],
            cwd=str(helper.parent), close_fds=True, creationflags=flags,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"Не удалось переключить профиль: {e}"}
    threading.Timer(1.2, _quit_application).start()
    return {"ok": True,
            "msg": "Программа перезапустится с другим профилем."}


class ReportUrlPayload(BaseModel):
    url: str = ""


@app.get("/api/diagnostics/url")
def api_diagnostics_url():
    """Текущий и штатный адрес сервера отчётов."""
    return {"ok": True, "url": diagnostics.report_url(),
            "default": diagnostics.DEFAULT_REPORT_URL}


@app.post("/api/diagnostics/url")
def api_diagnostics_set_url(payload: ReportUrlPayload):
    """Меняет адрес сервера отчётов (пустая строка — вернуть штатный)."""
    log_action("diagnostics/url")
    try:
        url = diagnostics.save_report_url(payload.url)
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "url": url,
            "default": diagnostics.DEFAULT_REPORT_URL}


@app.get("/api/diagnostics/ping")
def api_diagnostics_ping():
    """Проверка связи с сервером отчётов: блокировка провайдера или сбой."""
    log_action("diagnostics/ping")
    return diagnostics.ping_report_server()


# ── Проверка скачанных файлов (C1) ──────────────────────────────────────────


class VerifyPayload(BaseModel):
    scope: str = "disk"
    deep: bool = False


@app.post("/api/verify/start")
def api_verify_start(payload: VerifyPayload):
    """Запускает проверку файлов фоновой задачей — окно остаётся живым."""
    log_action("/api/verify/start", payload)
    scope = (payload.scope or "disk").strip().lower()
    if scope not in ("disk", "unlim", "both"):
        return {"ok": False, "msg": "Неизвестная база для проверки"}
    args = ["--scope", scope]
    if payload.deep:
        args.append("--deep")
    ok, msg = tm.start("verify", "verify_local.py", args)
    return {"ok": ok, "msg": msg, "scope": scope}


@app.post("/api/verify/stop")
def api_verify_stop():
    log_action("/api/verify/stop")
    ok, msg = tm.stop("verify")
    return {"ok": ok, "msg": msg}


class RequeueMissingPayload(BaseModel):
    scope: str = "disk"
    keys: list[str] = Field(default_factory=list)


@app.post("/api/verify/requeue_missing")
def api_verify_requeue_missing(payload: RequeueMissingPayload):
    """Возвращает в очередь файлы, которых нет по записанному пути.

    Берём только статус «missing» из последней проверки: файлы на отключённом
    диске и несовпадения размера/MD5 не трогаем.
    """
    scope = (payload.scope or "disk").strip().lower()
    if scope not in ("disk", "unlim", "both"):
        return {"ok": False, "msg": "Неизвестная база"}
    if _any_task_running(("disk", "unlim", "verify", "organize")):
        return {"ok": False, "msg": "Сначала остановите выгрузку и проверку"}
    keys = [str(k) for k in (payload.keys or []) if k]
    if not keys:
        data = verify_mod.read_status()
        keys = [
            str(item.get("path"))
            for item in (data.get("problems") or [])
            if item.get("status") == "missing" and item.get("path")
        ]
    scopes = ["disk", "unlim"] if scope == "both" else [scope]
    ok, item, err = _auto_backup("before-requeue", scopes)
    if not ok:
        return {"ok": False,
                "msg": f"Не удалось сохранить копию базы: {err}"}
    updated = 0
    try:
        for one in scopes:
            updated += relocate_mod.requeue_missing(one, keys)
    except relocate_mod.RelocateError as exc:
        return {"ok": False, "msg": str(exc)}
    except sqlite3.Error as exc:
        return {"ok": False, "msg": f"Ошибка базы: {exc}"}
    log_action(f"verify/requeue_missing/{scope}")
    for one in (("disk", "unlim") if scope == "both" else (scope,)):
        reset_session_counters(one)
    return {"ok": True, "updated": updated,
            "backup": item["id"] if item else "",
            "msg": f"Возвращено в очередь: {updated}"}


@app.get("/api/verify/report")
def api_verify_report():
    """Последний отчёт проверки текстом — показывается в окне."""
    try:
        text = paths.VERIFY_REPORT_FILE.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return {"ok": True, "report": text}


def _verify_status(data: dict, tasks: dict) -> dict:
    """Статус проверки для UI: прогресс из файла + жива ли задача."""
    out = dict(data or {})
    task = (tasks or {}).get("verify") or {}
    out["running"] = bool(task.get("running"))
    out["report_available"] = paths.VERIFY_REPORT_FILE.exists()
    return out


# ── Сверка с облаком (35.7.6) ───────────────────────────────────────────────


class CloudAuditPayload(BaseModel):
    scope: str = "both"


@app.post("/api/cloud/audit/start")
def api_cloud_audit_start(payload: CloudAuditPayload):
    """Запускает онлайн-сверку облака с базой в фоновой задаче."""
    log_action("/api/cloud/audit/start", payload)
    scope = (payload.scope or "both").strip().lower()
    if scope not in ("disk", "unlim", "both"):
        return {"ok": False, "msg": "Неизвестная база для сверки"}
    ok, msg = tm.start("audit", "cloud_audit_worker.py", ["--scope", scope])
    return {"ok": ok, "msg": msg, "scope": scope}


@app.post("/api/cloud/audit/stop")
def api_cloud_audit_stop():
    log_action("/api/cloud/audit/stop")
    data = cloud_audit_mod.read_status()
    ok, msg = tm.stop("audit")
    if ok:
        cloud_audit_mod.write_stopped(str(data.get("scope") or "both"))
    return {"ok": ok, "msg": msg}


@app.post("/api/cloud/audit/add_new")
def api_cloud_audit_add_new():
    """Ставит в очередь новые и изменившиеся файлы из последней сверки."""
    log_action("/api/cloud/audit/add_new")
    if _any_task_running(("disk", "unlim", "verify", "organize", "audit")):
        return {"ok": False, "msg": "Сначала остановите выгрузку и другие проверки"}
    audited = str((cloud_audit_mod.read_status() or {}).get("scope") or "disk")
    backup_scopes = ("disk", "unlim") if audited == "both" else (audited,)
    ok, item, err = _auto_backup("before-add-new", backup_scopes)
    if not ok:
        return {"ok": False,
                "msg": f"Не удалось сохранить копию базы: {err}"}
    try:
        result = cloud_audit_mod.add_new()
    except cloud_audit_mod.AuditError as exc:
        return {"ok": False, "msg": str(exc)}
    except sqlite3.Error as exc:
        return {"ok": False, "msg": f"Ошибка базы: {exc}"}
    data = cloud_audit_mod.read_status()
    for one in (("disk", "unlim") if data.get("scope") == "both"
                else (str(data.get("scope") or "disk"),)):
        reset_session_counters(one)
    msg = (
        f"Добавлено новых: {result['added']} · "
        f"поставлено на повтор: {result['requeued']} · "
        f"старых копий переименовано: {result['renamed']}"
    )
    if result["skipped"]:
        msg += f" · пропущено: {result['skipped']}"
    return {"ok": True, "msg": msg, "backup": item["id"] if item else "",
            **result}


@app.post("/api/cloud/audit/mark_gone")
def api_cloud_audit_mark_gone():
    """Помечает файлы, исчезнувшие из облака; на диске они остаются."""
    log_action("/api/cloud/audit/mark_gone")
    if _any_task_running(("disk", "unlim", "verify", "organize", "audit")):
        return {"ok": False, "msg": "Сначала остановите выгрузку и другие проверки"}
    audited = str((cloud_audit_mod.read_status() or {}).get("scope") or "disk")
    backup_scopes = ("disk", "unlim") if audited == "both" else (audited,)
    ok, item, err = _auto_backup("before-mark-gone", backup_scopes)
    if not ok:
        return {"ok": False,
                "msg": f"Не удалось сохранить копию базы: {err}"}
    try:
        result = cloud_audit_mod.mark_gone()
    except cloud_audit_mod.AuditError as exc:
        return {"ok": False, "msg": str(exc)}
    except sqlite3.Error as exc:
        return {"ok": False, "msg": f"Ошибка базы: {exc}"}
    return {
        "ok": True,
        "msg": f"Помечено «удалён из облака»: {result['marked']}",
        "backup": item["id"] if item else "",
        **result,
    }


@app.get("/api/cloud/audit/status")
def api_cloud_audit_status():
    return {"ok": True, "audit": _cloud_audit_status(tm.status())}


@app.get("/api/cloud/audit/report")
def api_cloud_audit_report():
    """Текст последней сверки для окна."""
    try:
        text = paths.CLOUD_AUDIT_REPORT_FILE.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return {"ok": True, "report": text}


def _cloud_audit_status(tasks: dict) -> dict:
    out = dict(cloud_audit_mod.read_status() or {})
    task = (tasks or {}).get("audit") or {}
    stage = str(out.get("stage") or "").lower()
    # Кнопка и подпись должны жить по одному источнику. Файл статуса пишет
    # «stopped» сразу, а процесс ещё секунду-две завершается: если верить
    # только `task.running`, старт остаётся серым при подписи «остановлена».
    finished_stage = stage in ("stopped", "done", "finished", "error")
    out["running"] = bool(task.get("running")) and not finished_stage
    out["state"] = stage or ("running" if out["running"] else "idle")
    out["report_available"] = paths.CLOUD_AUDIT_REPORT_FILE.exists()
    return out


class UpdateOpenPayload(BaseModel):
    url: str = ""


@app.get("/api/guide")
def api_guide():
    """Текст инструкции и флаг «уже показывали после установки»."""
    log_action("guide/open")
    return guide.status()


@app.post("/api/guide/seen")
def api_guide_seen():
    """Отмечает, что инструкция открыта сама: больше не мешаем окном."""
    guide.mark_seen()
    return {"ok": True}


@app.get("/api/updates/check")
def api_updates_check():
    """Сравнивает версию с последним GitHub Release. Сеть может быть недоступна."""
    log_action("updates/check")
    result = updater.check_for_updates()
    # Последняя проверка нужна отчёту диагностики: он не должен ждать сеть,
    # если окно уже проверяло обновления.
    updater.record_check(result)
    return result


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
        open_in_file_manager(paths.REPORT_DIR)
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
    "fetch_token.py": "fetch_token",
    "extract_cookies.py": "extract_cookies",
    "report_status.py": "report_status",
    "watch.py": "watch",
    "verify_local.py": "verify_local",
    "cloud_audit_worker.py": "cloud_audit_worker",
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
    print("  Yad.backup by Sergey Pukhir (ex3544, vovapuh's son)")
    print(f"  Дашборд: {url}")
    print("=" * 64)
    print("  Быстрые ссылки для настройки:")
    print("  * Получить OAuth-токен:  https://yandex.ru/dev/disk/poligon/")
    print("  * Скачать Cookie-Editor: https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm")
    print("  * Раздел фото (безлимит): https://disk.yandex.ru/client/photo")
    print("=" * 64)
    print()
    start_background_services()

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
